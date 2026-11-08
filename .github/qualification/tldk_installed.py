"""Check the Make-installed LLVM/musl plugin CLI and Sentry across the native Docker bridge."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import struct
from urllib.parse import unquote, urlsplit
import time
import traceback

import consumer
import native_cross_inputs as evidence

import public_assets as assets
import tldk_public_inputs as distfiles

SOURCE = "b2c944f33f8e85be86e3ae9c95f3a141ea57cbc2"
QUEUE_HEADER = "8b284031772b1ba2035d9b05b24f2cb9b23e7bd324bcccb5e3fcc57d34aafa48"
LAYOUT = sorted({"runsc", "containerd-shim-runsc-v1"} | {
    "gvisor-bin/" + name for name in ("checkpointgofer", "gvisor_sentry", "gvisor_sentry_plugin_stack",
        "runsc-metric-server", "runsc-netgofer", "runsc-fd-parking", "gvisor-sentry-prewarmer",
        "gvisor-proc-umounter")})
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--remote_cache=",
         "--remote_executor=", "--bes_backend=", "--disk_cache=", "--lockfile_mode=off",
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


module_versions = {}


def save():
    (DIR / "receipt.json").write_text(json.dumps(R, indent=2) + "\n")


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
        urls = sorted({m.group() for line in errors for m in distfiles.FAILED_URL.finditer(line)
                       if assets.URL.fullmatch(m.group()) or distfiles.ARCHIVE.fullmatch(m.group())})
        row["failed_public_urls"] = urls[:48]
        rows = R["receipts"]
        for url in urls:
            if url in rows or len(rows) >= 80:
                continue
            remaining = distfiles.LIMIT - sum(entry["bytes"] for entry in rows.values())
            owners = {m for line in errors if url in line for m in assets.DIR.findall(line)}
            record = None
            # Resolve only a registry record whose URL matches this actual
            # download. Handles released archives and full-commit archive URLs.
            if len(owners) == 1:
                module, = owners
                ref = distfiles.components(url)[3].removeprefix("refs/tags/").removeprefix("v")
                for version in dict.fromkeys([module_versions.get(module), ref]):
                    if not version:
                        continue
                    try:
                        directory, record = distfiles.materialize(module, version, url, None, None, None, remaining)
                    except (OSError, AssertionError, ValueError, KeyError):
                        continue
                    break
            try:
                if not record:
                    directory, record = assets.release_distfile(url, assets.DIST, remaining_bytes=remaining)
                flag = "--distdir=" + str(directory)
                if flag not in FLAGS:
                    FLAGS.append(flag)
                else:
                    continue
            except (ValueError, OSError, KeyError):
                continue
            rows[url] = record
            row["recovered"].append(url)
        save()
        if not row["recovered"]:
            raise RuntimeError((phase_name, rc, row["failed_owners"] if bep.exists() else [], row["failed_public_urls"]))
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
    assert set(machines) == {"62", "183"} and machines["62"]["embedded"], machines
    query = 'filter(":bsd_queue$", deps(@libpluginstack//:libpluginstack, 1))'
    rows = checked("bazel", "cquery", *FLAGS, "--config=plugin-tldk", "--output=label", query).decode().splitlines()
    assert len(rows) == 1 and rows[0].split()[0].endswith("libpluginstack//:bsd_queue"), rows
    copy, = checked("bazel", "cquery", *FLAGS, "--config=plugin-tldk", "--output=files",
                    "@libpluginstack//:queue_header").decode().splitlines()
    raw = Path(copy).read_bytes()
    assert len(raw) == 19539 and hashlib.sha256(raw).hexdigest() == QUEUE_HEADER
    R["header"] = {"sha256": QUEUE_HEADER, "bytes": len(raw), "path": copy}
    save()


def prepare():
    assert checked("git", "rev-parse", "HEAD").decode().strip() == SOURCE
    assert checked("bazel", "--version").strip() == b"bazel 9.3.1"
    phase("public dependency preparation")
    declared = re.findall(r'bazel_dep\(\s*name\s*=\s*"([^"]+)",\s*version\s*=\s*"([^"]+)"', Path("MODULE.bazel").read_text())
    module_versions.update(declared)
    seeds = json.loads((DIR / "tldk_installed_inputs.json").read_text())
    module_versions.update((r[0], r[1]) for r in seeds if r[0])
    R["unavailable_seeds"] = []
    for directory, entry in distfiles.seed(seeds):
        if directory is None:
            R["unavailable_seeds"].append(entry)
        else:
            flag = "--distdir=" + str(directory)
            if flag not in FLAGS:
                FLAGS.append(flag)
            R["receipts"][entry["source"]] = entry
        save()
    # Prep only the real selections. The Make targets below must independently
    # succeed, and the two installed plugin ELFs must equal these build defaults.
    plugin_bep = build_inputs("plugin", ["--config=plugin-tldk"], [consumer.RUNSC, consumer.SENTRY])
    build_inputs("release", [], ["//:release"])
    phase("canonical Make build and install")
    make = ["make", "DOCKER_BUILD=false", "RUNTIME_DIR=" + str(INSTALL), "BAZEL_OPTIONS=" + " ".join(FLAGS)]
    checked(*make, "runsc-plugin-stack", timeout=2400)
    R["make_runsc_plugin_stack_exit"] = 0
    checked(*make, "RUNSC_TARGET=--config=plugin-tldk " + consumer.RUNSC,
            "EXTRA_SIDECAR_TARGETS=--config=plugin-tldk " + consumer.SENTRY, "refresh", timeout=2400)
    R["make_refresh_exit"] = 0
    installed = sorted(str(p.relative_to(INSTALL)) for p in INSTALL.rglob("*") if p.is_file())
    assert installed == LAYOUT, installed
    R["binaries"] = {rel: consumer.elf(INSTALL / rel) for rel in installed}
    save()
    try:
        link_evidence(plugin_bep)
        R["link_evidence"] = "PASS"
    except (AssertionError, OSError, ValueError, KeyError) as err:
        R["link_evidence"] = {"error": type(err).__name__, "line": traceback.extract_tb(err.__traceback__)[-1].lineno,
                              "detail": str(err)[:400]}
    save()


def runtime():
    R["devices"] = consumer.devices()
    save()
    if not all(row["opened"] for row in R["devices"].values()):
        R["traffic_status"] = "BLOCKED: native device unavailable"
        save()
        return
    phase("public runsc install with sidecar downloads disabled")
    logs = DIR / "logs"
    logs.mkdir()
    checked("sudo", "-n", RUNSC, "install", "--runtime=" + consumer.RUNTIME,
            "--download-sidecars=NEVER", "--", "--network=plugin",
            "--platform=systrap", "--debug", "--debug-log=" + str(logs) + "/")
    R["runsc_install_exit"] = 0
    checked("sudo", "-n", "systemctl", "reload", "docker")
    entry = json.loads(checked("sudo", "-n", "cat", "/etc/docker/daemon.json"))["runtimes"][consumer.RUNTIME]
    assert entry["path"] == str(RUNSC)
    assert {"--network=plugin", "--platform=systrap"} <= set(entry["runtimeArgs"])
    for _ in range(20):
        if consumer.RUNTIME in json.loads(checked("docker", "info", "--format", "{{json .Runtimes}}")):
            break
        time.sleep(1)
    else:
        raise RuntimeError("Docker did not register the plugin runtime")
    R["docker_runtime"] = {"name": consumer.RUNTIME, "path": entry["path"],
                           "args": entry["runtimeArgs"]}
    fixture = "test/rbe/plugin_programs/"
    checked("git", "restore", "--source=" + R["carrier"], "--",
            fixture + "BUILD", fixture + "external_traffic.py")
    checked("docker", "pull", IMAGE, timeout=240)
    image_id = checked("docker", "image", "inspect", "-f", "{{.Id}}", IMAGE).decode().strip()
    phase("real plugin and runc guest exchanges")
    try:
        consumer.test_case(R, "external", ["bazel"], FLAGS, TEST, "TestExternalTraffic",
                           ["--test_timeout=180", "--test_env=REPORT_DIR=" + str(DIR)],
                           owners=("external",))
    finally:
        markers = {"plugin_sentry": 'Sidecar "gvisor_sentry_plugin_stack" found: booting sandbox with ' + str(SENTRY),
                   "plugin_init": "Initializing plugin network stack, config:"}
        R["observed"] = dict.fromkeys(markers, False)
        for path in logs.iterdir():
            rc, content, _ = evidence.command(["sudo", "-n", "tail", "-c", "4000000", str(path)], 30)
            if not rc:
                for key, needle in markers.items():
                    R["observed"][key] |= needle.encode() in content
        wire = DIR / "wire.json"
        if wire.is_file():
            R["wire"] = {}
            for name, row in json.loads(wire.read_text()).items():
                details = {k: row[k] for k in ("runtime", "ready", "ok", "guest_exit", "interface") if k in row}
                details["image_matches"] = row.get("image") == image_id
                details["host"] = {p: {k: v for k, v in data.items() if k in ("sent", "received", "ok", "error")}
                                   for p, data in row.get("host", {}).items()}
                details["bridge"] = {k: {"bytes": v["bytes"], "sha256": v["sha256"]}
                                     for k, v in row.get("wire", {}).items()}
                if err := row.get("error"):
                    details["error"] = {k: err[k] for k in ("type", "errno", "line", "exit") if k in err}
                    if diagnostic := err.get("diagnostic"):
                        details["docker_diagnostic"] = evidence.diagnostics(diagnostic.encode())
                R["wire"][name] = details
        save()
    assert set(R.get("wire", {})) == {"runc", consumer.RUNTIME}
    for name, row in R["wire"].items():
        assert row.get("runtime") == name and row.get("ok") and row["image_matches"], name
        assert row.get("ready") and row.get("guest_exit") == "0" and row.get("interface") == "docker0", name
        assert set(row["bridge"]) == {"tcp_request", "tcp_reply", "udp_request", "udp_reply"}, name
    assert R["external"]["status"] == "PASS" and all(R["observed"].values())
    R["traffic_status"] = "PASS"
    save()


def help_probe():
    rc, out, err = evidence.command([str(SENTRY), "help", "syscalls", "--format=json",
                                     "--os=linux", "--arch=amd64"], 60)
    probe = R["help_syscalls"] = {"exit": rc, "diagnostics": evidence.diagnostics(err)}
    if not rc:
        try:
            probe["read_entry"] = json.loads(out)["linux"]["amd64"]["syscalls"]["0"]["name"]
        except (ValueError, KeyError, TypeError) as exc:
            probe["collection_error"] = type(exc).__name__
    probe["status"] = "PASS" if probe.get("read_entry") == "read" else "FAIL"


try:
    prepare()
    try:
        runtime()
    finally:
        # An optional CLI probe cannot stop guest startup or wire observation.
        help_probe()
    if R.get("external", {}).get("status") == "PASS":
        bep = consumer.TMP / "external.bep"
        result, = [e["testResult"] for e in consumer.events(bep) if e["id"].get("testResult", {}).get("label") == TEST]
        record, = [f for f in result["testActionOutput"] if f["name"] == "test.xml"]
        uri = urlsplit(record["uri"])
        assert uri.scheme == "file" and not uri.netloc
        raw = Path(unquote(uri.path)).read_bytes()
        assert len(raw) == int(record["length"]) and hashlib.sha256(raw).hexdigest() == record["digest"]
        (DIR / "test.xml").write_bytes(raw)
        R["original_xml"] = {"bytes": len(raw), "sha256": record["digest"]}
    if R.get("traffic_status") == "PASS" and R.get("link_evidence") == "PASS" and "original_xml" in R:
        R["status"] = "PASS"
except Exception as exc:
    R["error"] = {"type": type(exc).__name__, "lines": [f.lineno for f in traceback.extract_tb(exc.__traceback__)], "detail": str(exc)[:600]}
finally:
    R["verdict_at"] = consumer.now()
    save()
raise SystemExit(0 if R["status"] == "PASS" else 1)
