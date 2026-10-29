"""Bind the direct-field graph comparisons to their real native reports."""
from pathlib import Path
import json
import sys
import xml.etree.ElementTree as ET

from verify import verified_file

temp=Path(sys.argv[1])
out=temp/"phase-results"
events=[json.loads(line) for line in (temp/"pointers.bep").open()]
[test]=[e["testResult"] for e in events if e.get("id",{}).get("testResult",{}).get("label")=="//pkg/state/tests:tests_test"]
[child]=[e["started"]["uuid"] for e in events if "started" in e]
assert int(sys.argv[2])==0 and test["status"]=="PASSED" and not test.get("cachedLocally")
assert not test.get("executionInfo",{}).get("cachedRemotely")
files={a["name"]:verified_file(a,out/("pointer-"+a["name"])) for a in test["testActionOutput"] if a["name"] in ("test.log","test.xml")}
assert set(files)=={"test.log","test.xml"}
xml=ET.parse(out/"pointer-test.xml")
assert not any(xml.findall(".//"+t) for t in ("failure","error","skipped"))
names=[n.attrib["name"] for n in xml.findall(".//testcase")]
assert len(names)==len(set(names))
assert {n.split("/")[0] for n in names}=={"TestEmptyStruct","TestEmbeddedPointers","TestInteriorPointersIntoSlices"}, names
assert {"TestInteriorPointersIntoSlices/ptr_first=false","TestInteriorPointersIntoSlices/ptr_first=true",
        "TestEmptyStruct/empty-struct-pointers0","TestEmptyStruct/empty-struct-pointers1","TestEmptyStruct/empty-struct-pointers2",
        "TestEmbeddedPointers/embedded-pointers0","TestEmbeddedPointers/embedded-pointers21"}<=set(names), names
(out/"pointer-summary.json").write_text(json.dumps({"child":child,"source":"29ad769425c5934352a279cc47d7c40b315ddb28","status":test["status"],"files":files,"cases":names})+"\n")
