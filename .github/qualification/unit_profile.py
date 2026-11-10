"""Compare the current Make and Bazel unit-test selections, including the runner."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import timer_teardown as public

WORK = Path(__file__).parent
OUT = WORK / "results"
CARRIER = os.environ["GITHUB_SHA"]
OLD = "deb6b3ea1355ea9798c043cf33bc02804e86ed53"
PRODUCT = subprocess.check_output(["git", "rev-parse", CARRIER + "^"], text=True).strip()
OWNER = "//pkg/safecopy:safecopy_x_test"
ROOTS = ["//:all", "//pkg/...", "//tools/...", "//runsc/...", "//vdso/...",
         "//sandboxexec/...", "//examples/...", "//test/runner/...", "//test/trace/..."]
END = time.monotonic() + 59 * 60
BEP = Path(".build_events.json")
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(WORK / "public.bazelrc")]
FLAGS = ["--jobs=3", "--loading_phase_threads=3", "--lockfile_mode=off"]
report = {"product": PRODUCT, "base": OLD, "steps": {}, "checks": {},
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
    runner = sorted(r[0] for r in selected if r[0].startswith("//test/runner"))
    assert "//test/runner:runner" in runner and "//test/runner:runner_test" in runner, runner
    row["runnerTargets"] = runner
    save()
    return selected


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
        compare("selection", lambda: make("make", OLD, "true"),
                lambda: make("profile", PRODUCT, "true"))
        report["complete"] = True
    finally:
        save()
        switch(CARRIER)


if __name__ == "__main__":
    main()
