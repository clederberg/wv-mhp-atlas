#!/usr/bin/env python3
"""
Build a mobile home park dataset for one WV county from the statewide
WV GIS Technical Center parcel service (no scraping, no token).

Source service (public, backs the WV Property Viewer / Flood Tool):
  https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels/MapServer
    /11  ParcelSummary   assessment attributes (owner, land use, values, deed, NewOwner)
    /0   WVParcels       parcel polygons, joined on CleanParcelID
    /5   Site Address Points   E-911 / SAMS points, used for lot counts

Two modes:
  python fetch.py --discover              list every land-use label in the county
  python fetch.py                         build docs/parks.geojson + docs/parks.csv

Run this on a machine with internet access. It writes into docs/ so the
GitHub Pages site picks the data up directly.
"""

import argparse
import csv
import json
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

BASE = "https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels/MapServer"
SUMMARY = f"{BASE}/11/query"   # attributes (table)
PARCELS = f"{BASE}/0/query"    # geometry
ADDRESSES = f"{BASE}/5/query"  # E-911 points

# Land-use labels that identify a park. Permissive on purpose. Run --discover
# first, then trim or extend this list to match how the county actually codes.
PARK_PATTERNS = [
    "MOBILE HOME PARK",
    "MOBILE HOME PK",
    "MOBILE HM PK",
    "TRAILER PARK",
    "TRAILER CT",
    "MANUFACTURED HOME PARK",
    "MHP",
]

# Attributes we keep from ParcelSummary.
SUMMARY_FIELDS = [
    "CleanParcelID", "ParcelID", "CountyName", "DistrictName",
    "FullOwnerName", "FullOwnerAddress",
    "FullPhysicalAddress", "SAMSAddress", "SAMSCity", "SAMSZip",
    "LandUse", "LandUseCode", "UseType",
    "PropertyClassCode", "PropertyClassDescription",
    "TotalAppraisal", "LandAppraisal", "BuildingAppraisal",
    "DeededAcres", "CalculatedAcres", "TaxYear",
    "DeedBook", "DeedPage",
    "NewOwner", "NewBook", "NewPage",
    "Units", "OBYCount", "YearBuilt",
]

TIMEOUT = 60
PAGE = 1000


def get(url, params):
    """GET an ArcGIS query with light retry. Returns parsed JSON."""
    q = urlencode(params)
    full = f"{url}?{q}"
    for attempt in range(4):
        try:
            req = Request(full, headers={"User-Agent": "wv-mhp-atlas/1.0"})
            with urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"service error: {data['error']}")
            return data
        except (URLError, HTTPError, RuntimeError) as e:
            if attempt == 3:
                raise
            wait = 2 ** attempt
            print(f"  retry in {wait}s ({e})", file=sys.stderr)
            time.sleep(wait)


def page_query(url, where, out_fields="*", geometry=None, out_sr=4326):
    """Query an ArcGIS layer, paging until all records are returned."""
    rows = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true" if geometry is None else "false",
            "outSR": out_sr,
            "resultOffset": offset,
            "resultRecordCount": PAGE,
            "f": "json",
        }
        if geometry is None and out_fields != "*":
            params["returnGeometry"] = "false"
        data = get(url, params)
        feats = data.get("features", [])
        rows.extend(feats)
        if len(feats) < PAGE:
            break
        offset += PAGE
    return rows


def discover(county):
    """List distinct land-use labels in the county, with parcel counts."""
    params = {
        "where": f"UPPER(CountyName)='{county.upper()}'",
        "outFields": "LandUse,LandUseCode",
        "returnDistinctValues": "true",
        "returnGeometry": "false",
        "orderByFields": "LandUse",
        "f": "json",
    }
    data = get(SUMMARY, params)
    seen = {}
    for f in data.get("features", []):
        a = f["attributes"]
        key = (a.get("LandUse"), a.get("LandUseCode"))
        seen[key] = seen.get(key, 0) + 1
    print(f"\nLand-use labels in {county.upper()} county:\n")
    for (lu, code), _ in sorted(seen.items(), key=lambda x: (x[0][0] or "")):
        print(f"  [{code or '----'}]  {lu}")
    print("\nCopy any that mean a park into PARK_PATTERNS at the top of fetch.py.\n")


def park_where(county):
    likes = " OR ".join(f"UPPER(LandUse) LIKE '%{p}%'" for p in PARK_PATTERNS)
    likes += " OR " + " OR ".join(f"UPPER(UseType) LIKE '%{p}%'" for p in PARK_PATTERNS)
    return f"UPPER(CountyName)='{county.upper()}' AND ({likes})"


def esri_rings_to_geojson(rings):
    """Esri polygon rings -> GeoJSON Polygon/MultiPolygon coordinates."""
    # Esri uses clockwise outer rings and counter-clockwise holes in one array.
    # For display we keep every ring as a polygon; good enough for a park outline.
    return {"type": "Polygon", "coordinates": rings}


def ring_centroid(rings):
    pts = [pt for ring in rings for pt in ring]
    if not pts:
        return None
    x = sum(p[0] for p in pts) / len(pts)
    y = sum(p[1] for p in pts) / len(pts)
    return [x, y]


def count_addresses(rings):
    """Count E-911 address points that fall inside the park polygon."""
    geom = {"rings": rings, "spatialReference": {"wkid": 4326}}
    params = {
        "where": "1=1",
        "geometry": json.dumps(geom),
        "geometryType": "esriGeometryPolygon",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true",
        "f": "json",
    }
    data = get(ADDRESSES, params)
    return data.get("count", 0)


def build(county, out_geojson, out_csv, do_lots=True):
    where = park_where(county)
    print(f"Querying parks in {county.upper()} ...")
    summary = page_query(SUMMARY, where, out_fields=",".join(SUMMARY_FIELDS))
    print(f"  {len(summary)} matching parcels")
    if not summary:
        print("  none found. Run: python fetch.py --discover  and adjust PARK_PATTERNS.")
        return

    by_id = {}
    for f in summary:
        a = f["attributes"]
        pid = a.get("CleanParcelID")
        if pid:
            by_id[pid] = a

    # Pull geometry for those parcels, in batches, from the polygon layer.
    ids = list(by_id.keys())
    features = []
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        inlist = ",".join("'" + b.replace("'", "''") + "'" for b in batch)
        geo = page_query(
            PARCELS,
            where=f"CleanParcelID IN ({inlist})",
            out_fields="CleanParcelID",
        )
        for g in geo:
            pid = g["attributes"].get("CleanParcelID")
            attrs = by_id.get(pid)
            geom = g.get("geometry")
            if not attrs or not geom or "rings" not in geom:
                continue
            rings = geom["rings"]
            lots = count_addresses(rings) if do_lots else None
            ta = attrs.get("TotalAppraisal") or 0
            props = dict(attrs)
            props["lot_count"] = lots
            props["est_market_value"] = round(ta / 0.6) if ta else None
            props["recent_transfer"] = bool((attrs.get("NewOwner") or "").strip())
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": esri_rings_to_geojson(rings),
            })
            if do_lots:
                print(f"  {attrs.get('FullOwnerName','?')[:32]:32}  lots~{lots}")

    fc = {"type": "FeatureCollection", "features": features,
          "meta": {"county": county.upper(), "built": time.strftime("%Y-%m-%d"),
                   "source": "WV GIS Technical Center WV_Parcels service"}}
    with open(out_geojson, "w") as fh:
        json.dump(fc, fh)
    print(f"\nwrote {out_geojson}  ({len(features)} parks)")

    cols = ["FullOwnerName", "FullPhysicalAddress", "lot_count",
            "TotalAppraisal", "est_market_value", "PropertyClassDescription",
            "DeedBook", "DeedPage", "recent_transfer", "NewOwner",
            "ParcelID", "FullOwnerAddress", "DeededAcres", "TaxYear"]
    with open(out_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in features:
            p = f["properties"]
            w.writerow([p.get(c, "") for c in cols])
    print(f"wrote {out_csv}")


def main():
    ap = argparse.ArgumentParser(description="Build a WV county MHP dataset.")
    ap.add_argument("--county", default="MONONGALIA")
    ap.add_argument("--discover", action="store_true",
                    help="list land-use labels in the county and exit")
    ap.add_argument("--no-lots", action="store_true",
                    help="skip the per-park address-point lot count")
    ap.add_argument("--out", default="docs/parks.geojson")
    ap.add_argument("--csv", default="docs/parks.csv")
    args = ap.parse_args()

    if args.discover:
        discover(args.county)
    else:
        build(args.county, args.out, args.csv, do_lots=not args.no_lots)


if __name__ == "__main__":
    main()
