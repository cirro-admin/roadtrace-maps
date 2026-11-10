#!/usr/bin/env python3
"""Build a RoadTrace routing graph (.rtgraph) from an OpenStreetMap extract.

    build_graph.py input.osm.pbf output.rtgraph --bbox w,s,e,n [--major-only] [--cell]

With --cell the box is one cell of the phone's 1-degree grid (see
tools/maps/build_cells.py), and neighbouring cells join on the phone
(mergeGraphs in lib/routing/route_graph.dart): each segment of road belongs
to the cell its first point is in (west/south edges inclusive, east/north
exclusive), so no road is in two cells, and the far point of a segment
that crosses a cell edge is a junction in both cells, at the same
coordinates. Networks that touch a cell edge are kept whatever their size,
since they may continue next door.

File format v4 (little-endian), read by lib/routing/route_graph.dart:
    magic 'RTGR', u16 version=4, u16 flags (bit0 major-only, bit1 cell)
    u32 nodeCount, u32 edgeCount
    i32 minLat, minLng, maxLat, maxLng (degrees * 1e7)
    i32 lat[n], i32 lng[n]                  (degrees * 1e7)
    u32 firstEdge[n + 1]                    (CSR: edges of node i are
                                             firstEdge[i] .. firstEdge[i+1])
    u32 target[m], u32 lengthDm[m]          (decimetres)
    u32 shapeStart[m], u32 shapeCount[m]    (intermediate points of edge j;
                                             both directions of a road share one
                                             run of points)
    i32 shapeLat[k], i32 shapeLng[k]
    u8  access[m]                           (bit0 car, bit1 motorcycle, bit2 foot,
                                             bit7 = walk the shape backwards)
    u8  roadClass[m]                        (see CLASSES)
    zero padding to a multiple of 4 bytes
    u32 nameIndex[m]                        (0 = unnamed, else 1 + index into
                                             the name table)
    u32 nameCount, u32 nameEnd[nameCount]   (UTF-8 bytes of name i end at
                                             nameEnd[i]; start at nameEnd[i-1])
    u8  nameBytes[]
v3 is the same without the padding and the name table.
Edges are directed and run junction to junction: chains of road with no
junction are collapsed into one edge that keeps its shape points, so the
graph a phone searches is several times smaller than the OSM node count.
One-way restrictions apply to car and motorcycle; on foot every way is
two-way.
"""
import argparse
import math
import struct
import sys

import osmium

CLASSES = [
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "track", "path", "steps",
    "pedestrian",
]
HIGHWAY_CLASS = {
    "motorway": 0, "motorway_link": 0, "trunk": 1, "trunk_link": 1,
    "primary": 2, "primary_link": 2, "secondary": 3, "secondary_link": 3,
    "tertiary": 4, "tertiary_link": 4, "unclassified": 5, "road": 5,
    "residential": 6, "living_street": 7, "service": 8, "track": 9,
    "path": 10, "footway": 10, "cycleway": 10, "bridleway": 10,
    "steps": 11, "pedestrian": 12,
}
CAR, MOTO, FOOT = 1, 2, 4
NO = {"no", "private", "agricultural", "forestry", "delivery"}


def access_for(tags, cls):
    vehicle = cls <= 9
    a = (CAR | MOTO) if vehicle else 0
    if cls != 0 and tags.get("foot") not in NO:
        a |= FOOT
    if tags.get("access") in NO:
        a &= ~(CAR | MOTO)
        if tags.get("foot") not in ("yes", "designated", "permissive"):
            a &= ~FOOT
    if tags.get("vehicle") in NO or tags.get("motor_vehicle") in NO:
        a &= ~(CAR | MOTO)
    if tags.get("motorcar") in NO:
        a &= ~CAR
    if tags.get("motorcycle") in NO:
        a &= ~MOTO
    if tags.get("foot") in ("yes", "designated", "permissive"):
        a |= FOOT
    return a


# Barriers on a road (a node tag): what they stop. Gates stop traffic only
# when tagged closed to it; foot always passes.
STOPS_VEHICLES = {
    "block", "jersey_barrier", "log", "rock", "chain", "kissing_gate",
    "stile", "turnstile", "full-height_turnstile", "motorcycle_barrier",
    "cycle_barrier", "debris", "barrier_board",
}
STOPS_CARS = {"bollard", "bus_trap", "planter"}
GATES = {"gate", "lift_gate", "swing_gate", "sliding_gate", "entrance"}


def barrier_stops(tags):
    """Access bits a barrier node removes, after its own access tags."""
    b = tags.get("barrier")
    if b in STOPS_VEHICLES:
        stop = CAR | MOTO
    elif b in STOPS_CARS:
        stop = CAR
    elif b in GATES:
        closed = tags.get("access") in NO or tags.get("motor_vehicle") in NO
        stop = (CAR | MOTO) if closed else 0
    else:
        return 0
    if tags.get("motor_vehicle") in ("yes", "designated", "permissive") or \
            tags.get("access") in ("yes", "designated", "permissive"):
        stop = 0
    if tags.get("motorcar") in ("yes", "designated", "permissive"):
        stop &= ~CAR
    if tags.get("motorcycle") in ("yes", "designated", "permissive"):
        stop &= ~MOTO
    return stop


def oneway(tags, cls):
    o = tags.get("oneway")
    if o in ("yes", "1", "true"):
        return 1
    if o == "-1":
        return -1
    if o == "no":
        return 0
    if tags.get("junction") in ("roundabout", "circular") or cls == 0:
        return 1
    return 0


class Collector(osmium.SimpleHandler):
    def __init__(self, bbox, major_only, cell=False):
        super().__init__()
        self.w, self.s, self.e, self.n = bbox
        self.major_only = major_only
        self.cell = cell
        self.border = set()  # node indices where a road crosses the cell edge
        self.ids = {}
        self.lat = []
        self.lng = []
        self.segs = []  # (a, b, lengthDm, fwdAccess, revAccess, cls, name)
        self.barriers = {}  # OSM node id -> access bits it stops

    def node(self, n):
        if "barrier" in n.tags:
            stop = barrier_stops(n.tags)
            if stop:
                self.barriers[n.id] = stop

    def inside(self, loc):
        if self.cell:
            return self.w <= loc.lon < self.e and self.s <= loc.lat < self.n
        return self.w <= loc.lon <= self.e and self.s <= loc.lat <= self.n

    def node_index(self, ref, loc):
        i = self.ids.get(ref)
        if i is None:
            i = len(self.lat)
            self.ids[ref] = i
            self.lat.append(round(loc.lat * 1e7))
            self.lng.append(round(loc.lon * 1e7))
        return i

    def way(self, w):
        hw = w.tags.get("highway")
        cls = HIGHWAY_CLASS.get(hw)
        if cls is None or w.tags.get("area") == "yes":
            return
        if self.major_only and cls > 5:
            return
        acc = access_for(w.tags, cls)
        if acc == 0:
            return
        ow = oneway(w.tags, cls)
        # Spoken in turn instructions: the street name, else its reference
        # ("N3"). Interned so a long road shares one string.
        name = sys.intern(w.tags.get("name") or w.tags.get("ref") or "")
        nodes = [n for n in w.nodes if n.location.valid()]
        last = len(nodes) - 2
        for k, (a, b) in enumerate(zip(nodes, nodes[1:])):
            a_in, b_in = self.inside(a.location), self.inside(b.location)
            if self.cell:
                if not a_in:
                    # The cell next door has this segment; where it comes
                    # in is a junction here too.
                    if b_in:
                        self.border.add(self.node_index(b.ref, b.location))
                    continue
                if not b_in:
                    self.border.add(self.node_index(b.ref, b.location))
            elif not (a_in or b_in):
                continue
            ia = self.node_index(a.ref, a.location)
            ib = self.node_index(b.ref, b.location)
            d = haversine(a.location.lat, a.location.lon, b.location.lat, b.location.lon)
            dm = max(1, round(d * 10))
            fwd = acc if ow >= 0 else acc & FOOT
            rev = acc if ow <= 0 else acc & FOOT
            # A barrier closes the stretch of road just past it (just
            # before it at the end of a way): one short segment, so both
            # sides of it stay reachable.
            stop = self.barriers.get(a.ref, 0)
            if k == last:
                stop |= self.barriers.get(b.ref, 0)
            fwd &= ~stop
            rev &= ~stop
            if ia != ib and fwd | rev:
                self.segs.append((ia, ib, dm, fwd, rev, cls, name))


def haversine(lat1, lng1, lat2, lng2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def largest_component(n, segs, border=frozenset()):
    """Keep the largest connected component: islands of road (a parking lot,
    a mapping error) would otherwise make snapping pick a road you cannot
    leave. Components touching [border] are kept too: they may join the
    largest in the next cell (the phone keeps the largest once joined)."""
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, *_ in segs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    sizes = {}
    for i in range(n):
        r = find(i)
        sizes[r] = sizes.get(r, 0) + 1
    best = max(sizes, key=sizes.get)
    roots = {best} | {find(i) for i in border}
    return [find(i) in roots for i in range(n)]


def contract(n, segs, forced=frozenset()):
    """Collapse degree-2 chains with identical attributes into single edges;
    [forced] nodes stay junctions. Returns (junction flags, list of
    (a, b, dm, fwd, rev, cls, via_nodes))."""
    inc = [[] for _ in range(n)]
    for k, (a, b, *_rest) in enumerate(segs):
        inc[a].append(k)
        inc[b].append(k)

    def attrs(k, start):
        a, b, d, f, r, c, nm = segs[k]
        return (f, r, c, nm) if a == start else (r, f, c, nm)

    junction = [False] * n
    for v in range(n):
        if len(inc[v]) != 2 or v in forced:
            junction[v] = True
        else:
            k1, k2 = inc[v]
            # Through v: entering by k1 (towards v), leaving by k2 (from v).
            a1 = attrs(k1, segs[k1][0] if segs[k1][1] == v else segs[k1][1])
            a2 = attrs(k2, v)
            if a1 != a2 or k1 == k2:
                junction[v] = True

    used = [False] * len(segs)
    out = []

    def walk(start, k):
        via, length = [], 0
        f, r, c, nm = attrs(k, start)
        cur = start
        while True:
            used[k] = True
            a, b, d, *_ = segs[k]
            nxt = b if a == cur else a
            length += d
            if junction[nxt] or nxt == start:
                return (start, nxt, length, f, r, c, nm, via)
            via.append(nxt)
            k = inc[nxt][0] if inc[nxt][1] == k else inc[nxt][1]
            cur = nxt

    for v in range(n):
        if junction[v]:
            for k in inc[v]:
                if not used[k]:
                    out.append(walk(v, k))
    # Pure loops with no junction at all: break them at an arbitrary node.
    for k in range(len(segs)):
        if not used[k]:
            v = segs[k][0]
            junction[v] = True
            out.append(walk(v, k))
    return junction, out


def simplify(points, lat, lng, a, b, tol_m=2.0):
    """Douglas-Peucker on the chain a, *points, b (indices), in metres.
    Returns the kept intermediate indices."""
    pts = [a] + points + [b]
    if len(pts) <= 2:
        return []
    k = math.cos(math.radians(lat[a] / 1e7))
    xy = [(lng[i] / 1e7 * 111320 * k, lat[i] / 1e7 * 110540) for i in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1.0, -1
        for t in range(i + 1, j):
            x, y = xy[t]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / L
            if d > best:
                best, bi = d, t
        if best > tol_m:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [pts[t] for t in range(1, len(pts) - 1) if keep[t]]


def write(path, c, bbox, major_only):
    n = len(c.lat)
    keep = largest_component(n, c.segs, c.border) if c.segs else [False] * n
    segs = [s for s in c.segs if keep[s[0]] and keep[s[1]]]
    junction, chains = contract(n, segs, c.border)
    ends = {x for ch in chains for x in ch[:2]}
    remap, lat, lng = {}, [], []
    for i in range(n):
        if keep[i] and junction[i] and i in ends:
            remap[i] = len(lat)
            lat.append(c.lat[i])
            lng.append(c.lng[i])
    edges = []  # (from, to, dm, access, cls, shapeStart, shapeCount, name)
    slat, slng = [], []
    names = {}
    for a, b, d, f, r, cls, nm, via in chains:
        ni = 0
        if nm:
            ni = names.setdefault(nm, len(names)) + 1
        via = simplify(via, c.lat, c.lng, a, b)
        start = len(slat)
        for v in via:
            slat.append(c.lat[v])
            slng.append(c.lng[v])
        if f:
            edges.append((remap[a], remap[b], d, f, cls, start, len(via), ni))
        if r:
            edges.append((remap[b], remap[a], d, r | 0x80, cls, start, len(via), ni))
    edges.sort(key=lambda e: (e[0], e[1]))
    n, m = len(lat), len(edges)
    first = [0] * (n + 1)
    for e in edges:
        first[e[0] + 1] += 1
    for i in range(n):
        first[i + 1] += first[i]
    w, s, e_, nn = bbox
    with open(path, "wb") as f:
        f.write(b"RTGR")
        flags = (1 if major_only else 0) | (2 if c.cell else 0)
        f.write(struct.pack("<HHII", 4, flags, n, m))
        f.write(struct.pack("<iiii", round(s * 1e7), round(w * 1e7), round(nn * 1e7), round(e_ * 1e7)))
        f.write(struct.pack(f"<{n}i", *lat))
        f.write(struct.pack(f"<{n}i", *lng))
        f.write(struct.pack(f"<{n + 1}I", *first))
        f.write(struct.pack(f"<{m}I", *[x[1] for x in edges]))
        f.write(struct.pack(f"<{m}I", *[x[2] for x in edges]))
        f.write(struct.pack(f"<{m}I", *[x[5] for x in edges]))
        f.write(struct.pack(f"<{m}I", *[x[6] for x in edges]))
        f.write(struct.pack(f"<{len(slat)}i", *slat))
        f.write(struct.pack(f"<{len(slng)}i", *slng))
        f.write(bytes(x[3] for x in edges))
        f.write(bytes(x[4] for x in edges))
        f.write(bytes((-2 * m) % 4))
        f.write(struct.pack(f"<{m}I", *[x[7] for x in edges]))
        encoded = [k.encode("utf-8") for k in names]  # insertion order = index
        ends, total = [], 0
        for e in encoded:
            total += len(e)
            ends.append(total)
        f.write(struct.pack(f"<I{len(ends)}I", len(ends), *ends))
        f.write(b"".join(encoded))
    return n, m, len(slat)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--bbox", required=True, help="w,s,e,n")
    ap.add_argument("--major-only", action="store_true")
    ap.add_argument("--cell", action="store_true")
    args = ap.parse_args()
    bbox = [float(x) for x in args.bbox.split(",")]
    c = Collector(bbox, args.major_only, args.cell)
    c.apply_file(args.input, locations=True, idx="flex_mem")
    n, m, k = write(args.output, c, bbox, args.major_only)
    print(f"{args.output}: {n} junctions, {m} edges, {k} shape points, "
          f"{len(c.barriers)} barriers", file=sys.stderr)


if __name__ == "__main__":
    main()
