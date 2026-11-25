#!/usr/bin/env python3
"""Build the offline map regions and their catalogue for maps.roadtrace.app.

    build_maps.py OUT_DIR [--date YYYYMMDD]

For each region in regions.json: a vector map cut from the newest
Protomaps planet build (by HTTP range requests; the planet is never
downloaded whole), and a routing graph built from the Geofabrik extract
with tools/routing/build_graph.py, and house numbers for search from the
same extract (build_addresses.py), with Overture Maps' addresses added. Writes OUT_DIR/osm-<date>/<files> and
OUT_DIR/catalog.json (format version 1, read by lib/map/region_catalog.dart)
pointing at https://maps.roadtrace.app/osm-<date>/.

Needs the `pmtiles` command (protomaps/go-pmtiles), pyosmium and duckdb.
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GRAPH = os.path.join(HERE, "..", "routing", "build_graph.py")
ADDRESSES = os.path.join(HERE, "build_addresses.py")
BASE_URL = "https://maps.roadtrace.app"


def newest_build():
    """The newest daily Protomaps build, looking back a week."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    for back in range(8):
        day = (today - datetime.timedelta(days=back)).strftime("%Y%m%d")
        # The build host refuses Python's default client (403).
        req = urllib.request.Request(
            f"https://build.protomaps.com/{day}.pmtiles",
            method="HEAD",
            headers={"User-Agent": "roadtrace-maps/1 (+https://roadtrace.app)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return day
        except Exception:
            pass
    sys.exit("no Protomaps build in the last week")


def described(path, url):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"file": os.path.basename(path), "url": url, "bytes": os.path.getsize(path), "sha256": h.hexdigest()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--date", help="Protomaps build, YYYYMMDD (default: newest)")
    args = ap.parse_args()

    date = args.date or newest_build()
    folder = f"osm-{date}"
    out = os.path.join(args.out, folder)
    cache = os.path.join(args.out, "osm-cache")
    os.makedirs(out, exist_ok=True)
    os.makedirs(cache, exist_ok=True)
    regions = json.load(open(os.path.join(HERE, "regions.json")))["regions"]

    entries = []
    for r in regions:
        bbox = ",".join(str(v) for v in r["bbox"])
        tiles = os.path.join(out, r["file"] + ".pmtiles")
        print(f"== {r['id']}: map", flush=True)
        subprocess.run(
            ["pmtiles", "extract", f"https://build.protomaps.com/{date}.pmtiles", tiles,
             f"--bbox={bbox}", f"--maxzoom={r['maxzoom']}"],
            check=True,
        )
        # One download per extract, shared by the regions cut from it.
        pbf = os.path.join(cache, os.path.basename(r["osm"]))
        if not os.path.exists(pbf):
            print(f"== downloading {r['osm']}", flush=True)
            subprocess.run(["curl", "-fsSL", "-o", pbf, r["osm"]], check=True)
        graph = os.path.join(out, r["file"] + ".rtgraph")
        print(f"== {r['id']}: routing graph", flush=True)
        cmd = [sys.executable, GRAPH, pbf, graph, "--bbox", bbox]
        if r.get("majorOnly"):
            cmd.append("--major-only")
        subprocess.run(cmd, check=True)
        # House numbers for search; an overview of main roads needs none.
        addresses = None
        if not r.get("majorOnly"):
            addresses = os.path.join(out, r["file"] + ".rtaddr")
            print(f"== {r['id']}: addresses", flush=True)
            subprocess.run([sys.executable, ADDRESSES, pbf, addresses, f"--bbox={bbox}",
                            "--overture"], check=True)

        entry = {
            "id": r["id"],
            "name": r["name"],
            "country": r["country"],
            **described(tiles, f"{BASE_URL}/{folder}/{r['file']}.pmtiles"),
            "bbox": r["bbox"],
            "maxzoom": r["maxzoom"],
            "graph": described(graph, f"{BASE_URL}/{folder}/{r['file']}.rtgraph"),
        }
        if addresses:
            entry["addresses"] = described(addresses, f"{BASE_URL}/{folder}/{r['file']}.rtaddr")
        entries.append(entry)

    catalog = {
        "version": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "source": f"Protomaps basemap build {date} (OpenStreetMap)",
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "routing": "Routing graphs built from Geofabrik OSM extracts with tools/routing/build_graph.py",
        "regions": entries,
    }
    with open(os.path.join(args.out, "catalog.json"), "w") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    print(f"== done: {folder}", flush=True)
    # For the upload step.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"folder={folder}\n")


if __name__ == "__main__":
    main()
