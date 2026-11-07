"""Compare the real release producers and verify the two optimized ELF layouts."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

W = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
sys.path.insert(0, str(W))
import full
import qualify

# rules_cc may unpack the linked binary under a directory of the same name.
# Keep both actual action-owner spellings in the compact-log selection.
full.PRODUCERS.update({owner + "_/" + Path(out).name: out for owner, out in full.C_LINKS.items()})

STOCK = "9e1db6e550d181ff2c691a5e8df8412b74316415"
TRIAL = "dd2be7670c1baaacebfdcab31bb6145ebce39037"
UNET = "//pkg/unet:unet_state_autogen"
OLD_STUB = "//pkg/sentry/platform/systrap/sysmsg:sighandler_binary"
VALIDATIONS = "//test/rbe/release_validation_roots:active"
AUTH_SUFFIX = "/bazel/private/oss/toolchains/prebuilt/validation_output.txt"
PLATFORM = "//test/rbe/release_prewarmer:amd64"
IMAGE = "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"
BAZEL = [str(W / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx6g"]
OFFLINE = ["--remote_executor=", "--remote_cache=", "--bes_backend="]
ARCH = {"amd64": ("x86_64", "k8", 62), "arm64": ("aarch64", "aarch64", 183)}


def save(name, result):
    data = json.dumps(result, indent=2)
    assert len(data) < 1800000 and not re.search(r"(?i)authorization\s*[:=]|x-buildbuddy-api-key|github_pat_|gh[pousr]_", data)
    (W / "public" / (name + ".json")).write_text(data + "\n")


def load(name):
    return json.loads((W / "public" / (name + ".json")).read_text())


def flags(arch):
    return ["--config=" + ARCH[arch][0], "-c", "opt", "--jobs=32", "--lockfile_mode=off",
            "--host_platform=" + PLATFORM, "--extra_execution_platforms=" + PLATFORM,
            "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
            "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain", "--color=no", "--curses=no", "--noshow_progress"]


def safe_path(s):
    return s if re.fullmatch(r"[\w+@./:=~-]{1,400}", s) else "<unclassified>"


def signals(text):
    return [k for k,v in {"enoent": "no such file", "missing-target": "no such target", "dependency": "error downloading",
                          "no-worker": "no registered executors", "disk-full": "no space left", "oom": "outofmemory",
                          "missing-toolchain": "no matching toolchains", "patch": "error applying patch",
                          "version-mismatch": "does not match", "syntax": "syntax error", "visibility": "visibility"}.items()
            if v in text.lower()]


def execute(args, name, timeout):
    path, log = W / (name + ".out"), W / (name + ".err")
    with path.open("wb") as out, log.open("wb") as err:
        try:
            code = subprocess.run(args, stdout=out, stderr=err, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            code = 124
    return code, path, signals(log.read_text(errors="replace"))


def graph(arch, stock):
    phase = "stock" if stock else "trial"
    name = phase + "-graph-" + arch
    doc = {"source": STOCK if stock else TRIAL, "target": arch, "exec": "amd64", "eligible": False}
    try:
        fmt = 'json.encode({"owner": str(target.label), "files": [f.path for f in target.files.to_list()]})'
        code, out, sig = execute([*BAZEL, "cquery", *flags(arch), *OFFLINE, "--output=starlark", "--starlark:expr=" + fmt,
                                  "set(//:release " + VALIDATIONS + ")"], name + "-roots", 550)
        doc.update(cquery_exit=code, signals=sig)
        if code:
            return False
        roots = {r["owner"].lstrip("@"): r["files"] for r in map(json.loads, out.read_text().splitlines())}
        release = sorted(roots["//:release"])
        assert all(re.fullmatch(r"bazel-out/[\w+-]+/bin/release/[\w/-]+", s) for s in release)
        doc["release"] = release
        validation = {p for p in roots[VALIDATIONS] if p.endswith(AUTH_SUFFIX)}
        doc.update(validation_count=len(roots[VALIDATIONS]), authentication_outputs=sorted(validation))
        assert {p.split("/bin/release/",1)[1] for p in release} == full.FILES
        # Ordinary cquery --output=files omits hidden output groups.
        query = 'mnemonic("^(GoImports|GoStateify|RunBinary|Genrule|ProtocAuthenticityCheck)$", filter("(//pkg/unet:unet_state_autogen|//pkg/sentry/platform/systrap/sysmsg:sighandler_(binary|data)|:authenticity_validation)$", deps(//:release)))'
        code, out, sig = execute([*BAZEL, "aquery", *flags(arch), *OFFLINE, "--output=jsonproto", "--include_commandline",
                                  "--include_artifacts", query], name + "-actions", 550)
        doc.update(aquery_exit=code, action_signals=sig)
        if code:
            return False
        data = json.loads(out.read_text())
        labels = {str(t["id"]): t["label"] for t in data["targets"]}
        configs = {str(t["id"]): t for t in data["configuration"]}
        frags = {str(f["id"]): f for f in data["pathFragments"]}
        arts = {str(a["id"]): str(a["pathFragmentId"]) for a in data["artifacts"]}
        def path(i):
            parts, frag = [], arts[str(i)]
            while frag != "0":
                parts.append(frags[frag]["label"])
                frag = str(frags[frag].get("parentId", 0))
            return "/".join(reversed(parts))
        targeted, observed = [], set()
        doc["actions"] = targeted
        expected = {UNET, OLD_STUB if stock else full.STUB, full.AUTH}
        for act in data["actions"]:
            owner = labels[str(act["targetId"])]
            if owner not in expected:
                continue
            outputs = [path(i) for i in act["outputIds"]]
            if owner == full.AUTH:
                relevant = validation.intersection(outputs)
                if not relevant:
                    continue
                observed.update(relevant)
            exe = act["arguments"][0]
            props = {p["key"]: p.get("value", "") for p in act["executionInfo"]}
            mn = act["mnemonic"]
            if owner == UNET:
                correct = ((mn == "GoImports" and exe.endswith("/goimports.sh")) if stock else
                           (mn == "GoStateify" and exe.endswith("/tools/go_stateify/stateify_/stateify")))
            elif owner == full.AUTH:
                correct = mn == "ProtocAuthenticityCheck" and (exe == "/bin/bash" if stock else exe.endswith("/test/rbe/go_bootstrap/ash"))
            else:
                correct = ((mn == "Genrule" and exe == "/bin/bash") if stock else
                           (mn == "RunBinary" and exe.endswith("/gen_stub_/gen_stub")))
            cfg = configs[str(act["configurationId"])]
            targeted.append({"owner": owner, "mnemonic": mn, "key": act["actionKey"], "config": cfg["checksum"],
                             "cpu": cfg["platformName"], "executable": safe_path(exe), "outputs": outputs,
                             "verified": (correct and act["executionPlatform"].lstrip("@") == PLATFORM
                                          and props.get("Arch") == "amd64" and props.get("container-image") == IMAGE
                                          and props.get("workload-isolation-type") == "oci")})
        doc["eligible"] = bool(validation and observed == validation and {a["owner"] for a in targeted} == expected
                               and all(a["verified"] for a in targeted))
        return doc["eligible"]
    finally:
        save(name, doc)


def physical(name, arch, roots, fresh, minutes):
    stage = name.startswith("release-")
    product = STOCK if name == "stock-unet" else TRIAL
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == product
    graph_doc = load(("stock" if product == STOCK else "trial") + "-graph-" + arch)
    assert graph_doc["eligible"] and (roots == ["//:release"] if stage else roots == [UNET])
    inv = str(uuid.uuid4())
    bep, compact = W / (name + ".bep"), W / (name + ".compact")
    doc = {"source": product, "target": arch, "exec": "amd64", "roots": roots, "invocation": inv, "verified": False}
    save(name, doc)
    try:
        args = [*BAZEL, "--bazelrc=" + str(W / "remote.bazelrc"), "build", *flags(arch), "--spawn_strategy=remote",
                "--noremote_local_fallback", "--remote_accept_cached", "--remote_download_outputs=toplevel",
                "--remote_download_regex=(.*/validation_output[.]txt|.*/(unet_state_autogen[.]go|sighandler[.](built-in[.]bin|go[.]arch)))$",
                "--modify_execution_info=^(" + fresh + ")$=+no-remote-cache", "--remote_timeout=900",
                "--build_event_publish_all_actions", "--invocation_id=" + inv, "--build_event_json_file=" + str(bep),
                "--execution_log_compact_file=" + str(compact), *roots]
        code, _, sig = execute(args, name, minutes * 60)
        doc.update(exit=code, signals=sig)
        started, ended, selected, failures = [], [], [], []
        evidence = {}
        config_cpus = {}
        for line in bep.open():
            event = json.loads(line)
            ident = event["id"]
            if "started" in event:
                started.append(event["started"].get("uuid"))
            if "finished" in event:
                ended.append(event["finished"].get("exitCode", {}).get("code", 0))
            if "configuration" in event:
                config_cpus[ident["configuration"]["id"]] = event["configuration"].get("cpu")
            if "targetCompleted" in ident and not ident["targetCompleted"].get("aspect") and "completed" in event:
                target = ident["targetCompleted"]
                if target["label"].lstrip("@") in roots:
                    selected.append([target["label"].lstrip("@"), event["completed"].get("success", False),
                                     target["configuration"]["id"]])
            a, action_id = event.get("action"), ident.get("actionCompleted", {})
            if not a:
                continue
            owner, mn, path = action_id.get("label", ""), a.get("type", ""), action_id.get("primaryOutput", "")
            if not a.get("success") and len(failures) < 12:
                failures.append({"owner": safe_path(owner), "mnemonic": safe_path(mn), "output": safe_path(path),
                                 "exit": a.get("exitCode"), "signals": signals(a.get("failureDetail", {}).get("message", ""))})
            if owner in {"//:release", UNET, full.STUB, full.AUTH, *full.PRODUCERS}:
                evidence[owner, mn, path] = [bool(a.get("success")), action_id.get("configuration", {}).get("id")]
        doc.update(started=started, finished=ended, selected=selected, failures=failures)
        save(name, doc)
        counts, actions = full.actions(compact, "amd64", owner=roots[0])
        doc["runners"] = counts
        doc["actions"] = []
        for a in actions:
            owner, mn = a["label"], a["mnemonic"]
            rec = {"owner": safe_path(owner), "mnemonic": safe_path(mn), "runner": a["runner"], "cached": bool(a["cache"]),
                   "exit": a["exit"], "worker": a["worker_matches"], "exe": safe_path(a.get("executable") or ""),
                   "digest": a.get("digest"), "outputs": []}
            for f in a["outputs"]:
                p = f["path"]
                found = evidence.get((owner, mn, p))
                rec["outputs"].append({"path": safe_path(p), "digest": f.get("digest"),
                                       "event": found, "cpu": config_cpus.get(found[1]) if found else None})
            doc["actions"].append(rec)
        save(name, doc)
        matching = [a for a in doc["actions"] if a["runner"] == "remote" and not a["cached"] and a["worker"]]
        common = (started == [inv] and ended == [code] and not counts.get("different_platform")
                  and not (set(counts) - {"remote", "remote cache hit"}))
        if name == "stock-unet":
            observed = [a for a in matching if a["owner"] == UNET and a["mnemonic"] == "GoImports"]
            doc["verified"] = bool(common and code and len(observed) == 1 and observed[0]["exit"]
                                   and observed[0]["exe"].endswith("/goimports.sh") and "enoent" in sig
                                   and any(f["owner"] == UNET and f["mnemonic"] == "GoImports" for f in failures))
        elif name == "trial-unet":
            fresh_outputs = [f for a in matching if a["owner"] == UNET and a["mnemonic"] == "GoStateify"
                             and a["exe"].endswith("/tools/go_stateify/stateify_/stateify") and a["exit"] == 0 for f in a["outputs"]]
            doc["verified"] = bool(common and code == 0 and len(fresh_outputs) == 1
                                   and output_matches(fresh_outputs[0]) and selected[0][:2] == [UNET, True])
        else:
            doc["verified"] = release_result(doc, graph_doc, matching, evidence, config_cpus) and common and code == 0
        return doc["verified"]
    finally:
        save(name, doc)


def output_matches(f):
    p = Path(f["path"])
    if not p.is_file() or not f["digest"]:
        return False
    with p.open("rb") as src:
        return (int(f["digest"]["sizeBytes"]) == p.stat().st_size and
                f["digest"]["hash"] == hashlib.file_digest(src, "sha256").hexdigest())


def release_result(doc, graph_doc, matching, evidence, cpus):
    arch = doc["target"]
    cpu, machine = ARCH[arch][1:]
    layout = doc["files"] = {}
    for path in graph_doc["release"]:
        name = path.split("/bin/release/", 1)[1]
        p = Path(path)
        if not p.is_file():
            continue
        elf = qualify.elf(p)
        copies = [f for a in matching if a["owner"] == "//:release" and a["mnemonic"] == "CopyFile" and not a["exit"]
                  for f in a["outputs"] if f["path"] == path and f["cpu"] == cpu and f["event"][0]]
        elf["original_copy"] = (len(copies) == 1 and copies[0]["digest"]["hash"] == elf["sha256"]
                                and int(copies[0]["digest"]["sizeBytes"]) == elf["bytes"])
        links = [f for a in matching if full.PRODUCERS.get(a["owner"]) == name and a["mnemonic"] in ("GoLink", "CppLink")
                 and not a["exit"] for f in a["outputs"] if f["digest"]["hash"] == elf["sha256"] and f["cpu"] == cpu
                 and f["event"] and f["event"][0]]
        elf["original_link"] = bool(links)
        layout[name] = elf
    doc["release_target_complete"] = (len(doc["selected"]) == 1 and doc["selected"][0][:2] == ["//:release", True]
                                      and cpus.get(doc["selected"][0][2]) == cpu)
    doc["all_native_copies"] = layout.keys() == full.FILES and all(x["machine"] == machine and x["load"] and x["original_copy"]
                                                                   for x in layout.values())
    doc["all_original_links"] = bool(layout) and all(x["original_link"] for x in layout.values())
    save("release-" + arch, doc)
    vals = doc["validations"] = {}
    prior = W / "public" / "release-amd64.json"
    shared = json.loads(prior.read_text()).get("validations", {}) if arch == "arm64" and prior.is_file() else {}
    for path in graph_doc["authentication_outputs"]:
        results = [f for a in matching if a["owner"] == full.AUTH and a["mnemonic"] == "ProtocAuthenticityCheck"
                   and not a["exit"] and a["exe"].endswith("/test/rbe/go_bootstrap/ash")
                   for f in a["outputs"] if f["path"] == path and f["event"] and f["event"][0]]
        previous = shared.get(path)
        if len(results) == 1 and output_matches(results[0]):
            vals[path] = {"origin": doc["invocation"], "digest": results[0]["digest"], "fresh_here": True}
        elif previous and previous.get("verified") and output_matches({"path": path, "digest": previous["digest"]}):
            vals[path] = {"origin": previous["origin"], "digest": previous["digest"], "fresh_here": False}
        else:
            vals[path] = {"verified": False}
            continue
        raw = Path(path).read_bytes()
        vals[path]["verified"] = raw.rstrip(b"\n") == b"libprotoc 35.1"
    stubs = doc["stub_actions"] = []
    for a in matching:
        if a["owner"] == full.STUB and a["mnemonic"] == "RunBinary" and a["exe"].endswith("/gen_stub_/gen_stub") and not a["exit"]:
            outputs = a["outputs"]
            if any(f["cpu"] == cpu and f["event"] and f["event"][0] for f in outputs):
                stubs.append({"digest": a["digest"], "verified": (len(outputs) == 2 and all(output_matches(f) for f in outputs)
                              and {Path(f["path"]).name for f in outputs} == {"sighandler.built-in.bin", "sighandler.go.arch"})})
    return bool(doc["release_target_complete"] and doc["all_native_copies"] and doc["all_original_links"]
                and vals and all(v["verified"] for v in vals.values()) and stubs and all(s["verified"] for s in stubs))


def guarded(f, name):
    try:
        return f()
    except (AssertionError, OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as e:
        import traceback
        save(name + "-observer", {"error": type(e).__name__, "line": traceback.extract_tb(e.__traceback__)[-1].lineno})
        return False


def main():
    phase, = sys.argv[1:]
    src = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert src == (STOCK if phase in ("stock-graph", "stock") else TRIAL)
    assert Path(".bazelversion").read_text().strip() == "9.3.1"
    if phase in ("stock-graph", "trial-graph"):
        outcomes = [guarded(lambda: graph(arch, phase == "stock-graph"), phase + "-" + arch) for arch in ARCH]
        return all(outcomes)
    baseline = "stock-unet"
    if phase == "stock":
        assert all(load("trial-graph-" + arch)["eligible"] and load("stock-graph-" + arch)["eligible"] for arch in ARCH)
        return guarded(lambda: physical(baseline, "amd64", [UNET], "GoImports", 12), baseline)
    assert phase == "releases" and load(baseline)["verified"]
    if not guarded(lambda: physical("trial-unet", "amd64", [UNET], "GoStateify", 12), "trial-unet"):
        return False
    results = []
    for arch in ARCH:
        name = "release-" + arch
        results.append(guarded(lambda: physical(name, arch, ["//:release"], "CopyFile|GoLink|CppLink|RunBinary|ProtocAuthenticityCheck", 32), name))
    return all(results)


if __name__ == "__main__":
    raise SystemExit(not main())
