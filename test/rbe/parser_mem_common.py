import json
import os
import urllib.request
def call(method, **args):
    url = "https://app.buildbuddy.io/" + (
        "rpc/BuildBuddyService/" if method in ("SearchInvocation", "GetExecution") else "api/v1/") + method
    request = urllib.request.Request(url, data=json.dumps(args).encode(), headers={
        "Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    with urllib.request.urlopen(request, timeout=50) as response:
        return json.load(response)
