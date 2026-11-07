"""Compare TestRunner shards selected by three source-presubmit profiles."""

from collections import Counter
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess


SOURCE = "2e72c3051b3d7a13761f90961f4e94b8cccc8b11"
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
    if rc != (1 if command == "test" else 0):
        # The graph commands have a clean environment and disabled service endpoints.
        for line in log.read_text(errors="replace").splitlines():
            if line.startswith(("ERROR:", "FATAL:", "Caused by:")):
                print(line[:500])
    return output, bep, rc


def selected(mode: str, tests_only: bool) -> dict[str, tuple[str, str]]:
    args = ["--nobuild"]
    if tests_only:
        args.append("--build_tests_only")
    _, bep, rc = invoke(mode, "tests" if tests_only else "all", "test", args)
    configs = {}
    kinds = {}
    finish = []
    with bep.open() as stream:
        for line in stream:
            event = json.loads(line)
            ident = event["id"]
            assert not {"action", "testResult", "testSummary"}.intersection(event)
            if (target := ident.get("targetConfigured")) and not target.get("aspect") and "configured" in event:
                kinds[target["label"]] = event["configured"]["targetKind"]
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
    assert rc == 1 and finish == [{"code": 1, "name": "BUILD_FAILURE"}] and CONTROL in configs, (rc, finish)
    return {k: (v, kinds[k]) for k, v in configs.items() if kinds[k] != "test_suite rule"}


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


def inventory(mode: str) -> dict[Shard, Placement]:
    owners = selected(mode, False)
    tests = selected(mode, True)
    assert tests.items() <= owners.items()
    build_only = sorted(owners.keys() - tests.keys())
    save(mode + "-roots", {"source": SOURCE, "profile": PROFILES[mode], "build_only": build_only})
    layouts = release_products(mode, owners)
    save(mode + "-release", layouts)
    expr = 'mnemonic("^TestRunner$", ' + target_set(tests) + ")"
    raw, _, rc = invoke(mode, "actions", "aquery", [
        "--output=jsonproto", "--include_artifacts", "--noinclude_commandline", query_file(mode, "actions", expr),
    ])
    assert rc == 0, rc
    # Free the server's action graph before parsing the full artifact and path tables.
    subprocess.run([*BAZEL, "shutdown"], check=True, timeout=120)
    with raw.open() as stream:
        graph = json.load(stream)
    targets = {str(row["id"]): row["label"] for row in graph["targets"]}
    configs = {str(row["id"]): row["checksum"] for row in graph["configuration"]}
    artifacts = {str(row["id"]): str(row["pathFragmentId"]) for row in graph["artifacts"]}
    fragments = {str(row["id"]): row for row in graph["pathFragments"]}
    records = {}
    auxiliary = Counter()
    for action in graph["actions"]:
        label = targets[str(action["targetId"])]
        assert action["mnemonic"] == "TestRunner" and label in tests, label
        if not (path := test_log(action, artifacts, fragments)):
            auxiliary[label] += 1
            continue
        config, rule = tests[label]
        assert config == configs[str(action["configurationId"])], (label, config)
        owner = label.removesuffix(SUFFIX) if mode == "mixed" else label
        arch = ("arm64" if label.endswith(SUFFIX) else "amd64") if mode == "mixed" else mode
        platform = action["executionPlatform"].removeprefix("@@")
        properties = {r["key"]: r.get("value", "") for r in action.get("executionInfo", [])}
        key = shard_key(owner, arch, label, path)
        assert key not in records, key
        records[key] = Placement(label, rule, config, platform, properties.get("Arch", CPU.get(platform, "unknown")),
                                 properties.get("workload-isolation-type", "oci"), properties.get("dockerUser", ""),
                                 properties.get("network-enable-ipv6", ""), properties)
    assert {record.label for record in records.values()} == tests.keys()
    save(mode + "-actions", {"source": SOURCE, "profile": PROFILES[mode], "auxiliary": auxiliary,
                             "shards": [{**asdict(k), **asdict(v)} for k, v in sorted(records.items())]})
    raw.unlink()
    return records


def compare(native: dict[Shard, Placement], mixed: dict[Shard, Placement]) -> dict:
    def records(keys: set, graph: dict) -> list[dict]:
        return [{**asdict(k), **asdict(graph[k])} for k in sorted(keys)]

    changed = []
    for key in sorted(native.keys() & mixed.keys()):
        left, right = native[key], mixed[key]
        # Named native and mixed platform labels intentionally differ. Compare
        # their CPU and execution class, and retain raw labels in the evidence.
        attrs = ("worker_arch", "isolation", "user", "ipv6", "execution_info")
        if any(getattr(left, attr) != getattr(right, attr) for attr in attrs):
            changed.append({"key": asdict(key), "native": asdict(left), "mixed": asdict(right)})
    return {"source": SOURCE, "native_shards": len(native), "mixed_shards": len(mixed),
            "missing": records(native.keys() - mixed.keys(), native),
            "extra": records(mixed.keys() - native.keys(), mixed), "changed": changed}


if __name__ == "__main__":
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    amd64 = inventory("amd64")
    arm64 = inventory("arm64")
    mixed = inventory("mixed")
    result = compare(amd64 | arm64, mixed)
    save("comparison", result)
    print("Native shards", result["native_shards"], "mixed", result["mixed_shards"])
    print("Missing", len(result["missing"]), "extra", len(result["extra"]), "changed", len(result["changed"]))
