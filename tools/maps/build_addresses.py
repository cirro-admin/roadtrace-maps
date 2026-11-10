#!/usr/bin/env python3
"""House numbers for a map region, for search ("Camí de la Galera 12").

    build_addresses.py EXTRACT.osm.pbf OUT.rtaddr --bbox W,S,E,N [--cell]

With --cell the box is a cell of the 1-degree grid (build_cells.py): its
east and north edges belong to the next cells, and no file is written when
it has no house numbers.

The Protomaps maps hold no house numbers; OpenStreetMap does (Spain's
from the Catastro, Cameroon's hardly any). Every node and building with
addr:housenumber and addr:street (or addr:place) inside the box, a
building at the middle of its outline. Read by lib/search/addresses.dart.

Format: gzip, UTF-8 lines. "RTADDR 1", then for each street (sorted)
"=<street>" and one "<number>\\t<lat>\\t<lng>" line per house; the same
number twice on a street is kept once.
"""

import argparse
import gzip
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--cell", action="store_true")
    args = ap.parse_args()
    bbox = [float(v) for v in args.bbox.split(",")]
    h = Addresses(bbox, args.cell)
    h.apply_file(args.pbf, locations=True)
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
