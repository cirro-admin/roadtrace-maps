#!/usr/bin/env python3
"""The world map on R2, for .github/workflows/world-map.yml.

  pick [YYYYMMDD]       key=<date>.pmtiles, size=<bytes>, present=yes|no
                        (for $GITHUB_OUTPUT): the newest Protomaps build,
                        or the one named
  copy <key> <size>     copies the build to world/<key> in 1 GB parts,
                        4 at a time, each fetched with its own retries
                        (one long stream breaks; a part is just retried)
  cors                  lets roadtrace.app pages read the bucket (range
                        requests), keeping any rule already there
  publish <key> <size>  world/world.json points at world/<key>; world
                        files older than the last two are deleted

Uses the aws CLI with the workflow's R2 environment (R2, BUCKET).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BUILDS = "https://build-metadata.protomaps.dev/builds.json"
BASE_URL = "https://maps.roadtrace.app"
UA = {"User-Agent": "roadtrace-maps/1 (+https://roadtrace.app)"}
ORIGINS = ["https://roadtrace.app"]


def aws(*args, check=True):
    r = subprocess.run(
        ["aws", *args, "--endpoint-url", os.environ["R2"]],
        capture_output=True, text=True,
    )
    if check and r.returncode != 0:
        sys.exit(r.stderr)
    return r


def bucket():
    return os.environ.get("BUCKET", "roadtrace-maps")


def pick(date):
    with urllib.request.urlopen(urllib.request.Request(BUILDS, headers=UA), timeout=60) as r:
        builds = json.load(r)
    if date:
        chosen = next((b for b in builds if b["key"] == f"{date}.pmtiles"), None)
        if chosen is None:
            sys.exit(f"no Protomaps build {date}")
    else:
        chosen = max(builds, key=lambda b: b["key"])
    key, size = chosen["key"], int(chosen["size"])
    head = aws("s3api", "head-object", "--bucket", bucket(), "--key", f"world/{key}", check=False)
    present = head.returncode == 0 and json.loads(head.stdout).get("ContentLength") == size
    print(f"key={key}")
    print(f"size={size}")
    print(f"present={'yes' if present else 'no'}")


PART = int(os.environ.get("WORLD_PART", 1 << 30))
WORKERS = 4


def copy(key, size):
    size = int(size)
    src = os.environ.get("WORLD_SRC", "https://build.protomaps.com") + f"/{key}"
    dest = f"world/{key}"
    # An earlier run that died mid-copy leaves parts stored, and paid for.
    stale = aws("s3api", "list-multipart-uploads", "--bucket", bucket(), "--prefix", "world/", check=False)
    for u in (json.loads(stale.stdout or "{}").get("Uploads") or []):
        aws("s3api", "abort-multipart-upload", "--bucket", bucket(), "--key", u["Key"],
            "--upload-id", u["UploadId"], check=False)
        print("aborted an unfinished upload of", u["Key"])
    up = json.loads(aws("s3api", "create-multipart-upload", "--bucket", bucket(), "--key", dest,
                        "--content-type", "application/octet-stream",
                        "--cache-control", "public, max-age=31536000, immutable").stdout)["UploadId"]
    count = (size + PART - 1) // PART
    tmp = tempfile.mkdtemp()

    def part(n):
        start = (n - 1) * PART
        end = min(size, start + PART) - 1
        path = os.path.join(tmp, f"{n}.part")
        for attempt in range(6):
            got = subprocess.run(
                ["curl", "-fsSL", "--http1.1", "-A", UA["User-Agent"], "-r", f"{start}-{end}", "-o", path, src])
            if got.returncode == 0 and os.path.getsize(path) == end - start + 1:
                r = aws("s3api", "upload-part", "--bucket", bucket(), "--key", dest, "--upload-id", up,
                        "--part-number", str(n), "--body", path, check=False)
                if r.returncode == 0:
                    os.remove(path)
                    print(f"part {n}/{count}", flush=True)
                    return {"PartNumber": n, "ETag": json.loads(r.stdout)["ETag"]}
            time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"part {n} failed")

    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            parts = list(pool.map(part, range(1, count + 1)))
        aws("s3api", "complete-multipart-upload", "--bucket", bucket(), "--key", dest, "--upload-id", up,
            "--multipart-upload", json.dumps({"Parts": parts}))
    except BaseException:
        # Parts left behind would be stored, and paid for, unseen.
        aws("s3api", "abort-multipart-upload", "--bucket", bucket(), "--key", dest, "--upload-id", up, check=False)
        raise
    print("copied", dest)


def cors():
    got = aws("s3api", "get-bucket-cors", "--bucket", bucket(), check=False)
    rules = json.loads(got.stdout).get("CORSRules", []) if got.returncode == 0 else []
    ours = {
        "AllowedOrigins": ORIGINS,
        "AllowedMethods": ["GET", "HEAD"],
        "AllowedHeaders": ["range", "if-match", "if-none-match"],
        "ExposeHeaders": ["etag", "content-range", "content-length", "accept-ranges"],
        "MaxAgeSeconds": 86400,
    }
    rules = [r for r in rules if r.get("AllowedOrigins") != ORIGINS] + [ours]
    aws("s3api", "put-bucket-cors", "--bucket", bucket(),
        "--cors-configuration", json.dumps({"CORSRules": rules}))
    print("cors:", json.dumps(rules))


def publish(key, size):
    world = {
        "date": key.split(".")[0],
        "url": f"{BASE_URL}/world/{key}",
        "bytes": int(size),
        "glyphs": f"{BASE_URL}/world/fonts/{{fontstack}}/{{range}}.pbf",
        "sprites": f"{BASE_URL}/world/sprites/v4",
        "attribution": '<a href="https://openstreetmap.org/copyright">© OpenStreetMap</a>',
    }
    with open("world.json", "w") as f:
        json.dump(world, f, ensure_ascii=False)
    aws("s3", "cp", "world.json", f"s3://{bucket()}/world/world.json",
        "--content-type", "application/json", "--cache-control", "public, max-age=3600")
    # A page opened just before the switch still reads the previous file:
    # keep two, delete the rest.
    listed = aws("s3api", "list-objects-v2", "--bucket", bucket(), "--prefix", "world/",
                 "--delimiter", "/", "--query", "Contents[].Key", "--output", "json")
    keys = sorted(k for k in (json.loads(listed.stdout) or []) if k.endswith(".pmtiles"))
    for old in keys[:-2]:
        if old != f"world/{key}":
            aws("s3", "rm", f"s3://{bucket()}/{old}")
            print("deleted", old)
    print(json.dumps(world))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "pick":
        pick(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "copy":
        copy(sys.argv[2], sys.argv[3])
    elif cmd == "cors":
        cors()
    elif cmd == "publish":
        publish(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
