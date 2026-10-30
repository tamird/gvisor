from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
rows = {}
for arm, sha in (("current",'422d2cb0c0846d7fc1eecef51bfdc26a0070f9f1'),("stock",'657a2bd3464951c71050aa31e15d8e42a73f34e7')):
    directory = root / arm
    def read(n):
        f = directory/n
        return f.read_text() if f.is_file() else ""
    text = read("test.log")
    records = re.findall(r"^\s*--- (PASS|SKIP|FAIL): ([^\s]+) \(",text,re.M)
    found = [s for s,n in records if n=="TestRejectedTCPSACKWrite"]
    status = read("exit").strip()
    current = arm=="current"
    ok = (read("source").strip()==sha and read("target").strip()=="//pkg/sentry/fsimpl/proc:proc_test" and
          status==("0" if current else "3") and found==(["PASS"] if current else ["FAIL"]))
    if current:
        ok &= len(records)>1 and all(s!="FAIL" for s,n in records)
    else:
        ok &= len(records)==1 and 'Generate() = "1\\n", want "0\\n"' in text
    rows[arm] = {"source":sha,"target":read("target").strip(),"exit":status,"verified":ok,
                 "expected":"full package" if current else ["TestRejectedTCPSACKWrite"],
                 "start":read("start").strip(),"end":read("end").strip(),"fixture":read("fixture-sha256").strip(),"records":records}
    print(arm,status,len(records),found,ok)
valid = all(r["verified"] for r in rows.values()) and len({r["fixture"] for r in rows.values()})==1
(root/"verdict.json").write_text(json.dumps({"verified":valid,"arms":rows,"verified_at":datetime.now(timezone.utc).isoformat()},indent=2))
sys.exit(0 if valid else 1)
