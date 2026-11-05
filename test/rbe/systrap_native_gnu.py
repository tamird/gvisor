"""Verify the approved native stub with the unchanged GNU offset consumers."""
import json
import platform
import re
import struct
from pathlib import Path
import sys

import native_cross_inputs as q

PRODUCT = "005743727b3d80d373085133b5da9cd906cc9d7c"
BASE = "da95e9bce7391557f4cd4102a96ed7f97156f214"
q.ROOTS = [q.SYSMSG + "sighandler_binary_arch", q.SYSMSG + "sighandler_go_arch"]
FLAGS = ["--config=x86_64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
QUERY = 'mnemonic("CppCompile|CppLink|Genrule", deps(set(' + ' '.join(q.ROOTS) + ')))'


def phase(result):
    q.source(PRODUCT)
    assert q.command(["git", "rev-parse", "HEAD^"])[1].decode().strip() == BASE
    assert platform.machine() == "x86_64"
    assert q.command([q.BAZEL, "--version"])[1].decode().strip() == "bazel 9.3.1"
    assert Path("pkg/sentry/platform/systrap/sysmsg/gen_offsets_go.sh").read_bytes() == q.command([
        "git", "show", BASE + ":pkg/sentry/platform/systrap/sysmsg/gen_offsets_go.sh"])[1]
    bep = q.TASK / "gnu.bep"
    command = [q.BAZEL, "build", *FLAGS, "--keep_going", "--verbose_failures",
               "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), *q.ROOTS]
    rc, _, err = q.command(command, 900)
    result.update(build_exit=rc, build_diagnostics=q.diagnostics(err), events=q.events(bep))
    q.write(result)
    configurations = {}
    if bep.exists():
        for line in bep.read_text().splitlines():
            e = json.loads(line)
            if "configuration" in e.get("id", {}):
                configurations[e["id"]["configuration"]["id"]] = e.get("configuration", {}).get("cpu")
    result["configuration_cpus"] = configurations
    ar, raw, err = q.command([q.BAZEL, "aquery", *FLAGS, "--output=jsonproto", "--include_param_files", QUERY], 240)
    result.update(aquery_exit=ar, aquery_diagnostics=q.diagnostics(err))
    rows = result["actions"] = q.graph(raw) if ar == 0 else []
    for row in rows:
        if row["mnemonic"] not in ("CppCompile", "CppLink"):
            continue
        driver = re.fullmatch(r"/usr/bin/(x86_64|aarch64)-linux-gnu-(?:gcc|g\+\+)", row["argv"][0])
        row["target_arch"] = driver.group(1) if driver else None
        row["bep_cpu"] = configurations.get(row["configuration"])
        row["fresh_gnu_action"] = bool(driver) and row["bep_cpu"] == {"x86_64": "k8", "aarch64": "aarch64"}[driver.group(1)] and any(
            e["success"] and e["exit"] == 0 and e["owner"] == row["owner"] and e["type"] == row["mnemonic"]
            and e["configuration"] == row["configuration"] and e["output"] in row["outputs"]
            for e in result["events"]["actions"])
    cr, out, err = q.command([q.BAZEL, "cquery", *FLAGS, "--output=files", "set(" + " ".join(q.ROOTS) + ")"], 180)
    result.update(output_query_exit=cr, output_query_diagnostics=q.diagnostics(err))
    ir, root, err = q.command([q.BAZEL, "info", "execution_root"])
    result["execution_root_exit"] = ir
    if cr == 0 and ir == 0:
        result["outputs"] = q.output_proof(Path(root.decode().strip()), out.decode().splitlines(), rows, result["events"])


def main():
    result = {"source": PRODUCT, "base": BASE, "worker": platform.machine(), "bazel": "9.3.1",
              "roots": q.ROOTS, "flags": FLAGS, "scope": "GNU build and old offset/blob consumers; zero tests or guest executions"}
    try:
        phase(result)
    except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
        result["reader_error"] = type(ex).__name__ + ": " + str(ex)[:300]
    rows = result.get("actions", [])
    compiles = [a for a in rows if a["mnemonic"] == "CppCompile"]
    links = [a for a in rows if a["mnemonic"] == "CppLink"]
    linked = result.get("outputs", {}).get("linked", [])
    staged = result.get("outputs", {}).get("staged", [])
    events = result.get("events", {})
    result["verified"] = (
        result.get("build_exit") == 0 and events.get("exit", {}).get("name") == "SUCCESS"
        and result.get("aquery_exit") == result.get("output_query_exit") == 0
        and not result.get("reader_error") and not events.get("aborted")
        and len(compiles) == 8 and len(links) == 2
        and all(a.get("fresh_gnu_action") for a in compiles + links)
        and {a["target_arch"] for a in links} == {"x86_64", "aarch64"}
        and len(linked) == 2 and all(a.get("fresh_action") and not a.get("read_error") for a in linked)
        and {a.get("machine") for a in linked} == {62, 183}
        and len(staged) == 4 and all(not a.get("read_error") and (a.get("linked_exports_match") or a.get("linked_blob_match")) for a in staged)
        and len(events.get("targets", [])) == 2 and all(a["success"] for a in events["targets"])
        and events.get("test_events") == 0)
    q.write(result)
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
