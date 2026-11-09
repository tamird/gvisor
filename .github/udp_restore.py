"""Retain the one public AMD64 UDP leaf and its actual restore transactions."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

OWNER = "//test/syscalls:socket_ipv6_udp_unbound_external_networking_test_runsc_systrap_directfs_save"
CASE = "IPv6UDPUnboundSockets/IPv6UDPUnboundExternalNetworkingSocketTest.AddV4MembershipToV6Socket/0"
TOPOLOGY = {"lo": "::1", "eth0": "2001:db8::1"}
SECRET = re.compile(rb"(?i)(?:authorization|x-buildbuddy-api-key)\s*[:=]\s*\S+|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN .*PRIVATE KEY-----")


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def check_log(raw: bytes) -> dict:
    """A real restore consumes the previous command's nonempty state file."""
    text = raw.decode(errors="replace")
    starts = re.findall(r"^\[ RUN +\] (\S+)", text, re.M)
    passes = re.findall(r"^\[ +OK +\] (\S+)", text, re.M)
    require(starts == passes == [CASE], "expected exactly one named RUN and OK")
    first = text.index("[ RUN")
    last = text.index("[       OK ]", first)
    body = text[first:last]
    for name, address in TOPOLOGY.items():
        require(re.search(rf" name={name} addr={re.escape(address)} index=[1-9][0-9]* ", body), "missing guest address: " + name)
    bundle = sandbox = current = executable = ""
    pending_delete = False
    restores = inside = 0
    for m in re.finditer(r"Executing: \[([^\r\n]+)\]$", text, re.M):
        args = m[1].split()
        if "run" in args and "--bundle" in args:
            require(not sandbox, "second initial run")
            executable, sandbox = args[0], args[-1]
            bundle = args[args.index("--bundle") + 1]
            current = args[args.index("-TESTONLY-autosave-image-path") + 1]
            require(Path(current).name.startswith("state.0."), "wrong initial autosave path")
        elif "delete" in args and "-force=true" in args and args[0] == executable:
            require(args[-1] == sandbox and not pending_delete, "unpaired sandbox deletion")
            pending_delete = True
        elif "restore" in args and "--image-path" in args:
            require(pending_delete and args[0] == executable and args[-1] == sandbox, "restore without preceding delete of this sandbox")
            require(args[args.index("--bundle") + 1] == bundle, "restore changed bundle")
            require(args[args.index("--image-path") + 1] == current, "restore did not consume preceding image")
            restores += 1
            following = args[args.index("-TESTONLY-autosave-image-path") + 1]
            require(Path(following).parent == Path(current).parent and Path(following).name.startswith(f"state.{restores}."), "wrong next checkpoint path")
            current, pending_delete = following, False
            inside += int(first < m.start() < last)
    require(inside > 0 and not pending_delete, "no complete restore within the selected case")
    return {"case": CASE, "interface_addresses": TOPOLOGY, "ordered_restores": restores, "during_case": inside}


def check_xml(raw: bytes) -> str:
    cases = list(ET.fromstring(raw).iter("testcase"))
    require(len(cases) == 1, "expected one XML leaf")
    case = cases[0]
    require(case.get("classname", "") + "." + case.get("name", "") == CASE, "unexpected XML identity")
    require(case.get("status") == "run" and case.get("result") == "completed" and len(case.findall("failure") + case.findall("error") + case.findall("skipped")) == 0, "expected completed passing XML")
    return CASE


def main(work: Path, source: str, exit_code: int) -> int:
    out = work / "results"
    report: dict = {"source": source, "owner": OWNER, "requested_leaf": CASE, "bazel_exit": exit_code, "originals": {}, "errors": []}
    errors: list[str] = report["errors"]
    root = Path("bazel-testlogs/test/syscalls") / OWNER.split(":")[1]
    originals = {}
    for name, limit, check in (("test.log", 12 << 20, check_log), ("test.xml", 1 << 20, check_xml)):
        try:
            path = root / name
            size = path.stat().st_size
            require(0 < size <= limit, name + ": absent/empty/oversize")
            raw = path.read_bytes()
            require(len(raw) == size and not SECRET.search(raw), name + ": cannot retain original")
            (out / name).write_bytes(raw)
            originals[name] = (path.resolve(), size, hashlib.sha256(raw).hexdigest())
            report["originals"][name] = {"bytes": size, "sha256": originals[name][2]}
            report[name] = check(raw)
        except (OSError, ValueError, IndexError, ET.ParseError) as error:
            errors.append(f"{name}: {error}"[:260])
    try:
        results, summaries, finishes = [], [], []
        count = 0
        with (work / "events.jsonl").open() as events:
            for line in events:
                event = json.loads(line)
                ident = event["id"]
                if "testResult" in ident:
                    count += 1
                    if ident["testResult"]["label"] == OWNER:
                        results.append((ident["testResult"], event["testResult"]))
                if ident.get("testSummary", {}).get("label") == OWNER:
                    summaries.append((ident["testSummary"], event["testSummary"]))
                if "buildFinished" in ident:
                    finishes.append(event["finished"]["exitCode"].get("name"))
        require(len(results) == len(summaries) == count == 1, "expected exactly one TestResult and owner summary")
        ident, result = results[0]
        sid, summary = summaries[0]
        config = ident["configuration"]["id"]
        strategy = result.get("executionInfo", {}).get("strategy")
        report["test_result"] = {"status": result["status"], "configuration": config, "strategy": strategy, "summary": summary.get("overallStatus"), "finish": finishes}
        require(bool(config) and config == sid["configuration"]["id"] and [ident.get(k) for k in ("run", "shard", "attempt")] == [1, 1, 1], "unmatched result or attempt")
        require(strategy == "local" and not result.get("cachedLocally") and not result.get("executionInfo", {}).get("cachedRemotely"), "not a fresh local TestRunner")
        for name in ("test.log", "test.xml"):
            entries = [item for item in result.get("testActionOutput", []) if item.get("name") == name]
            require(len(entries) == 1 and name in originals, "missing original " + name)
            item = entries[0]
            uri = urlparse(item["uri"])
            path, size, digest = originals[name]
            require(uri.scheme == "file" and uri.netloc in ("", "localhost") and Path(unquote(uri.path)).resolve() == path, "wrong " + name + " URI")
            require(("length" not in item or int(item["length"]) == size) and ("digest" not in item or item["digest"] == digest), name + ": digest/length mismatch")
        require(result["status"] == summary.get("overallStatus") == "PASSED" and finishes == ["SUCCESS"] and exit_code == 0, "original native test or Bazel failed")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        errors.append(f"events: {error}"[:260])
    if exit_code and (work / "bazel.log").exists():
        # Keep a diagnostic when the build stops before Bazel creates test outputs.
        diagnostics = []
        with (work / "bazel.log").open(errors="replace") as log:
            for line in log:
                if len(diagnostics) >= 8:
                    break
                if len(line) < 1500 and re.search(r"(^ERROR:|(?:fatal )?error:|^FAILED:)", line) and not SECRET.search(line.encode()):
                    diagnostics.append(re.sub(r"(?:/[^\s':]+/)+", "", line.strip())[:360])
        report["diagnostics"] = diagnostics
    require(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source, "source changed")
    report["verified"] = not errors
    (out / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"verified": not errors, "bazel_exit": exit_code, "arch": "amd64", "errors": errors}))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])))
