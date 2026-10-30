# Qualification-only recovery of existing build/test evidence.
import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen

out = Path(os.environ["RUNNER_TEMP"]) / "records"
out.mkdir(exist_ok=True)
headers = {"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]}
def call(method, data):
    req = Request("https://app.buildbuddy.io/api/v1/" + method, data=json.dumps(data).encode(), headers=headers)
    with urlopen(req, timeout=45) as response:
        return json.load(response)

def logs(inv, label, terms):
    token = ""
    found = []
    for _ in range(20):
        response = call("GetLog", {"selector":{"invocation_id":inv}, "page_token":token})
        found += [line for line in response.get("log",{}).get("contents","").splitlines() if any(term in line for term in terms) and "header" not in line.lower()]
        token = response.get("nextPageToken", response.get("next_page_token", ""))
        if not token:
            break
    (out / label).write_text("\n".join(found[-70:])+"\n")
    print(label, len(found))

logs("cd25e1d9-a266-484a-9f0d-cf11592c8b23", "remote-inventory", ("eligible_tpu_groups",))
result=call("GetInvocation", {"selector":{"commit_sha":"a3e8093e6e83ef347c36de3fc8e3868ebf64658d"}})
for invocation in result.get("invocation", []):
    info = invocation.get("id", {})
    key = info.get("invocationId", info.get("invocation_id",""))
    if re.fullmatch(r"[0-9a-f-]{36}", key):
        logs(key, "link-error-"+key, ("ld.lld: error:", "undefined reference", "cannot find", "undefined symbol", ">>> referenced by", "cannot open", "linker command failed"))
