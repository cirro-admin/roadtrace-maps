#!/usr/bin/env python3
"""House numbers for a map region, for search ("Camí de la Galera 12").

    build_addresses.py EXTRACT.osm.pbf OUT.rtaddr --bbox W,S,E,N [--cell]
                       [--extra ADDRESSES.tsv]

With --cell the box is a cell of the 1-degree grid (build_cells.py): its
east and north edges belong to the next cells, and no file is written when
it has no house numbers.

The Protomaps maps hold no house numbers; OpenStreetMap does (Spain's
from the Catastro, Cameroon's hardly any). Every node and building with
addr:housenumber and addr:street (or addr:place) inside the box, a
building at the middle of its outline. Read by lib/search/addresses.dart.

--extra adds addresses from elsewhere (build_cells.py: Overture Maps,
from governments' open address registers), one "street<TAB>number<TAB>
lat<TAB>lng" line each. OpenStreetMap's come first: one with the same
number within 50 m is the same house, and is left out.

Format: gzip, UTF-8 lines. "RTADDR 1", then for each street (sorted)
"=<street>" and one "<number>\\t<lat>\\t<lng>" line per house; the same
number twice on a street is kept once.
"""

import argparse
import gzip
import math
import os
import sys

import osmium


class Addresses(osmium.SimpleHandler):
    def __init__(self, bbox, cell=False):
        super().__init__()
        self.w, self.s, self.e, self.n = bbox
        self.cell = cell
        self.streets = {}

    def _add(self, tags, lat, lng):
        number = tags.get("addr:housenumber")
        street = tags.get("addr:street") or tags.get("addr:place")
        if not number or not street:
            return
        if self.cell:
            if not (self.s <= lat < self.n and self.w <= lng < self.e):
                return
        elif not (self.s <= lat <= self.n and self.w <= lng <= self.e):
            return
        # "12;14" and "12-14" are one building with two numbers.
        for one in number.replace(",", ";").split(";"):
            one = one.strip()
            if one and len(one) <= 12:
                self.streets.setdefault(street.strip(), {}).setdefault(one, (lat, lng))

    def add_extra(self, path):
        """Addresses from a TSV, skipping houses OpenStreetMap already has."""
        near = {}  # (number, grid y, grid x) -> [(lat, lng)]
        for houses in self.streets.values():
            for number, (lat, lng) in houses.items():
                near.setdefault((number,) + _grid(lat, lng), []).append((lat, lng))
        added = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 4:
                    continue
                street, number, lat, lng = parts
                street = tidy_street(street)
                number = number.strip()
                # "S-N", "SN": no number.
                if not street or not any(c.isdigit() for c in number) or len(number) > 12:
                    continue
                lat, lng = float(lat), float(lng)
                if self.cell:
                    if not (self.s <= lat < self.n and self.w <= lng < self.e):
                        continue
                elif not (self.s <= lat <= self.n and self.w <= lng <= self.e):
                    continue
                gy, gx = _grid(lat, lng)
                if any(_metres(lat, lng, a, b) < 50
                       for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                       for a, b in near.get((number, gy + dy, gx + dx), ())):
                    continue
                houses = self.streets.setdefault(street, {})
                if number not in houses:
                    houses[number] = (lat, lng)
                    added += 1
        print(f"{added} addresses added from {path}", file=sys.stderr)

    def node(self, n):
        if "addr:housenumber" in n.tags:
            self._add(n.tags, n.location.lat, n.location.lon)

    def way(self, w):
        if "addr:housenumber" not in w.tags:
            return
        pts = [(nd.location.lat, nd.location.lon) for nd in w.nodes if nd.location.valid()]
        if not pts:
            return
        self._add(w.tags, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _grid(lat, lng):
    # About 220 m north-south: a house within 50 m is in the cell or next.
    return int(math.floor(lat * 500)), int(math.floor(lng * 500 * max(0.1, math.cos(math.radians(lat)))))


def _metres(lat1, lng1, lat2, lng2):
    x = math.radians(lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371000 * math.hypot(x, math.radians(lat2 - lat1))


# Joining words, lower case inside a name.
SMALL = set("de del la las los el les le l d des du da do dos das dels en na "
            "di della delle dello dei degli lo der die am an im zum zur von vom "
            "auf van het ter ten the of y i e et a al au aux".split())
ARTICLES = {"la", "el", "los", "las", "els", "les", "l'"}


def tidy_street(name):
    """Registers write names in capitals, and Spain's Catastro puts the
    article last ("CAMINO GALERA LA"): "Camino la Galera"."""
    name = " ".join(name.split())
    if not name or name != name.upper():
        return name
    words = name.lower().split(" ")
    if len(words) > 2 and words[-1] in ARTICLES:
        words = words[:1] + [words[-1]] + words[1:-1]
    return " ".join(w if i and w in SMALL else w[:1].upper() + w[1:]
                    for i, w in enumerate(words))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--cell", action="store_true")
    ap.add_argument("--extra")
    args = ap.parse_args()
    bbox = [float(v) for v in args.bbox.split(",")]
    h = Addresses(bbox, args.cell)
    h.apply_file(args.pbf, locations=True)
    if args.extra and os.path.exists(args.extra):
        h.add_extra(args.extra)
    if args.cell and not h.streets:
        print("no addresses", file=sys.stderr)
        return
    count = 0
    # mtime=0: the same addresses give the same file, byte for byte.
    with gzip.GzipFile(args.out, "wb", mtime=0) as f:
        f.write(b"RTADDR 1\n")
        for street in sorted(h.streets):
            f.write(f"={street}\n".encode())
            for number, (lat, lng) in h.streets[street].items():
                f.write(f"{number}\t{lat:.6f}\t{lng:.6f}\n".encode())
                count += 1
    print(f"{count} addresses on {len(h.streets)} streets", file=sys.stderr)


if __name__ == "__main__":
    main()
