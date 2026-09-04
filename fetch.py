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
import re
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


def page_query(url, where, out_fields="*", want_geometry=False, out_sr=4326):
    """Query an ArcGIS layer, paging until all records are returned."""
    rows = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true" if want_geometry else "false",
            "outSR": out_sr,
            "resultOffset": offset,
            "resultRecordCount": PAGE,
            "f": "json",
        }
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


# ---------------------------------------------------------------------------
# Park-name enrichment. Free, no API key. Two passes:
#   1) the street name off the 911 address (park drives often carry the name)
#   2) OpenStreetMap around the parcel, for anything the street name misses
# ---------------------------------------------------------------------------

OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "wv-mhp-atlas/1.0 (SCH Properties internal tool)"

# If OpenStreetMap starts failing, stop hammering it and finish on street names.
_OSM = {"fails": 0, "off": False}

# If the street ends in one of these, it is a public road, not a park name.
ROAD_SUFFIX = {
    "RD", "ROAD", "ST", "STREET", "AVE", "AVENUE", "DR", "DRIVE", "LN", "LANE",
    "WAY", "HWY", "HIGHWAY", "PIKE", "BLVD", "CIR", "CIRCLE", "PL", "PLACE",
    "ROUTE", "RT", "RUN", "HOLLOW", "HOLW", "BRANCH", "FORK", "PATH", "TRL",
    "TRAIL", "LOOP", "BEND", "CROSSING", "XING", "PKWY",
}
# If the street ends in one of these, it is almost certainly the park name.
PARK_SUFFIX = {
    "VILLAGE", "ESTATES", "ESTATE", "PARK", "ACRES", "MANOR", "TERRACE",
    "MEADOWS", "COURT", "CT", "COMMUNITY", "MHP", "MHC", "VILLA", "VILLAS",
    "HEIGHTS", "GARDENS", "GROVE", "COMMONS",
}


_ACRONYMS = {"MHP", "MHC", "LLC", "RV", "II", "III"}


def _titlecase(s):
    out = []
    for w in s.split():
        out.append(w.upper() if w.upper() in _ACRONYMS else w.capitalize())
    return " ".join(out)


# Common abbreviations that show up in park street names.
_ABBR = {
    "VLG": "VILLAGE", "VLGE": "VILLAGE", "VLGS": "VILLAGES",
    "ESTS": "ESTATES", "MDWS": "MEADOWS", "HTS": "HEIGHTS",
    "GRV": "GROVE", "TER": "TERRACE", "TERR": "TERRACE",
    "TRLR": "TRAILER", "MBL": "MOBILE", "HMS": "HOMES",
    "CMTY": "COMMUNITY", "CT": "COURT", "PK": "PARK",
}

_STATES = {"WV", "VA", "MD", "PA", "OH", "KY"}


def _street_tokens(props):
    """Uppercase street words with house number, city, state, and ZIP removed."""
    raw = (props.get("SAMSAddress") or "").strip()
    if not raw:
        raw = (props.get("FullPhysicalAddress") or "").split(",")[0].strip()
    city_words = set((props.get("SAMSCity") or "").upper().split())
    toks = [t for t in re.split(r"\s+", raw.upper()) if t]
    if toks and re.fullmatch(r"\d+[A-Z]?", toks[0]):
        toks = toks[1:]                       # drop leading house number
    while toks:                               # drop trailing city / state / zip / stray numbers
        t = toks[-1]
        if (re.fullmatch(r"\d{5}(-\d{4})?", t) or re.fullmatch(r"\d+", t)
                or t in _STATES or t in city_words):
            toks.pop()
        else:
            break
    return toks


def name_from_street(props):
    """Park name derived from the street, or '' if it's a plain public road."""
    toks = _street_tokens(props)
    if not toks:
        return ""
    toks = [_ABBR.get(t, t) for t in toks]    # expand abbreviations
    if toks[-1] in ROAD_SUFFIX:
        inner = toks[:-1]
        # keep only if the road name itself is park-ish, e.g. "Sunrise Village Rd"
        if inner and any(w in PARK_SUFFIX for w in inner):
            toks = inner
        else:
            return ""                         # plain road, not a park name
    return _titlecase(" ".join(toks))


def _centroid_lonlat(rings):
    pts = rings[0] if rings else []
    if not pts:
        return None
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return lon, lat


def name_from_osm(lat, lon):
    """Nearest named mobile-home-ish feature in OpenStreetMap, or ''."""
    if _OSM["off"]:
        return ""
    ql = (
        "[out:json][timeout:8];("
        f'nwr(around:220,{lat},{lon})["name"]["landuse"="residential"];'
        f'nwr(around:220,{lat},{lon})["name"]["tourism"="caravan_site"];'
        f'nwr(around:220,{lat},{lon})["name"]["place"="neighbourhood"];'
        ");out center tags;"
    )
    try:
        body = urlencode({"data": ql}).encode()
        req = Request(OVERPASS, data=body, headers={"User-Agent": UA})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        _OSM["fails"] = 0
    except Exception:
        _OSM["fails"] += 1
        if _OSM["fails"] >= 3:
            _OSM["off"] = True
            print("  OpenStreetMap slow/unreachable; finishing on street names only")
        return ""
    best, best_d = "", 1e9
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        if "lat" in el:
            elat, elon = el["lat"], el["lon"]
        elif "center" in el:
            elat, elon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        blob = (tags.get("residential", "") + " " + name).lower()
        parky = any(w in blob for w in (
            "trailer", "mobile", "manufactured", "caravan", "mhp", "mhc",
            "village", "estates", "park", "court", "community"))
        if tags.get("tourism") == "caravan_site" or parky:
            d = (elat - lat) ** 2 + (elon - lon) ** 2
            if d < best_d:
                best_d, best = d, name
    return best


def enrich_name(props, rings):
    """Set props['park_name'] and props['name_source'] using the free waterfall."""
    name = name_from_street(props)
    src = "street" if name else ""
    if not name:
        c = _centroid_lonlat(rings)
        if c:
            lon, lat = c
            name = name_from_osm(lat, lon)
            src = "osm" if name else ""
            if not _OSM["off"]:
                time.sleep(0.4)   # be a good OpenStreetMap citizen
    props["park_name"] = name
    props["name_source"] = src


def build(county, out_geojson, out_csv, do_lots=True, do_names=True, min_lots=0):
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
        pid = (a.get("CleanParcelID") or "").strip()
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
            want_geometry=True,          # the fix: we need the shapes here
        )
        for g in geo:
            pid = (g["attributes"].get("CleanParcelID") or "").strip()
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

    if not features:
        print("  parks matched but no geometry came back. Send Claude the run log.")

    # Drop very small parks if a threshold was given (we don't chase those).
    if min_lots and any(f["properties"].get("lot_count") is not None for f in features):
        before = len(features)
        features = [f for f in features
                    if (f["properties"].get("lot_count") or 0) >= min_lots]
        print(f"  kept {len(features)} parks with >= {min_lots} lots (from {before})")

    # Resolve park names: street name first, then OpenStreetMap. Free, no key.
    if do_names:
        print("Resolving park names ...")
        for f in features:
            enrich_name(f["properties"], f["geometry"]["coordinates"])
            p = f["properties"]
            label = p.get("park_name") or "(unnamed)"
            print(f"  {label[:34]:34}  [{p.get('name_source') or '-'}]")
    else:
        for f in features:
            f["properties"].setdefault("park_name", "")
            f["properties"].setdefault("name_source", "")

    fc = {"type": "FeatureCollection", "features": features,
          "meta": {"county": county.upper(), "built": time.strftime("%Y-%m-%d"),
                   "source": "WV GIS Technical Center WV_Parcels service"}}
    with open(out_geojson, "w") as fh:
        json.dump(fc, fh)
    print(f"\nwrote {out_geojson}  ({len(features)} parks)")

    cols = ["park_name", "name_source", "FullOwnerName", "FullPhysicalAddress",
            "lot_count", "TotalAppraisal", "est_market_value",
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
    ap.add_argument("--no-names", action="store_true",
                    help="skip park-name resolution (street name + OpenStreetMap)")
    ap.add_argument("--min-lots", type=int, default=0,
                    help="drop parks with fewer than this many lots (0 = keep all)")
    ap.add_argument("--out", default="docs/parks.geojson")
    ap.add_argument("--csv", default="docs/parks.csv")
    args = ap.parse_args()

    if args.discover:
        discover(args.county)
    else:
        build(args.county, args.out, args.csv,
              do_lots=not args.no_lots,
              do_names=not args.no_names,
              min_lots=args.min_lots)


if __name__ == "__main__":
    main()
