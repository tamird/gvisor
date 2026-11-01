"""Read original target results without re-running the TCP suite."""

from collections import Counter
from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree


key = os.environ.pop("BUILDBUDDY_API_KEY")
output = Path(os.environ["RUNNER_TEMP"]) / "original-rack.json"
records = {}


def rpc(name, request, binary=False):
    connection = HTTPSConnection("app.buildbuddy.io", timeout=20)
    try:
        connection.request("POST", "/api/v1/" + name, json.dumps(request),
                           {"Content-Type":"application/json","x-buildbuddy-api-key":key})
        with connection.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f"{name}: HTTP {response.status}")
            body = response.read(24 << 20)
            if binary:
                return body
            return json.loads(body)
    finally:
        connection.close()


def items(method, field, query):
    request = {"selector": query}
    while True:
        obj = rpc(method, request)
        yield from obj.get(field, [])
        next_page = obj.get("nextPageToken", obj.get("next_page_token"))
        if not next_page:
            return
        request["pageToken"] = next_page


def original(uri):
    p=urlsplit(uri)
    m = re.search(r"/blobs/([0-9a-f]{64})/([0-9]+)$",p.path)
    if p.scheme != "bytestream" or not m:
        raise ValueError("Missing original digest")
    body = rpc("GetFile",{"uri":uri},True)
    if len(body)!=int(m[2]) or sha256(body).hexdigest()!=m[1]:
        raise ValueError("Original size or SHA256 mismatched")
    return body


for arm,inv in {"product":"954f32c2-fcaa-45fd-921d-257579d834d5","race":"9b1e438b-06ef-4f15-84dc-e6a40eefa665",
                "nogo-amd64":"bf7a564c-6e6b-4dbd-b204-2eb37f10824c","nogo-arm64":"8ee99f8d-ac2c-428b-9be3-08f88ca7d390"}.items():
    targets = list(items("GetTarget","target",{"invocation_id":inv}))
    row = {"invocation":inv,"targets":{}}
    records[arm] = row
    for target in targets:
        label = target.get("label","")
        if "/tcp" not in label:
            continue
        rec = {"status":target.get("status")}
        row["targets"][label] = rec
        if arm.startswith("nogo"):
            continue
        for action in items("GetAction","action",{"invocation_id":inv,"target_label":label}):
            for file in action.get("file",[]):
                path = file.get("name","").split("/")[-1]
                if path not in ("test.xml","test.log") or path in rec:
                    continue
                b=original(file["uri"])
                rec[path]={"sha256":sha256(b).hexdigest(),"bytes":len(b)}
                if path=="test.xml":
                    root=ElementTree.fromstring(b)
                    rec["xml"]={"total":len(list(root.iter("testcase"))),
                                "failure":[x.get("name") for x in root.iter("testcase") if x.find("failure") is not None or x.find("error") is not None],
                                "skips":sum(x.find("skipped") is not None for x in root.iter("testcase"))}
                if path=="test.log":
                    text=b.decode("utf-8","replace")
                    cases=re.findall(r"^\s*--- (PASS|FAIL|SKIP): (\S+) \(",text,re.M)
                    rec["go"]=dict(Counter(a for a,_ in cases))
                    rec["failed"]=[n for a,n in cases if a=="FAIL"]
                    if "TestRACKWithWindowFull" in rec["failed"]:
                        start=text.index("=== RUN   TestRACKWithWindowFull")
                        end=text.index("--- FAIL: TestRACKWithWindowFull",start)
                        rec["diagnostic"]=[x[:1000] for x in text[start:end].splitlines() if any(w in x for w in ("_test.go:", "context.go:", "packet", "unexpected", "expected"))][:8]
    output.write_text(json.dumps(records,indent=2)+"\n")
