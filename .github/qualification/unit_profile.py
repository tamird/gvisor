"""Compare actual Bazel test selections, then execute only the admitted AMD64 owner."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import timer_teardown as public

WORK = Path(__file__).parent
OUT = WORK / "results"
CARRIER = os.environ["GITHUB_SHA"]
OLD = "975fb172241c7946a9f67b4f52e0349d9240589a"
PRODUCT = "3c4df972e49ddaeb43edf2a66fd25ebdda60bb41"
MIXED = "1b8d903a787fa5cd71acd42f78ae97a15a1220af"
OWNER = "//pkg/safecopy:safecopy_x_test"
ROOTS = ["//:all", "//pkg/...", "//tools/...", "//runsc/...", "//vdso/...",
         "//sandboxexec/...", "//examples/...", "//test/trace/..."]
EXTRA = ["//test/rbe:native_syscalls", "//test/packetimpact/tests:mixed_tests"]
STATIC = ["--test_tag_filters=-nogo,-requires-kvm", "--build_tag_filters=-network_plugins"]
END = time.monotonic() + 59 * 60
BEP = Path(".build_events.json")
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(WORK / "public.bazelrc")]
FLAGS = ["--jobs=3", "--loading_phase_threads=3", "--lockfile_mode=off"]
report = {"product": PRODUCT, "base": OLD, "mixed": MIXED, "steps": {}, "checks": {},
          "inputs": public.seen, "complete": False}


def save():
    (OUT / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def switch(source):
    subprocess.run(["git", "checkout", "--detach", "-q", source], check=True)
    assert git("rev-parse", "HEAD") == source


def prefetch():
    public.assets.DIST = WORK / "dist"
    public.assets.DIST.mkdir(exist_ok=True)
    public.dirs.clear()
    public.dirs.add(public.assets.DIST)
    rows = json.loads((WORK / "inputs.json").read_bytes())
    defaults = json.loads((WORK / "bazel-defaults.json").read_bytes())
    assert len(defaults) == 27 and sum(r[4] for r in defaults) == 45503548
    public.known.update((r[2], r) for r in rows + defaults)
    errors = report["preload_errors"] = {}
    for directory, row in public.inputs.seed(defaults):
        if directory is None:
            errors[row["source"]] = row["unavailable"]
        else:
            public.dirs.add(directory)
            public.seen[row["source"]] = row
    save()


def snapshot(name, row):
    events = [json.loads(line) for line in BEP.read_bytes().splitlines() if line]
    configs, kinds, roots, tests, patterns, canon = {}, {}, set(), [], [], {}
    proof = []
    for event in events:
        key = event["id"]
        if "configuration" in event:
            configs[key["configuration"]["id"]] = event["configuration"]
        if "configured" in event:
            label = key["targetConfigured"]["label"]
            kinds[label] = event["configured"]
        if "targetCompleted" in key:
            target = key["targetCompleted"]
            if not target.get("aspect") and (event.get("aborted", {}).get("reason") == "NO_BUILD"
                    or event.get("completed", {}).get("success")):
                roots.add((target["label"], target["configuration"]["id"]))
        if "expanded" in event and "pattern" in key:
            patterns.extend(key["pattern"].get("pattern", []))
        if "testResult" in event:
            tests.append(event)
            if name == "run-one" and key["testResult"]["label"] == OWNER:
                for item in event["testResult"]["testActionOutput"]:
                    if item["name"] not in ("test.log", "test.xml"):
                        continue
                    uri = urlsplit(item["uri"])
                    assert uri.scheme == "file" and not uri.netloc
                    b = Path(unquote(uri.path)).read_bytes()
                    assert 0 < len(b) < 5 << 20
                    assert not item.get("digest") or item["digest"] == hashlib.sha256(b).hexdigest()
                    if "length" in item:
                        assert len(b) == int(item["length"])
                    (OUT / item["name"]).write_bytes(b)
        if "structuredCommandLine" in event and event["structuredCommandLine"].get("commandLineLabel") == "canonical":
            for section in event["structuredCommandLine"]["sections"]:
                for option in section.get("optionList", {}).get("option", []):
                    if option.get("optionName") in ("test_tag_filters", "build_tag_filters", "test_env", "build_tests_only"):
                        canon.setdefault(option["optionName"], []).append(option["optionValue"])
        if set(key) & {"pattern", "targetConfigured", "targetCompleted", "configuration", "testResult", "buildFinished"}:
            proof.append(event)
    data = ("\n".join(json.dumps(e, separators=(",", ":")) for e in proof) + "\n").encode()
    (OUT / (name + ".events.jsonl.gz")).write_bytes(gzip.compress(data, mtime=0))
    # A target that Bazel tagged as filtered may still have a configured event.
    # Keep only owners that were admitted to the action graph.
    selected = sorted((label, config, kinds.get(label, {}).get("targetKind", ""),
                       kinds.get(label, {}).get("testSize", "")) for label, config in roots)
    row.update(patterns=patterns, options=canon, roots=len(selected),
               test_rules=sum(bool(r[3]) for r in selected), results=len(tests),
               samples=[r for r in selected if r[0] in (OWNER, "//pkg/buffer:buffer", "//:release")],
               event_sha256=hashlib.sha256(data).hexdigest())
    save()
    return selected, tests


def invoke(name, command):
    row = report["steps"][name] = {"source": git("rev-parse", "HEAD"), "attempts": []}
    save()
    for _ in range(32):
        if BEP.exists():
            BEP.unlink()
        (WORK / "public.bazelrc").write_text("".join("common --distdir=" + str(d) + "\n" for d in sorted(public.dirs)))
        remaining = END - time.monotonic()
        if remaining < 20:
            raise TimeoutError("qualification deadline")
        p = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=remaining)
        log = p.stdout.decode(errors="replace")
        errors = [s[:450] for s in log.splitlines() if s.startswith("ERROR:") and "?" not in s]
        row["attempts"].append({"exit": p.returncode, "errors": errors[-4:]})
        save()
        if not p.returncode:
            return snapshot(name, row)
        if BEP.is_file():
            _, results = snapshot(name, row)
            if results:
                break
        if "Error downloading [" not in log or not public.acquire(log):
            break
    raise ValueError(name + " did not complete")


def make(name, source, cgroup):
    switch(source)
    # These caller options occur before the unit test selection on both arms.
    # The actual Make command must leave the recipe's filters/cgroup effective.
    flags = FLAGS + ["--nobuild", "--test_tag_filters=requires-kvm", "--build_tag_filters=network_plugins",
                     "--test_env=CGROUPV2=caller"]
    selected, results = invoke(name, ["make", "DOCKER_BUILD=false", "unit-tests", "CGROUPV2=" + cgroup,
                                      "BAZEL=" + " ".join(BAZEL), "BAZEL_OPTIONS=" + " ".join(flags)])
    row = report["steps"][name]
    norm = [p if p.startswith("//") else "//" + p for p in row["patterns"]]
    assert norm == ROOTS, norm
    assert row["options"]["test_tag_filters"][-1] == "-nogo,-requires-kvm", row["options"]
    assert row["options"]["build_tag_filters"][-1] == "-network_plugins", row["options"]
    assert [v for v in row["options"]["test_env"] if v.startswith("CGROUPV2=")][-1] == "CGROUPV2=" + cgroup
    assert not results and row["roots"] > 2000 and row["test_rules"] > 200
    assert any(r[0] == OWNER and r[3] for r in selected) and any(r[0] == "//:release" and not r[3] for r in selected)
    return selected


def mixed(name, composed):
    switch(MIXED)
    path = Path(".bazelrc")
    old_rc = path.read_text()
    combined = '--config=source-presubmit-mixed'
    source_profile = [s for s in git("show", PRODUCT + ":.bazelrc").splitlines() if s.startswith("test:unit-tests ")]
    target_file = Path("test/rbe/source-presubmit.targets")
    roots = [s for s in target_file.read_text().splitlines() if s and not s.startswith("#")]
    assert roots == ROOTS + EXTRA
    old_filters = "test:source-presubmit-common " + " ".join(STATIC)
    old_file = "test:source-presubmit-common --target_pattern_file=test/rbe/source-presubmit.targets"
    assert old_rc.count(old_filters) == old_rc.count(old_file) == 1
    try:
        if composed:
            path.write_text(old_rc.replace(old_filters, "test:source-presubmit-common --config=unit-tests")
                            .replace(old_file, "test:source-presubmit-common " + " ".join(EXTRA))
                            + "\n" + "\n".join(source_profile) + "\n")
        # Only analysis takes place, with the historical registered target and
        # exec platforms. The coordinator is disconnected from the RBE service.
        selected, results = invoke(name, BAZEL + ["test", combined, *FLAGS, "--nobuild", "--remote_executor=",
                "--remote_cache=", "--bes_backend=", "--spawn_strategy=local", "--strategy=TestRunner=local",
                "--build_event_json_file=" + str(BEP)])
        suites = {r[0] for r in selected}
        packet = {l for l in suites if l.startswith("//test/packetimpact/tests:") and
                  ("_native_test" in l or "_netstack_test" in l)}
        native = {"//test/rbe:netfilter_syscalls", "//test/rbe:netfilter_syscalls_presubmit_arm64",
                  "//test/syscalls:uname_test_native", "//test/syscalls:uname_test_native_presubmit_arm64"}
        assert len(packet) == 164 and native <= suites and not results, (len(packet), native - suites)
        assert len(selected) > 2500
        return selected
    finally:
        path.write_text(old_rc)


def native():
    switch(PRODUCT)
    # The second source rule with this tag is manual. Always check the
    # resulting single owner in the real profile before executing the test.
    args = BAZEL + ["test", "--config=unit-tests", *FLAGS, "--build_tests_only",
                    "--test_tag_filters=not_run:arm,-manual,-nogo", "--test_env=CGROUPV2=true",
                    "--test_filter=^TestCheckXstateSuccess$", "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1",
                    "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
                    "--build_event_json_file=" + str(BEP)]
    selected, tests = invoke("admit-one", args + ["--nobuild"])
    assert {r[0] for r in selected if r[3]} == {OWNER} and not tests, selected[:12]
    _, tests = invoke("run-one", args)
    event, = tests
    result, ident = event["testResult"], event["id"]["testResult"]
    row = report["steps"]["run-one"]
    row.update(owner=ident["label"], config=ident["configuration"]["id"], status=result["status"],
               execution=result.get("executionInfo", {}), cached=result.get("cachedLocally", False))
    save()
    assert ident["label"] == OWNER and ident["configuration"]["id"] in {r[1] for r in selected if r[0] == OWNER}
    assert result["status"] == "PASSED" and not row["cached"] and not row["execution"].get("cachedRemotely")
    assert row["execution"].get("strategy") in ("local", "standalone", "linux-sandbox", "processwrapper-sandbox")
    assert b"--- PASS: TestCheckXstateSuccess" in (OUT / "test.log").read_bytes()
    xml = ElementTree.parse(OUT / "test.xml")
    assert [s.get("name") for s in xml.iter("testcase") if s.get("name") == "TestCheckXstateSuccess"] == ["TestCheckXstateSuccess"]
    assert not any(s.tag in ("skipped", "failure", "error") for s in xml.iter())


def check(name, function):
    try:
        function()
        report["checks"][name] = {"pass": True}
    except Exception as exc:
        report["checks"][name] = {"pass": False, "reason": repr(exc)[:450]}
    save()


def compare(name, old, new):
    values = {}
    errors = {}
    for side, run in (("old", old), ("new", new)):
        try:
            values[side] = set(run())
        except Exception as exc:
            errors[side] = repr(exc)[:280]
    if errors:
        raise ValueError(errors)
    left, right = values["old"], values["new"]
    report[name] = {"rows": len(left), "missing": sorted(left-right)[:10], "extra": sorted(right-left)[:10]}
    assert left == right, report[name]


def main():
    save()
    prefetch()
    try:
        for value in ("true","false"):
            check("make-" + value, lambda: compare("selection-" + value,
                  lambda: make("old-" + value, OLD, value), lambda: make("new-" + value, PRODUCT, value)))
        check("one-physical-owner", native)
        check("mixed-composition", lambda: compare("mixed-selection", lambda: mixed("old-mixed", False),
                                                  lambda: mixed("new-mixed", True)))
    finally:
        switch(CARRIER)
    report["complete"] = len(report["checks"]) == 4 and all(v["pass"] for v in report["checks"].values())
    save()
    if not report["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
