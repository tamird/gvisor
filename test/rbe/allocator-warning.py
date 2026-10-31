import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

out = Path(os.environ["RUNNER_TEMP"]) / "allocator"
out.mkdir(exist_ok=True)
p = Path("test/util/errno_safe_allocator.cc")
source = "bcb5de5b245b8264e4db387120dbc3ac31b4e603"
master = "38db31869e3014bc7c364c3c62007f59a1e315ed"
fixed = "a0c312aaa76631e10d1aa77e9f1cd0233fa352f2"
strict = "--cxxopt=-Werror"
rows = {}
report = {"source": source, "masterAllocator": master, "noexceptOnly": fixed, "arms": rows}

def output(*args):
    return subprocess.check_output(args).decode().strip()

def records(path):
    data = path.read_text().strip()
    if data.startswith("["):
        return json.loads(data)
    dec = json.JSONDecoder()
    result = []
    while data:
        row, offset = dec.raw_decode(data)
        result.append(row)
        data = data[offset:].lstrip()
    return result

def run(name, blob, flags):
    d = out / name
    d.mkdir()
    assert output("git", "hash-object", str(p)) == blob
    argv = ["bazelisk", "build", "--config=x86_64", "--spawn_strategy=linux-sandbox",
            "--noshow_progress", "--color=no", "--curses=no",
            "--execution_log_json_file=" + str(d/"execution.json"), *flags, "//test/util:errno_safe_allocator"]
    (d/"command.json").write_text(json.dumps(argv)+"\n")
    with (d/"bazel.log").open("w") as f:
        r = subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT)
    rows[name] = {"flags": flags, "code": r.returncode}
    # Keep stdout even if Bazel exited before registering an action.
    log = (d/"bazel.log").read_text(errors="replace")
    actions = [a for a in records(d/"execution.json") if a.get("mnemonic") == "CppCompile"
               and any(arg.endswith("test/util/errno_safe_allocator.cc") for arg in a.get("commandArgs", []))]
    rows[name]["actions"] = [{k:a.get(k) for k in ("runner","cacheHit","exitCode","status","targetLabel","commandArgs")} for a in actions]
    rows[name]["diagnostics"] = [s for s in log.splitlines() if "implicit-exception-spec-mismatch" in s]
    assert actions, "no matching CppCompile: " + log[-2000:]
    for a in actions:
        args = a["commandArgs"]
        assert a.get("runner") == "linux-sandbox" and not a.get("cacheHit"), a
        assert ("clang" in args[0] or any("clang" in v for v in args[:3])) and not args[0].startswith("/usr/bin/"), args[:3]
        assert ("-Werror" in args) == bool(flags), args
    (d/"execution.json").unlink()  # argv/runner from every pertinent action saved in verdict
    return r.returncode

try:
    assert output("git", "rev-parse", "HEAD^") == source
    subprocess.run(["git", "checkout", "--quiet", "--detach", source], check=True)
    tracked = "test/util/errno_safe_allocator.cc"
    # Unchanged in current master and in the ancestor within the shallow checkout.
    raw = subprocess.check_output(["git", "cat-file", "blob", master])
    prefix = b"void operator delete"
    old = b"(void* p) {"
    new = b"(void* p) noexcept {"
    lines = raw.splitlines(keepends=True)
    assert sum(line.startswith(prefix) and old in line for line in lines) == 2
    patched = b"".join(line.replace(old, new) if line.startswith(prefix) else line for line in lines)
    p.write_bytes(raw)
    assert set(output("git", "diff", "--name-only", source).splitlines()) == {tracked}
    os.environ["USE_BAZEL_VERSION"] = Path("images/default/bazelversion").read_text().strip()
    os.environ["LC_ALL"] = "C"
    report["bazelVersion"] = os.environ["USE_BAZEL_VERSION"]
    ordinary = run("master", master, [])
    required = run("master-strict", master, [strict])
    p.write_bytes(patched)
    accepted = run("noexcept-strict", fixed, [strict])
    assert ordinary == 0 and accepted == 0, "ordinary or corrected strict did not build"
    if required:
        assert rows["master-strict"]["diagnostics"] and any(a.get("exitCode",0) for a in rows["master-strict"]["actions"]), "strict failed for another reason"
        report["conclusion"] = "strict syscall build requires noexcept"
    else:
        report["conclusion"] = "strict syscall build accepted the master allocator unchanged"
    report["status"] = "PASS"
except Exception as e:
    report.update(status="FAIL", error=repr(e))
finally:
    (out/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
print(report["status"], report.get("conclusion",report.get("error")), flush=True)
sys.exit(report["status"] != "PASS")
