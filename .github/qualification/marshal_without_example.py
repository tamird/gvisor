"""Run each current marshal test and compare to the original per-library case names."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

import marshal_cases as old
import previous

PRODUCT = "ba161b63861b408e4f9ff1a7e43690570b2eaf55"
TREE = "f7024695c5cc4240ae5ae4ffb3034f2aec2944a9"
BASE = "451b92800b9cb82262dc8a735388f7cf4d17d895"
WORK = old.HERE
OUT = old.OUT
ORACLE = json.loads((WORK / "oracle.json").read_text())
DIRECTORY = Path("tools/go_marshal/qualification_tag_fixture")
LIB = "//" + str(DIRECTORY) + ":qualification_tag_fixture"
SYNTH = LIB + "_amd64_abi_autogen"
MARKED = LIB + "_abi_autogen_test"
COMMON = ["--lockfile_mode=off", "--jobs=4", "--remote_executor=", "--remote_cache=", "--bes_backend="]
SUMMARY = {"product": PRODUCT, "tree": TREE, "parent": BASE, "verified": False, "phases": {}}
CLI = old.CLI
DIST_RC = WORK / "inputs.bazelrc"
CLI.append("--bazelrc=" + str(DIST_RC))


def save(name, value):
    old.save(name, value)


def refresh_dist():
    DIST_RC.write_text("".join("common --distdir=" + str(p) + "\n" for p in sorted(previous.DIRS)))


def checked(name, args):
    commands = SUMMARY.setdefault("commands", {})
    for i in range(12):
        p = subprocess.run([*CLI, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = p.stderr.decode(errors="replace")
        commands.setdefault(name, []).append({
            "exit": p.returncode, "stderr_sha256": hashlib.sha256(p.stderr).hexdigest(),
            "errors": [line[:600] for line in text.splitlines() if "ERROR:" in line][-10:],
        })
        if not p.returncode:
            return p.stdout.decode()
        if not previous.recover(text):
            break
        refresh_dist()
    raise RuntimeError(f"{name} failed; see commands")


def install_fixture():
    DIRECTORY.mkdir()
    (DIRECTORY / "BUILD").write_text(
        'load("//tools:defs.bzl", "go_library")\n'
        'filegroup(name = "common_src", srcs = ["common.go"])\n'
        'go_library(name = "qualification_tag_fixture", '
        'srcs = [":common_src", "value_amd64.go"], marshal = True, stateify = False, nogo = False)\n'
    )
    (DIRECTORY / "common.go").write_text("package qualification_tag_fixture\nconst Always = 1\n")
    (DIRECTORY / "value_amd64.go").write_text(
        "//go:build amd64\n\npackage qualification_tag_fixture\n\n"
        "// +marshal\ntype FixtureValue struct { A uint64 }\n"
    )


def get_files(groups, table):
    stack = list(groups)
    while stack:
        group = table[stack.pop()["id"]]
        yield from group.get("files", [])
        stack.extend(group.get("fileSets", []))


def outputs(arm, cpu, generators):
    key = f"outputs-{arm}-{cpu}"
    bep = WORK / (key + ".bep")
    config = "--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[cpu]
    checked(key, ["build", config, *COMMON, "--build_event_json_file=" + str(bep), *sorted(generators)])
    events = [json.loads(x) for x in bep.read_text().splitlines()]
    sets = {e["id"]["namedSet"]["id"]: e["namedSetOfFiles"] for e in events if "namedSetOfFiles" in e}
    rows = {}
    for e in events:
        target = e["id"].get("targetCompleted", {})
        label = target.get("label")
        if label not in generators or target.get("aspect") or "completed" not in e:
            continue
        assert e["completed"]["success"] and label not in rows, label
        group, = [g for g in e["completed"]["outputGroup"] if g["name"] == "default"]
        name = label.split(":")[1]
        row = {"configuration": target["configuration"]["id"], "files": {}}
        rows[label] = row
        for f in get_files(group["fileSets"], sets):
            uri = urlsplit(f["uri"])
            assert uri.scheme == "file" and not uri.netloc
            path = Path(unquote(uri.path))
            assert path.name not in row["files"] and path.parent.as_posix().endswith(
                "/bin/" + label.split(":")[0][2:]), path
            record = old.report.file_record(path)
            assert not f.get("digest") or f["digest"] == record["sha256"]
            assert "length" not in f or int(f["length"]) == record["bytes"]
            row["files"][path.name] = record
            if arm == "product":
                assert not (path.parent / (name + "_unconditional_test.go")).exists(), label
        required = {name + "_test.go", name + "_unsafe.go"}
        if arm == "base":
            required.add(name + "_unconditional_test.go")
        assert row["files"].keys() == required, label
    save(key, {"bep": old.report.file_record(bep), "owners": rows})
    assert rows.keys() == generators, (key, generators - rows.keys())
    return rows


def run_tests(cpu, labels):
    config = "--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[cpu]
    checked("prepare-" + cpu, ["build", "--nobuild", config, *COMMON,
            "--@bazel_tools//tools/test:incompatible_use_default_test_toolchain=false", *sorted(labels)])
    original = old.output_file

    def retain(items, name, owner):
        path, record = original(items, name, owner)
        dest = OUT / cpu / owner[2:].replace(":", "/") / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        return path, record

    old.output_file = retain
    name = "product-" + cpu
    try:
        cell = old.run(name, cpu, labels, PRODUCT)
    finally:
        old.output_file = original
    bep = WORK / (name + ".bep")
    if bep.exists():
        with (OUT / (name + "-bep.jsonl")).open("w") as compact:
            for line in bep.open():
                event = json.loads(line)
                owner = event["id"].get("targetCompleted", event["id"].get("testResult", {}))
                if "finished" in event or (owner.get("label") in labels and not owner.get("aspect")):
                    compact.write(line)
    checks = {}
    expected = ORACLE["baseline"][cpu]
    for owner, row in cell["tests"].items():
        names = sorted(row.get("cases", {}))
        digest = hashlib.sha256(json.dumps(names, separators=(",", ":")).encode()).hexdigest()
        if owner == MARKED:
            want = {"Test" + stem + "FixtureValue" for stem in (
                "SizeNonZero", "SuspectAlignment", "SafeMarshalUnmarshalPreservesData",
                "WriteToUnmarshalPreservesData", "SizeBytesOnTypedNilPtr")}
            checks[owner] = set(names) == (want if cpu == "amd64" else set())
        else:
            checks[owner] = [len(names), digest] == expected["per_library"][labels[owner]]
    save(cpu + "-verified", {"baseline": expected, "comparison": checks})
    assert cell["verified"] and checks.keys() == labels.keys() and all(checks.values()), name
    assert all(row["elf"]["bytes"] > 0 for row in cell["tests"].values())
    return {"owners": len(labels) - 1, "cases": sum(len(r["cases"]) for k,r in cell["tests"].items() if k != MARKED),
            "runner": "native" if cpu == "amd64" else "qemu-aarch64", "fixture_cases": len(cell["tests"][MARKED]["cases"]),
            "binary_bytes": {k: r["elf"]["bytes"] for k,r in cell["tests"].items()}}


def phase(name, call):
    try:
        value = call()
    except Exception as err:
        SUMMARY["phases"][name] = {"error": f"{type(err).__name__}: {err}"}
        return None
    else:
        SUMMARY["phases"][name] = {"passed": True}
        return value
    finally:
        save("summary", SUMMARY)


def main():
    assert old.read(["git", "rev-parse", "HEAD", "HEAD^{tree}", "HEAD^"]).splitlines() == [PRODUCT, TREE, BASE]
    assert old.machine(old.QEMU) == 62 and os.uname().machine == "x86_64"
    SUMMARY["qemu"] = old.report.file_record(old.QEMU)
    assert old.read([str(WORK / "bazel"), "--version"]) == "bazel 9.3.1"
    previous.OUT = OUT
    previous.save = lambda: save("public-assets", previous.REPORT["assets"])
    refresh_dist()
    install_fixture()
    labels = {lib + "_abi_autogen_test": lib for lib in old.LIBRARIES}
    assert all(set(arm["per_library"]) == old.LIBRARIES for arm in ORACLE["baseline"].values())
    labels[MARKED] = LIB
    gens = set(ORACLE["generators"])
    assert len(gens) == 46 and len(labels) == 21
    scope = " ".join([*("//" + p + ":all" for p in old.PACKAGES), "//" + str(DIRECTORY) + ":all"])
    query = "set(" + scope + ")"
    def selections():
        found = checked("select-owners", ["query", "--lockfile_mode=off", 'filter("_abi_autogen_test$", ' + query + ")"])
        generated = checked("select-generators", ["query", "--lockfile_mode=off", 'kind("go_marshal rule", ' + query + ")"])
        assert set(found.splitlines()) == labels.keys()
        assert set(generated.splitlines()) == gens | {SYNTH}
    phase("selection", selections)
    fixed = {}
    for cpu in ("amd64", "arm64"):
        fixed[cpu] = phase("outputs-" + cpu, lambda: outputs("product", cpu, gens | {SYNTH}))
        SUMMARY[cpu] = phase("tests-" + cpu, lambda: run_tests(cpu, labels))
    subprocess.run(["git", "checkout", "-q", "--detach", BASE], check=True)
    assert old.read(["git", "rev-parse", "HEAD"]) == BASE
    baseline = phase("base-outputs", lambda: outputs("base", "amd64", gens))
    def parity():
        assert baseline
        bad = {}
        for cpu, rows in fixed.items():
            assert rows
            for label in sorted(gens):
                current = rows[label]["files"]
                differences = [k for k,v in current.items() if v != baseline[label]["files"].get(k)]
                if differences:
                    bad[cpu + ":" + label] = differences
        save("output-comparison", {"real_generators": len(gens), "files_per_cpu": len(gens) * 2, "differing": bad})
        assert not bad, bad
    phase("byte-parity", parity)
    SUMMARY["verified"] = all(row.get("passed") for row in SUMMARY["phases"].values())
    save("summary", SUMMARY)
    return SUMMARY["verified"]


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
