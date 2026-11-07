"""Follow the declared action inputs of the files exported by //:release."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

W = Path(os.environ["RUNNER_TEMP"]) / "release-graph"
sys.path.insert(0, str(W))
from native_graph import fields, output_ids, varint

SOURCE = "9e1db6e550d181ff2c691a5e8df8412b74316415"
BAZEL = [str(W / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx6g"]
PLATFORM = "//test/rbe/release_prewarmer:amd64"
IMAGE = "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"
FOCUS = {"//pkg/unet:unet_state_autogen", "//pkg/marshal/primitive:primitive_state_autogen", "//pkg/bpf:bpf_state_autogen",
         "//pkg/sentry/platform/systrap/sysmsg:sighandler_binary", "//pkg/sentry/platform/systrap/sysmsg:sighandler_go",
         "//pkg/sentry/platform/kvm:mmapfilter_precompiled_gen", "//:release",
         "protobuf+//bazel/private/oss/toolchains/prebuilt:authenticity_validation",
         "rules_go++go_sdk+main___download_0_linux_amd64//:builder"}


def get(row, num):
    return (row[num] or [b""])[0].decode()


def interpreter(header):
    if not header.startswith(b"#!"):
        return ""
    first = header.split(b"\n", 1)[0].removeprefix(b"#!").strip().split()
    if not first:
        return ""
    known = {b"/bin/sh", b"/bin/bash", b"/usr/bin/env", b"/usr/bin/python3", b"/bin/dash", b"/usr/bin/bash"}
    if first[0] not in known:
        return "other-interpreter"
    if first[0] == b"/usr/bin/env" and len(first) > 1 and first[1] in (b"bash", b"sh", b"python3", b"python"):
        return " ".join(s.decode() for s in first[:2])
    return first[0].decode()


@dataclass
class Action:
    label: int
    config: int
    mnemonic: str
    key: str
    platform: str
    worker_pin: bool
    exe: str
    header: str
    inputs: list
    outputs: list


def read_graph(path):
    labels, configs, fragments, artifacts, deps, producers = {}, {}, {}, {}, {}, defaultdict(list)
    actions = []
    with path.open("rb") as stream:
        while prefix := stream.read(1):
            while prefix[-1] & 128:
                chunk = stream.read(1)
                assert chunk and len(prefix) < 10
                prefix += chunk
            size, _ = varint(prefix, 0)
            assert 0 < size <= 150 << 20
            raw = stream.read(size)
            assert len(raw) == size
            for kind, entries in fields(raw).items():
                for entry in entries:
                    if kind not in (1, 2, 3, 4, 5, 8):
                        continue
                    r = fields(entry)
                    if kind == 1:
                        artifacts[r[1][0]] = r[2][0]
                    elif kind == 3:
                        labels[r[1][0]] = get(r, 2)
                    elif kind == 4:
                        deps[r[1][0]] = output_ids(r[2]), output_ids(r[3])
                    elif kind == 5:
                        configs[r[1][0]] = {"cpu": get(r, 3), "id": get(r, 4)}
                    elif kind == 8:
                        fragments[r[1][0]] = get(r, 2), (r[3] or [0])[0]
                    else:
                        props = {get(kv, 1): get(kv, 2) for kv in (fields(v) for v in r[11])}
                        outs = output_ids(r[9])
                        for out in outs:
                            producers[out].append(len(actions))
                        header = (r[17] or r[15] or [b""])[0][:240]
                        actions.append(Action(r[1][0], (r[5] or [0])[0], get(r, 4), get(r, 3), get(r, 14),
                                              props.get("Arch") == "amd64" and props.get("container-image") == IMAGE
                                              and props.get("workload-isolation-type") == "oci",
                                              get(r, 6), interpreter(header), output_ids(r[8]), outs))
    return labels, configs, fragments, artifacts, deps, producers, actions


def project(dump, release, base, arch):
    labels, configs, fragments, artifacts, deps, producers, actions = read_graph(dump)
    def artifact_path(ident):
        parts = []
        frag = artifacts[ident]
        while frag:
            name, frag = fragments[frag]
            parts.append(name)
        return "/".join(reversed(parts))

    paths = {ident: artifact_path(ident) for ident in artifacts}
    by_path = defaultdict(list)
    for ident in producers:
        by_path[paths[ident]].append(ident)
    stack = []
    for p in release:
        ids = [ident for ident in by_path[p] if any(labels[actions[i].label].lstrip("@") == "//:release"
               and configs[actions[i].config]["cpu"] == {"amd64": "k8", "arm64": "aarch64"}[arch] for i in producers[ident])]
        assert len(ids) == 1
        stack.append((ids[0], None))
    dep_stack, visited_artifacts, visited_sets, links = [], set(), set(), {}
    ambiguities = set()
    while stack or dep_stack:
        if stack:
            ident, parent = stack.pop()
            if ident in visited_artifacts:
                continue
            visited_artifacts.add(ident)
            choices = producers.get(ident, [])
            if len({(actions[i].key, actions[i].mnemonic, actions[i].label) for i in choices}) > 1:
                ambiguities.add(ident)
            for i in choices:
                if i not in links:
                    links[i] = parent
                    dep_stack.extend((j, i) for j in actions[i].inputs)
        else:
            ident, owner = dep_stack.pop()
            if ident in visited_sets:
                continue
            visited_sets.add(ident)
            children, files = deps[ident]
            dep_stack.extend((child, owner) for child in children)
            stack.extend((f, owner) for f in files)

    def exe_kind(a):
        exe = a.exe
        if not exe:
            return "Bazel-internal", ""
        if Path(exe).is_absolute():
            return "absolute-executable", exe if exe in ("/bin/sh", "/bin/bash", "/usr/bin/env", "/usr/bin/python3") else "other"
        if exe in by_path:
            headers = {actions[i].header for ident in by_path[exe] for i in producers[ident] if actions[i].header}
            return ("generated-script", "|".join(sorted(headers))) if headers else ("generated-executable", "")
        path = Path(base) / exe if exe.startswith("external/") else Path(exe)
        if ".." not in path.parts and path.is_file():
            with path.open("rb") as src:
                first = src.read(240)
            shell = interpreter(first)
            if shell:
                return "source-script", shell
            if first[:4] == b"\x7fELF":
                return "source-elf", ""
        return "other-executable", ""

    counts, hazards, examples, focus, cache = Counter(), defaultdict(list), {}, [], {}
    worker_mismatches = Counter()
    for i, parent in links.items():
        a = actions[i]
        owner = labels[a.label]
        counts[a.mnemonic] += 1
        if a.exe and (a.platform.lstrip("@") != PLATFORM or not a.worker_pin):
            worker_mismatches[a.mnemonic] += 1
        if a.exe not in cache:
            cache[a.exe] = exe_kind(a)
        category, shell = cache[a.exe]
        key = "|".join((a.mnemonic, category, shell))
        goal = owner.lstrip("@") in FOCUS
        if shell:
            hazards[key].append((owner, configs.get(a.config, {}).get("id"), a.key))
        if not goal and (not shell or key in examples):
            continue
        row = {"owner": owner, "action": a.mnemonic, "key": a.key, "config": configs.get(a.config), "platform": a.platform,
               "worker_pin": a.worker_pin, "class": category, "interpreter": shell,
               "executable": a.exe if re.fullmatch(r"[\w@./+~=-]{0,350}", a.exe) else "other",
               "outputs": [paths[o] for o in a.outputs[:4]], "consumers": []}
        while parent is not None and len(row["consumers"]) < 12:
            p = actions[parent]
            row["consumers"].append([labels[p.label], p.mnemonic])
            parent = links[parent]
        if goal:
            focus.append(row)
        if shell and key not in examples:
            examples[key] = row
    return {"release": release, "actions_in_deps_query": len(actions), "reachable": len(links),
            "ambiguous_outputs": len(ambiguities), "mnemonics": dict(counts), "worker_mismatches": dict(worker_mismatches),
            "interpreter_groups": {k: {"actions": len(v), "owners": len({i[0] for i in v}),
                                        "digest": hashlib.sha256(json.dumps(sorted(v)).encode()).hexdigest(),
                                        "example": examples[k]} for k,v in hazards.items()}, "focus": focus}


def run(arch, row):
    flags = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch], "-c", "opt", "--jobs=4", "--lockfile_mode=off",
             "--host_platform=" + PLATFORM, "--extra_execution_platforms=" + PLATFORM,
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--remote_executor=", "--remote_cache=", "--bes_backend=", "--color=no", "--curses=no", "--noshow_progress"]
    files, dump = W / (arch + ".files"), W / (arch + ".pb")
    for cmd, options, destination in [("cquery", ["--output=files", "//:release"], files),
                                      ("aquery", ["--output=streamed_proto", "--include_commandline", "--include_artifacts",
                                                   "--include_file_write_contents", "deps(//:release)"], dump)]:
        stderr = W / (arch + "." + cmd + ".err")
        with destination.open("wb") as out, stderr.open("wb") as err:
            rc = subprocess.run([*BAZEL, cmd, *flags, *options], stdout=out, stderr=err, timeout=650).returncode
        row[cmd] = rc
        if rc:
            log = stderr.read_text(errors="replace").lower()
            row["signals"] = [s for s in ("unrecognized option", "no such target", "no such package", "no matching toolchains",
                                          "no space left", "outofmemory", "error downloading", "failed to fetch", "patch")
                              if s in log]
            return
    roots = sorted(files.read_text().splitlines())
    row["root_files"] = roots
    assert roots and len(roots) == len(set(roots)) and all(re.fullmatch(r"bazel-out/[\w+/-]+/bin/release/[\w/-]+", s) for s in roots)
    assert any(p.endswith("/release/runsc") for p in roots) and any(p.endswith("/containerd-shim-runsc-v1") for p in roots)
    base = subprocess.check_output([*BAZEL, "info", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=",
                                    "--bes_backend=", "output_base"], text=True, stderr=subprocess.DEVNULL).strip()
    row.update(project(dump, roots, base, arch))
    row["complete"] = row["reachable"] > 0 and row["mnemonics"].get("GoToolchainBinaryBuild", 0) > 0 and not row["ambiguous_outputs"]


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    result = []
    for arch in ("amd64", "arm64"):
        row = {"source": SOURCE, "target": arch, "executor": "amd64", "phase": "analysis", "complete": False}
        try:
            run(arch, row)
        except (OSError, KeyError, ValueError, AssertionError, IndexError, subprocess.SubprocessError) as error:
            import traceback
            row["observer_error"] = [type(error).__name__, traceback.extract_tb(error.__traceback__)[-1].lineno]
        finally:
            (W / (arch + ".pb")).unlink(missing_ok=True)
        out = json.dumps(row, indent=2)
        assert len(out) < 1000000 and not re.search(r"(?i)authorization\s*[:=]|x-buildbuddy-api-key|github_pat_", out)
        (W / "receipt" / (arch + ".json")).write_text(out + "\n")
        print(arch, row["complete"], row.get("reachable"), flush=True)
        result.append(row["complete"])
    return all(result)


if __name__ == "__main__":
    raise SystemExit(not main())
