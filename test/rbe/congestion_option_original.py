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

def main() -> None:
    from original_reader import request
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    path = ROOT / "congestion-selection.json"
    raw = (ROOT / "held" / "congestion-selection.json").read_bytes()
    assert HASH(raw).hexdigest() == "278dc921faf11dbcd91d8ec30185441176e0a343f08e351aa08d31c10df5b6a4"
    result = json.loads(raw)
    assert result["retained_controls"] == {"cubic_none":"stable", "cubic_same":"reset", "removed_row":"rejected"}
    result["original_projection_sha256"] = HASH(raw).hexdigest()
    resolutions = []
    for arm, rec in result["owners"].items():
        try:
            assert rec["errors"] == [{"line":94,"stage":"GetExecution","type":"AssertionError"}]
            child = rec["child"]; target = rec["label"]
            sourcearm, release = arm.split("/")
            assert child == ARMS[sourcearm][0] and release in ARMS[sourcearm][1] and target == TARGET + release + "_test"
            data = request(key, "/rpc/BuildBuddyService/GetExecution", {"executionLookup":{"invocationId":child,"targetLabel":target},"inlineExecuteResponse":True}, 24 << 20, None)
            [e] = [e for e in data.get("execution",[]) if e.get("actionMnemonic") == "TestRunner" and e.get("targetLabel") == target and "test-setup.sh" in e.get("commandSnippet", "")]
            resp = e["executeResponse"]; res = resp["result"]
            rec["native_exit"] = int(res.get("exitCode", 0))
            assert rec["native_exit"] == int(e.get("exitCode", 0))
            rec["action_sha256"] = e["actionDigest"]["hash"]
            assert e.get("stage") in (4,"COMPLETED") and not resp.get("status",{}).get("code") and not resp.get("cachedResult")
            rec["physical_worker"] = bool(res.get("executionMetadata",{}).get("worker") or e.get("executedActionMetadata",{}).get("worker") or e.get("executorHostname"))
            assert rec["physical_worker"]
            [xml] = [o for o in res.get("outputFiles",[]) if o["path"].endswith("/test.xml")]
            assert digest(xml["digest"]) == rec["reports"]["test.xml"]
            rec["physical_xml_match"] = True
            if std := res.get("stdoutDigest"):
                rec["physical_stdout_digest"] = digest(std)
                rec["physical_stdout_matches_test_log"] = digest(std) == rec["reports"]["test.log"]
            ok = arm != "stock/netstack"
            assert (rec["native_exit"] == 0) == ok and rec["cases"][SUITE] == ("passed" if ok else "failed")
            assert rec["bep_action"] and set(rec["observations"]) == MODES
            for mode,row in rec["observations"].items():
                cc,method = mode.split("_")
                assert row["selected"] == (("reno" if cc == "cubic" else "cubic") if method == "other" else cc)
                stable = ok or method == "none"
                assert unchanged(row) == stable and rec["cases"][SUITE+"/"+mode] == ("passed" if stable else "failed")
                assert 0 < row["threshold_before"] <= row["cwnd_before"] < row["initial"] and row["state_before"] == row["state_after"] == 0
                if not stable: assert (row["cwnd_after"],row["threshold_after"]) == (10,4294967295)
            assert rec["window_failure_markers"] == (0 if ok else 4)
            resolutions.append(arm)
        except Exception as exc:
            retain_error(rec,"original_execution",exc)
    result["resolved"] = sorted(resolutions)
    result["status"] = "COMPLETE" if len(resolutions) == 3 else "INCOMPLETE"
    path.write_text(json.dumps(result, sort_keys=True,indent=2)+"\n")
    sys.exit(result["status"] != "COMPLETE")

if __name__ == "__main__":
    main()
