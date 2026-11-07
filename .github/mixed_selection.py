"""Read the roots of Bazel's test command and their physical TestRunner actions."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess

from inventory import shard_path

SOURCE = "4b87bd4875ecde1ab6b994030d59871b59363ca1"
WORK = Path(os.environ["RUNNER_TEMP"]) / "mixed-selection"
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx8g"]
FLAGS = ["--jobs=4", "--keep_going", "--noshow_progress", "--color=no", "--curses=no",
         "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=local",
         "--strategy=TestRunner=local"]
CONTROL = "//test/syscalls:uname_test_native"
SUFFIX = "_presubmit_arm64"
PREVIOUS = set((WORK / "owners.txt").read_text().splitlines())
REPORT = {"source": SOURCE, "checks": {}}


def save():
    (WORK / "selection.json").write_text(json.dumps(REPORT, sort_keys=True) + "\n")


def run(name, kind, flags, arguments):
    bep = WORK / (name + ".bep")
    out = WORK / (name + ".out")
    log = WORK / (name + ".log")
    with out.open("w") as stdout, log.open("w") as stderr:
        code = subprocess.run(["timeout", "--kill-after=40s", "13m", *BAZEL, kind,
                               *flags, *FLAGS, "--build_event_json_file=" + str(bep),
                               *arguments], stdout=stdout, stderr=stderr).returncode
    diagnostics = []
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith(("ERROR:", "FATAL:", "Caused by:")):
            msg = re.sub(r"(https?://\S+|/(?:home|tmp|opt|usr)/\S+)", "<path>", line)[:420]
            if msg not in diagnostics and len(diagnostics) < 6:
                diagnostics.append(msg)
    return out, bep, {"exit": code, "diagnostics": diagnostics}


def selected(name, flags, arguments):
    _, bep, result = run(name, "test", flags, ["--nobuild", "--build_tests_only", *arguments])
    targets = {}
    suites = set()
    endings = Counter()
    work = Counter()
    if bep.exists():
        for line in bep.open():
            event = json.loads(line)
            ident = event["id"]
            if event.get("configured", {}).get("targetKind") == "test_suite rule":
                suites.add(ident["targetConfigured"]["label"])
            for kind in ("action", "testResult", "testSummary"):
                if kind in event:
                    work[kind] += 1
            target = ident.get("targetCompleted", {})
            if "aborted" in event:
                reason = event["aborted"].get("reason", "other")
                endings[reason] += 1
                if reason == "NO_BUILD" and target and not target.get("aspect"):
                    targets[target["label"]] = target.get("configuration", {}).get("id")
    for suite in suites:
        targets.pop(suite, None)
    result.update(selected=sorted(targets), suites=sorted(suites), terminated=dict(endings), execution_events=dict(work))
    REPORT["checks"][name] = result
    save()
    return targets, result


def graph(mode, flags, selected_targets):
    if not selected_targets:
        return {}
    query = WORK / (mode + ".query")
    query.write_text('mnemonic("^TestRunner$",set(' + " ".join(sorted(selected_targets)) + '))\n')
    out, _, result = run(mode + "-graph", "aquery", flags,
                         ["--output=jsonproto", "--include_artifacts", "--noinclude_commandline",
                          "--query_file=" + str(query)])
    REPORT["checks"][mode]["graph"] = result
    save()
    if not out.stat().st_size:
        return result
    subprocess.run([*BAZEL, "shutdown"], check=False, timeout=90)
    with out.open() as f:
        data = json.load(f)
    labels = {str(t["id"]): t["label"] for t in data.get("targets", [])}
    configs = {str(c["id"]): c.get("checksum") for c in data.get("configuration", [])}
    fragments = {str(p["id"]): p for p in data.get("pathFragments", [])}
    artifacts = {str(a["id"]): str(a["pathFragmentId"]) for a in data.get("artifacts", [])}
    rows = []
    for action in data.get("actions", []):
        shard = shard_path(action, artifacts, fragments)
        if not shard:
            continue
        label = labels[str(action["targetId"])]
        platform = action["executionPlatform"].removeprefix("@@")
        properties = {p["key"]: p.get("value", "") for p in action.get("executionInfo", [])}
        rows.append((label, *shard, platform, properties.get("workload-isolation-type", "oci"),
                     selected_targets.get(label) == configs.get(str(action.get("configurationId")))))
    result.update(rows=sorted(rows), workers=dict(Counter(r[4] for r in rows)),
                  isolation=dict(Counter(r[5] for r in rows)))
    save()
    return result


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    assert len(PREVIOUS) == 363
    query = 'kind("^go_test rule$",set(' + " ".join(sorted(PREVIOUS)) + '))'
    go_result = subprocess.run([*BAZEL, "query", "--output=label", query], capture_output=True, text=True)
    go_tests = set(go_result.stdout.splitlines()) if go_result.returncode == 0 else set()
    REPORT.update(original_owners=len(PREVIOUS), original_go_owners=len(go_tests), kind_query_exit=go_result.returncode)
    save()
    profiles = {"mixed": "source-presubmit-mixed", "amd64": "source-presubmit", "arm64": "source-presubmit-arm64"}
    for mode, profile in profiles.items():
        flags = ["--config=" + profile]
        targets, result = selected(mode, flags, [])
        expected = set(PREVIOUS)
        if mode == "mixed":
            expected.update(label + SUFFIX for label in (go_tests | {CONTROL}))
        result.update(missing=sorted(expected - targets.keys()), extra=sorted(targets.keys() - expected))
        save()
        g = graph(mode, flags, targets)
        counts = Counter()
        for label in expected:
            base = label.removesuffix(SUFFIX)
            n = {"//pkg/tcpip/stack:stack_x_test": 8, "//pkg/tcpip/transport/tcp/test/e2e:tcp_test": 4}.get(base, 1)
            cpu = "arm64" if mode == "arm64" or label.endswith(SUFFIX) else "amd64"
            for shard in range(1, n + 1):
                counts[label, 1, shard, n, "//test/rbe:ubuntu_" + cpu] += 1
        actual = Counter(tuple(r[:5]) for r in g.get("rows", []))
        result.update(expected_shards=counts.total(), actual_shards=actual.total(),
                      missing_shards=list((counts - actual).elements())[:20],
                      extra_shards=list((actual - counts).elements())[:20],
                      verified=(result["exit"] in (0, 1) and not result["execution_events"]
                                and not result["missing"] and not result["extra"] and g.get("exit") == 0
                                and actual == counts and all(r[6] for r in g.get("rows", []))))
        save()
        print(mode, len(targets), actual.total(), result["verified"], flush=True)
    pkg = "//pkg/atomicbitops"
    expected_wildcard = {pkg + ":atomicbitops_test", pkg + ":atomicbitops_benchmark_test"}
    deliberate = {"//pkg/atomicbitops:atomicbitops_test_arm64", "//test/util:posix_error_test_arm64",
                  "//sandboxexec/sandbox:sandbox_test_arm64"}
    for mode, arch in (("ordinary-amd64", "x86_64"), ("ordinary-arm64", "aarch64"), ("manual", "x86_64")):
        cpu = "arm64" if arch == "aarch64" else "amd64"
        platforms = "//test/rbe:ubuntu_" + cpu
        if mode == "manual":
            platforms += ",//test/rbe:ubuntu_arm64"
        flags = ["--config=" + arch, "--extra_execution_platforms=" + platforms]
        if cpu == "arm64":
            flags.append("--host_platform=//tools/bazeldefs:linux_arm64")
        expect = deliberate if mode == "manual" else expected_wildcard
        args = sorted(expect) if mode == "manual" else [pkg + "/..."]
        targets, result = selected(mode, flags, args)
        result.update(missing=sorted(expect - targets.keys()), extra=sorted(targets.keys() - expect))
        # The ordinary wildcard may also select a Nogo test created by the repository's Go macro.
        extras = result["extra"]
        expected_extras = all(l.endswith("_nogo") for l in extras) if mode != "manual" else not extras
        result["verified"] = (result["exit"] in (0, 1) and not result["execution_events"]
                              and not result["missing"] and expected_extras)
        save()
    REPORT["verified"] = (len(go_tests) == 340 and all(check["verified"] for check in REPORT["checks"].values()))
    save()
    assert REPORT["verified"]


if __name__ == "__main__":
    main()
