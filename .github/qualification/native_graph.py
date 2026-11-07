"""Compare TestRunner shards selected by three source-presubmit profiles."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback


SOURCE = "493a1a89937e667a41381dcceb66442333c5f309"
WORK = Path(os.environ["RUNNER_TEMP"], "native-graph")
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx8g"]
FLAGS = [
    "--jobs=4", "--keep_going", "--noshow_progress", "--color=no", "--curses=no",
    "--remote_executor=", "--remote_cache=", "--bes_backend=",
    "--spawn_strategy=local", "--strategy=TestRunner=local",
]
PROFILES = {
    "amd64": "source-presubmit",
    "arm64": "source-presubmit-arm64",
    "mixed": "source-presubmit-mixed",
}
LABEL = re.compile(r"//[\w+./-]*:[\w+./-]+")
CPU = {
    "//tools/bazeldefs:linux_amd64": "amd64",
    "//tools/bazeldefs:linux_arm64": "arm64",
    "//test/rbe:ubuntu_amd64": "amd64",
    "//test/rbe:ubuntu_arm64": "arm64",
}
SUFFIX = "_presubmit_arm64"
CONTROL = "//test/syscalls:uname_test_native"
NATIVE_FINGERPRINTS = {
    "amd64": "77a8f1cc0eaea106f3f2f32398e014b200ddd2f894d22cea98e12b3023ae28c2",
    "arm64": "4f2e280cecf376f60bc3f7a398d8df8289866aa42a67fdee0e04dbd93533846e",
}
DEFAULT_IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"


def save(name: str, value: object) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert len(data) < 2_000_000, (name, len(data))
    (WORK / "results" / (name + ".json")).write_text(data + "\n")


def invoke(mode: str, phase: str, command: str, args: list[str]) -> tuple[Path, Path, int]:
    prefix = f"{mode}-{phase}"
    output = WORK / (prefix + ".out")
    bep = WORK / (prefix + ".bep")
    log = WORK / (prefix + ".log")
    print(f"{mode}: {phase}", flush=True)
    with output.open("w") as out, log.open("w") as err:
        rc = subprocess.run([
            *BAZEL, command, "--config=" + PROFILES[mode], *FLAGS,
            "--build_event_json_file=" + str(bep), *args,
        ], stdout=out, stderr=err).returncode
    if rc:
        # The graph commands have a clean environment and disabled service endpoints.
        for line in log.read_text(errors="replace").splitlines():
            if line.startswith(("ERROR:", "FATAL:", "Caused by:")) and "Couldn't start the build" not in line:
                print(line[:500])
    return output, bep, rc


def selected(mode: str, tests_only: bool, *, fixture: bool = False, dump: Path | None = None) -> dict[str, tuple[str, str]]:
    args = ["--nobuild"]
    if fixture:
        patterns = WORK / "bits.targets"
        patterns.write_text("//pkg/bits:all\n")
        args.append("--target_pattern_file=" + str(patterns))
    if dump:
        args.extend(["--experimental_aquery_dump_after_build_format=streamed_proto",
                     "--experimental_aquery_dump_after_build_output_file=" + str(dump)])
    if tests_only:
        args.append("--build_tests_only")
    _, bep, rc = invoke(mode, "tests" if tests_only else "all", "test", args)
    configs = {}
    kinds = {}
    sizes = {}
    finish = []
    with bep.open() as stream:
        for line in stream:
            event = json.loads(line)
            ident = event["id"]
            assert not {"action", "testResult", "testSummary"}.intersection(event)
            if (target := ident.get("targetConfigured")) and not target.get("aspect") and "configured" in event:
                kinds[target["label"]] = event["configured"]["targetKind"]
                if size := event["configured"].get("testSize"):
                    sizes[target["label"]] = size
            if abort := event.get("aborted"):
                assert abort["reason"] in ("NO_BUILD", "SKIPPED"), abort["reason"]
                target = ident.get("targetCompleted")
                if abort["reason"] == "NO_BUILD" and target and not target.get("aspect"):
                    label = target["label"]
                    assert LABEL.fullmatch(label), label
                    checksum = target["configuration"]["id"]
                    assert configs.setdefault(label, checksum) == checksum, label
            if finished := event.get("finished"):
                finish.append(finished["exitCode"])
    control = "//pkg/bits:bits_test" if fixture else CONTROL
    assert rc == 1 and finish == [{"code": 1, "name": "BUILD_FAILURE"}] and control in configs, (rc, finish)
    result = {k: (v, kinds[k]) for k, v in configs.items() if kinds[k] != "test_suite rule"}
    if tests_only and not fixture:
        save(mode + "-sizes", {label: sizes[label] for label in result})
    return result


def target_set(owners: dict) -> str:
    return "set(" + " ".join(sorted(owners)) + ")"


def query_file(mode: str, phase: str, expression: str) -> str:
    path = WORK / f"{mode}-{phase}.query"
    path.write_text(expression + "\n")
    return "--query_file=" + str(path)


def release_products(mode: str, owners: dict) -> dict:
    # A mixed-only test may pull in a second //:release configuration as data.
    # Retain target-side release layouts throughout the actual selected closure.
    expr = 'filter("^(@@)?//:release$",deps(' + target_set(owners) + "))"
    formatter = WORK / "release.star"
    formatter.write_text('''
def format(target):
    opts = build_options(target)
    if opts.get("//command_line_option:is exec configuration", False):
        return ""
    return "\\t".join([str(opts["//command_line_option:cpu"]),
                       str(opts["//command_line_option:platforms"]),
                       "|".join(sorted([f.short_path for f in target.files.to_list()]))])
''')
    output, _, rc = invoke(mode, "release", "cquery", [
        "--output=starlark", "--starlark:file=" + str(formatter), query_file(mode, "release", expr),
    ])
    if rc:
        return {"exit": rc, "layouts": []}
    layouts = []
    for line in output.read_text().splitlines():
        if line:
            cpu, platforms, files = line.split("\t")
            layouts.append({"cpu": cpu, "platforms": platforms, "files": files.split("|")})
    return {"exit": rc, "direct_root": "//:release" in owners, "layouts": layouts}


@dataclass(frozen=True, order=True)
class Shard:
    owner: str
    arch: str
    run: int
    total_runs: int
    shard: int
    total: int


@dataclass(frozen=True)
class Placement:
    label: str
    rule: str
    config: str
    platform: str
    worker_arch: str
    isolation: str
    user: str
    ipv6: str
    execution_info: dict[str, str]


def test_log(action: dict, artifacts: dict, fragments: dict) -> list[str]:
    paths = []
    for output in action["outputIds"]:
        fragment = fragments[artifacts[str(output)]]
        if fragment["label"] != "test.log":
            continue
        parts = [fragment["label"]]
        while parent := fragment.get("parentId"):
            fragment = fragments[str(parent)]
            parts.append(fragment["label"])
        paths.append(parts)
    if not paths:
        return []
    path, = paths
    return path


def shard_key(owner: str, arch: str, label: str, reversed_path: list[str]) -> Shard:
    path = list(reversed(reversed_path))
    relative = path[path.index("testlogs") + 1:-1]
    label_path = label[2:].replace(":", "/").lstrip("/").split("/")
    assert relative[:len(label_path)] == label_path, (label, relative)
    dimensions = {}
    for part in relative[len(label_path):]:
        matches = list(re.finditer(r"(run|shard)_(\d+)_of_(\d+)", part))
        assert matches and "_".join(m[0] for m in matches) == part, (label, part)
        for match in matches:
            dimension, index, total = match.groups()
            assert dimension not in dimensions and 1 <= int(index) <= int(total), (label, part)
            dimensions[dimension] = int(index), int(total)
    return Shard(owner, arch, *dimensions.get("run", (1, 1)), *dimensions.get("shard", (1, 1)))


def varint(raw: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        byte = raw[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("invalid protobuf varint")


def fields(raw: bytes) -> dict:
    result = defaultdict(list)
    offset = 0
    while offset < len(raw):
        tag, offset = varint(raw, offset)
        if tag & 7 == 0:
            value, offset = varint(raw, offset)
        else:
            assert tag & 7 == 2, tag
            size, offset = varint(raw, offset)
            value = raw[offset:offset + size]
            assert len(value) == size
            offset += size
        result[tag >> 3].append(value)
    return result


def output_ids(values: list) -> list[int]:
    ids = []
    for value in values:
        if isinstance(value, int):
            ids.append(value)
        else:
            offset = 0
            while offset < len(value):
                index, offset = varint(value, offset)
                ids.append(index)
    return ids


def project_dump(path: Path) -> dict:
    # Bazel's analysis_v2.proto streamed_proto is length-delimited
    # ActionGraphContainers. Consume one message at a time, retaining only
    # the indexes needed to resolve TestRunner output paths and placement.
    graph = {name: {} for name in ("targets", "configs", "artifacts", "fragments")}
    graph["actions"] = []
    with path.open("rb") as stream:
        while prefix := stream.read(1):
            while prefix[-1] >= 128:
                b = stream.read(1)
                assert b and len(prefix) < 10
                prefix += b
            size, _ = varint(prefix, 0)
            raw = stream.read(size)
            assert len(raw) == size
            for table, rows in fields(raw).items():
                if table not in (1, 2, 3, 5, 8):
                    continue
                for row in rows:
                    item = fields(row)
                    index = str(item[1][0])
                    if table == 1:
                        graph["artifacts"][index] = str(item[2][0])
                    elif table == 3:
                        graph["targets"][index] = item[2][0].decode()
                    elif table == 5:
                        graph["configs"][index] = item[4][0].decode()
                    elif table == 8:
                        graph["fragments"][index] = {"label": item[2][0].decode(),
                                                     "parentId": (item[3] or [0])[0]}
                    elif item[4] == [b"TestRunner"]:
                        info = [fields(entry) for entry in item[11]]
                        graph["actions"].append({
                            "targetId": index, "configurationId": item[5][0], "mnemonic": "TestRunner",
                            "executionPlatform": (item[14] or [b""])[0].decode(), "outputIds": output_ids(item[9]),
                            "executionInfo": [{"key": v[1][0].decode(), "value": (v[2] or [b""])[0].decode()} for v in info],
                        })
    return graph


def inventory(mode: str, tests: dict, graph: dict, phase: str) -> dict[Shard, Placement]:
    targets = graph["targets"]
    configs = graph["configs"]
    artifacts = graph["artifacts"]
    fragments = graph["fragments"]
    records = {}
    auxiliary = Counter()
    unselected = 0
    for action in graph["actions"]:
        label = targets[str(action["targetId"])]
        assert action["mnemonic"] == "TestRunner", label
        config = configs[str(action["configurationId"])]
        if label not in tests or tests[label][0] != config:
            # The stored graph also contains TestRunners for tests built as
            # dependencies (and these can use another target configuration).
            unselected += 1
            continue
        if not (path := test_log(action, artifacts, fragments)):
            auxiliary[label] += 1
            continue
        _, rule = tests[label]
        owner = label.removesuffix(SUFFIX) if mode == "mixed" else label
        arch = ("arm64" if label.endswith(SUFFIX) else "amd64") if mode == "mixed" else mode
        platform = action["executionPlatform"].removeprefix("@@")
        properties = {r["key"]: r.get("value", "") for r in action.get("executionInfo", [])}
        key = shard_key(owner, arch, label, path)
        assert key not in records, key
        records[key] = Placement(label, rule, config, platform, properties.get("Arch", CPU.get(platform, "unknown")),
                                 properties.get("workload-isolation-type", "oci"), properties.get("dockerUser", ""),
                                 properties.get("network-enable-ipv6", ""), properties)
    missing = sorted(tests.keys() - {record.label for record in records.values()})
    save(phase + "-actions", {"source": SOURCE, "profile": PROFILES[mode], "auxiliary": auxiliary,
                             "unselected_actions": unselected, "selected_without_test_log": missing,
                             "shards": [{**asdict(k), **asdict(v)} for k, v in sorted(records.items())]})
    assert not missing, (mode, missing[:8])
    return records


def probe() -> bool:
    mode = "mixed"
    tests = selected(mode, True, fixture=True)
    expected = {"//pkg/bits:bits_test", "//pkg/bits:bits_test" + SUFFIX}
    assert tests.keys() == expected, tests
    report = {"source": SOURCE, "selected": tests}
    for name, args in [
        ("skyframe", ["--skyframe_state", 'mnemonic("^TestRunner$")']),
        ("ordinary", ['mnemonic("^TestRunner$", ' + target_set(tests) + ")"]),
    ]:
        output, _, rc = invoke(mode, name, "aquery", ["--output=jsonproto", "--noinclude_commandline", *args])
        data = json.loads(output.read_text()) if rc == 0 else {}
        labels = {str(i["id"]): i["label"] for i in data.get("targets", [])}
        checksums = {str(i["id"]): i["checksum"] for i in data.get("configuration", [])}
        pairs = {(labels[str(a["targetId"])], checksums[str(a["configurationId"])])
                 for a in data.get("actions", []) if a.get("mnemonic") == "TestRunner"}
        report[name] = {"exit": rc, "bytes": output.stat().st_size,
                        "tables": {key: len(items) for key, items in data.items()},
                        "test_configs": sorted(p for p in pairs if p[0] in expected)}
        if name == "ordinary":
            diffs = {}
            for label, observed in pairs:
                if label in tests and tests[label][0] != observed:
                    prior = tests[label][0]
                    control = subprocess.run([*BAZEL, "config", "--output=json", prior, observed],
                                             capture_output=True, text=True)
                    delta = json.loads(control.stdout) if control.returncode == 0 else {}
                    diffs[label] = {"exit": control.returncode, "option_names": {
                        fragment["name"]: sorted(fragment.get("optionsDiff", {}))
                        for fragment in delta.get("fragmentsDiff", [])
                    }}
            report[name]["config_differences"] = diffs
        save("control", report)
    path = WORK / "bits.pb"
    actual = selected(mode, True, fixture=True, dump=path)
    assert actual == tests, (actual, tests)
    graph = project_dump(path)
    report["in_command"] = {"bytes": path.stat().st_size, "tables": {k: len(v) for k, v in graph.items()}}
    save("control", report)
    rows = inventory(mode, tests, graph, "control")
    observed = {(k.owner, k.arch, k.run, k.total_runs, k.shard, k.total, v.worker_arch) for k, v in rows.items()}
    report["qualified"] = observed == {("//pkg/bits:bits_test", arch, 1, 1, 1, 1, arch) for arch in ("amd64", "arm64")}
    save("control", report)
    path.unlink()
    return report["qualified"]


def compare(native: dict[Shard, Placement], mixed: dict[Shard, Placement]) -> dict:
    def records(keys: set, graph: dict) -> list[dict]:
        return [{**asdict(k), **asdict(graph[k])} for k in sorted(keys)]

    changed = []
    for key in sorted(native.keys() & mixed.keys()):
        left, right = native[key], mixed[key]
        # Named native and mixed platform labels intentionally differ. Compare
        # their CPU and execution class, and retain raw labels in the evidence.
        defaults = {"Arch": key.arch, "OSFamily": "linux", "container-image": DEFAULT_IMAGE, "workload-isolation-type": "oci"}
        attrs = ("worker_arch", "isolation", "user", "ipv6")
        if (any(getattr(left, attr) != getattr(right, attr) for attr in attrs)
                or defaults | left.execution_info != right.execution_info):
            changed.append({"key": asdict(key), "native": asdict(left), "mixed": asdict(right)})
    return {"source": SOURCE, "native_shards": len(native), "mixed_shards": len(mixed),
            "missing": records(native.keys() - mixed.keys(), native),
            "extra": records(mixed.keys() - native.keys(), mixed), "changed": changed}


if __name__ == "__main__":
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    qualified = False
    try:
        qualified = probe()
    except Exception:
        traceback.print_exc()
    # Keep each native/mixed selection independently even when the optional
    # action projection is unavailable. An incomplete graph cannot pass.
    records = {}
    release_observed = dict.fromkeys(PROFILES, False)
    for mode in PROFILES:
        try:
            tests = selected(mode, True)
            save(mode + "-selection", {"source": SOURCE, "test_configurations_and_rules": tests})
            if qualified:
                path = WORK / (mode + ".pb")
                owners = selected(mode, False, dump=path)
                assert tests.items() <= owners.items()
                save(mode + "-roots", {"source": SOURCE, "profile": PROFILES[mode],
                                       "build_only": sorted(owners.keys() - tests.keys())})
                graph = project_dump(path)
                save(mode + "-dump", {"bytes": path.stat().st_size, "tables": {k: len(v) for k, v in graph.items()}})
                path.unlink()
                records[mode] = inventory(mode, tests, graph, mode)
                del graph
                release = release_products(mode, owners)
                save(mode + "-release", release)
                release_observed[mode] = bool(release["exit"] == 0 and release["direct_root"] and release["layouts"])
        except Exception:
            traceback.print_exc()
        finally:
            subprocess.run([*BAZEL, "shutdown"], check=False, timeout=120)
    assert records.keys() == PROFILES.keys(), sorted(records)
    result = compare(records["amd64"] | records["arm64"], records["mixed"])
    result["optional_release_layouts_observed"] = release_observed
    fingerprints = {}
    for mode in ("amd64", "arm64"):
        rows = sorted((k.owner, k.arch, k.run, k.total_runs, k.shard, k.total, v.worker_arch,
                       v.isolation, v.user, v.ipv6) for k, v in records[mode].items())
        fingerprints[mode] = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    sizes = {p: json.loads((WORK / "results" / (p + "-sizes.json")).read_text()) for p in PROFILES}
    mismatched_sizes = []
    for k, value in records["mixed"].items():
        if sizes[k.arch].get(k.owner) != sizes["mixed"].get(value.label):
            mismatched_sizes.append(value.label)
    result.update(native_fingerprints=fingerprints, mismatched_sizes=sorted(set(mismatched_sizes)))
    result["qualified"] = (fingerprints == NATIVE_FINGERPRINTS and result["native_shards"] == result["mixed_shards"] == 748
                           and not any(result[key] for key in ("missing", "extra", "changed")) and not mismatched_sizes)
    save("comparison", result)
    print("Native shards", result["native_shards"], "mixed", result["mixed_shards"])
    print("Missing", len(result["missing"]), "extra", len(result["extra"]), "changed", len(result["changed"]))
    assert result["qualified"], "source graph has not qualified for physical execution"
