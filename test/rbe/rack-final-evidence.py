"""Qualify one final source and its packet-identical control in one native coordinator."""
from pathlib import Path
import os


directory = Path(os.environ["RUNNER_TEMP"])
code = (directory / "rack-flight-evidence.py").read_text()
code = code[:code.index('\ngit("checkout","--quiet","--detach",product)')]
exec(compile(code, "rack-flight-evidence.py", "exec"))

getter = (temp / "rack-receipts.py").read_text()
getter = getter[getter.index("from collections import Counter"):getter.index("\nfor arm,inv")]
getter = getter.replace('records = {}', '')
exec(compile(getter,"rack-receipts.py","exec"))


def verify(arm, required_cases=(), min_cases=None, late=(), nogo=False):
    row = records[arm]
    inv, = row["invocations"]
    for target in list(items("GetTarget","target",{"invocation_id":inv})):
        label = target.get("label","")
        if label not in row["targets"]:
            continue
        detail = row.setdefault("originals", {}).setdefault(label, {})
        for action in items("GetAction","action",{"invocation_id":inv,"target_label":label}):
            for file in action.get("file",[]):
                path = file.get("name","").split("/")[-1]
                if path not in ("test.xml","test.log") or path in detail:
                    continue
                b=original(file["uri"])
                detail[path]={"sha256":sha256(b).hexdigest(),"bytes":len(b)}
                if path=="test.xml":
                    cases = list(ElementTree.fromstring(b).iter("testcase"))
                    detail["xml"]=len(cases)
                    detail["failure"]=[x.get("name") for x in cases if x.find("failure") is not None or x.find("error") is not None]
                    detail["skip"]=[x.get("name") for x in cases if x.find("skipped") is not None]
                else:
                    text=b.decode("utf-8","replace")
                    cases=re.findall(r"^\s*--- (PASS|FAIL|SKIP): (\S+) \(",text,re.M)
                    detail["go"]=dict(Counter(a for a,n in cases))
                    detail["cases"]=[(a,n) for a,n in cases if n in required_cases or a!="PASS"]
                    if nogo:
                        detail["arch"]=[arch for arch in ("amd64","arm64") if arch+"..." in text]
                        detail["findings"]=[line[:250] for line in text.splitlines() if ".go:" in line][:4]
    views = list(row.get("originals",{}).values())
    counts = {k:sum(x.get("go",{}).get(k,0) for x in views) for k in ("PASS","FAIL","SKIP")}
    hits = set(n for view in views for state,n in view.get("cases",[]) if state=="PASS")
    misses = set(n for view in views for n in view.get("failure",[]))
    row["goOriginals"]=counts
    row["complete"]=row["code"]==row["expected"] and not row["missing"] and set(row["originals"])==set(row["targets"]) and all("test.xml" in v and "test.log" in v for v in views)
    if nogo:
        row["complete"] &= all(set(v["arch"])=={"amd64","arm64"} and not v["failure"] for v in views)
    else:
        row["complete"] &= set(required_cases)<=hits and counts["SKIP"]==0
        if not late:
            row["complete"] &= not misses and not counts["FAIL"]
        else:
            row["complete"] &= misses==set(late) and counts["FAIL"]==len(late)
        if min_cases:
            row["complete"] &= counts["PASS"]+counts["FAIL"]>=min_cases
    store()


git("checkout","--quiet","--detach",os.environ["CONTROL"])
baz("control",[rack],"^(TestRACKRepairAboveHighestSACK|TestRACKWithWindowFull)$",expected=3)
verify("control",["TestRACKWithWindowFull"],min_cases=2,late=[name])
git("checkout","--quiet","--detach",product)
baz("product",[unit,gsos,rack,sack])
verify("product",[name,"TestRACKWithWindowFull","TestPartialGSOProgress/ack","TestPartialGSOProgress/timeout","TestPartialGSOProgress/restore","TestACKForRefusedRetransmission","TestRefusedWriteBehindFlight"],min_cases=535)
baz("race",[unit,gsos,rack],"^(TestRACK.*|TestPartialGSOProgress|TestACKForRefusedRetransmission|TestRefusedWriteBehindFlight)$",race=True)
verify("race",[name,"TestRACKWithWindowFull","TestPartialGSOProgress/restore"],min_cases=42)
owners=[tcp+":tcp_nogo",e2e+"tcp_rack_test_nogo",e2e+"tcp_test_nogo"]
baz("nogo",owners)
verify("nogo",nogo=True)
sys.exit(not all(row["complete"] for row in records.values()))
