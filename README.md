# RoadTrace offline maps

Offline map regions for the RoadTrace app, as vector [PMTiles](https://github.com/protomaps/PMTiles) archives.
`catalog.json` (attached to each release) lists the regions, sizes and SHA-256 checksums; the app downloads from it.

## Data and licence

Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, available under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/). Tiles are extracts of the
[Protomaps](https://protomaps.com) basemap build. This repository contains map data and the tools that
build it — no app code and no user data.

## Regenerating

```
pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles <region>.pmtiles --bbox=<w,s,e,n> --maxzoom=15
```

## Routing cells

Routing graphs and house numbers for the whole world, in 1° cells, built from
[Geofabrik](https://download.geofabrik.de) extracts by `tools/maps/build_cells.py`
(`.github/workflows/cells.yml`, a third of the world each month) and published at
`https://maps.roadtrace.app/cells/index.json`. A phone downloads the cells under the
area it keeps offline and joins them for turn-by-turn directions with no network.
See the docstrings in `tools/routing/build_graph.py` for the file format.

House numbers come from OpenStreetMap and from the
[Overture Maps](https://overturemaps.org) addresses theme (governments' open
address registers), read anonymously for each block's box. Their data keeps
its sources' licences and credits.
