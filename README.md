# WV Mobile Home Park Atlas

A self-hosted, CoStar-style index of West Virginia mobile home parks, built
from public state parcel data. No scraping. The whole thing is one static site
plus one Python script.

Proof of concept scope: **Monongalia County**. The same pipeline fans out to
all 55 counties by changing one flag.

## How it works

Everything comes from one public service run by the WV GIS Technical Center at
WVU (it backs the state's own WV Property Viewer and Flood Tool, so it is meant
to be queried and needs no key):

```
https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/WV_Parcels/MapServer
  /11  ParcelSummary       owner, owner mailing address, land use, assessed
                           values, deed book/page, and NewOwner (the incoming
                           transfer, before it fully records)
  /0   WVParcels           parcel polygons, joined to /11 on CleanParcelID
  /5   Site Address Points  E-911 / SAMS points, used to count lots
```

`fetch.py` filters ParcelSummary to parks by land use, pulls the matching
polygons, counts the E-911 address points inside each one for a lot estimate,
and writes `docs/parks.geojson` and `docs/parks.csv`. The site reads those two
files. That is the entire loop.

## Run it

Needs Python 3 and internet. From the repo root:

```bash
# 1. See exactly how this county labels its parks, then tune PARK_PATTERNS
#    at the top of fetch.py if needed.
python fetch.py --discover

# 2. Build the dataset into docs/
python fetch.py
```

Other options:

```bash
python fetch.py --county KANAWHA        # any WV county
python fetch.py --no-lots               # skip the per-park address-point count
```

Open `docs/index.html` in a browser to preview locally.

## Publish on GitHub Pages

1. Push this repo to GitHub.
2. Settings > Pages > Build from branch > `main` > `/docs`.
3. The site goes live at `https://<you>.github.io/<repo>/`.

The table is the front page; the map is one click away. Same two-page shape as
the Section 8 site you described.

## What each field means

`Assessed` is the county's appraised value. WV assesses at 60% of market, so the
site shows `Est. market = Assessed / 0.6`. `Lots` is the number of E-911 address
points that fall inside the parcel, which is a solid pad-count proxy. `Deed` is
the current recorded book/page. `Transfer pending` means the record already
carries a `NewOwner`, so a sale is in flight.

## Known limits (and the honest gaps)

- **Sale price is not in this data.** WV assessment records carry the deed
  reference but not the amount. To get price you pull the deed at the Monongalia
  County Clerk and back it out from the transfer stamps (WV excise tax, about
  $1.10 per $500 of value). That is the one piece that stays semi-manual.
- **Park classification is only as good as the county's coding.** `--discover`
  exists so you can see the real labels and confirm nothing is being missed.
  Validate the first county against your existing 42-park list before trusting
  the filter, then fan out.
- **Data vintage is Tax Year 2023** on the statewide layer. Good enough to map
  and to establish a baseline; the roadmap below is how it becomes live.

## Roadmap

1. **Sale detection.** Run `fetch.py` on a schedule (GitHub Actions cron),
   commit each run, and diff `FullOwnerName` between snapshots. Any change is a
   flagged transaction. Then pull only those deeds for price. Watching the
   `NewOwner` field gives you a head start on the ones already in flight.
2. **Ownership piercing.** Join `FullOwnerName` to the WV Secretary of State
   business search for registered agent and organizers, then cluster by shared
   mailing address to link LLCs back to real principals.
3. **Statewide.** Loop all 55 counties into one dataset, keep the same site.

## Files

```
fetch.py            ingestion (state service -> docs/parks.geojson + .csv)
docs/index.html     table page
docs/map.html       map page (Leaflet)
docs/app.js         shared data loading + formatting
docs/style.css      shared styles
docs/parks.geojson  data (ships with sample rows; fetch.py overwrites)
```
