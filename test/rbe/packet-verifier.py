"""Verify named, executed GTest results against the expected three-source matrix."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as XML


def actual_cases(xml):
    if not xml.exists():
        return {}
    result = {}
    for case in XML.parse(xml).iter("testcase"):
        name = f"{case.get('classname')}.{case.get('name')}"
        if name in result:
            raise ValueError(f"duplicate case {name}")
        errors = [*case.findall("failure"), *case.findall("error")]
        skipped = (case.find("skipped") is not None or
                   case.get("status") == "notrun" or
                   case.get("result") in ("skipped", "suppressed"))
        status = "SKIP" if skipped else "FAIL" if errors else "PASS"
        result[name] = [status, [n.text or n.get("message", "") for n in errors]]
    return result


def main(directory):
    root = Path(directory)
    matrix = json.loads((root / "matrix.json").read_text())
    result = {}
    for arm, expected in matrix.items():
        lane = root / arm
        fields = {f: (lane/f).read_text().strip() if (lane/f).is_file() else None
                  for f in ("exit", "start", "end", "source", "target", "fixture-sha256")}
        got = actual_cases(lane / "test.xml")
        passed = (set(got) == set(expected["cases"]) and
                  fields["exit"] == str(expected["exit"]) and
                  fields["source"] == expected["source"] and
                  fields["target"] == expected["target"] and
                  fields["fixture-sha256"] == expected["fixture-sha256"])
        for name, (wanted, needle) in expected["cases"].items():
            status, failures = got.get(name, ["MISSING", []])
            passed &= status == wanted and (not needle or
                                             any(needle in x for x in failures))
        result[arm] = {**fields, "expected": expected["cases"],
                       "actual": got, "verified": passed}
        print(arm, passed, [(n, a[0]) for n, a in got.items()])
    verified = all(row["verified"] for row in result.values())
    verdict = {"verified": verified, "arms": result,
               "verified_at": datetime.now(timezone.utc).isoformat()}
    (root/"verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
