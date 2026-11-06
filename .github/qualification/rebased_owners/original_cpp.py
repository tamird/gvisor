"""Classify errors in the original failed C++ compilation; no builds or retries."""
import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import subprocess

INVOCATION = "7127a127-67f2-4653-b9ad-0cdf7b9aae5e"
DEST = Path(os.environ["RUNNER_TEMP"]) / "original-variant-compiler.json"
KEY = os.environ.pop("BUILDBUDDY_API_KEY")
FILES = {p.decode() for p in subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0") if p}
LIBS = ("abseil", "googletest", "gtest", "re2", "protobuf", "rules_cc", "llvm",
        "bazel_tools", "gcc", "sysroot", "bits/", "libstdc++", "crosstool")
TYPES = ("no such file", "not found", "not executable", "unrecognized", "unexpected",
         "wrong architecture", "exec format", "not supported", "required from", "requires",
         "missing", "outside", "undeclared", "not declared", "invalid", "cannot find",
         "not a member", "no matching", "undeclared inclusion", "absolute path", "built-in",
         "C++17", "C++20", "constexpr", "static assertion", "not permitted", "denied",
         "incompatible", "different exception specifier", "exception specification", "conversion",
         "duplicate", "implicit declaration", "undefined reference", "does not name",
         "template argument", "does not support", "not allowed", "cannot convert",
         "const qualifier", "all warnings being treated as errors", "did you mean",
         "ambiguous", "not defined", "fatal error", "internal compiler error")
TOOLS = ("x86_64-linux-gnu-gcc", "aarch64-linux-gnu-gcc", "gcc-11", "gcc-12", "gcc-13",
         "gcc-14", "aarch64-linux-gnu-g++", "x86_64-linux-gnu-g++", "cc1plus", "clang",
         "CppCompile", "cc_wrapper", "libc-header-start.h", "bits/c++config.h", "cstdlib",
         "stddef.h", "stdint.h", "features.h", "type_traits", "thread", "string_view",
         "libatomic", "coral_crosstool", "c++/11", "c++/12", "c++/13", "c++/14", "c++/15",
         "sysroot", "-Werror", "-std=gnu++", "-std=c++", "gtest-port.h",
         "x86_64-linux-gnu", "aarch64-linux-gnu", "x86_64", "aarch64")
LOCATION = re.compile(r"(?:^|\s)([a-zA-Z0-9_./+@-]+\.(?:c|cc|cpp|h|hpp|inc)):(\d+)(?::(\d+))?")
report = {"invocation": INVOCATION, "pages": 0, "diagnostics": [], "markers": []}

def line_summary(line):
    lower = line.lower()
    markers = [w for w in LIBS + TOOLS if w.lower() in lower]
    classes = [w for w in TYPES if w.lower() in lower]
    if not any(k in lower for k in ("error", "fatal", "failed", "not found", "undeclared", "absolute path",
                                     "outside", "no such file", "isn't", "not permitted", "invalid",
                                     "is not", "was not", "toolchain", "warning:", "c++/")):
        return
    for m in markers + classes:
        if m not in report["markers"]:
            report["markers"].append(m)
    if len(report["diagnostics"]) >= 24:
        return
    row = {"classes": classes, "markers": markers}
    m = LOCATION.search(line)
    if m:
        row["source"] = m[1] if m[1] in FILES else "external"
        row["line"] = int(m[2]); row["column"] = int(m[3] or 0)
    if row not in report["diagnostics"]:
        report["diagnostics"].append(row)

try:
    token = "0000"
    tail = ""
    digest = hashlib.sha256()
    for _ in range(32):
        connection = HTTPSConnection("app.buildbuddy.io", timeout=20)
        try:
            payload = {"selector": {"invocation_id": INVOCATION}, "page_token": token}
            connection.request("POST", "/api/v1/GetLog", json.dumps(payload),
                               {"Content-Type": "application/json", "x-buildbuddy-api-key": KEY})
            response = connection.getresponse()
            report["http_status"] = response.status
            if response.status != 200:
                raise ValueError("GetLog failed")
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("GetLog pagination exceeded the byte cap")
        finally:
            connection.close()
        page = json.loads(raw)
        body = page.get("log", {}).get("contents", "")
        digest.update(body.encode());report["pages"] += 1
        lines = (tail + body).split("\n")
        tail = lines.pop()
        token = page.get("nextPageToken") or page.get("next_page_token")
        if not token and tail:
            lines.append(tail)
        for line in lines:
            line_summary(line)
        if not token:
            break
        if not re.fullmatch("[a-zA-Z0-9_+=/-]{1,256}", token):
            raise ValueError("unexpected page token")
    report["sha256"] = digest.hexdigest()
    report["complete"] = not token
finally:
    DEST.write_text(json.dumps(report, indent=2) + "\n")
