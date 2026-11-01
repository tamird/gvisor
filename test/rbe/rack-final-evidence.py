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


def diagnostics(text):
    result = []
    for line in text.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
        if any(word in line.lower() for word in ("header=", "token=", "api-key", "authorization", "bearer ", "password", "secret=")):
            continue
        match = re.search(r"(?:pkg/tcpip/[^ :]+|\w+\.go):[0-9]+|Resource exhausted|RESOURCE_EXHAUSTED|amd64\.\.\.|arm64\.\.\.", line, re.I)
        if match:
            line = line[match.start():][:450]
            if line not in result:
                result.append(line)
    return result[:30]


def verify(arm, required_cases=(), min_cases=None, late=(), nogo=False):
    row = records[arm]
    inv, = row["invocations"]
    row["originals"] = {}
    for target in list(items("GetTarget","target",{"invocation_id":inv})):
        label = target.get("label","")
        if label not in row["targets"]:
            continue
        detail = row.setdefault("originals", {}).setdefault(label, {})
        detail["status"] = target.get("status")
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
                        detail["findings"] = diagnostics(text)
                store()
    views = list(row.get("originals",{}).values())
    counts = {k:sum(x.get("go",{}).get(k,0) for x in views) for k in ("PASS","FAIL","SKIP")}
    hits = set(n for view in views for state,n in view.get("cases",[]) if state=="PASS")
    misses = set(n for view in views for n in view.get("failure",[]))
    row["goOriginals"]=counts
    row["complete"]=row["code"]==row["expected"] and not row["missing"] and set(row["originals"])==set(row["targets"]) and all("test.xml" in v and "test.log" in v for v in views)
    if nogo:
        row["complete"] &= all(set(v.get("arch",()))=={"amd64","arm64"} and v.get("failure") == [] for v in views)
    else:
        row["complete"] &= set(required_cases)<=hits and counts["SKIP"]==0
        if not late:
            row["complete"] &= not misses and not counts["FAIL"]
        else:
            row["complete"] &= misses==set(late) and counts["FAIL"]==len(late)
        if min_cases:
            row["complete"] &= counts["PASS"]+counts["FAIL"]>=min_cases
    store()


if path := os.environ.get("ORIGINAL_VERDICT"):
    records.update(json.loads(Path(path).read_text())["records"])
    verify("nogo", nogo=True)
    row = records["nogo"]
    request = {"selector": {"invocation_id": row["invocations"][0]}}
    messages = []
    phases = []
    while True:
        obj = rpc("GetLog", request)
        contents = obj.get("log", {}).get("contents", "")
        messages.extend(diagnostics(contents))
        for line in contents.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
            if any(k in line.lower() for k in ("key=", "token", "authorization", "bearer", "header", "secret")):
                continue
            phase = re.search(r"(Computing main repo mapping|Analyzing|Loading|Fetching|Checking cached actions|Analyzed|Building|Starting local Bazel server|ERROR:|WARNING:|INFO:)", line)
            counts = re.findall(r"([0-9][0-9,]* (?:packages loaded|targets configured|processes|actions running)|\[[0-9, ]+ / [0-9,]+\])", line)
            labels = re.findall(r"(?:@@?[a-zA-Z0-9_+.~-]+)?//[a-zA-Z0-9_/.+~-]*:[a-zA-Z0-9_/.+~-]+", line)
            mnemonics = re.findall(r"(?:Analyzing|Compiling|Generating|Building|Testing|Checking|Linking|Fetching repository|Downloading) [@a-zA-Z_/.0-9:+~-]+", line)
            kinds = re.findall(r"(?:Connection refused|timed out|Not Found|not found|Resource [Ee]xhausted|RESOURCE_EXHAUSTED|HTTP [45][0-9][0-9]|No space left on device|No such file or directory|Connection reset|Operation cancelled|unavailable|[0-9]+s remote|still waiting)", line, re.I)
            if phase or counts or kinds:
                crumb = [phase[1] if phase else "", *counts, *labels[:3], *mnemonics[:3], *kinds[:3]]
                if not phases or phases[-1] != crumb:
                    phases.append(crumb)
        next_page = obj.get("nextPageToken", obj.get("next_page_token"))
        if not next_page:
            break
        request["pageToken"] = next_page
    row["invocationDiagnostics"] = list(dict.fromkeys(messages))[:35]
    row["progress"] = phases[:4] + phases[-55:]
    row["execution"] = {}
    for label in [None, *row["targets"]]:
        lookup = {"invocationId":row["invocations"][0]}
        if label:
            lookup["targetLabel"] = label
        conn = HTTPSConnection("app.buildbuddy.io", timeout=25)
        try:
            conn.request("POST","/rpc/BuildBuddyService/GetExecution",json.dumps({"executionLookup":lookup}),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
            with conn.getresponse() as resp:
                if resp.status != 200:
                    item = {"httpStatus":resp.status}
                else:
                    raw = resp.read(48 << 20)
                    if len(raw) == 48 << 20:
                        item = {"largerThan48MiB":True}
                    else:
                        executions = json.loads(raw).get("execution",[])
                        groups = Counter((str(x.get("stage")), x.get("actionMnemonic",""), int(x.get("status",{}).get("code",0)), int(x.get("exitCode",0))) for x in executions)
                        incomplete = Counter((x.get("targetLabel",""), x.get("actionMnemonic",""), x.get("stage")) for x in executions if str(x.get("stage")) not in ("4","COMPLETED") or x.get("status",{}).get("code",0) or x.get("exitCode",0))
                        item={"count":len(executions),"groups":groups.most_common(35),"unfinishedOrFailed":incomplete.most_common(30)}
        except (OSError,ValueError) as err:
            item = {"readError":type(err).__name__}
        finally:
            conn.close()
        row["execution"][label or "all"]=item
        store()
    store()
    sys.exit(0)

if os.environ.get("NOGO_ONLY"):
    git("checkout", "--quiet", "--detach", product)
    owners = [tcp + ":tcp_nogo", e2e + "tcp_rack_test_nogo", e2e + "tcp_test_nogo"]
    baz("nogo", owners)
    verify("nogo", nogo=True)
    sys.exit(not records["nogo"]["complete"])

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
