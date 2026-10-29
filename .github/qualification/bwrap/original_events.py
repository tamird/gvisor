"""Report only bwrap target outcomes; keep all raw action/command metadata private."""
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen

ORIGINAL = "4808c33b-309c-4bff-a371-949553295ce4"
LABELS = {"//runsc/cmd/alias/bwrap:bwrap_test", "//runsc/cmd/alias/bwrap:bwrap_integration_test"}


def facts(events):
    rows = []
    for event in events:
        for kind, status_key in [("testResult", "status"), ("testSummary", "overallStatus"), ("completed", "success")]:
            ids = event.get("id", {}).get("targetCompleted" if kind == "completed" else kind, {})
            if ids.get("label") not in LABELS or kind not in event:
                continue
            value = event[kind]
            rows.append({"id": ids, "event": kind, "status": value.get(status_key), "strategy": value.get("executionInfo", {}).get("strategy"), "reports": [x.get("name") for x in value.get("testActionOutput", [])], "failure_detail": bool(value.get("failureDetail"))})
    return rows


def rpc(path, payload):
    req = Request("https://app.buildbuddy.io/"+path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    with urlopen(req, timeout=45) as response:
        raw = response.read(64*1024*1024+1)
    if len(raw) > 64*1024*1024:
        raise ValueError("response too large")
    return json.loads(raw)


def main():
    mode, output, *args = sys.argv[1:]
    if mode == "native":
        source, = args
        with Path(source).open() as stream:
            rows = facts(json.loads(line) for line in stream)
        result = {"source": "original local Bazel JSON file", "rows": rows}
    else:
        try:
            (inv,) = rpc("rpc/BuildBuddyService/GetInvocation", {"lookup": {"invocationId": ORIGINAL}})["invocation"]
            assert inv.get("invocationId") == ORIGINAL
            events = inv.get("event", [])
            rows = facts(e.get("buildEvent", {}) for e in events)
            result = {"source": ORIGINAL, "events": len(events), "rows": rows}
        except Exception as err:
            result = {"source": ORIGINAL, "error": type(err).__name__, "status": getattr(err, "code", None)}
        result["api_targets"] = {}
        for label in sorted(LABELS):
            try:
                reply = rpc("api/v1/GetTarget", {"selector": {"invocation_id": ORIGINAL, "label": label}})
                result["api_targets"][label] = [{"label": v.get("label"), "status": v.get("status")} for v in reply.get("target", [])]
            except Exception as err:
                result["api_targets"][label] = {"error": type(err).__name__, "status": getattr(err, "code", None)}
    Path(output).write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
