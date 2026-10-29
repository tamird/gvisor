"""Classify the original Go-helper failure; read only its fixed official invocation."""

from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re

ID = "da6efa53-b448-4cbd-ad92-1ec3b9335ad1"
key = os.environ.pop("BUILDBUDDY_API_KEY")
token = "0000"
seen, parts = set(), []
for _ in range(16):
    assert token not in seen
    seen.add(token)
    c = HTTPSConnection("app.buildbuddy.io", timeout=25)
    try:
        c.request("POST", "/api/v1/GetLog", json.dumps({"selector":{"invocation_id":ID},"page_token":token}),
                  {"Content-Type":"application/json","x-buildbuddy-api-key":key})
        with c.getresponse() as r:
            assert r.status == 200, r.status
            b = r.read((2 << 20) + 1)
            assert len(b) <= 2 << 20
        j = json.loads(b)
        parts.append(j.get("log",{}).get("contents",""))
        token = j.get("nextPageToken", j.get("next_page_token",""))
        if not token:
            break
    finally:
        c.close()
else:
    raise ValueError("original log page bound")
text = re.sub(r"\x1b\[[;\d]*[A-Za-z]", "", "\n".join(parts)).replace("\r","\n")
assert text and key not in text
hints = [
    r"GoToolchainBinaryBuild", r"build Go toolchain", r"Go SDK", r"Failed to pull image", r"failed to (?:pull|resolve|authorize)",
    r"Unauthenticated", r"Unauthorized", r"PermissionDenied", r"ResourceExhausted", r"NotFound", r"UNAVAILABLE",
    r"DeadlineExceeded", r"No such file or directory", r"no such file or directory", r"exec format error",
    r"OOMKilled", r"out of memory", r"No space left on device",r"no space left on device", r"no executor",
    r"authentication required",r"unknown blob",r"Cannot execute",r"cannot find",r"failed to run command",
    r"signal: killed",r"exit [Cc]ode [0-9]+",r"failed to create container",r"failed to initialize",
    r"non-root",r"permission denied",r"not found",r"environment variable (?:HOME|GOCACHE|PATH)",
    r"undefined reference",r"missing (?:file|toolchain)",r"exec: .{0,20}/(?:bash|env)",
    r"exec user process caused",r"failed to make directory",r"GOCACHE.{0,15}(?:is not defined|not set)",
    r"dockerUser",r"invalid (?:argument|reference|digest)",r"rpc error: code = [A-Za-z]+",
]
rows={}
for i,line in enumerate(text.splitlines(),1):
    found=[]
    for pattern in hints:
        found.extend(re.findall(pattern,line,re.I))
    if found: rows[i]=sorted(set(found))
report={"original":ID,"source":"32bf2915538481adcfddd866650318cbfec441c7","bytes":len(text.encode()),"sha256":sha256(text.encode()).hexdigest(),"matches":rows}
# Project only the missing executable/library and packaging bootstrap subjects,
# never a command line, argument, URL, or authentication-bearing header.
missing = []
for line in text.splitlines():
    pat = r"(?:^|[ :\x60\x27\x22])([^ :\x60\x27\x22]{1,170})[ :\x60\x27\x22]*(?:command )?not found"
    for item in re.findall(pat, line, re.I):
        name = item.rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.+-]{0,30}", name) and not re.search(r"(?i)(secret|token|password|auth|key)", name):
            missing.append(name)
keywords = ("go", "bash", "sh", "GOROOT", "GOPATH", "GOCACHE", "HOME", "PATH", "env", "stat", "exec", "rules_go", "ruby", "gcc", "clang", "ld", "GLIBC", "glibc", "LIBC", "libc", "version", "library", "executable", "Program", "musl", "loader", "libc.so.6", "libm.so.6", "libstdc++.so.6", "libgcc_s.so.1", "mktemp", "docker", "platform", "container", "error", "not", "found", "No", "file", "or", "directory", "required", "by", "line", "binary_wrapper", "libpthread.so.0")
details = {}
for n,line in enumerate(text.splitlines(),1):
    if 35 <= n <= 43:
        facts = [m.group(0) for m in re.finditer(r"GLIBC_[.0-9]+|[A-Za-z_][.A-Za-z0-9_+-]*", line) if m.group(0) in keywords or m.group(0).startswith("GLIBC_")]
        if facts: details[n]=facts
report.update(missing_names=missing, original_compiler_terms=details)
(Path(os.environ["RUNNER_TEMP"])/"original-catalog.json").write_text(json.dumps(report,indent=2)+"\n")
assert rows, "no classified original log facts"
