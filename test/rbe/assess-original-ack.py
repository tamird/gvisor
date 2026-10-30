"""Classify the two established ACK cases separately from setup failure."""
import json
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET

def main():
    path, source, raw_status = sys.argv[1:]
    path = Path(path)
    result = {"source":source, "reproduced":False}
    try:
        wire = json.loads((path/"wire.json").read_text())
        result["invocation"] = wire["invocation"]
        assert wire["exit"] == 3 and int(raw_status) != 0, wire
        target = "//test/packetimpact/tests:tcp_unacc_seq_ack_native_test"
        events = [json.loads(line) for line in (path/"wire/events.jsonl").read_text().splitlines()]
        actual = [e["testResult"] for e in events if e.get("id",{}).get("testResult",{}).get("label")==target and "testResult" in e]
        assert len(actual)==1 and actual[0]["status"]=="FAILED" and actual[0]["executionInfo"]["strategy"] in {"local","standalone"}, actual
        assert not actual[0].get("cachedLocally") and not actual[0]["executionInfo"].get("cachedRemotely"), actual
        files = list((path/"wire/reports").rglob("test.xml"))
        assert len(files)==1, files
        cases = {c.get("name"):c for c in ET.parse(files[0]).iter("testcase") if "/" in c.get("name")}
        assert len(cases)==24, cases.keys()
        failed = [key for key,c in cases.items() if c.find("failure") is not None or c.find("error") is not None]
        assert set(failed)=={f"TestEstablishedUnaccSeqAck/UnaccAck:offset={n}" for n in (1,2)}, failed
        assert all(c.find("skipped") is None for c in cases.values())
        log=files[0].with_suffix(".log").read_text()
        assert log.count("expected no ack but got one:")==2
        result.update(reproduced=True, observed=failed, passes=len(cases)-len(failed), kernel=(path/"native-kernel").read_text().strip())
    except Exception as err:
        result["error"]=repr(err)[:1800]
    (path/"control.json").write_text(json.dumps(result,indent=2)+"\n")
    print(result)
    return not result["reproduced"]

if __name__ == "__main__":
    sys.exit(main())
