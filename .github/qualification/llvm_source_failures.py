"""Retrieve only five preidentified CAS blobs and report closed diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

INV = "bc29a60b-916d-49ee-b194-acb6c21cf98f"
BLOBS = {
    "llvm_cpp1": ("7da48afe634220df95d869949b6fa7d1370e1cefa25cfacbec00f62d463e66b4",24469),
    "llvm_cpp2": ("263d86933a76fea1d34e9fb83ba998adfd9eddab0991a2afd2e3edf8019582ee",14304),
    "llvm_cpp3": ("60ad9d8fc71a95dca661e64d9543dc715efdce2c51f08f2c2c860d2bb52d29df",12338),
    "vdso_test": ("3e2c07f43550cd42d70d37e52ba51b70de4634a93d4e23aad3a34756b3b44323",2821),
    "sandbox_test": ("41f1222f2fe195c25a3d30505f4fc73f7da24ce4f492fbee76d01cdebc1f47b9",7477),
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
    "No PT_LOAD segments in VDSO", "First PT_LOAD segment has non-zero file offset",
    "PT_LOAD segment has different memory and file offsets", "PT_LOAD segment memsz != filesz",
    "PT_LOAD segments out of order", "PT_LOAD segments share a page", "VDSO contains non-empty .data section",
    "VDSO contains non-empty .bss section", "VDSO contains no/empty .text section",
    "program header", "loadable segment", "aligned", "expected contigu", "misaligned", "overlap",
    "offset", "MemSz", "FileSz", "contains relocations", "VDSO has", "VDSO contains", "CRITICAL", ".rela", ".got", ".plt",
    "readelf", "FileNotFoundError", "CalledProcessError", "No module named", "ModuleNotFoundError", "Traceback",
    "check_vdso.py", "python3", "bazel_tools", "runfiles", "LLVM ERROR", "fatal error:", "error:", "warning:", "note:",
    "no member named", "no type named", "unknown type name", "does not name a type", "incomplete type", "incomplete definition",
    "is not a member", "undeclared", "unused", "ambiguous", "redefinition", "cannot initialize", "no matching",
    "cannot be used", "cannot be narrowed", "can't be narrowed", "shadows", "out of range", "implicit conversion", "invalid operands",
    "exception", "C++ exceptions", "RTTI", "standard library", "invalid argument", "unwind", "cxxabi", "libunwind",
    "pure virtual", "override", "consteval", "constexpr", "implicit instantiation", "deleted", "visibility", "-Werror", "unsupported",
    "cannot convert", "too few arguments", "too many arguments", "macro", "requires", "format", "string literal", "template",
    "does not refer to a value", "not allowed", "too many errors", "in file included from", "expected expression",
    "generated", "argument unused during compilation", "read-only", "has no member", "linker", "initializer", "was not declared",
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


def locations(raw):
    # Only projected relative C/C++ source paths, never the sandbox prefix
    # or the command. All accepted path characters are printable ASCII.
    paths = []
    for m in re.finditer(r"(?:^|[/ '\"])((?:llvm|clang|clang-tools-extra|libunwind|libcxxabi|libcxx|tools/nvidia_driver_differ|vdso)/(?:[A-Za-z0-9_.+-]+/){0,12}[A-Za-z0-9_.+-]+):([0-9]{1,6})(?::([0-9]{1,5}))?", raw):
        parts = m[1].split("/")
        if any(p in (".", "..") or len(p)>70 for p in parts):
            continue
        paths.append([m[1], int(m[2]), int(m[3]) if m[3] else None])
    return paths[:3]


def project(data):
    names,tests = witnesses()
    result=[]
    for i,raw in enumerate(data.decode(errors="replace").splitlines(),1):
        row={"line":i}
        if found := [s for s in TOKENS if s.lower() in raw.lower()]:
            row["signals"]=found
        if cases := sorted(t for t in tests if re.search(r"\b"+re.escape(t)+r"\b",raw)):
            row["source_tests"]=cases
        if "x-buildbuddy-api-key" in raw.lower() or re.search(r"(?i)authorization\s*[:=]|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}",raw):
            continue
        code_locations=locations(raw)
        if code_locations:
            row["code_locations"]=code_locations
        if m:=re.search(r'(?:File "[^"]*/)?(check_vdso\.py|subprocess\.py|stage2_bootstrap\.py|__main__\.py)(?:", line |:)([0-9]{1,5})', raw):
            row["python_frame"]=[m[1], int(m[2])]
        if "error:" in raw.lower():
            # Collected from published compiler diagnostics only. Keep names
            # of strong, narrow types and toolchain switches; no free text.
            symbols=re.findall(r"(?<![\w-])((?:_Unwind_|_URC_|__cxa_|__gxx_|__builtin_|std::|llvm::|clang::)[A-Za-z0-9_:]{1,80}|-W[a-z][a-z-]{2,65})(?![\w-])",raw)
            if symbols: row["compiler_symbols"]=sorted(set(symbols))[:8]
        source_locations=[]
        for basename,number in re.findall(r"\b(\w+_test\.go):([0-9]{1,6})",raw):
            if basename in names:
                source_locations.append([str(names[basename]),int(number)])
        if source_locations:
            row["source_locations"]=source_locations
        numbers={name:float(match[1]) for name,pat in NUMERIC.items() if (match:=re.search(pat,raw,re.I))}
        if numbers:
            row["values"]=numbers
        if len(row)>1:
            result.append(row)
    important = [r for r in result if "error:" in r.get("signals",[]) or "CRITICAL" in r.get("signals",[])]
    kept = {r["line"]:r for r in important[:45]+result[:100]}
    return {"lines":len(data.splitlines()),"rows":[kept[n] for n in sorted(kept)]}


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
    dest.write_text(json.dumps(report,separators=(",",":"))+"\n")


if __name__=="__main__":
    main()
