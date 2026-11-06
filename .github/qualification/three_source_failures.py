"""Retrieve only three preidentified CAS blobs and report closed diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

INV = "2e8e8f7d-8d06-4b9e-83c6-e6134aaf7924"
BLOBS = {
    "cstate_wake_probe": ("93d83e907b7155c03235d83a040401b0e865b23ae5116af0771ee5f5d3c20f12",164),
    "sandbox_test": ("aae6000060263f3c41799d614fa07a8c6a1e27a3003a5d854499699fb82744d9",7477),
    "netgofer_test": ("043a1134db064e166ce49304e5cd8e65ccc9b45dd02e312fd92aab7cdb43508f",552),
}
TOKENS = (
    "No such file or directory", "command not found", "executable file not found", "cannot execute",
    "execve", "execvp", "fork/exec", "x86_64-linux-gnu-gcc", "aarch64-linux-gnu-gcc",
    "/bin/bash", "/usr/bin/bash", "/bin/sh", "/usr/bin/sh", "/usr/bin/env",
    "/usr/bin/cc", "/bin/cc", "/usr/bin/gcc", "/usr/bin/g++", "/usr/bin/clang",
    "gcc-13", "gcc-14", "gcc-12", "cc1plus", "cc1", "collect2", "ld.lld", "clang++",
    "error while loading shared libraries", "cc_wrapper", "linux-sandbox", "process-wrapper",
    "precondition", "failed to start", "Permission denied", "Operation not permitted",
    "Invalid argument", "Invalid cross-device link", "Operation not supported", "No space left on device",
    "Read-only file system", "Device or resource busy", "Connection refused", "Connection reset by peer",
    "network namespace", "mount namespace", "user namespace", "unshare", "setns", "mount", "netlink",
    "cgroup", "no such network interface", "no such device", "network is unreachable",
    "address already in use", "cannot assign requested address", "protocol not supported",
    "not implemented", "context deadline exceeded", "deadline exceeded", "timeout", "timed out", "panic:",
    "Runtime configuration", "unexpected", "wanted", "want", "expected", "got", "SIGKILL", "SIGABRT",
    "SIGTERM", "SIGQUIT", "SIGSEGV", "SIGINT", "segmentation fault", "data race", "exit status",
    "killed", "goroutine", "application error", "SIGURG", "no tests to run", "unixpacket", "waitid",
    "AF_NETLINK", "SOCK_SEQPACKET", "SOCK_STREAM", "socketpair", "EINVAL", "ENOENT", "EPERM", "EACCES",
    "EADDRINUSE", "EAFNOSUPPORT", "ESRCH", "ECONNRESET", "ETIMEDOUT", "EOPNOTSUPP", "ENODEV", "EEXIST",
    "=== RUN", "--- PASS", "--- SKIP", "--- FAIL", "FAIL", "PASS",
)
NUMERIC = {
    "elapsed_s": r"\(([0-9]{1,4}(?:\.[0-9]{1,3})?)s\)",
    "timeout_s": r"(?:timeout|timed out|running tests:?)\D{0,26}([0-9]{1,4}(?:\.[0-9]{1,3})?)s",
    "timeout_m": r"(?:timeout|timed out)\D{0,26}([0-9]{1,3})m",
    "exit": r"exit (?:status|code)[: ]+([0-9]{1,3})",
    "want_int": r"\b(?:want|expected)[: =]+([0-9]{1,8})",
    "got_int": r"\bgot[: =]+([0-9]{1,8})",
}
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):
        return None


def witnesses():
    files = {}
    names = set()
    for directory in (Path("runsc/sandbox"),Path("runsc/gofer/net")):
        for path in directory.glob("*_test.go"):
            files[path.name] = path
            text=path.read_text()
            names.update(re.findall(r"(?m)^func (Test\w+)\(",text))
    return files, names


def project(data):
    names,tests = witnesses()
    result=[]
    for i,raw in enumerate(data.decode(errors="replace").splitlines(),1):
        row={"line":i}
        if found := [s for s in TOKENS if s.lower() in raw.lower()]:
            row["signals"]=found
        if cases := sorted(t for t in tests if re.search(r"\b"+re.escape(t)+r"\b",raw)):
            row["source_tests"]=cases
        locations=[]
        for basename,number in re.findall(r"\b(\w+_test\.go):([0-9]{1,6})",raw):
            if basename in names:
                locations.append([str(names[basename]),int(number)])
        if locations:
            row["source_locations"]=locations
        numbers={name:float(match[1]) for name,pat in NUMERIC.items() if (match:=re.search(pat,raw,re.I))}
        if numbers:
            row["values"]=numbers
        if len(row)>1:
            result.append(row)
    return {"lines":len(data.splitlines()),"rows":result}


def main():
    key=os.environ.pop("BUILDBUDDY_API_KEY")
    opener=urllib.request.build_opener(NoRedirect)
    report={"invocation":INV,"blobs":{}}
    for name,(sha,size) in BLOBS.items():
        url=f"https://remote.buildbuddy.io/file/download?invocation_id={INV}&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F{sha}%2F{size}"
        entry={"sha256":sha,"size":size}
        report["blobs"][name]=entry
        try:
            req=urllib.request.Request(url,headers={"x-buildbuddy-api-key":key})
            with opener.open(req,timeout=25) as response:
                data=response.read(size+1)
            assert len(data)==size and hashlib.sha256(data).hexdigest()==sha
            entry.update(verified=True, **project(data))
        except urllib.error.HTTPError as exc:
            entry["http_status"]=exc.code
    dest=Path(os.environ["RUNNER_TEMP"])/"closed-source-failures.json"
    dest.write_text(json.dumps(report,indent=2)+"\n")


if __name__=="__main__":
    main()
