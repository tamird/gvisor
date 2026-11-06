"""Retain the libc selection, actual TestRunner receipts and the first build error."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


ROOT = Path(os.environ["RUNNER_TEMP"], "musl-race-smoke")
SOURCE = "3dab3494e2a2154244e8b2fc77699c02c2277a56"
LABEL = "//test/rbe/musl_smoke:guest"
RACE = ["//runsc:runsc-race", "//runsc/cmd/sentry:gvisor_sentry-race"]
FLAGS = ["-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
REPORT = {"source": SOURCE, "llvm": "0.8.24", "original_link_artifact": 13459164255,
          "passed_normal_guest_artifact": 13507627714, "arms": {}}


def write():
    (ROOT / "receipt.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def bazel(tag, args, cap):
    path = ROOT / (tag + ".log")
    with path.open("wb") as log:
        try:
            status = subprocess.run([str(ROOT / "bazel"), "--nosystem_rc", "--nohome_rc", *args],
                                    stdout=log, stderr=subprocess.STDOUT, timeout=cap).returncode
        except subprocess.TimeoutExpired:
            status = 124
    return status, path


def relevant(text):
    return [line[:800] for line in text.splitlines() if not line.startswith("GUEST_RECEIPT ")
            and re.search(r"error:|Error in |undefined reference|unable to find|undefined symbol|undeclared|unavailable|permission denied|Analysis of target|Traceback|^AssertionError", line, re.I)][:20]

def producers(mode, flags, runtime):
    labels = RACE if mode == "race" else [label.removesuffix("-race") for label in RACE]
    names = dict(zip(labels, ("runsc", "gvisor-bin/gvisor_sentry")))
    names.update({
        "//shim:containerd-shim-runsc-v1": "containerd-shim-runsc-v1",
        "//runsc/checkpointgofer:checkpointgofer_binary": "gvisor-bin/checkpointgofer",
        "//runsc/cmd/metricserver:runsc-metric-server": "gvisor-bin/runsc-metric-server",
        "//runsc/cmd/netgofer:runsc-netgofer": "gvisor-bin/runsc-netgofer",
        "//runsc/fdparking:fdparking_binary": "gvisor-bin/runsc-fd-parking",
        "//runsc/prewarmer:prewarmer_binary": "gvisor-bin/gvisor-sentry-prewarmer",
        "//runsc/procumounter:procumounter_binary": "gvisor-bin/gvisor-proc-umounter",
    })
    formatter = '"PRODUCER %s|%s" % (target.label, target.files.to_list()[0].path)'
    code, log = bazel(mode + "_producers", ["cquery", *flags, "--output=starlark",
                                           "--starlark:expr=" + formatter, "set(%s)" % " ".join(names)], 300)
    out = {"exit": code, "binaries": {}}
    for line in log.read_text(errors="replace").splitlines():
        if not line.startswith("PRODUCER "):
            continue
        owner, path = line.removeprefix("PRODUCER ").split("|")
        owner = "//" + owner.split("//", 1)[1]
        assert owner in names and path.startswith("bazel-out/") and ".." not in Path(path).parts
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        name = names[owner]
        out["binaries"][owner] = {"file": path, "sha256": digest,
                                  "matches_install": digest == runtime["binaries"][name]["staged_sha256"]}
    return out


def test(mode):
    flags = [*FLAGS, *(["--config=race"] if mode == "race" else [])]
    bep = ROOT / (mode + ".bep")
    status, output = bazel(mode, ["test", *flags, "--test_strategy=standalone", "--nocache_test_results",
                                  "--test_env=GLIBC_TUNABLES=glibc.pthread.rseq=0", "--test_output=errors",
                                  "--build_event_json_file=" + str(bep), "--color=no", "--curses=no", LABEL], 2500)
    row = REPORT["arms"][mode] = {"bazel_exit": status, "results": [], "diagnostics": relevant(output.read_text(errors="replace"))}
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                identity = event["id"].get("testResult", {})
                if identity.get("label") != LABEL:
                    continue
                result = event["testResult"]
                entry = {"status": result["status"], "cached": bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely")),
                         "strategy": result.get("executionInfo", {}).get("strategy")}
                row["results"].append(entry)
                for item in result.get("testActionOutput", []):
                    if item["name"] != "test.log":
                        continue
                    uri = urlsplit(item["uri"])
                    assert uri.scheme == "file" and uri.netloc == ""
                    raw = Path(unquote(uri.path)).read_bytes()
                    entry["test_log_sha256"] = hashlib.sha256(raw).hexdigest()
                    entry["test_log_bytes"] = len(raw)
                    text = raw.decode(errors="replace")
                    entry["diagnostics"] = relevant(text)
                    receipts = [json.loads(s.removeprefix("GUEST_RECEIPT ")) for s in text.splitlines() if s.startswith("GUEST_RECEIPT ")]
                    if receipts:
                        entry["runtime"] = receipts[-1]
    write()
    for result in row["results"]:
        runtime = result.get("runtime", {})
        if "gvisor-bin/gvisor_sentry" in runtime.get("binaries", {}):
            row["producers"] = producers(mode, flags, runtime)
            write()


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    write()
    expr = 'filter("(_impl|_musl)$", deps(set(%s), 2))' % " ".join(RACE)
    formatter = '"%s %s" % (target.label, build_options(target).get("//command_line_option:platforms", []))'
    code, log = bazel("selection", ["cquery", *FLAGS, "--config=race", "--output=starlark",
                                    "--starlark:expr=" + formatter, expr], 420)
    text = log.read_text(errors="replace")
    selected = [line for line in text.splitlines() if any(label + suffix + " " in line for label in RACE for suffix in ("_musl", "_impl"))]
    REPORT["selection"] = {"exit": code, "targets": selected, "diagnostics": relevant(text)}
    write()
    assert code == 0 and len(selected) == 4 and all("linux_x86_64_musl" in line for line in selected)
    test("race")
    for mode, row in REPORT["arms"].items():
        result, = row["results"]
        assert row["bazel_exit"] == 0 and result["status"] == "PASSED" and not result["cached"]
        assert result["runtime"]["mode"] == mode and result["runtime"]["guest"]["marker"]
        outputs = row["producers"]
        assert outputs["exit"] == 0 and len(outputs["binaries"]) == 9
        assert all(binary["matches_install"] for binary in outputs["binaries"].values())


if __name__ == "__main__":
    main()
