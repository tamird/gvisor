"""Read the original Bazel logs; publish classifications, never complete command lines."""
import json
import os
from pathlib import Path
import re
from urllib import request

endpoints = {"stock": "7bf91418-58ba-4530-9b98-60ff00a57057", "candidate": "b9ddd224-cd98-4cdd-b80b-d36d264e3236"}
api_key = os.environ["BUILDBUDDY_API_KEY"]
public = {}
for case, invocation in endpoints.items():
    summary = {"invocation": invocation, "matches": [], "pages": 0}
    public[case] = summary
    page = ""
    while True:
        body = {"selector": {"invocationId": invocation}, "pageToken": page}
        req = request.Request("https://app.buildbuddy.io/api/v1/GetLog", data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json", "x-buildbuddy-api-key": api_key})
        with request.urlopen(req, timeout=30) as r:
            reply = json.load(r)
        summary["pages"] += 1
        for raw in reply["log"]["contents"].splitlines():
            s = re.sub(r"\x1b\[[;\d]*[a-zA-Z]", "", raw).strip()
            # Exclude action commands and configuration; retain only named
            # tool failures and compiler diagnostics, never auth parameters.
            if (not s or len(s) > 1600 or api_key in s or
                any(t in s.lower() for t in ("token", "credential", "header=", "api-key", "secret", "bearer", "://", "exec env", " --"))):
                continue
            reasons = [r for r in ("non-root Bazel module is forbidden", "No such file or directory",
                                   "executable file not found", "Permission denied", "missing dynamic library",
                                   "Exec format error", "command not found", "unrecognized command-line option",
                                   "unsupported option", "no enabled platforms", "undefined reference",
                                   "connection refused", "error executing", "failed to execute", "not executable",
                                   "no such package", "no such target", "not declared", "did not match",
                                   "fatal error", "unrecognized runfiles") if r.lower() in s.lower()]
            if reasons:
                tools = [t for t in ("builder_binary_wrapper.sh", "GoToolchainBinaryBuild", "GoStdlib", "gcc",
                                     "clang", "/bin/sh", "bash", "/usr/bin/env", "python3", "cgo", "cc1",
                                     "go_deps.gazelle_override") if t in s]
                summary["matches"].append("; ".join(tools+reasons))
        page = reply.get("nextPageToken", reply.get("next_page_token"))
        if not page:
            break
        assert summary["pages"] < 30
    summary["matches"] = list(dict.fromkeys(summary["matches"]))[:20]
    assert summary["matches"], case
Path(os.environ["RUNNER_TEMP"], "original-go-logs.json").write_text(json.dumps(public, indent=2)+"\n")
