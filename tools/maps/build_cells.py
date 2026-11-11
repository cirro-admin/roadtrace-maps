#!/usr/bin/env python3
"""Routing graphs and house numbers for the world, in 1-degree cells.

    build_cells.py plan BLOCKS            print the blocks to build, as JSON
    build_cells.py block BLOCK OUT_DIR    build one 10-degree block's cells
    build_cells.py index OUT BLOCKS_DIR [CURRENT]
                                          index.json: CURRENT (the one
                                          published) with the listings in
                                          BLOCKS_DIR swapped in

BLOCKS is "all", or block ids separated by spaces or commas ("n00e010").
A cell is named by its south-west corner: n04e011 is 4-5 N, 11-12 E; a
block is the 10x10 cells from a corner on a multiple of 10.

Data comes from Geofabrik's extracts (download.geofabrik.de), the smallest
ones (leaves of its index), so no machine ever needs the whole planet. For
a block: each extract that reaches it is downloaded once and cut into its
cells, with a margin (osmium extract); a cell that several extracts reach
(at a border) gets their pieces merged (osmium merge), so roads crossing
the border join. Then, per cell:

    <cell>.rtgraph        every road (build_graph.py --cell)
    <cell>.major.rtgraph  main roads only, for long trips
    <cell>.rtaddr         house numbers (build_addresses.py --cell), from
                          OpenStreetMap and Overture Maps' addresses
                          (governments' open registers: about 550 million
                          in some 40 countries, none yet in Africa)

A file with nothing in it is not written. OUT_DIR/<block>.json lists what
was (format below); the app reads it through index.json (lib/map/
area_downloads.dart):

    {"version": 1, "block": "n00e010", "date": "20261201",
     "cells": {"n04e011": {"graph": {file, url, bytes, sha256},
                            "major": {...}, "addresses": {...}}}}

Needs osmium-tool (the `osmium` command), pyosmium and duckdb. Overture is
read anonymously from its public S3 bucket, for the block's box only; if
that fails the block is built with OpenStreetMap's numbers alone.
"""

import concurrent.futures
import datetime
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GRAPH = os.path.join(HERE, "..", "routing", "build_graph.py")
ADDRESSES = os.path.join(HERE, "build_addresses.py")
BASE_URL = "https://maps.roadtrace.app/cells"
GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1.json"
UA = {"User-Agent": "roadtrace-maps/1 (+https://roadtrace.app)"}

# Extracts that only repeat others (unions of countries or states).
OVERLAPPING = {
    "dach", "alps", "britain-and-ireland", "us-midwest", "us-northeast",
    "us-pacific", "us-south", "us-west", "us", "sea", "great-britain",
    "south-africa-and-lesotho",
}


def cell_id(lat, lng):
    return f"{'s' if lat < 0 else 'n'}{abs(lat):02d}{'w' if lng < 0 else 'e'}{abs(lng):03d}"


def parse_id(cid):
    lat = int(cid[1:3]) * (-1 if cid[0] == "s" else 1)
    lng = int(cid[4:7]) * (-1 if cid[3] == "w" else 1)
    return lat, lng


def block_of(lat, lng):
    return cell_id(math.floor(lat / 10) * 10, math.floor(lng / 10) * 10)


def geofabrik_leaves(cache):
    """Every smallest extract: (id, pbf url, cells it reaches)."""
    path = os.path.join(cache, "geofabrik-index.json")
    if not os.path.exists(path):
        req = urllib.request.Request(GEOFABRIK_INDEX, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f)
    features = json.load(open(path))["features"]
    parents = {f["properties"].get("parent") for f in features}
    out = []
    for f in features:
        p = f["properties"]
        if p["id"] in parents or p["id"] in OVERLAPPING or not f.get("geometry"):
            continue
        pbf = p.get("urls", {}).get("pbf")
        if not pbf:
            continue
        geom = f["geometry"]
        polygons = geom["coordinates"] if geom["type"] == "MultiPolygon" \
            else [geom["coordinates"]]
        cells = set()
        # Polygon by polygon: islands and lands across the date line do not
        # make one box around half the world.
        for poly in polygons:
            xs = [pt[0] for pt in poly[0]]
            ys = [pt[1] for pt in poly[0]]
            box = (max(min(xs), -180), max(min(ys), -90),
                   min(max(xs), 180), min(max(ys), 90))
            cells.update(cells_of(box))
        out.append((p["id"], pbf, cells))
    return out


def cells_of(bbox):
    w, s, e, n = bbox
    return [
        (lat, lng)
        for lat in range(math.floor(s), math.ceil(n))
        for lng in range(math.floor(w), math.ceil(e))
    ]


def plan(blocks, cache):
    leaves = geofabrik_leaves(cache)
    have = sorted({block_of(lat, lng) for _, _, cells in leaves for lat, lng in cells})
    if blocks == ["all"]:
        return have
    return [b for b in blocks if b in have]


def described(path, url):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"file": os.path.basename(path), "url": url,
            "bytes": os.path.getsize(path), "sha256": h.hexdigest()}


def edge_count(rtgraph):
    with open(rtgraph, "rb") as f:
        head = f.read(16)
    return struct.unpack("<I", head[12:16])[0]


def overture_addresses(block, work):
    """Overture's addresses in the block, a TSV per cell: {cell id: path}."""
    try:
        sys.path.insert(0, HERE)
        from build_addresses import fetch_overture
        blat, blng = parse_id(block)
        tsv = os.path.join(work, "overture.tsv")
        release = fetch_overture(blng, blat, blng + 10, blat + 10, tsv)
        files, out = {}, {}
        rows = 0
        with open(tsv, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 4:
                    continue
                cid = cell_id(math.floor(float(parts[2])), math.floor(float(parts[3])))
                if cid not in files:
                    out[cid] = os.path.join(work, f"overture-{cid}.tsv")
                    files[cid] = open(out[cid], "w", encoding="utf-8")
                files[cid].write(line)
                rows += 1
        for f in files.values():
            f.close()
        os.remove(tsv)
        print(f"== {block}: {rows} Overture addresses ({release})", flush=True)
        return out
    except Exception as e:  # noqa: BLE001 - OpenStreetMap's numbers still go out
        print(f"== {block}: no Overture addresses ({e})", flush=True)
        return {}


def build_cell(cid, parts, out, date, extra=None):
    """One cell from its pieces; returns (cid, entry) or (cid, None)."""
    lat, lng = parse_id(cid)
    bbox = f"{lng},{lat},{lng + 1},{lat + 1}"
    work = os.path.dirname(parts[0])
    src = parts[0]
    if len(parts) > 1:
        src = os.path.join(work, "merged.osm.pbf")
        subprocess.run(["osmium", "merge", "--overwrite", "-o", src, *parts],
                       check=True, capture_output=True)
    entry = {}
    for key, name, cmd in [
        ("graph", f"{cid}.rtgraph", [GRAPH]),
        ("major", f"{cid}.major.rtgraph", [GRAPH, "--major-only"]),
        ("addresses", f"{cid}.rtaddr",
         [ADDRESSES] + ([f"--extra={extra}"] if extra else [])),
    ]:
        target = os.path.join(out, name)
        subprocess.run([sys.executable, cmd[0], src, target, f"--bbox={bbox}",
                        "--cell", *cmd[1:]], check=True, capture_output=True)
        # The address builder writes nothing for a cell without numbers.
        if extra and key == "addresses" and os.path.exists(extra):
            os.remove(extra)
        if not os.path.exists(target):
            continue
        if key != "addresses" and edge_count(target) == 0:
            os.remove(target)
            continue
        entry[key] = described(target, f"{BASE_URL}/{date}/{name}")
    shutil.rmtree(work, ignore_errors=True)
    return cid, entry or None


def build_block(block, out, date, cache):
    blat, blng = parse_id(block)
    os.makedirs(out, exist_ok=True)
    work = os.path.join(cache, "work", block)
    os.makedirs(work, exist_ok=True)
    pieces = {}  # cell id -> [pbf piece per extract]
    for leaf, url, reach in geofabrik_leaves(cache):
        cells = sorted((lat, lng) for lat, lng in reach
                       if blat <= lat < blat + 10 and blng <= lng < blng + 10)
        if not cells:
            continue
        pbf = os.path.join(cache, "extract.osm.pbf")
        print(f"== {block}: {leaf} ({len(cells)} cells)", flush=True)
        subprocess.run(["curl", "-fsSL", "--retry", "3", "-A", UA["User-Agent"],
                        "-o", pbf, url], check=True)
        # One cell per pass of the extract: osmium keeps id tables of 2 GB
        # or more per output, and a runner has little more. "simple" (the
        # parts of roads inside the box) needs the least; the box is the
        # cell with a 0.1° margin, so a road crossing the cell's edge keeps
        # its next point outside (the cell's own edge is applied by the
        # builders).
        per_pass = 1
        for i in range(0, len(cells), per_pass):
            config = {"extracts": []}
            for lat, lng in cells[i:i + per_pass]:
                cid = cell_id(lat, lng)
                d = os.path.join(work, cid)
                os.makedirs(d, exist_ok=True)
                piece = os.path.join(d, f"{leaf.replace('/', '_')}.osm.pbf")
                config["extracts"].append(
                    {"output": piece,
                     # Clamped: osmium refuses a box past the date line
                     # or a pole.
                     "bbox": [max(lng - 0.1, -180), max(lat - 0.1, -90),
                              min(lng + 1.1, 180), min(lat + 1.1, 90)]})
                pieces.setdefault(cid, []).append(piece)
            cfg = os.path.join(cache, "extract.json")
            json.dump(config, open(cfg, "w"))
            subprocess.run(["osmium", "extract", "--overwrite", "-s", "simple",
                            "-c", cfg, pbf], check=True, capture_output=True)
        os.remove(pbf)

    extra = overture_addresses(block, os.path.join(cache, "work"))
    cells = {}
    workers = os.cpu_count() or 2
    with concurrent.futures.ProcessPoolExecutor(workers) as pool:
        jobs = [pool.submit(build_cell, cid, parts, out, date, extra.get(cid))
                for cid, parts in sorted(pieces.items())]
        for job in concurrent.futures.as_completed(jobs):
            cid, entry = job.result()
            if entry:
                cells[cid] = entry
                sizes = ", ".join(f"{k} {v['bytes'] // 1000} KB" for k, v in entry.items())
                print(f"   {cid}: {sizes}", flush=True)
    listing = {"version": 1, "block": block, "date": date,
               "cells": dict(sorted(cells.items()))}
    with open(os.path.join(out, f"{block}.json"), "w") as f:
        json.dump(listing, f, separators=(",", ":"))
    print(f"== {block}: {len(cells)} cells", flush=True)


def index(out, blocks_dir, current):
    """index.json: the current one with these blocks' listings swapped in."""
    idx = {"version": 1, "blocks": {}}
    if current and os.path.exists(current):
        idx = json.load(open(current))
    for name in sorted(os.listdir(blocks_dir)):
        if not name.endswith(".json"):
            continue
        listing = json.load(open(os.path.join(blocks_dir, name)))
        idx["blocks"][listing["block"]] = \
            f"{BASE_URL}/{listing['date']}/{listing['block']}.json"
    idx["generated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    with open(out, "w") as f:
        json.dump(idx, f, separators=(",", ":"), sort_keys=True)
    print(f"{len(idx['blocks'])} blocks in the index")


def main():
    args = sys.argv[1:]
    cache = os.environ.get("CELLS_CACHE", "cache")
    os.makedirs(cache, exist_ok=True)
    if args[:1] == ["plan"]:
        blocks = " ".join(args[1:]).replace(",", " ").split()
        print(json.dumps(plan(blocks, cache)))
    elif args[:1] == ["block"]:
        date = os.environ.get("CELLS_DATE") or \
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
        build_block(args[1], args[2], date, cache)
    elif args[:1] == ["index"]:
        index(args[1], args[2], args[3] if len(args) > 3 else None)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
