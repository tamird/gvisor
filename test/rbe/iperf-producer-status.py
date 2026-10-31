"""Read bounded public build status for the active sibling job without exposing invocation metadata."""
import json
import os
import urllib.error
import urllib.request

KEYS = ("id", "invocation_id", "invocationId", "success", "command", "pattern", "bazel_exit_code", "bazelExitCode", "timing", "invocation_status", "invocationStatus")
sources = ["e61da06dc253bd3a62c6583f3350520d305d07c5", "bcb5e7e8765cb03cec455730ef8a3341716cf066"]
result = {}
for sha in sources:
    req = urllib.request.Request(
        "https://app.buildbuddy.io/api/v1/GetInvocation",
        data=json.dumps({"selector": {"commit_sha": sha}}).encode(),
        headers={"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]},
    )
    try:
        with urllib.request.urlopen(req,timeout=15) as response:
            data = json.load(response)
    except (OSError, ValueError) as err:
        result[sha] = {"status":getattr(err,"status",None),"errorType":type(err).__name__}
        continue
    found = data.get("invocation", data.get("invocations", []))
    result[sha] = [{k:v for k,v in row.items() if k in KEYS} for row in found]
print(json.dumps(result,indent=2))
