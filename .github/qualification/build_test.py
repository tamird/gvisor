"""Record the real source build-test and its dependency barriers on Linux."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse

WORK = Path(os.environ["RUNNER_TEMP"]) / "source-build-test"
PUBLIC = WORK / "result.json"
PKG = "//test/rbe/source_build_test:"
ROOT = "//:build_test"
LAUNCH = "//:qualification_build_test_launcher"
MARKERS = ["//:build_test_15__deps", PKG + "stock_small_0__deps"]
SOURCE = "61ff148f0df0efa29c9f500914477525c5debe62"
ARCH = {
    "amd64": ("x86_64", "d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"),
    "arm64": ("aarch64", "b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"),
}
LABELS = re.findall(r'"(//test/[^"]+)"', Path("BUILD").read_text().split('name = "build_test",')[1].split("],")[0])
assert len(LABELS) == len(set(LABELS)) == 16 and LABELS[-1] == "//test/benchmarks/network:ruby_test"
report = json.loads(PUBLIC.read_text()) if PUBLIC.exists() else {"source": SOURCE, "dependencies": LABELS, "cells": {}}


def save():
    text = json.dumps(report, indent=2, sort_keys=True)
    assert len(text) < 1500000 and not re.search(r"(?i)x-buildbuddy-api-key|authorization\s*[:=]", text)
    PUBLIC.write_text(text + "\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def clean(s):
    return s if re.fullmatch(r"[-\w/.+@:=]{1,450}", s or "") else None


def startup(native, remote):
    args = [str(WORK / ("native" if native else "stock")), "--nosystem_rc", "--nohome_rc"]
    if remote:
        args.append("--bazelrc=" + str(WORK / "remote.bazelrc"))
    return args


def opts(arch, native, remote):
    p = "//test/rbe/release_prewarmer:" + arch
    flags = ["--jobs=32", "--lockfile_mode=off", "--config=" + ARCH[arch][0], "-c", "opt",
             "--color=no", "--curses=no", "--noshow_progress", "--workspace_status_command=",
             "--keep_going", "--nocache_test_results", "--nobuild_tests_only"]
    if remote:
        flags += ["--host_platform=" + p, "--extra_execution_platforms=" + p,
                  "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
                  "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                  "--spawn_strategy=remote", "--noremote_local_fallback", "--remote_timeout=900",
                  "--remote_download_outputs=minimal", "--noremote_upload_local_results",
                  "--modify_execution_info=^(TestRunner|InspectBuildTestLauncher|BuildTestBarrier|BuildTestFixture|Genrule)$=+no-remote-cache",
                  "--remote_download_regex=.*(/qualification_build_test_launcher[.]json|/test[.](log|xml)|/compiled_posix_test_wrapper)$"]
    else:
        flags += ["--spawn_strategy=linux-sandbox", "--remote_cache=", "--remote_executor=", "--bes_backend="]
    if native:
        flags += ["--noincompatible_exclude_starlark_flags_from_exec_config", "--incompatible_use_native_posix_test_wrapper"]
    return flags


def missing_tool(data):
    lower = data.decode("utf-8", "replace").lower()
    return [name for name, pattern in {
        "shell-enoent": r"(?:exec|stat|open|env:).*?(?:/bin/(?:ba)?sh|/usr/bin/env).*?(?:no such file|not found)",
        "touch-missing": r"(?:^|[ /:])touch.{0,100}(?:not found|no such file|not recognized)",
    }.items() if re.search(pattern, lower, re.MULTILINE)]


def diagnostics(data):
    # The client may print its configuration. Keep only public source labels,
    # known options, and error codes. Never reproduce an arbitrary command.
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        if not re.search(r"(?i)error:|failed:|unrecognized option|does not have|no matching|not visible|not declared|does not exist", line):
            continue
        labels = re.findall(r"(?:@[a-zA-Z0-9_+.-]+)?//[a-zA-Z0-9_./+-]*:[a-zA-Z0-9_./+-]+", line)
        options = re.findall(r"--(?:no)?(?:incompatible|experimental|features)[a-z_]+", line)
        text = [clean(x) for x in labels[:5] + options[:3]]
        detail = re.search(r"(?i)(not visible|No such file or directory|permission denied|no matching toolchains|not declared|undefined|syntax error|does not have|not a valid|not recognized|unrecognized option|no such target|does not exist|error loading package|exit [0-9]+)", line)
        entry = {"source": text, "class": detail[0].lower() if detail else "error", "line_sha256": sha(line.encode())}
        if (text or detail) and entry not in out:
            out.append(entry)
        if len(out) == 12:
            break
    return out


def owner(label):
    return clean(label.removeprefix("@@") if label and label.startswith("@@//") else label)


def link_lines(raw):
    headers = []
    current = None
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("ERROR:"):
            current = None
            if "Linking " in line and " failed:" in line:
                for owner in ("//vdso:vdso.so", "//tools/ioctl_sniffer:ioctl_hook"):
                    if owner + ")" in line and len(headers) < 6:
                        out = re.search(r"Linking ([^ ]+) failed:", line)
                        current = {"owner": owner, "output": clean(out[1]) if out else None, "messages": []}
                        headers.append(current)
        elif current is not None and len(current["messages"]) < 6:
            # Only linker diagnostics; command lines and action environments are excluded.
            if re.match(r"^(?:ld[.]lld|clang(?:[+][+])?|ld): (?:fatal )?error:", line):
                if not re.search(r"(?i)https?://|authorization|credential|api.?key|password|token|--.*header", line):
                    current["messages"].append(line[:400])
    return headers


def local_detail(message):
    # This path is used only by the credential-free env-cleared host workflow.
    out = []
    for line in message.splitlines():
        if not re.search(r"(?i)error:|invalid|not registered|available strateg|not supported|not available|requested|not found|no such|permission denied|exception|failed", line):
            continue
        if re.search(r"(?i)authorization|credential|api.?key|password|token|--.*header|command line:|subcommand:", line):
            continue
        short = line.replace(str(WORK), "<qualification>").replace(str(Path.home()), "~").strip()
        if short and short not in out:
            out.append(short[:900])
        if len(out) == 7:
            break
    return out


def file_data(item):
    uri = urllib.parse.urlsplit(item.get("uri", ""))
    if uri.scheme != "file":
        return None
    path = Path(urllib.parse.unquote(uri.path))
    if not path.is_file() or path.stat().st_size > 2000000:
        return None
    data = path.read_bytes()
    return data if sha(data) == item.get("digest") and len(data) == int(item.get("length", 0)) else None


def observe(bep, row, host=False):
    if not bep.exists():
        return
    actions, tests, targets = [], [], {}
    files = {}
    roots = []
    for line in bep.open():
        event = json.loads(line)
        ident = event["id"]
        if host and "finished" in event:
            f = event["finished"]
            detail = f.get("failureDetail", {})
            row["finished"] = {"exit": f.get("exitCode"), "diagnosis": local_detail("ERROR: " + detail.get("message", "")) if detail else [],
                               "failure_codes": {key: value.get("code") for key, value in detail.items() if isinstance(value, dict)}}
        if host and "testSummary" in event:
            row.setdefault("summaries", {})[owner(ident["testSummary"]["label"])] = event["testSummary"].get("overallStatus")
        if "started" in event:
            row["invocation"] = clean(event["started"].get("uuid"))
        if "namedSetOfFiles" in event:
            files[ident["namedSet"]["id"]] = event["namedSetOfFiles"]
        if "targetCompleted" in ident and not ident["targetCompleted"].get("aspect"):
            tag = ident["targetCompleted"]
            label = owner(tag.get("label"))
            targets[label] = bool(event.get("completed", {}).get("success"))
            if label == LAUNCH:
                for group in event.get("completed", {}).get("outputGroup", []):
                    if group["name"] == "default":
                        roots.extend(r["id"] for r in group.get("fileSets", []))
        if "actionCompleted" in ident:
            tag, action = ident["actionCompleted"], event.get("action", {})
            label, kind = owner(tag.get("label")), clean(action.get("type"))
            if (label in LABELS and kind in ("GoLink", "GoTestLink")) or (kind == "CppLink" and label in ("//vdso:vdso.so", "//tools/ioctl_sniffer:ioctl_hook")) or label in MARKERS or label == LAUNCH or (label or "").startswith(PKG) or not action.get("success"):
                if len(actions) < 140 or label in ("//vdso:vdso.so", "//tools/ioctl_sniffer:ioctl_hook") or label in MARKERS or label == LAUNCH or (label or "").startswith(PKG):
                    actions.append({"owner": label, "kind": kind, "success": bool(action.get("success")),
                                    "exit": int(action.get("exitCode", 0)), "failure": clean(action.get("failureDetail", {}).get("spawn", {}).get("code"))})
            if kind == "CppLink" and label in ("//vdso:vdso.so", "//tools/ioctl_sniffer:ioctl_hook") and actions and actions[-1]["owner"] == label:
                actions[-1].update(config=clean(tag.get("configuration", {}).get("id")), output=clean(tag.get("primaryOutput")),
                                   stderr_sha=clean(action.get("stderr", {}).get("digest")))
            if label in MARKERS and actions and actions[-1]["owner"] == label:
                item = action.get("stderr", {})
                data = file_data(item)
                actions[-1]["stderr_digest"] = clean(item.get("digest"))
                actions[-1]["missing_tool"] = sorted(set(missing_tool(data or b"") + missing_tool(
                    action.get("failureDetail", {}).get("message", "").encode())))
        if "testResult" in ident and "testResult" in event:
            tag, test = ident["testResult"], event["testResult"]
            info = test.get("executionInfo", {})
            entry = {"owner": owner(tag["label"]), "status": clean(test.get("status")), "strategy": clean(info.get("strategy")),
                     "local_cache": bool(test.get("cachedLocally")), "remote_cache": bool(info.get("cachedRemotely"))}
            for item in test.get("testActionOutput", []):
                if item["name"] in ("test.log", "test.xml"):
                    data = file_data(item)
                    direct = False
                    if data is None and not item.get("digest") and (entry["owner"] == ROOT or entry["owner"].startswith(PKG)):
                        uri = urllib.parse.urlsplit(item.get("uri", ""))
                        path = Path(urllib.parse.unquote(uri.path))
                        package, name = entry["owner"][2:].split(":")
                        suffix = "/testlogs/" + (package + "/" if package else "") + name + "/" + item["name"]
                        if uri.scheme == "file" and path.as_posix().endswith(suffix) and path.is_file() and 0 < path.stat().st_size < 2000000:
                            observed = path.read_bytes()
                            if "length" not in item or len(observed) == int(item["length"]):
                                data, direct = observed, True
                    entry[item["name"]] = {"verified": data is not None, "digest": clean(item.get("digest")),
                                           "observed_sha256": sha(data) if data is not None else None, "direct_local_output": direct}
                    if data is not None and item["name"] == "test.log":
                        entry["machines"] = [m.decode() for m in re.findall(rb"(?m)^build_test native machine=(x86_64|aarch64)\r?$", data)]
            tests.append(entry)
    visited = set()
    while roots:
        id_ = roots.pop()
        if id_ in visited:
            continue
        visited.add(id_)
        node = files[id_]
        roots.extend(r["id"] for r in node.get("fileSets", []))
        for item in node.get("files", []):
            if Path(item["name"]).name == "qualification_build_test_launcher.json":
                data = file_data(item)
                if data is not None:
                    observation = json.loads(data)
                    keys = ("arch", "sha256", "bytes", "enoent", "exit", "skylib", "/usr/bin/env_absent",
                            "/bin/sh_absent", "/bin/bash_absent", "/usr/bin/touch_absent", "/bin/touch_absent")
                    row["launcher"] = {k: observation[k] for k in keys}
                    row["launcher"]["output_sha256"] = sha(data)
                    row["launcher"]["machine"] = observation.get("machine")
    row.update(actions=actions, tests=tests, targets=targets)


def spawns(path, row, arch):
    if not path.exists():
        return
    expr = '''select(has("spawn")) | .spawn | select(.mnemonic == "TestRunner" or .mnemonic == "BuildTestFixture" or .mnemonic == "BuildTestBarrier" or .mnemonic == "InspectBuildTestLauncher" or (.mnemonic == "Genrule" and (.targetLabel | contains("__deps"))) or .mnemonic == "GoLink" or (.mnemonic == "CppLink" and (.targetLabel | test("//(vdso:vdso[.]so|tools/ioctl_sniffer:ioctl_hook|tools/test:compiled_posix_test_wrapper)$")))) | {kind:.mnemonic,owner:.targetLabel,exe:.args[0],test_program:(if .mnemonic == "TestRunner" then .args[1] else null end),touch:(if .mnemonic == "Genrule" then any(.args[]; test("(?<![A-Za-z0-9_])touch +")) else false end),runner:.runner,cache:.cacheHit,exit:.exitCode,status:.status,digest:.digest.hash,properties:(.platform.properties // [])}'''
    # These files are already recorded in the original compact log. Restrict
    # the projection to this configured architecture's 16 named root inputs.
    configured = report["cells"].get("graph-" + arch, {}).get("labels", {})
    wanted = {path: label for label in LABELS for path in configured.get(label, []) if path}
    expr = 'if has("file") then (.file | select($wanted[.path] != null and .digest.hash) | {output_path:.path, hash:.digest.hash, bytes:.digest.sizeBytes}) else (' + expr + ') end'
    metadata = {}
    result = []
    with subprocess.Popen([WORK / "bb", "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", "--argjson", "wanted", json.dumps(wanted), expr], stdin=decoder.stdout, stdout=subprocess.PIPE, text=True) as project:
            decoder.stdout.close()
            for line in project.stdout:
                item = json.loads(line)
                if "output_path" in item:
                    metadata[item["output_path"]] = {"owner": wanted[item["output_path"]], "sha256": clean(item["hash"]), "bytes": int(item["bytes"] or 0)}
                    continue
                item["owner"] = owner(item["owner"])
                if item["kind"] == "GoLink" and item["owner"] not in LABELS:
                    continue
                props = {p["name"]: p["value"] for p in item.pop("properties")}
                item["image"] = props.get("container-image") == "docker://gcr.io/distroless/static-debian13@sha256:" + ARCH[arch][1]
                item["cpu"] = clean(props.get("Arch"))
                item["exit"] = int(item["exit"] or 0)
                item["cache"] = bool(item["cache"])
                for key in ("kind", "owner", "exe", "test_program", "runner", "status", "digest"):
                    if key != "runner" or item[key] != "remote cache hit":
                        item[key] = clean(item[key])
                result.append(item)
            row["projection_exit"] = project.wait()
        row["decode_exit"] = decoder.wait()
    row["spawns"] = result
    if wanted:
        row["configured_input_files"] = metadata
        row["input_bytes_observed"] = sum(f["bytes"] for f in metadata.values())
        row["input_paths_complete"] = set(metadata) == set(wanted)


def run(name, arch, native, remote, command, labels, host_strategy=None):
    d = WORK / name
    d.mkdir()
    bep, compact = d / "events", d / "compact"
    args = startup(native, remote) + [command] + opts(arch, native, remote)
    args += ["--build_event_publish_all_actions", "--nobuild_event_json_file_path_conversion", "--build_event_json_file=" + str(bep)]
    args += ["--execution_log_compact_file=" + str(compact)]
    if host_strategy:
        assert not remote and host_strategy in ("linux-sandbox", "processwrapper-sandbox")
        args[args.index("--spawn_strategy=linux-sandbox")] = "--spawn_strategy=" + host_strategy
    with (d / "output").open("wb") as log:
        status = subprocess.run(args + labels, stdout=log, stderr=subprocess.STDOUT).returncode
    raw = (d / "output").read_bytes()
    row = {"exit": status, "start_labels": labels, "client_output_sha256": sha(raw), "signals":
           [s for s in ("No such file or directory", "intentional source build-test dependency failure",
                        "Unrecognized option", "no matching toolchains", "No matching execution platform",
                        "No such target", "no space left", "OutOfMemoryError", "No registered executors",
                        "Permission denied", "is not allowed to depend", "error loading package") if s.lower().encode() in raw.lower()]}
    row["link_diagnostics"] = link_lines(raw)
    row["missing_tool"] = missing_tool(raw)
    row["diagnostics"] = diagnostics(raw)
    report["cells"][name] = row
    save()
    if host_strategy:
        row["client_diagnosis"] = local_detail(raw.decode("utf-8", "replace"))
    observe(bep, row, host=bool(host_strategy))
    spawns(compact, row, arch)
    save()
    print(name, "exit", status, "results", len(row.get("tests", [])), "signals", row["signals"], flush=True)
    return row


def passed(row, label, arch, remote):
    observed = [t for t in row.get("tests", []) if t["owner"] == label]
    if len(observed) != 1:
        return False
    t = observed[0]
    return (t["status"] == "PASSED" and not t["local_cache"] and not t["remote_cache"]
            and t.get("machines") == [ARCH[arch][0]] and t["test.log"]["verified"]
            and (not remote or (t["strategy"] == "remote" and fresh(row, label, "TestRunner", arch))))


def fresh(row, label, kind, arch):
    return any(s["owner"] == label and s["kind"] == kind and s["image"] and s["cpu"] == arch
               and s["runner"] == "remote" and not s["cache"] for s in row.get("spawns", []))


def control(form, suite, remote=False, arch="amd64", host_strategy=None, prefix=""):
    suffix = "_via_suite" if suite else ""
    tests = [PKG + form + "_" + state for state in ("valid", "broken")]
    args = [name + ("_suite" if suite else "") for name in tests]
    good, bad = [name + suffix for name in tests]
    label = ("remote-" + arch if remote else "host") + "-" + form + ("-suite" if suite else "-direct")
    row = run(prefix + label, arch, remote, remote, "test", args, host_strategy)
    def input_ran(owner, expected):
        return (any(a["owner"] == owner + "_input" and a["kind"] == "BuildTestFixture" and a["success"] == (expected == 0)
                    for a in row.get("actions", [])) and any(s["owner"] == owner + "_input" and s["kind"] == "BuildTestFixture"
                    and s["exit"] == expected and not s["cache"] and (not remote or (s["runner"] == "remote"
                    and s["image"] and s["cpu"] == arch)) for s in row.get("spawns", [])))
    row["contract"] = (row["exit"] != 0 and passed(row, good, arch, remote) and input_ran(good, 0)
                       and input_ran(bad, 41) and "intentional source build-test dependency failure" in row["signals"]
                       and not any(t["owner"] == bad and t["status"] == "PASSED" for t in row.get("tests", [])))
    save()
    return row["contract"]


def host():
    forms = {}
    for form in ("defaults", "barrier"):
        forms[form] = all([control(form, False), control(form, True)])
    report["choice"] = next((key for key, ok in forms.items() if ok), None)
    save()
    if not report["choice"]:
        print("Neither dependency shape qualified; collecting original remote evidence", flush=True)


def graph(arch):
    args = startup(True, False) + ["cquery"] + opts(arch, True, True)
    expr = '[str(target.label), [f.path for f in target.files.to_list()], [f.path for f in providers(target)["DefaultInfo"].default_runfiles.files.to_list()] if str(target.label).endswith("//:build_test") else []]'
    # Cquery cannot submit spawns; explicitly clear every remote endpoint.
    args.remove("--spawn_strategy=remote")
    args += ["--remote_executor=", "--remote_cache=", "--bes_backend=", "--output=starlark", "--starlark:expr=" + expr,
             'labels(targets, //:build_test) + //:build_test']
    cp = subprocess.run(args, capture_output=True)
    row = {"exit": cp.returncode, "stderr_sha256": sha(cp.stderr), "labels": {}, "runfiles": []}
    row["diagnostics"] = diagnostics(cp.stderr)
    row["client_diagnosis"] = local_detail(cp.stderr.decode("utf-8", "replace"))
    if not cp.returncode:
        import ast
        for line in cp.stdout.decode().splitlines():
            label, files, runfiles = ast.literal_eval(line)
            label = label.lstrip("@")
            row["labels"][label] = [clean(f) for f in files]
            if label == ROOT:
                row["runfiles"] = [clean(f) for f in runfiles]
    row["verified"] = (set(row["labels"]) == set(LABELS + [ROOT]) and all(row["labels"].values())
                       and not set(row["runfiles"]).intersection(f for l in LABELS for f in row["labels"].get(l, [])))
    report["cells"]["graph-" + arch] = row
    save()
    return row["verified"]


def main(mode):
    if mode == "host":
        return host()
    if mode == "stock":
        for arch in ARCH:
            row = run("stock-" + arch, arch, False, True, "build", MARKERS + [LAUNCH])
            launcher = row.get("launcher", {})
            row["verified_launcher"] = (launcher.get("arch") == arch and launcher.get("enoent") and launcher.get("skylib")
                                        and all(launcher[k] for k in launcher if k.endswith("_absent"))
                                        and fresh(row, LAUNCH, "InspectBuildTestLauncher", arch))
            row["failed_markers"] = [label for label in MARKERS if fresh(row, label, "Genrule", arch)
                                    and any(a["owner"] == label and a["kind"] == "Genrule" and not a["success"] for a in row["actions"])
                                    and any(s["owner"] == label and s["touch"] and (s["exit"] != 0 or s["status"] not in (None, "SUCCESS", "OK")) for s in row["spawns"])]
            row["markers_with_missing_tool"] = [label for label in row["failed_markers"] if any(a["owner"] == label and
                                                a.get("missing_tool") for a in row["actions"])]
            # Use the client diagnosis only when its shell failure agrees with
            # the corresponding executed argv and the independent image probe.
            for s in row.get("spawns", []):
                if (s["owner"] in row["failed_markers"] and s["owner"] not in row["markers_with_missing_tool"]
                    and s["exe"] in ("/bin/bash", "/bin/sh", "/usr/bin/env") and "shell-enoent" in row["missing_tool"]
                    and s["exe"] + "_absent" in launcher and launcher[s["exe"] + "_absent"]):
                    row["markers_with_missing_tool"].append(s["owner"])
            save()
        return
    if mode == "select":
        if not report.get("choice"):
            raise SystemExit("Original remote cells recorded; neither dependency shape qualified locally")
        p = Path("BUILD")
        original = 'load("//tools:defs.bzl", "build_test", "gazelle", "go_path")'
        replacement = 'load("//test/rbe/source_build_test:controls.bzl", build_test = "' + report["choice"] + '_test")\nload("//tools:defs.bzl", "gazelle", "go_path")'
        assert p.read_text().count(original) == 1
        p.write_text(p.read_text().replace(original, replacement))
        for arch in ARCH:
            graph(arch)
        return
    valid = []
    for arch in ARCH:
        if not report["cells"].get("graph-" + arch, {}).get("verified"):
            valid.append(False)
            continue
        results = [control(report["choice"], suite, True, arch) for suite in (False, True)]
        row = run("real-" + arch, arch, True, True, "test", [ROOT, LAUNCH])
        launcher = row.get("launcher", {})
        row["verified"] = (row["exit"] == 0 and passed(row, ROOT, arch, True) and row["targets"].get(ROOT)
                           and launcher.get("arch") == arch and launcher.get("exit") == 0 and not launcher.get("skylib")
                           and fresh(row, LAUNCH, "InspectBuildTestLauncher", arch))
        stock = report["cells"]["stock-" + arch]
        valid.append(all(results) and row["verified"] and stock["verified_launcher"] and MARKERS[0] in stock["markers_with_missing_tool"])
        save()
    report["verified"] = all(valid) and len(valid) == 2
    save()
    if not report["verified"]:
        raise SystemExit("See original per-target observations")


if __name__ == "__main__":
    main(sys.argv[1])
