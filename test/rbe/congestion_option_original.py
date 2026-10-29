"""Read only the two completed invocations' original TCP_INFO tests."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import traceback
import xml.etree.ElementTree as ET

SUITE = "TestCongestionControlSelection"
MODES = {f"{cc}_{m}" for cc in ("cubic", "reno") for m in ("none", "same", "other")}
FIELDS = "mss initial cwnd_before threshold_before cwnd_after threshold_after state_before state_after rto_before rto_after rtt_before rtt_after".split()
ROW = re.compile(rb"CCA_SELECT from=(cubic|reno) mode=(none|same|other) to=(cubic|reno) mss=(\d+) initial=(\d+) before=(\d+)/(\d+) after=(\d+)/(\d+) state=(\d+)/(\d+) rto=(\d+)/(\d+) rtt=(\d+)/(\d+)")
TARGET = "//test/packetimpact/tests:tcp_rack_"
ARMS = {"stock": ("e2fa6315-f2e6-449e-9935-57557f0bb313", ["native", "netstack"], "d99e62da4621c0d79a798e7e886a78e7d628ee938fbb5ec8eada110e66a15bf5"),
        "fixed": ("a89e4d82-a51f-49d7-b6e7-e56887288bab", ["netstack"], "80d82af7f180c10e2935020c58d2b3d08b05df5db3b635fcb86b6f68e26d04a7")}
NAMES = ("test.log", "test.xml")
ROOT = Path(os.environ["RUNNER_TEMP"])
HASH = hashlib.sha256

def observations(log: bytes, expected=frozenset(MODES)) -> dict:
    rows = {}
    for m in ROW.finditer(log):
        cc, mode, selected = (s.decode() for s in m.groups()[:3])
        k = f"{cc}_{mode}"
        row = dict(zip(FIELDS, map(int, m.groups()[3:]), strict=True), selected=selected)
        assert k not in rows or rows[k] == row, "conflicting socket rows"
        rows[k] = row
    assert rows.keys() == expected, "missing socket row"
    return rows

def cases(xml: bytes) -> dict[str, str]:
    nodes = ET.fromstring(xml).findall(".//testcase")
    result = {n.attrib["name"]: ("failed" if n.find("failure") is not None or n.find("error") is not None
             else "skipped" if n.find("skipped") is not None else "passed") for n in nodes}
    assert result.keys() == {SUITE} | {SUITE + "/" + m for m in MODES} and len(result) == len(nodes), "missing XML case"
    return result

def unchanged(row: dict) -> bool:
    return (row["cwnd_before"], row["threshold_before"], row["state_before"]) == (row["cwnd_after"], row["threshold_after"], row["state_after"])

def retain_error(rec: dict, stage: str, exc: Exception) -> None:
    rec.setdefault("errors", []).append({"stage":stage, "type":type(exc).__name__, "line":traceback.extract_tb(exc.__traceback__)[-1].lineno})

def digest(v: dict) -> dict:
    h, n = v["hash"], int(v["sizeBytes"])
    assert re.fullmatch("[0-9a-f]{64}", h) and 0 < n < 256 * 1024
    return {"sha256":h, "bytes":n}

def fetch(secret: str, child: str, target: str, prior: dict, rec: dict) -> None:
    from original_reader import request
    assert any(child == i and target in [TARGET + t + "_test" for t in ts] for i, ts, _ in ARMS.values())
    refs = dict(prior.get("reports", {}))
    try:
        actions = request(secret, "/api/v1/GetAction", {"selector":{"invocationId":child, "targetLabel":target}}, 2 << 20, None)
        assert not actions.get("nextPageToken")
        attempts = []
        for action in actions.get("action", []):
            assert action["id"]["invocationId"] == child
            if action.get("targetLabel") != target:
                continue
            files = {}
            for f in action.get("file", []):
                name = f["name"].split("/")[-1]
                if name not in NAMES:
                    continue
                match = re.fullmatch(r"bytestream://remote\.buildbuddy\.io/(?:[^?#]+/)?blobs/([a-f0-9]{64})/(\d+)", f["uri"])
                assert match and name not in files
                files[name] = {"sha256":match[1], "bytes":int(match[2])}
                assert not f.get("hash") or f["hash"] == match[1]
            if files:
                attempts.append(files)
        [bep] = attempts
        assert set(bep) == set(NAMES) and (not refs or refs == bep)
        refs.update(bep)
        rec["bep_action"] = True
    except Exception as exc:
        retain_error(rec, "GetAction", exc)
    try:
        result = request(secret, "/rpc/BuildBuddyService/GetExecution", {"executionLookup":{"invocationId":child,"targetLabel":target},
            "inlineExecuteResponse":True}, 24 << 20, None)
        rows = []
        for e in result.get("execution", []):
            if e.get("actionMnemonic") != "TestRunner" or e.get("targetLabel") != target:
                continue
            if "test-setup.sh" not in e.get("commandSnippet", ""):
                continue
            resp = e["executeResponse"]
            native = resp["result"]
            ex = int(native.get("exitCode", 0))
            out = [o for o in native.get("outputFiles", []) if o["path"].endswith("/test.xml")]
            stdout = native.get("stdoutDigest")
            assert e.get("worker") and not resp.get("cachedResult") and not resp.get("status", {}).get("code")
            rows.append((ex, e["actionDigest"]["hash"], out, stdout))
        [(ex, action_hash, out, stdout)] = rows
        rec.update(native_exit=ex, action_sha256=action_hash, remote_uncached=True)
        [xml] = out
        physical = {"test.xml":digest(xml["digest"]), "test.log":digest(stdout)}
        assert not refs or refs == physical, "physical/report digest mismatch"
        refs.update(physical)
        rec["physical_report_match"] = True
    except Exception as exc:
        retain_error(rec, "GetExecution", exc)
    rec["reports"] = refs
    for name in NAMES:
        try:
            f = refs[name]
            h, n = f["sha256"], f["bytes"]
            assert 0 < n < 256 * 1024 and re.fullmatch("[0-9a-f]{64}", h)
            buf = bytearray()
            uri = f"bytestream://remote.buildbuddy.io/blobs/{h}/{n}"
            request(secret, "/api/v1/GetFile", {"uri":uri}, n, buf.extend)
            assert len(buf) == n and HASH(buf).hexdigest() == h
            if name == "test.log":
                rec["observations"] = observations(bytes(buf))
                rec["window_failure_markers"] = buf.count(b"TCP_CONGESTION changed the post-loss sender window")
                (ROOT / ("log-" + child + "-" + target.split(":")[1])).write_bytes(buf)
            else:
                rec["cases"] = cases(bytes(buf))
        except Exception as exc:
            retain_error(rec, name, exc)

def main() -> None:
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    result = {"status":"INCOMPLETE", "owners":{}}
    path = ROOT / "congestion-selection.json"
    for arm, (child, targets, checksum) in ARMS.items():
        raw = (ROOT / arm / "result.json").read_bytes()
        assert HASH(raw).hexdigest() == checksum
        src = json.loads(raw)
        assert src["child"] == child and src["status"] == "INCOMPLETE"
        for name in targets:
            target = TARGET + name + "_test"
            rec = result["owners"][arm + "/" + name] = {"child":child,"label":target,"source":src["source"],"carrier":src["carrier"],
                "native_bazel_exit":src["code"],"original_summary_sha256":checksum}
            prior = src.get("owners", {}).get(target, {})
            rec["original_bep_status"] = prior.get("status")
            try:
                fetch(key, child, target, prior, rec)
            except Exception as exc:
                retain_error(rec, "transfer", exc)
            path.write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
    try:
        log = (ROOT / ("log-" + ARMS["stock"][0] + "-tcp_rack_netstack_test")).read_bytes()
        controls = {}
        for mode, stable in (("cubic_none", True), ("cubic_same", False)):
            [m] = [m for m in ROW.finditer(log) if "_".join(i.decode() for i in m.groups()[:2]) == mode]
            [row] = observations(m.group(), {mode}).values()
            assert unchanged(row) == stable, "incorrect retained control"
            controls[mode] = "stable" if stable else "reset"
        try:
            observations(ROW.sub(lambda m: b"" if m.group(1, 2) == (b"cubic", b"same") else m.group(), log))
        except AssertionError as exc:
            assert str(exc) == "missing socket row"
            controls["removed_row"] = "rejected"
        else:
            raise ValueError("incomplete log accepted")
        result["retained_controls"] = controls
        for arm, rec in result["owners"].items():
            assert not rec.get("errors"), "report unresolved"
            ok = arm != "stock/netstack"
            assert (rec["native_exit"] == 0) == ok
            assert (rec["cases"][SUITE] == "passed") == ok
            for mode, row in rec["observations"].items():
                cc, method = mode.split("_")
                stable = ok or method == "none"
                assert row["selected"] == (cc if method != "other" else ("cubic" if cc == "reno" else "reno"))
                assert 0 < row["threshold_before"] <= row["cwnd_before"] < row["initial"] and row["state_before"] == row["state_after"] == 0
                assert unchanged(row) == stable and rec["cases"][SUITE + "/" + mode] == ("passed" if stable else "failed")
                if not stable:
                    assert (row["cwnd_after"], row["threshold_after"]) == (10, 4294967295)
            assert rec["window_failure_markers"] == 0 if ok else rec["window_failure_markers"] >= 4
        result["status"] = "COMPLETE"
    except Exception as exc:
        retain_error(result, "verification", exc)
    path.write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
    sys.exit(result["status"] != "COMPLETE")

if __name__ == "__main__":
    main()
