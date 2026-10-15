# RoadTrace offline maps

Offline map regions for the RoadTrace app, as vector [PMTiles](https://github.com/protomaps/PMTiles) archives.
`catalog.json` (attached to each release) lists the regions, sizes and SHA-256 checksums; the app downloads from it.

## Data and licence

Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, available under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/). Tiles are extracts of the
[Protomaps](https://protomaps.com) basemap build. This repository contains map data only — no app code
and no user data.

## Regenerating

```
pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles <region>.pmtiles --bbox=<w,s,e,n> --maxzoom=15
```
