#!/usr/bin/env python3
"""Check Puerto Rico address parity in the iPad Simulator using invented fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from urllib.parse import urlencode

IPAD = Path(__file__).resolve().parents[1]
ROOT = IPAD.parents[1]
sys.path.insert(0, str(ROOT))

COLLISION = "19 KM4HM4 BLVD, Las Penas"
EXPLICIT_ROAD = "PR-19 km 4.4, Bo. Las Peñas"
PARCELS = EXPLICIT_ROAD + ", Villa Ejemplo"
TX_NAME = "19 KM4HM4 Boulevard"


def check(condition, detail):
    if not condition:
        raise AssertionError(detail)


def command(*args):
    return subprocess.check_output(args, text=True, timeout=180).strip()


def fixture(filename, cls):
    spec = importlib.util.spec_from_file_location(cls, ROOT / "app/tests" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case = getattr(module, cls)()
    try:
        case.setUp()
    except BaseException:
        case.doCleanups()
        raise
    return case


def index(root, state):
    return root / "derived/geo/addresses" / (state + ".sqlite")


def add_tx(root, *, exact, prefix=False):
    with closing(sqlite3.connect(index(root, "TX"))) as db, db:
        db.execute("CREATE TABLE addr(num,street,norm,city,state,zip,lat,lon)")
        if prefix:
            db.execute("INSERT INTO addr VALUES('19','KM4HM4 Boulevard Extension','km4hm4 blvd extension','Las Penas','TX','75000',7,8)")
        if exact:
            db.execute("INSERT INTO addr VALUES('19','KM4HM4 Boulevard','km4hm4 blvd','Las Penas','TX','75000',5,6)")


def query(sim, tool, root, *requests):
    specs = ["GET /api/places?" + urlencode({"q": q, "parcelOffset": offset}) for q, offset in requests]
    lines = command("xcrun", "simctl", "spawn", sim, str(tool), str(root), str(ROOT), *specs).splitlines()
    check(len(lines) == 2 * len(specs), lines)
    for meta in lines[::2]:
        check(meta.startswith("200 "), meta)
    return [json.loads(line) for line in lines[1::2]]


def test_places(sim, tool):
    road = fixture("test_pr_rural.py", "RuralLookup")
    try:
        add_tx(road.root, exact=False, prefix=True)
        prefix, = query(sim, tool, road.root, (COLLISION, 0))
        check(prefix and prefix[0]["kind"] == "Road kilometer", prefix)
        with closing(sqlite3.connect(index(road.root, "TX"))) as db, db:
            db.execute("INSERT INTO addr VALUES('19','KM4HM4 Boulevard','km4hm4 blvd','Las Penas','TX','75000',5,6)")
        exact, beyond, explicit = query(sim, tool, road.root, (COLLISION, 0), (COLLISION, 1), (EXPLICIT_ROAD, 0))
        check([r["kind"] for r in exact] == ["Street address", "Public school"], exact)
        check(tuple(exact[0][k] for k in ("name", "detail", "lat", "lon")) ==
              (TX_NAME, "Las Penas, TX, 75000", 5, 6), exact)
        check(not beyond and explicit and explicit[0]["kind"] == "Road kilometer", (beyond, explicit))
        print("PASS: exact Texas address precedes the PR road approximation; prefix, school and explicit road controls")
    finally:
        road.doCleanups()

    parcel = fixture("test_pr_parcels.py", "ParcelSearch")
    try:
        add_tx(parcel.root, exact=True)
        first, second, empty, collision = query(sim, tool, parcel.root, (PARCELS, 0), (PARCELS, 20),
                                                 (PARCELS, 24), (COLLISION, 0))
        check((len(first), len(second), len(empty)) == (20, 4, 0), (len(first), len(second), len(empty)))
        rows = first + second
        check([r["parcelNumber"] for r in rows] == [f"001-002-003-{i:02d}" for i in range(1, 25)], rows)
        check(all(r["kind"] == "Possible parcel" and r["parcelCount"] == 24 and r["rings"] for r in rows), rows)
        check(collision and all(r["kind"] == "Possible parcel" for r in collision), collision)
        count = collision[0]["parcelCount"]
        exhausted, = query(sim, tool, parcel.root, (COLLISION, count))
        check(not exhausted, exhausted)
        print("PASS: PR parcels return 20/4/empty pages without duplicates or a restart into Texas")
        with closing(sqlite3.connect(index(parcel.root, "PR"))) as db, db:
            db.execute("DELETE FROM pr_parcel_address")
        fallback, later = query(sim, tool, parcel.root, (COLLISION, 0), (COLLISION, 1))
        check([r["name"] for r in fallback] == [TX_NAME] and not later, (fallback, later))
        print("PASS: ordinary exact address fallback applies on the first page only")
    finally:
        parcel.doCleanups()


def main():
    sim = os.environ.get("LB_SIM")
    if not sim:
        raise SystemExit("Set LB_SIM to the UDID of a booted iPad from xcrun simctl list devices booted.")
    groups = json.loads(command("xcrun", "simctl", "list", "devices", "booted", "-j"))["devices"]
    if not any(d["udid"].upper() == sim.upper() and d["name"].startswith("iPad")
               for devices in groups.values() for d in devices):
        raise SystemExit("LB_SIM must identify a booted iPad simulator.")
    sdk = command("xcrun", "--sdk", "iphonesimulator", "--show-sdk-path")
    sources = [p for area in ("Engine", "Engine/Install", "Engine/AI", "API")
               for p in sorted((IPAD / "Lastbook" / area).glob("*.swift"))]
    with tempfile.TemporaryDirectory(prefix="lastbook-ipad-pr-") as temp:
        tool = Path(temp) / "apidump"
        subprocess.run(["xcrun", "--sdk", "iphonesimulator", "swiftc", "-sdk", sdk,
                        "-target", "arm64-apple-ios18.0-simulator", "-o", str(tool),
                        *map(str, sources), str(IPAD / "tools/apidump/main.swift")],
                       check=True, timeout=300)
        test_places(sim, tool)


if __name__ == "__main__":
    main()
