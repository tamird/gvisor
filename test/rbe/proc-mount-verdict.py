"""Read actual named GTest assertions and preserve independent lane outcomes."""
from collections import Counter
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as XML

root = Path(sys.argv[1])
result = {}
case_names = ['ProcSysNetIpv4Sack.SharedAcrossProcMounts', 'ProcSysNetPortRange.SharedAcrossProcMounts']
for name in ("current-native", "current-guest", "stock-guest"):
    directory = root / name
    status = int((directory / "exit").read_text()) if (directory / "exit").is_file() else None
    assertions = {}
    if (directory/"test.xml").is_file():
        for case in XML.parse(directory/"test.xml").iter("testcase"):
            test = case.get("classname","")+"."+case.get("name","")
            failures = case.findall("failure")+case.findall("error")
            assertions[test] = {
                "status": "SKIP" if case.find("skipped") is not None or case.get("status")=="notrun" or case.get("result") in ("skipped","suppressed")
                          else "FAIL" if failures else "OK",
                "details": [node.text or node.get("message","") for node in failures],
            }
    exact = set(assertions) == set(case_names)
    if name == "stock-guest":
        verified = exact and status == 3 and all(
            item["status"] == "FAIL" and len(item["details"]) == 1 and
            "GetContents(original_path)" in item["details"][0] and
            "updated" in item["details"][0] for item in assertions.values())
    else:
        verified = exact and status == 0 and all(
            item["status"] == "OK" for item in assertions.values())
    result[name] = {"exit":status,"verified":verified,"assertions":assertions,
                    "counts":dict(Counter(item["status"] for item in assertions.values()))}
    print(name,"exit",status,"verified",verified,result[name]["counts"])
for name,row in result.items():
    d=root/name
    for field in ("source", "target", "start", "end", "fixture-sha256"):
        row[field] = (d/field).read_text().strip() if (d/field).is_file() else None
    row["expected"]= case_names
if len({row["fixture-sha256"] for row in result.values()}) != 1:
    raise RuntimeError("fixture differs across arms")
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
if not all(record["verified"] for record in result.values()):
    raise SystemExit(1)
