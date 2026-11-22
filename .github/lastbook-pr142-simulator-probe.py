#!/usr/bin/env python3
"""Scratch-only iPad Simulator probe. Public invented data only; not an app change."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path.cwd()
APP = "local.lastbook.ipad"
ENTRY = ROOT / "mobile/ipad/Lastbook/Shell/LastbookApp.swift"
SWIFT = r"""
        #if DEBUG && targetEnvironment(simulator)
        if UserDefaults.standard.bool(forKey: "LBFixtureProbe") {
            let input = Library.documents.appendingPathComponent("lb-fixture-input.json")
            let output = Library.documents.appendingPathComponent("lb-fixture-output.json")
            do {
                let data = try Data(contentsOf: input)
                guard let queries = try JSONSerialization.jsonObject(with: data) as? [[String: String]],
                      Library.shared.root == Library.inApp else {
                    NSLog("iPad fixture probe rejected: bad input or no sandbox library")
                    return
                }
                let answers: [[String: Any]] = queries.map { query in
                    let response = Router.shared.route(APIRequest(method: "GET", path: "/api/places",
                                                                  query: query, headers: [:], body: nil))
                    let value: Any
                    if case .data(let payload) = response.body,
                       let parsed = try? JSONSerialization.jsonObject(with: payload) {
                        value = parsed
                    } else { value = NSNull() }
                    return ["status": response.status, "body": value]
                }
                try JSONSerialization.data(withJSONObject: answers, options: [.sortedKeys])
                    .write(to: output, options: .atomic)
            } catch { NSLog("iPad fixture probe failed: %@", String(describing: error)) }
        }
        #endif
"""

def inject():
    marker = "        Router.shared.install(Routes.all)\n"
    text = ENTRY.read_text()
    assert text.count(marker) == 1, "App startup hook no longer unique"
    ENTRY.write_text(text.replace(marker, marker + SWIFT, 1))

def command(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True, timeout=60)

def fixture(test_file, test_class, target):
    path = ROOT / "app/tests" / test_file
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    case = getattr(mod, test_class)()
    try:
        case.setUp()
        shutil.copytree(case.root / "derived", target / "derived")
    finally:
        case.doCleanups()
    (target / "library-data").mkdir(parents=True)
    (target / "library-data/catalog.json").write_text("[]\n")

def tx(target, exact):
    db = sqlite3.connect(target / "derived/geo/addresses/TX.sqlite")
    try:
        db.execute("CREATE TABLE addr(num,street,norm,city,state,zip,lat,lon)")
        db.execute("INSERT INTO addr VALUES('19','KM4HM4 Boulevard Extension','km4hm4 blvd extension','Las Penas','TX','75000',7,8)")
        if exact:
            db.execute("INSERT INTO addr VALUES('19','KM4HM4 Boulevard','km4hm4 blvd','Las Penas','TX','75000',5,6)")
        db.commit()
    finally:
        db.close()

def simrun(udid, docs, root, requests):
    command("xcrun", "simctl", "terminate", udid, APP, check=False)
    target = docs / "LastbookData"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(root, target)
    output = docs / "lb-fixture-output.json"
    output.unlink(missing_ok=True)
    (docs / "lb-fixture-input.json").write_text(json.dumps([{"q": q, "parcelOffset": str(off)} for q, off in requests]))
    launch = command("xcrun", "simctl", "launch", udid, APP, "-LBFixtureProbe", "YES")
    for _ in range(45):
        if output.exists():
            break
        time.sleep(1)
    assert output.exists(), f"Probe did not produce JSON ({launch.stdout.strip()})"
    results = json.loads(output.read_text())
    assert len(results) == len(requests)
    assert all(r["status"] == 200 and type(r["body"]) == list for r in results), results
    return [r["body"] for r in results]

def run(udid):
    info = command("xcrun", "simctl", "get_app_container", udid, APP, "data")
    docs = Path(info.stdout.strip()) / "Documents"
    docs.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lastbook-pr142-synthetic-") as scratch:
        stage = Path(scratch)
        prefix = stage / "prefix"
        exact = stage / "exact"
        parcels = stage / "parcels"
        empty = stage / "empty"
        fixture("test_pr_rural.py", "RuralLookup", prefix)
        fixture("test_pr_rural.py", "RuralLookup", exact)
        fixture("test_pr_parcels.py", "ParcelSearch", parcels)
        tx(prefix, False)
        tx(exact, True)
        tx(parcels, True)
        shutil.copytree(parcels, empty)
        db = sqlite3.connect(empty / "derived/geo/addresses/PR.sqlite")
        try:
            db.execute("DELETE FROM pr_parcel_address")
            db.commit()
        finally:
            db.close()
        collision = "19 KM4HM4 BLVD, Las Penas"
        explicit = "PR-19 km 4.4, Bo. Las Peñas"
        paging = explicit + ", Villa Ejemplo"

        def prefix_case():
            first, higher = simrun(udid, docs, prefix, [(collision, 0), (collision, 1)])
            assert (first[0]["kind"], first[0]["name"]) == ("Road kilometer", "PR-19 km 4.4"), first
            assert not higher, higher

        def exact_case():
            first, higher, road = simrun(udid, docs, exact, [(collision, 0), (collision, 1), (explicit, 0)])
            assert [r["kind"] for r in first] == ["Street address", "Public school"], first
            assert tuple(first[0][k] for k in ("name", "detail", "lat", "lon")) == ("19 KM4HM4 Boulevard", "Las Penas, TX, 75000", 5, 6), first
            assert not higher, higher
            assert (road[0]["kind"], road[0]["name"]) == ("Road kilometer", "PR-19 km 4.4"), road

        def parcel_case():
            first, second, exhausted, candidates = simrun(
                udid, docs, parcels, [(paging, 0), (paging, 20), (paging, 100), (collision, 0)])
            assert (len(first), len(second)) == (20, 4), (len(first), len(second))
            ids = [r["parcelNumber"] for r in first + second]
            assert ids == [f"001-002-003-{i:02d}" for i in range(1, 25)], ids
            assert all(r["kind"] == "Possible parcel" and r["parcelCount"] == 24 and r["rings"] for r in first + second)
            assert first[0]["name"] == "Solar 1 · PR-19 km 4.4", first[0]
            assert second[-1]["name"] == "Solar 24 · PR-19" and "km unrecorded" in second[-1]["detail"], second[-1]
            assert not exhausted, exhausted
            assert candidates and all(r["kind"] == "Possible parcel" for r in candidates), candidates
            count = candidates[0]["parcelCount"]
            # Run the exhausted ambiguous query without changing the fixture.
            at, beyond = simrun(udid, docs, parcels, [(collision, count), (collision, count+1)])
            assert not at and not beyond, (at, beyond)

        def fallback_case():
            first, higher = simrun(udid, docs, empty, [(collision, 0), (collision, 1)])
            assert [r["name"] for r in first] == ["19 KM4HM4 Boulevard"], first
            assert not higher, higher

        reports = []
        for desc, test in [("TX prefix does not displace PR road", prefix_case),
                           ("Exact TX > PR approximation; school and explicit PR road preserved", exact_case),
                           ("PR parcel pages (20 + 4) and no restart after exhaustion", parcel_case),
                           ("Zero-offset ordinary fallback after no PR match", fallback_case)]:
            try:
                test()
                reports.append((desc,"PASS"))
            except Exception as e:
                reports.append((desc,"FAIL"))
                print(f"{desc}: {e!r}", file=sys.stderr)
        summary = "\n".join(f"{result}: {desc}" for desc,result in reports)
        print(summary)
        if path := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(path, "a") as out:
                out.write("\n### Native iPad app route, using synthetic indexes\n\n" + summary + "\n")
        if any(result != "PASS" for _,result in reports):
            raise SystemExit(1)

if __name__ == "__main__":
    if sys.argv[1:] == ["--instrument"]:
        inject()
    else:
        run(sys.argv[1])
