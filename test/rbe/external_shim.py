"""Qualify a pinned Gazelle default against the original failing shim build."""

from base64 import b64encode
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.request import urlopen
from zipfile import ZipFile

from external_go_policy import AREA, MASTER, SOURCE, client, details, git, now, read_events, select

SPLIT = "03763b0448f9e15a2247ba0a50e8b710cf4ba0e9"
RAW = "c01386e38186772e042196f4b03ce25eed8993ea8ffb8c012f43a2286b7752a5"
SHIM = "//shim:containerd-shim-runsc-v1"
EXT = "//pkg/shim/v1/extension:extension"
PRODUCT = "815492968634bdb416aef176f3999a4639d53b74"
CGROUPS = "@com_github_containerd_cgroups_v3"
FILES = {
    "cgroup1/stats/metrics.pb.go": "04a7e78359e4b1d0ab7f093059c3539684030bee639e22f92e96f6e7a8243370",
    "cgroup2/stats/metrics.pb.go": "8733105f685f21c564cd48bca7c8f66e36025b676ea5ec8335c8991ffd8f1c83",
    "cgroup1/cpuacct.go": "880237243de0332d4d37514d88525847dd772e87609fe5b37eeadf455256f7b6",
    "cgroup1/memory.go": "3a3647aa7a50fe2d88bb87429f0e8c4f5db2f2bf73c46bd393c6a288a39dbda2",
    "cgroup2/manager.go": "619d9119f8476fce135cf7febcc665897f9428e410f811182f72ad4d1153e01e",
}
RESULT = {"verdict": "INCOMPLETE", "builds": {}, "upstream": [
    "https://github.com/bazel-contrib/bazel-gazelle/pull/2491",
    "https://github.com/containerd/cgroups/pull/220",
]}
REGISTRIES = ["--registry=" + (AREA / "registry").as_uri(), "--registry=https://bcr.bazel.build"]
DEFAULTS_SHA = "4f95c9122a714f39d06b8aa3fe4b5bd70a982e571ac3a31599b73079b267d3db"
BAZEL = [s for s in client("8.8.1") if s != "--noworkspace_rc"]


def save():
    (AREA.parent / "external-go.json").write_text(json.dumps(RESULT, indent=2) + "\n")


def original():
    archive = AREA / "original.zip"
    digest = sha256(archive.read_bytes()).hexdigest()
    assert digest == "4c5bb50ed0eceeadfdccea8c5a5a1a8c14e92c22dd16c46c2f50d5f64741b9e0"
    with ZipFile(archive) as z:
        assert z.namelist() == ["external-go.json"]
        prior = json.loads(z.read("external-go.json"))
    assert prior["verdict"] == "RECOVERED" and prior["compiler_owners_with_diagnostic"] == ["cgroup1", "cgroup2"]
    assert prior["original_states"]["split_ignore"] == [SPLIT, "d1e538c4-070c-4200-bdb8-b3317f74d1c8", "BUILD_FAILED", 1]
    assert prior["original_states"]["stock_ignore"] == [MASTER, "4067ef71-d32c-430a-b16a-f9c6725df18e", "BUILT", 0]
    RESULT["original"] = {"run": 41402235566, "artifact": 13122370481, "sha256": digest,
                          "build_run": prior["original_run"], "build_artifact": prior["artifact"],
                          "archive": prior["archive"], "original_states": prior["original_states"]}


def prepare_registry():
    files = {
        "MODULE.bazel": "618a729142f66de1e2cb776d026413763be5b80d5e3d29ffb9d3d90c5defde90",
        "source.json": "fbe5312a01fb4a2a58caff4a0d40dcf384b6f0bda75e85546663360da1d7538a",
        "patches/module_dot_bazel_version.patch": "dec954e9b8f9dc27b23d430050146e01d18f73ec2b11980734e2e5f8cde0eade",
    }
    root = AREA / "registry"
    dest = root / "modules/gazelle/0.51.3"
    (dest / "patches").mkdir(parents=True)
    (root / "bazel_registry.json").write_text("{}\n")
    for name, digest in files.items():
        with urlopen("https://bcr.bazel.build/modules/gazelle/0.51.3/" + name, timeout=30) as response:
            data = response.read()
        assert sha256(data).hexdigest() == digest
        (dest / name).write_bytes(data)
    source = dest / "source.json"
    descriptor = json.loads(source.read_text())
    patch = (AREA / "gazelle-cgroups-proto.patch").read_bytes()
    assert git("hash-object", str(AREA / "gazelle-cgroups-proto.patch")) == git("rev-parse", PRODUCT + ":tools/gazelle-cgroups-proto.patch")
    (dest / "patches/cgroups.patch").write_bytes(patch)
    descriptor["patches"]["cgroups.patch"] = "sha256-" + b64encode(sha256(patch).digest()).decode()
    source.write_text(json.dumps(descriptor))
    RESULT["registry"] = {"original_sha256": files, "source": descriptor, "patched_source_json_sha256": sha256(source.read_bytes()).hexdigest()}
    save()


def preflight():
    row = RESULT["preflight"] = {"verified": False, "ignore_dev_dependency": True, "source": SPLIT}
    save()
    flags = [*REGISTRIES, "--ignore_dev_dependency"]
    packages = " + ".join(CGROUPS + "//" + n + "/stats:all" for n in ("cgroup1", "cgroup2"))
    query = 'labels(srcs, kind("go_library", ' + packages + '))'
    p = subprocess.run([*BAZEL, "query", *flags, query], capture_output=True, text=True)
    labels = [s for s in p.stdout.splitlines() if s.startswith("@") and "com_github_containerd_cgroups_v3//" in s]
    row.update(query=query, exit=p.returncode, labels=labels, errors=details(p.stderr) if p.returncode else [])
    save()
    base = subprocess.check_output([*BAZEL, "info", *flags, "output_base"], text=True).strip()
    external = Path(base) / "external"
    defaults = external / "gazelle+/internal/bzlmod/default_gazelle_overrides.bzl"
    row["effective_defaults_sha256"] = sha256(defaults.read_bytes()).hexdigest()
    metrics = {n: sha256((external / "gazelle++go_deps+com_github_containerd_cgroups_v3" / n).read_bytes()).hexdigest()
               for n in FILES if n.endswith("/metrics.pb.go")}
    row["effective_stats_sha256"] = metrics
    row["verified"] = p.returncode == 0 and row["effective_defaults_sha256"] == DEFAULTS_SHA and all(
        metrics[n] == FILES[n] and any(label.endswith("//" + n.replace("/metrics.pb.go", ":metrics.pb.go")) for label in labels)
        for n in metrics)
    save()
    return row["verified"]


def startup(name, exe, digest):
    observed = {"state": "INCOMPLETE"}
    RESULT["builds"][name]["startup"] = observed
    save()
    if not exe.is_file() or sha256(exe.read_bytes()).hexdigest() != digest:
        return
    exe = exe.resolve()
    trace = AREA / (name + ".trace")
    strace = shutil.which("strace")
    args = [strace, "-qq", "-f", "-yy", "-s", "0", "-e", "trace=pread64", "-o", str(trace)] if strace else []
    p = subprocess.run([*args, str(exe), "-v"], capture_output=True, text=True, timeout=30)
    observed.update(exit=p.returncode, size=exe.stat().st_size, sha256=digest, strace=bool(strace),
                    version_ok=all(s in p.stdout for s in ("containerd-shim-runsc-v1:", "Version:", "Revision:", "Go version:")))
    save()
    if p.returncode or not observed["version_ok"] or not trace.is_file():
        return
    offsets = [int(x) for x in re.findall(r"pread64\(\d+<" + re.escape(str(exe)) +
                                          r'>, [^\n]*?, 64, (\d+)\)\s+= 64\b', trace.read_text())]
    expected = [i * observed["size"] // 8 for i in range(8)]
    observed.update(state="OBSERVED", self_pread64_offsets=offsets,
                    eight_protobuf_samples=offsets == expected)
    save()


def actions(path, collection):
    collection[path.suffix] = "missing"
    if not path.is_file():
        return
    # The full shim has many transitive Go compilations. Project one spawn at
    # a time instead of keeping the entire execution log in memory.
    filt = '''select((.exitCode // 0) != 0 or ((.targetLabel // "") |
        endswith("//shim:containerd-shim-runsc-v1") or endswith("//pkg/shim/v1/extension:extension") or
        contains("/internal/detrand:") or contains("//pkg/protobuf/types:") or contains("com_github_containerd_cgroups_v3//"))) |
        {targetLabel,mnemonic,exitCode,runner,cacheHit,status,platform,actualOutputs,
         inputs: [.inputs[]? | select(.path | endswith("/internal/detrand/rand.go") or endswith("/metrics.pb.go") or
             endswith("/cpuacct.go") or endswith("/memory.go") or endswith("/manager.go"))]}'''
    with subprocess.Popen(["jq", "-c", filt, str(path)], stdout=subprocess.PIPE, text=True) as p:
        for line in p.stdout:
            yield json.loads(line)
        collection[path.suffix] = "complete" if p.wait() == 0 else "invalid"


def build(name, sha, cwd):
    shim, ext = SHIM, EXT
    row = RESULT["builds"][name] = {"source": sha, "started": now(), "consumer": False,
                                    "ignore_dev_dependency": True, "state": "INCOMPLETE", "collection": {}}
    save()
    bep, spawns = AREA / (name + ".bep"), AREA / (name + ".spawns")
    args = [*BAZEL, "build", *REGISTRIES, "--jobs=32", "--keep_going",
            "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
            "--remote_default_exec_properties=EstimatedMemory=2GB",
            "--remote_default_exec_properties=workload-isolation-type=firecracker",
            "--remote_default_exec_properties=container-image=docker://" + os.environ["GNU"],
            "--remote_download_outputs=toplevel", "--build_event_json_file=" + str(bep),
            "--execution_log_json_file=" + str(spawns)]
    args += ["--ignore_dev_dependency", shim, ext, CGROUPS + "//cgroup1:cgroup1", CGROUPS + "//cgroup2:cgroup2"]
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors="replace")
    log = p.stderr + p.stdout
    row.update(exit=p.returncode, returned=now(), safe_errors=details(log) if p.returncode else [],
               log_sha256=sha256(log.encode()).hexdigest())
    row["go_diagnostics"] = list(dict.fromkeys(re.findall(
        r"(?:^|/)([A-Za-z0-9_.-]+\.go:[0-9]+:[0-9]+: (?:undefined: [\w.]+|[\w.]+ undefined \(type [\w.*]+ has no field or method [\w.]+\)))", log, re.M)))[:16]
    save()
    completed = {}
    for e in read_events(bep, row["collection"]):
        if "started" in e:
            row.update(bazel=e["started"]["buildToolVersion"], invocation=e["started"]["uuid"])
        if "finished" in e:
            row["bazel_exit"] = e["finished"]["exitCode"].get("code", 0)
        if "targetCompleted" in e.get("id", {}):
            completed[e["id"]["targetCompleted"]["label"]] = e.get("completed", {}).get("success", False)
    row.update(completed=completed, actions=[], cgroups=[], protobuf_sources=[], containerd_types=[], failures=[])
    save()
    for s in actions(spawns, row["collection"]):
        label = s.get("targetLabel") or ""
        mnemonic = s.get("mnemonic")
        if s.get("exitCode", 0) and len(row["failures"]) < 10:
            row["failures"].append([label, mnemonic, s["exitCode"], s.get("status")])
        if mnemonic == "GoCompilePkg" and "com_github_containerd_cgroups_v3//" in label:
            sources = {key: f.get("digest", {}).get("hash") for f in s.get("inputs", [])
                       for key in FILES if f["path"].endswith("/" + key)}
            if sources:
                props = {p["name"]: p["value"] for p in (s.get("platform") or {}).get("properties", [])}
                row["cgroups"].append({"label": label, "sources": sources, "exit": s.get("exitCode", 0),
                                      "cached": s.get("cacheHit", False), "runner": s.get("runner"),
                                      "image": props.get("container-image"), "outputs": s.get("actualOutputs") or []})
        if mnemonic == "GoCompilePkg" and "//pkg/protobuf/types:" in label and "com_github_containerd_containerd_v2" in label:
            row["containerd_types"].append({"label": label, "exit": s.get("exitCode") or 0,
                                           "outputs": s.get("actualOutputs") or []})
        if mnemonic == "GoCompilePkg":
            for f in s.get("inputs", []) if "/internal/detrand:" in label else ():
                if f.get("path", "").endswith("/internal/detrand/rand.go"):
                    row["protobuf_sources"].append(f.get("digest", {}))
        if not (label.endswith(SHIM) and mnemonic == "GoLink" or label.endswith(EXT) and mnemonic == "GoCompilePkg"):
            continue
        outputs = [f for f in s.get("actualOutputs") or [] if f.get("digest", {}).get("hash") and
                   (Path(f["path"]).name == "containerd-shim-runsc-v1" or f["path"].endswith((".a", ".x")))]
        props = {k["name"]: k["value"] for k in (s.get("platform") or {}).get("properties", [])}
        row["actions"].append({"owner": label, "mnemonic": mnemonic, "outputs": outputs,
                               "exit": s.get("exitCode") or 0, "runner": s.get("runner"),
                               "cached": s.get("cacheHit", False), "image": props.get("container-image")})
    row["identity_ok"] = (git("rev-parse", "HEAD") == sha and row.get("bazel") == "8.8.1" and
                          row.get("bazel_exit") == p.returncode and bool(row.get("invocation")))
    save()
    links = [(a, f) for a in row["actions"] if a["mnemonic"] == "GoLink" and not a["exit"] for f in a["outputs"]]
    if row["identity_ok"] and len(links) == 1:
        a, f = links[0]
        if (a["cached"] or a["runner"] == "remote") and a["image"] == "docker://" + os.environ["GNU"]:
            startup(name, cwd / f["path"], f["digest"]["hash"])
    if row["identity_ok"] and row["collection"].get(".bep") == "complete" and (
            row["collection"].get(".spawns") == "complete" or p.returncode and row["collection"].get(".spawns") == "missing"):
        if p.returncode and (row["safe_errors"] or row["failures"]):
            row["state"] = "BUILD_FAILED"
        elif not p.returncode and row.get("startup", {}).get("state") == "OBSERVED" and all(
                any(ok and (label == alias or label.endswith(owner)) for label, ok in completed.items())
                for alias, owner in ((shim, SHIM), (ext, EXT))):
            row["state"] = "BUILT"
    save()


def main():
    assert git("rev-parse", "HEAD^") == "0d08a91ce44afcd87d543150e1224033af66caed"
    assert git("rev-parse", "HEAD^^") == PRODUCT and git("rev-parse", PRODUCT + "^") == SPLIT
    select(SPLIT)
    try:
        original()
        with ZipFile(AREA / "no-override.zip") as z:
            prior = json.loads(z.read("external-go.json"))
        assert sha256((AREA / "no-override.zip").read_bytes()).hexdigest() == "cef77a84fac1cff4a9a2550240cc4190e5a0e43f44a5e0d1451d70fbeaea3f0a"
        prev = prior["builds"]["default"]
        assert prev["source"] == PRODUCT and prev["state"] == "BUILD_FAILED" and prev["exit"] == 1
        assert prior["effective_defaults_sha256"] == "68d62b8184488797a92004da081dc5f0f5044ac2ee2427445f4b0242b9c69688"
        RESULT["ignored_override_control"] = {"run": 41402974439, "artifact": 13121654249, "source": PRODUCT,
                                              "invocation": prev["invocation"], "defaults_sha256": prior["effective_defaults_sha256"],
                                              "go_diagnostics": prev["go_diagnostics"]}
        prepare_registry()
        if not preflight():
            return True
        build("default", SPLIT, SOURCE)
        row = RESULT["builds"]["default"]
        sources = {}
        for a in row["cgroups"]:
            assert not a["exit"] and (a["runner"] == "remote" or a["cached"])
            assert a["image"] == "docker://" + os.environ["GNU"]
            assert any(f.get("digest", {}).get("hash") and f["path"].endswith(".a") for f in a["outputs"])
            package = a["label"].split("//")[-1].split(":")[0]
            for key, value in a["sources"].items():
                assert key.rsplit("/", 1)[0] == package
                assert sources.setdefault(key, value) == value
        complete = row["completed"]
        obs = row.get("startup", {})
        checks = {
            "pinned_default": RESULT["preflight"]["verified"],
            "real_shim_built": row["state"] == "BUILT" and row["bazel_exit"] == row["exit"] == 0 and all(complete.values()),
            "cgroup_targets_built": all(any("com_github_containerd_cgroups_v3//" + name + ":" + name in k and v
                                            for k, v in complete.items()) for name in ("cgroup1", "cgroup2")),
            "actual_shipped_sources": sources == FILES,
            "unpatched_protobuf": {d.get("hash") for d in row["protobuf_sources"]} == {RAW},
            "actual_version_and_eight_self_reads": obs.get("state") == "OBSERVED" and obs["eight_protobuf_samples"],
        }
        RESULT.update(checks=checks, verdict="PASS" if all(checks.values()) else "INCOMPLETE")
    finally:
        RESULT["finished"] = now()
        save()
    return RESULT["verdict"] != "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
