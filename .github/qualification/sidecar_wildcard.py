"""Compare actual wildcard expansion before and after the sidecar manual tag."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
from urllib.parse import unquote, urlparse
import uuid

ROOT = Path(os.environ["RUNNER_TEMP"]).resolve()
PUBLIC = ROOT / "out/verdict.json"
BASE = "59bc02b23955bc1d4c6a4c1423da514f38d05287"
FIXED = "c329a6e31a5968a410831950c8a457ee243384c8"
CARRIER = os.environ["GITHUB_SHA"]
ALLOCATOR = "//test/rbe:hosted_allocator"
OWNERS = {
    "//runsc/fdparking:fdparking_binary": "runsc-fd-parking",
    "//runsc/prewarmer:prewarmer_binary": "gvisor-sentry-prewarmer",
    "//runsc/procumounter:procumounter_binary": "gvisor-proc-umounter",
}
INNER = "//runsc/fdparking:_linked/runsc-fd-parking"
TARGETS = ["//runsc/fdparking:all", *list(OWNERS)[1:]]
OPTIONS = ["--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
           "--remote_cache=", "--remote_executor=", "--bes_backend=", "--custom_malloc=" + ALLOCATOR,
           "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox"]
# These are the exact allocator fixture and forced symbol used by the prior sidecar qualification.
BUILD = '''load("@rules_cc//cc:defs.bzl", "cc_library")
cc_library(
    name = "hosted_allocator",
    srcs = ["hosted_allocator.c"],
    alwayslink = True,
    linkopts = ["-Wl,-u,sidecar_qualification_allocator"],
    visibility = ["//visibility:public"],
)
'''
ALLOC_SRC = "extern void requires_a_hosted_runtime(void);\nvoid sidecar_qualification_allocator(void) { requires_a_hosted_runtime(); }\n"
SIGNALS = ("requires_a_hosted_runtime", "sidecar_qualification_allocator", "undefined reference", "undefined symbol",
           "no such target", "no such file or directory", "no matching toolchains", "no space left", "permission denied")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def save() -> None:
    R["verified_at"] = now()
    PUBLIC.write_text(json.dumps(R, sort_keys=True, indent=2) + "\n")


def normalize(label: str) -> str:
    return label[2:] if label.startswith("@@//") else label[1:] if label.startswith("@//") else label


def invoke(name: str, args: list[str], limit: int = 1200) -> tuple[dict, list[dict], str]:
    row = R["phases"][name] = {"started_at":now(), "requested_invocation":str(uuid.uuid4())}
    bep, console, stdout = (ROOT / (name + suffix) for suffix in (".bep", ".console", ".stdout"))
    save()
    command = ["timeout", "--signal=INT", "--kill-after=30s", str(limit), "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
    with stdout.open("wb") as out, console.open("wb") as err:
        row["native_exit"] = subprocess.run(command + args + ["--build_event_json_file=" + str(bep), "--invocation_id=" + row["requested_invocation"]], stdout=out, stderr=err).returncode
    row["finished_at"] = now()
    save()
    assert console.stat().st_size < 128*1024**2 and stdout.stat().st_size < 32*1024**2
    raw = console.read_bytes()
    row["console"] = {"sha256":hashlib.sha256(raw).hexdigest(), "bytes":len(raw),
                      "signals":[s for s in SIGNALS if s in raw.decode(errors="replace")]}
    assert not bep.exists() or bep.stat().st_size < 128*1024**2
    events = [json.loads(line) for line in bep.read_text().splitlines()] if bep.exists() else []
    row["observed_invocations"] = [e["started"]["uuid"] for e in events if "started" in e]
    assert not row["observed_invocations"] or row["observed_invocations"] == [row["requested_invocation"]]
    row["test_attempts"] = sum("testResult" in e.get("id", {}) for e in events)
    row["selected"] = sorted({normalize(e["id"]["targetConfigured"]["label"]) for e in events if "targetConfigured" in e.get("id", {})})
    row["completed"] = [{"label":normalize(e["id"]["targetCompleted"]["label"]),
                         "configuration":e["id"]["targetCompleted"].get("configuration", {}).get("id"),
                         "success":e["completed"].get("success", False)}
                        for e in events if "targetCompleted" in e.get("id", {}) and not e["id"]["targetCompleted"].get("aspect")]
    row["failed_actions"] = [{"owner":normalize(e["action"].get("label", "")), "type":e["action"].get("type"), "exit":e["action"].get("exitCode")}
                             for e in events if "action" in e and not e["action"].get("success", False)]
    save()
    assert row["observed_invocations"] and row["test_attempts"] == 0
    return row, events, stdout.read_text()


def binaries(row: dict, events: list[dict]) -> None:
    sets = {e["id"]["namedSet"]["id"]: e["namedSetOfFiles"] for e in events if "namedSet" in e.get("id", {})}
    result = row["binaries"] = {}
    for owner, filename in OWNERS.items():
        completed, = [e["completed"] for e in events if normalize(e.get("id", {}).get("targetCompleted", {}).get("label", "")) == owner and not e["id"]["targetCompleted"].get("aspect")]
        assert completed.get("success"), owner
        pending = [s["id"] for g in completed["outputGroup"] if g["name"] == "default" for s in g.get("fileSets", [])]
        seen, files = set(), []
        while pending:
            key = pending.pop()
            if key not in seen:
                seen.add(key)
                files.extend(sets[key].get("files", []))
                pending.extend(s["id"] for s in sets[key].get("fileSets", []))
        item, = [f for f in files if Path(f.get("name", "")).name == filename]
        uri = urlparse(item["uri"])
        assert uri.scheme == "file" and not (uri.netloc or uri.query or uri.fragment)
        path = Path(unquote(uri.path)).resolve(strict=True)
        output = Path("bazel-out").resolve(strict=True)
        output.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
        rel = path.relative_to(output)
        package = owner[2:].split(":")[0]
        assert rel.parts[1:3] == ("bin", "runsc") and str(Path(*rel.parts[2:])).startswith(package + "/")
        assert path.name == filename and path.is_file() and os.access(path, os.X_OK)
        data = path.read_bytes()
        assert 0 < len(data) <= 4096 and data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", data, 18)[0] == 62
        offset, width, count = struct.unpack_from("<Q",data,32)[0], struct.unpack_from("<H",data,54)[0], struct.unpack_from("<H",data,56)[0]
        assert width >= 56 and offset + width*count <= len(data)
        phdrs = [struct.unpack_from("<IIQQQQQQ",data,offset+i*width) for i in range(count)]
        assert all(p[0] != 3 for p in phdrs)
        for p in phdrs:
            if p[0] == 2:
                assert p[2]+p[5] <= len(data) and p[5] % 16 == 0
                assert all(struct.unpack_from("<q",data,i)[0] != 1 for i in range(p[2],p[2]+p[5],16))
        result[owner] = {"sha256":hashlib.sha256(data).hexdigest(), "bytes":len(data), "machine":62, "static":True}
    save()


R = {"status":"INCOMPLETE", "base":BASE, "product":FIXED, "carrier":CARRIER, "phases":{}, "errors":{},
     "scope":"AMD64 native GNU Bazel build only: one real package wildcard and three public sidecars with custom_malloc; no runtime tests or new ARM64/sanitizer qualification",
     "targets":TARGETS, "environment":{"system":platform.system(), "machine":platform.machine(), "bazel":"9.3.1"},
     "allocator_sha256":hashlib.sha256(ALLOC_SRC.encode()).hexdigest(), "started_at":now()}
save()
assert platform.system() == "Linux" and platform.machine() == "x86_64"
assert git("rev-parse", "HEAD") == CARRIER and git("rev-parse", CARRIER+"^") == FIXED and git("rev-parse",FIXED+"^") == BASE
assert git("diff", "--name-only", BASE, FIXED) == "runsc/sidecar.bzl"
assert git("show",FIXED+":runsc/sidecar.bzl").replace('        tags = ["manual"],\n', '') == git("show",BASE+":runsc/sidecar.bzl")
for name, source in (("stock", BASE), ("fixed", FIXED)):
    try:
        git("switch", "--quiet", "--detach", source)
        assert git("rev-parse", "HEAD") == source and not git("status", "--porcelain", "--untracked-files=no")
        for filename, data in (("BUILD", BUILD), ("hosted_allocator.c", ALLOC_SRC)):
            path = Path("test/rbe") / filename
            if path.exists():
                assert path.read_text() == data
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(data)
        row, events, _ = invoke(name, ["build", *OPTIONS, "--keep_going", *TARGETS])
        row["source"] = source
        if name == "stock":
            assert row["native_exit"] == 1 and INNER in row["selected"]
            assert "requires_a_hosted_runtime" in row["console"]["signals"]
            assert {r["label"] for r in row["completed"] if not r["success"]} == {INNER}
            assert any(a["owner"] == INNER and a["type"] == "CppLink" and a["exit"] for a in row["failed_actions"])
            row["disposition"] = "EXPECTED_FAILURE"
        else:
            assert row["native_exit"] == 0 and INNER not in row["selected"]
            assert all(r["success"] for r in row["completed"])
            assert set(R["phases"]["stock"]["selected"]) - set(row["selected"]) == {INNER}
            assert set(row["selected"]) - set(R["phases"]["stock"]["selected"]) == set()
            assert set(OWNERS) <= set(row["selected"])
            binaries(row, events)
            row["disposition"] = "PASS"
        save()
    except Exception as exc:
        R["errors"][name] = type(exc).__name__
        save()
try:
    expression = "filter('_linked/', deps(set(" + " ".join(OWNERS) + "), 1))"
    projection = "str(target.label) + ' ' + str(build_options(target).get('//command_line_option:custom_malloc'))"
    row, _, output = invoke("transition", ["cquery", *OPTIONS, "--output=starlark", "--starlark:expr="+projection, expression], 300)
    observations = sorted([normalize(x[0]), x[1]] for line in output.splitlines() if (x := line.split()))
    expected = sorted([owner.split(":")[0]+":_linked/"+filename, "None"] for owner,filename in OWNERS.items())
    row["observations"] = observations if observations == expected else []
    row["projection_matches"] = observations == expected
    save()
    assert row["native_exit"] == 0 and observations == expected
    row["disposition"] = "PASS"
except Exception as exc:
    R["errors"]["transition"] = type(exc).__name__
R["status"] = "PASS" if not R["errors"] else ("INCOMPLETE" if any(r.get("native_exit") in (124, 137) for r in R["phases"].values()) else "FAIL")
save()
raise SystemExit(0 if R["status"] == "PASS" else 1)
