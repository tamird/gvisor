"""Check the Make-installed LLVM/musl plugin CLI and Sentry across the native Docker bridge."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
from urllib.parse import unquote, urlsplit
import time
import traceback
import xml.etree.ElementTree as ET

import consumer
import native_cross_inputs as evidence

import public_assets as assets
import tldk_queue as inputs

ARM, = sys.argv[1:]
assert ARM in ("current-gnu", "current-musl", "layout")
CURRENT = "244ef8019a851cd26488169e8437e5ea446bfac9"
SOURCE = "146a0c0601ffa43e63d68c1bc9bbb640522c557a" if ARM == "integrated" else CURRENT
MODE = ["--config=plugin-tldk"]
if ARM == "current-musl":
    MODE.extend(inputs.original.MUSL)

LAYOUT = sorted({"runsc", "containerd-shim-runsc-v1"} | {
    "gvisor-bin/" + name for name in ("checkpointgofer", "gvisor_sentry", "gvisor_sentry_plugin_stack",
        "runsc-metric-server", "runsc-netgofer", "runsc-fd-parking", "gvisor-sentry-prewarmer",
        "gvisor-proc-umounter")})
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--remote_cache=",
         "--remote_executor=", "--bes_backend=", "--disk_cache=", "--lockfile_mode=off", "--module_mirrors=https://bcr.cloudflaremirrors.com",
         "--distdir=" + str(assets.DIST)]
DIR = Path(os.environ["RUNNER_TEMP"]) / "tldk-llvm"
INSTALL = DIR / "installed"
RUNSC = INSTALL / "runsc"
SENTRY = INSTALL / "gvisor-bin/gvisor_sentry_plugin_stack"
IMAGE = "python@sha256:69ebe56e2a764c399514a0a6117131cb730cc4d0e71e85c08deb9af74cbb9f41"
TEST = "//test/rbe/plugin_programs:external_traffic"
R = {"status": "INCOMPLETE", "source": SOURCE, "carrier": os.environ["CARRIER"],
     "prior_static_gnu": 13474526238, "prior_musl_vdso_failure": 13504744022,
     "prior_header_and_backtrace": 13613757783, "preparation": {}, "receipts": {}}


def save():
    (DIR / (ARM + "-receipt.json")).write_text(json.dumps(R, indent=2) + "\n")


def phase(name):
    R["phase"] = name
    save()


def checked(*args, timeout=120):
    rc, out, err = evidence.command(list(map(str, args)), timeout)
    if rc:
        R["command_failure"] = {"program": Path(args[0]).name, "exit": rc,
                                "diagnostics": evidence.diagnostics(err + b"\n" + out)}
        raise RuntimeError("command failed")
    return out


def build_inputs(phase_name, mode, targets):
    attempts = R["preparation"][phase_name] = []
    for i in range(32):
        bep = DIR / (phase_name + ".bep")
        bep.unlink(missing_ok=True)
        argv = ["bazel", "build", *FLAGS, *mode, "--keep_going", "--build_event_publish_all_actions",
                "--nobuild_event_json_file_path_conversion", "--build_event_json_file=" + str(bep), *targets]
        rc, out, err = evidence.command(argv, 3000)
        text = (err + out).decode(errors="replace")
        row = {"exit": rc, "diagnostics": evidence.diagnostics(err), "recovered": []}
        if rc:
            messages = [line.strip()[:800] for line in text.splitlines()
                        if ("error:" in line.lower() or "undefined reference" in line or "duplicate symbol" in line)
                        and "://" not in line]
            row["first_errors"] = messages[:15]
        attempts.append(row)
        if bep.is_file():
            failures = []
            links = R.setdefault("link_actions", {})
            for line in bep.open():
                event = json.loads(line)
                ident = event["id"].get("actionCompleted")
                if not ident or "action" not in event:
                    continue
                action = event["action"]
                owner = ident.get("label")
                if not action.get("success"):
                    if len(failures) < 8:
                        failures.append([owner, action.get("type"), action.get("exitCode")])
                elif phase_name == "plugin" and ((owner == "//vdso:vdso.so" and action.get("type") == "CppLink")
                    or (owner in (consumer.RUNSC, consumer.SENTRY) and action.get("type") == "GoLink")):
                    output = ident["primaryOutput"]
                    links[output] = [owner, action["type"], ident["configuration"]["id"]]
            row["failed_owners"] = failures
        save()
        if rc == 0:
            return bep
        errors = [line for line in text.splitlines() if "Error downloading" in line or "Download from " in line and "failed:" in line]
        urls = sorted({m.group() for line in errors for m in assets.URL.finditer(line)})
        row["failed_release_urls"] = urls[:40]
        rows = R["receipts"]
        for url in urls:
            if url in rows or len(rows) >= 48:
                continue
            mods = {m for line in errors if url in line for m in assets.DIR.findall(line)}
            record = None
            if len(mods) == 1:
                try:
                    record = assets.acquire(mods.pop(), assets.URL.fullmatch(url)[3].removeprefix("v"), wanted_url=url)
                except (OSError, AssertionError, ValueError, KeyError):
                    pass
            try:
                if not record:
                    remaining = (1 << 30) - sum(entry.get("bytes", 0) for entry in rows.values())
                    directory, record = assets.release_distfile(url, assets.DIST, remaining_bytes=remaining)
                    flag = "--distdir=" + str(directory)
                    if flag not in FLAGS:
                        FLAGS.append(flag)
                    elif not record["acquired"]:
                        continue
            except (ValueError, OSError, KeyError):
                continue
            rows[url] = record
            row["recovered"].append(url)
        save()
        if not row["recovered"]:
            raise RuntimeError((phase_name, rc, row["failed_owners"] if bep.exists() else [], row["failed_release_urls"]))
    raise RuntimeError("dependency preparation limit")


def link_evidence(bep):
    # Compare the actual canonical build defaults and their GoLink configuration
    # with the exact files installed by Make. A globally disabled remote cache
    # permits reuse of this job's earlier partial builds.
    events = consumer.events(bep)
    sentry = SENTRY.read_bytes()
    for owner, installed in ((consumer.RUNSC, RUNSC), (consumer.SENTRY, SENTRY)):
        (path,) = consumer.outputs(events, owner).values()
        cfg, = [e["id"]["targetCompleted"]["configuration"]["id"] for e in events
                if e["id"].get("targetCompleted", {}).get("label") == owner and not e["id"]["targetCompleted"].get("aspect")]
        rows = [out for out, row in R.get("link_actions", {}).items() if row == [owner, "GoLink", cfg]]
        assert any(Path(out).samefile(path) for out in rows)
        assert installed.read_bytes() == path.read_bytes()
        R.setdefault("installed_owner", {})[owner] = {"path": str(installed.relative_to(INSTALL)), "cfg": cfg,
                                                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    machines = {}
    for name, (owner, kind, cfg) in R["link_actions"].items():
        if owner != "//vdso:vdso.so":
            continue
        raw = Path(name).read_bytes()
        assert raw[:7] == b"\x7fELF\x02\x01\x01" and struct.unpack_from("<H",raw,16)[0] == 3
        machine = struct.unpack_from("<H",raw,18)[0]
        machines[str(machine)] = {"sha256": hashlib.sha256(raw).hexdigest(), "cfg": cfg, "embedded": raw in sentry}
    R["vdsos"] = machines
    assert set(machines) == {"62", "183"} and all(v["embedded"] for v in machines.values()), machines
    query = 'filter(":bsd_queue$", deps(@libpluginstack//:libpluginstack, 1))'
    rows = checked("bazel", "cquery", *FLAGS, "--config=plugin-tldk", "--output=label", query).decode().splitlines()
    assert len(rows) == 1 and rows[0].split()[0].endswith("libpluginstack//:bsd_queue"), rows
    copy, = checked("bazel", "cquery", *FLAGS, "--config=plugin-tldk", "--output=files",
                    "@libpluginstack//:amd64_queue_header").decode().splitlines()
    root = Path(checked("bazel", "info", "execution_root").decode().strip())
    raw = (root / copy).read_bytes()
    assert len(raw) == 19539 and hashlib.sha256(raw).hexdigest() == inputs.HEADER
    R["header"] = {"sha256": inputs.HEADER, "bytes": len(raw), "path": copy}
    save()


def xml_result(event, filename):
    record, = [row for row in event["testActionOutput"] if row["name"] == "test.xml"]
    uri = urlsplit(record["uri"])
    assert uri.scheme == "file" and not uri.netloc
    raw = Path(unquote(uri.path)).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if "length" in record:
        assert int(record["length"]) == len(raw)
    if record.get("digest"):
        assert digest == record["digest"]
    (DIR / filename).write_bytes(raw)
    return ET.fromstring(raw), {"bytes": len(raw), "sha256": digest}


def stack_oracle():
    target = "//test/rbe/tldk_layout_probe:backtrace_test"
    phase("compile DPDK stack test")
    build_inputs("stack-oracle", MODE, [target])
    phase("exercise DPDK stack test")
    bep = DIR / "stack-test.bep"
    rc, out, err = evidence.command(["bazel", "test", *FLAGS, *MODE,
        "--cache_test_results=no", "--test_arg=-test.v", "--test_output=errors",
        "--nobuild_event_json_file_path_conversion", "--build_event_json_file=" + str(bep), target], 900)
    oracle = R["oracle"] = {"target": target, "exit": rc, "diagnostics": evidence.diagnostics(err + out)}
    if bep.is_file():
        found = [e["testResult"] for e in consumer.events(bep) if "testResult" in e]
        if found:
            event, = found
            oracle["status"] = event["status"]
            xml, oracle["xml"] = xml_result(event, ARM + "-stack.xml")
            oracle["cases"] = [{"name": c.get("name"), "children": [n.tag for n in c]} for c in xml.iter("testcase")]
    save()
    assert rc == 0 and oracle["status"] == "PASSED", oracle
    cases = [c for c in oracle["cases"] if c["name"] == "TestDumpStack"]
    assert len(cases) == 1 and not set(cases[0]["children"]) & {"failure", "error", "skipped"}, oracle
    case, = [c for c in oracle["cases"] if c["name"] == "TestOverlayRunfiles"]
    assert not {"failure", "error", "skipped"} & set(case["children"]), case
    archive = "@libpluginstack//:libpluginstack"
    outputs = checked("bazel", "cquery", *FLAGS, *MODE, "--output=files", archive).decode().splitlines()
    linked = [Path(path) for path in outputs if path.endswith(".a") and Path(path).is_file()]
    assert linked, outputs
    symbol = "backtrace_symbols" if ARM == "current-gnu" else "_Unwind_Backtrace"
    oracle["archive_symbols"] = {
        str(path): [line.strip() for line in checked("nm", "-g", "--undefined-only", path).decode().splitlines()
                    if line.split()[-1:] == [symbol]]
        for path in linked
    }
    assert any(oracle["archive_symbols"].values()), symbol
    save()


def standalone():
    assert checked("git", "rev-parse", "HEAD").decode().strip() == SOURCE
    assert checked("bazel", "--version").strip() == b"bazel 9.3.1"
    phase("public dependency preparation")
    inputs.seed()
    R["seeds"] = inputs.REPORT["seeds"]
    R["flags"] = MODE
    save()
    try:
        stack_oracle()
    except Exception as exc:
        R["stack_error"] = {"type": type(exc).__name__, "detail": str(exc)[:800]}
        save()
        if ARM != "current-gnu":
            raise
    if ARM == "current-gnu":
        labels = (consumer.RUNSC, consumer.SENTRY)
        bep = build_inputs("existing-gnu-plugin-links", MODE, labels)
        events = consumer.events(bep)
        R["default_plugin_binaries"] = {}
        for label in labels:
            path, = consumer.outputs(events, label).values()
            desc = checked("readelf", "-lWd", path)
            assert b"INTERP" not in desc and b"NEEDED" not in desc, label
            R["default_plugin_binaries"][label] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size, "static": True,
            }
        original = {consumer.RUNSC: "47c0e7c41ec4390534a432af76e76587b9396d7637ad885a20bb74921a2ebb0b",
                    consumer.SENTRY: "267b0b152fe21ab37a90a745278622e1c7a10d3c34f6ec27b75c9489fd867807"}
        R["matches_prior_link_bytes"] = {k: v["sha256"] == original[k] for k, v in R["default_plugin_binaries"].items()}
        expr = 'filter("libpluginstack", deps(//pkg/sentry/socket/plugin/cgo:cgo))'
        default_deps = checked("bazel", "cquery", *FLAGS, "--output=label", expr, timeout=500)
        R["ordinary_cgo_plugin_deps"] = default_deps.decode().splitlines()
        assert not R["ordinary_cgo_plugin_deps"]
    assert "stack_error" not in R
    R["status"] = "PASS"
    save()


def layout():
    assert checked("git", "rev-parse", "HEAD").decode().strip() == SOURCE
    common = ["--distdir=" + str(assets.DIST), "--lockfile_mode=off", "--module_mirrors=https://bcr.cloudflaremirrors.com"]
    phase("query all main packages")
    old = "641c450420c005c84d09b974388996e39ba177e4"
    prefix = "external/tools/plugin-stack"
    for name, commit in (("before", old), ("after", SOURCE)):
        checked("git", "switch", "-q", "--detach", commit)
        output = checked("bazel", "query", *common, "--output=package", "//...:*", timeout=1200)
        labels = sorted(set(output.decode().splitlines()))
        matches = [p for p in labels if prefix in p]
        R.setdefault("workspace", {})[name] = {"packages": len(labels), "plugin_packages": matches, "first": labels[:3]}
        save()
    assert R["workspace"]["before"]["plugin_packages"] == R["workspace"]["after"]["plugin_packages"]
    assert all("/upstream/" not in p for p in R["workspace"]["after"]["plugin_packages"])
    phase("check both exported labels and the actual http_archive BUILD bytes")
    for subdir, target in (("tldk", "@libpluginstack//:libpluginstack"), ("dpdk", "@plugin_dpdk//:sources")):
        relative = "upstream/" + subdir + "/BUILD"
        owner = "//" + prefix + ":" + relative
        name = checked("bazel", "query", *common, owner).decode().strip()
        where = checked("bazel", "query", *common, "--output=location", target).decode().strip()
        expected = Path(prefix, relative).read_bytes()
        repo_name = target.split("//")[0][1:]
        bdir = Path(checked("bazel", "info", "output_base").decode().strip()) / "external"
        found = Path(where.split(":", 1)[0])
        paths = {found} if found.is_file() else set()
        for dirname in (repo_name, "*+" + repo_name, "*~" + repo_name):
            for path in bdir.glob(dirname + "/BUILD*"):
                if path.name in ("BUILD.bazel", "BUILD") and path.is_file():
                    paths.add(path)
        candidates = {str(path.relative_to(path.parent.parent)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}
        digest = hashlib.sha256(expected).hexdigest()
        R.setdefault("consumers", {})[subdir] = {"source": name, "target": target, "source_sha256": digest,
                                               "location": where.split(":", 1)[0].split("/external/")[-1], "actual_build_files": candidates}
        save()
        assert name.endswith(owner) and digest in candidates.values(), (name, owner, candidates)
    phase("build the ordinary ARM64 vDSO")
    bep = build_inputs("arm64-vdso", ["--config=aarch64"], ["//vdso:vdso.so"])
    vdso, = consumer.outputs(consumer.events(bep), "//vdso:vdso.so").values()
    raw = vdso.read_bytes()
    assert raw[:5] == b"\x7fELF\x02" and int.from_bytes(raw[18:20], "little") == 183
    R["arm64_vdso"] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "machine": 183}
    for cpu in ("x86_64", "aarch64"):
        flag = ["--config=" + cpu]
        rows = checked("bazel", "cquery", *FLAGS, *flag, 'filter("libpluginstack",deps(//pkg/sentry/socket/plugin/cgo:cgo))', timeout=450)
        assert not rows.strip(), rows
        incompatible = checked("bazel", "cquery", *FLAGS, *flag, "--config=plugin-tldk", "--output=starlark",
                               '--starlark:expr="IncompatiblePlatformProvider" in providers(target)', "@libpluginstack//:libpluginstack",
                               timeout=450).decode().strip()
        assert incompatible == str(cpu == "aarch64"), (cpu, incompatible)
        R.setdefault("platforms", {})[cpu] = {"ordinary_plugin_deps": 0, "plugin_incompatible": incompatible == "True"}
    R["status"] = "PASS"
    save()


try:
    if ARM == "layout":
        layout()
    else:
        standalone()
except Exception as exc:
    R["error"] = {"type": type(exc).__name__, "lines": [f.lineno for f in traceback.extract_tb(exc.__traceback__)], "detail": str(exc)[:600]}
finally:
    R["verdict_at"] = consumer.now()
    save()
raise SystemExit(0 if R["status"] == "PASS" else 1)
