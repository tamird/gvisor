"""Qualify the real 16-output test after applying the published sniffer fix."""
import io
import json
from pathlib import Path
import re
import sys
import zipfile

import build_test as q
import build_test_closure


ARCHIVE = "5f88b9686024d36f6dec213b9c3587b00b3204811b7b68f1608978f2b4106be8"
SUPPLIER = "1b96205bdc8a21a33681fd2f2742253aca2c4a51"


def prepare():
    raw = (q.WORK / "prior.zip").read_bytes()
    assert q.sha(raw) == ARCHIVE
    prior = json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("result.json"))
    assert prior["source"] == q.SOURCE and prior["dependencies"] == q.LABELS
    assert prior["choice"] == "defaults" and prior["host_contracts"]["defaults"] == [True, True]
    proof = {}
    for arch in q.ARCH:
        row = prior["cells"]["native-" + arch]
        assert row["contracts"] == {"direct": True, "suite": True}
        assert {a["owner"] for a in row["actions"] if not a["success"] and a["kind"] != "BuildTestFixture"} == {"//pkg/test/dockerutil:run_sniffer_bin"}
        proof[arch] = {"invocation": row["invocation"], "root_program_sha256": row["launcher"]["sha256"],
                       "contracts": row["contracts"], "launcher_machine": row["launcher"]["machine"]}
    q.report.update(previous={"artifact": 13588675951, "sha256": ARCHIVE, "native": proof}, supplier=SUPPLIER, choice="defaults")
    q.save()
    q.main("select")


def physical(arch):
    # The source, destination, platform and target are fixed even if the
    # auxiliary closure reader encounters a construct it cannot interpret.
    row = q.run("root-" + arch, arch, True, True, "test", [q.ROOT])
    conf = q.report["cells"]["graph-" + arch].get("labels", {})
    outputs = {}
    for owner in q.LABELS:
        paths = conf.get(owner, [])
        if len(paths) != 1:
            continue
        path = paths[0]
        bep = row.get("root_default_files", {}).get(path)
        compact = row.get("configured_input_files", {}).get(path)
        if bep and compact and bep["sha256"] and compact["sha256"]:
            assert (bep["sha256"], bep["bytes"]) == (compact["sha256"], compact["bytes"])
        source = compact if compact and compact["sha256"] else bep or {}
        links = [s for s in row.get("spawns", []) if s["owner"] == owner and s["kind"] == "GoLink"]
        completed = [a for a in row.get("actions", []) if a["owner"] == owner and a["kind"] in ("GoLink", "GoTestLink") and a["success"] and a.get("output") == path]
        outputs[owner] = {"producer_completed": bool(completed), "path": path, "root_default": bep is not None, "bytes": source.get("bytes"),
                          "sha256": source.get("sha256"), "go_links": [[s["runner"], s["cache"], s["exit"]] for s in links]}
    row["outputs_by_owner"] = outputs
    declared = set(conf.get(q.ROOT, []))
    dependency_paths = {v["path"] for v in outputs.values()}
    extra = declared - dependency_paths
    row["root_membership"] = (len(dependency_paths) == 16 and dependency_paths <= declared and len(extra) == 1
                              and Path(next(iter(extra))).name == "build_test")
    row["all_outputs"] = row["root_membership"] and len(outputs) == 16 and all((v["bytes"] and re.fullmatch("[0-9a-f]{64}", v["sha256"] or "")) or (v["root_default"] and v["producer_completed"]) for v in outputs.values())
    spawns = [s for s in row.get("spawns", []) if s["owner"] == q.ROOT and s["kind"] == "TestRunner"]
    primary = [s for s in spawns if Path(s["test_program"] or "").name == "build_test"]
    wrapper = {"amd64": "posix_tw", "arm64": "compiled_posix_test_wrapper"}[arch]
    row["actual_root_test"] = (q.passed(row, q.ROOT, arch, True) and len(primary) == 1 and primary[0]["runner"] == "remote" and not primary[0]["cache"]
                               and all(Path(s["exe"] or "").name == wrapper and s["cpu"] == arch and s["image"] and
                                       s["exit"] == 0 for s in spawns)
                               and all(s is primary[0] or s["test_program"] == "--generate-xml" for s in spawns))
    row["verified"] = row["exit"] == 0 and row["targets"].get(q.ROOT) and row["all_outputs"] and row["actual_root_test"]
    q.save()


if sys.argv[1] == "prepare":
    prepare()
elif sys.argv[1] == "scan":
    for arch in q.ARCH:
        build_test_closure.scan(arch)
else:
    for arch in q.ARCH:
        physical(arch)
    q.report["verified"] = all(q.report["cells"]["root-" + arch]["verified"] for arch in q.ARCH)
    q.save()
    if not q.report["verified"]:
        raise SystemExit("See the actual configured owner and test observations")
