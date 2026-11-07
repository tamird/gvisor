"""Schedule the real release staging rule and its direct public producers."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

D = Path(os.environ["RUNNER_TEMP"]) / "chainguard-release"
SOURCE = "f462794c042c7325ac0452551b1f18e7083dede4"
PRODUCT = "1824f3ee0a9e500491535331312ac08fe8da5004"
STAMP = "release-20261106.0"
STAGE = "//debian:release-artifacts"
IMAGE = "docker://cgr.dev/chainguard/static@sha256:fc42af33e643f20d08baf03cddcc1c02ba57052dfb02edb0d79f3fc47bcea8d9"
WORKER = "//test/rbe/stamped_release:"
FORMATS = {
    "runsc.deb": ("//debian:debian", "MakeDeb"),
    "gvisor.tar.bz2": ("//debian:gvisor-release-tar-bz2", "PackageTar"),
    "gvisor.tar.zstd": ("//debian:gvisor-release-tar-zstd", "PackageTar"),
}
CONFIG = {"amd64": "x86_64", "arm64": "aarch64"}
JOBS = ("stage", *CONFIG)
TERMS = ("no such file", "no matching toolchain", "no matching execution platform",
         "not a valid", "unknown option", "invalid platform", "no registered executors",
         "permission denied", "exec format error", "out of memory", "no space left",
         "deadline exceeded", "failed to start", "starlark", "conflicting actions",
         "no module named", "not executable", "libgcc_s.so", "error while loading shared libraries",
         "failed to fetch", "python", "compressor", "zstd", "build_tar", "make_deb")


def save(key, obj):
    (D / (key + ".json")).write_text(json.dumps(obj, sort_keys=True, indent=2) + "\n")


def args(key, command, remote):
    bazel = ["bazelisk", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(D / "heap.rc")]
    if remote:
        bazel.append("--bazelrc=" + str(D / "client.rc"))
    flags = [
        command, "--config=" + CONFIG.get(key, "x86_64"), "--config=release_version",
        "--embed_label=" + STAMP, "-c", "opt", "--lockfile_mode=off", "--jobs=48",
        "--platform_mappings=test/rbe/stamped_release/platform_mappings",
        "--extra_execution_platforms=" + ",".join(WORKER + a for a in CONFIG),
        "--extra_toolchains=" + ",".join("//test/rbe/go_bootstrap:linux_" + a + "_ash" for a in CONFIG),
        "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
        "--@rules_python//python/config_settings:bootstrap_impl=native",
        "--color=no", "--curses=no", "--noshow_progress",
    ]
    props = "^(MakeDeb|PackageTar)$=+no-remote-cache"
    if key != "stage":
        props += ",^(MakeDeb|PackageTar)$=+qualification-direct-" + key
    return bazel + flags + ["--modify_execution_info=" + props]


def targets(key):
    return [STAGE] if key == "stage" else [label for label, _ in FORMATS.values()]


def diagnostic(path):
    if not path.is_file():
        return []
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        text = stream.read().decode(errors="replace").lower()
    return [term for term in TERMS if term in text]


def analyze(key):
    graph = D / ("plan-" + key + ".graph")
    log = D / ("plan-" + key + ".log")
    query = 'mnemonic("^(MakeDeb|PackageTar)$", deps(set(' + " ".join(targets(key)) + ')))'
    cmd = args(key, "aquery", False) + ["--output=jsonproto", "--noinclude_commandline",
                                        "--include_artifacts", query]
    with graph.open("wb") as output, log.open("wb") as error:
        code = subprocess.run(["timeout", "18m", *cmd], stdout=output, stderr=error).returncode
    row = {"exit": code, "terms": diagnostic(log), "verified": False}
    if code:
        return row
    data = json.loads(graph.read_bytes())
    names = {str(t["id"]): t["label"].removeprefix("@@") for t in data["targets"]}
    configs = {str(c["id"]): c for c in data["configuration"]}
    fragments = {str(f["id"]): f for f in data["pathFragments"]}
    artifacts = {str(f["id"]): str(f["pathFragmentId"]) for f in data["artifacts"]}
    actions = []
    for action in data["actions"]:
        pair = (names[str(action["targetId"])], action["mnemonic"])
        if pair not in FORMATS.values():
            continue
        label = action.get("executionPlatform", "").removeprefix("@@")
        arch = next((a for a in CONFIG if label == WORKER + a), "other")
        conf = configs[str(action["configurationId"])]
        cpu = conf["mnemonic"].split("-", 1)[0]
        target_cpu = {"k8": "amd64", "x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(cpu, "other")
        filename = next(n for n, owner in FORMATS.items() if owner == pair)
        candidates = []
        for output in action.get("outputIds", []):
            node = artifacts[str(output)]
            parts = []
            while node in fragments:
                part = fragments[node]
                parts.append(part["label"])
                node = str(part.get("parentId", ""))
            path = "/".join(reversed(parts))
            if path.endswith("/bin/debian/" + filename):
                assert re.fullmatch(r"bazel-out/[A-Za-z0-9_.+-]{1,100}/bin/debian/[A-Za-z0-9.]+", path)
                candidates.append(path)
        checksum = conf["checksum"]
        assert re.fullmatch(r"[0-9a-f]{64}", checksum) and len(candidates) == 1
        actions.append({"owner": pair[0], "mnemonic": pair[1], "target": target_cpu, "worker": arch,
                        "configuration": checksum, "output": candidates[0]})
    expected = [[*pair, a, a] for pair in FORMATS.values() for a in (CONFIG if key == "stage" else [key])]
    observed = [[a[k] for k in ("owner", "mnemonic", "target", "worker")] for a in actions]
    row.update(actions=actions, verified=sorted(expected) == sorted(observed))
    return row


def public_bep(path, key):
    report = {"completed": {}, "failures": []}
    if not path.is_file():
        return report
    roots = {}
    sets = {}
    with path.open() as events:
        for line in events:
            event = json.loads(line)
            identity = event["id"]
            if "finished" in event:
                report["exit"] = event["finished"]["exitCode"].get("code", 0)
            if "namedSet" in identity and "namedSetOfFiles" in event:
                sets[identity["namedSet"]["id"]] = event["namedSetOfFiles"]
            target = identity.get("targetCompleted", {})
            label = target.get("label")
            if label in targets(key) and not target.get("aspect") and "completed" in event:
                report["completed"][label] = {"built": bool(event["completed"].get("success")),
                                               "configuration": target.get("configuration", {}).get("id")}
                roots[label] = [x["id"] for g in event["completed"].get("outputGroup", [])
                                if g["name"] == "default" for x in g.get("fileSets", [])]
            action = event.get("action")
            if action and not action.get("success") and len(report["failures"]) < 6:
                tag = action.get("type", "")
                owner = identity.get("actionCompleted", {}).get("label", "")
                fail = {"mnemonic": tag if re.fullmatch(r"[A-Za-z0-9_]{1,60}", tag) else "other",
                        "owner": owner if re.fullmatch(r"(?:@@?[\w+~.-]+)?//[\w./+:-]{1,190}", owner) else "other",
                        "exit": action.get("exitCode")}
                for stream in ("stderr", "stdout"):
                    uri = action.get(stream, {}).get("uri", "")
                    match = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$", uri)
                    if match:
                        fail[stream] = [match[1], int(match[2])]
                report["failures"].append(fail)
    for label, ids in roots.items():
        files = set()
        seen = set()
        while ids:
            i = ids.pop()
            if i in seen:
                continue
            seen.add(i)
            group = sets.get(i, {})
            ids.extend(x["id"] for x in group.get("fileSets", []))
            for entry in group.get("files", []):
                name = entry.get("name", "")
                for arch, config in CONFIG.items():
                    for filename, (owner, _) in FORMATS.items():
                        suffix = "debian/release-artifacts/" + config + "/" + filename if key == "stage" else "debian/" + filename
                        if (key == "stage" or (key == arch and label == owner)) and (name == suffix or name.endswith("/" + suffix)):
                            files.add(arch + "/" + filename)
        report["completed"][label]["declarations"] = sorted(files)
    return report


def build(key):
    inv = str(uuid.uuid4())
    log, bep = D / (key + ".log"), D / (key + ".bep")
    row = {"invocation": inv, "source": SOURCE, "saved": []}
    save("build-" + key, row)
    limit = "65m" if key == "stage" else "35m"
    cmd = args(key, "build", True) + [
        "--keep_going", "--remote_download_outputs=toplevel", "--invocation_id=" + inv,
        "--symlink_prefix=" + str(D / (key + "-")), "--build_event_publish_all_actions",
        "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(D / (key + ".compact")),
        *targets(key)]
    with log.open("wb") as output:
        row["exit"] = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", limit, *cmd],
                                     stdout=output, stderr=subprocess.STDOUT).returncode
    row["terms"] = diagnostic(log)
    save("build-" + key, row)
    row["bep"] = public_bep(bep, key)
    save("build-" + key, row)
    root = D / (key + "-bin") / "debian"
    for arch in (CONFIG if key == "stage" else [key]):
        dest = D / "files" / key / arch
        dest.mkdir(parents=True, exist_ok=True)
        for filename, (owner, _) in FORMATS.items():
            target = STAGE if key == "stage" else owner
            if not row["bep"]["completed"].get(target, {}).get("built"):
                continue
            source = root / "release-artifacts" / CONFIG[arch] / filename if key == "stage" else root / filename
            if source.is_file():
                shutil.copyfile(source, dest / filename)
                row["saved"].append(arch + "/" + filename)
                save("build-" + key, row)


if __name__ == "__main__":
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    mode, key = sys.argv[1:]
    assert key in JOBS and mode in ("plan", "build")
    if mode == "plan":
        try:
            row = analyze(key)
        except (KeyError, OSError, ValueError, TypeError) as err:
            row = {"error": type(err).__name__, "verified": False}
        save("plan-" + key, row)
    else:
        plan = json.loads((D / ("plan-" + key + ".json")).read_text())
        if plan.get("verified"):
            build(key)
        else:
            save("build-" + key, {"not_executed": "unconfirmed_worker_route"})
