"""Exercise the public libraries and inspect the actual Gazelle repositories."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE = Path.cwd()
BASE = "59b69962bd510505fa8538aa1c4dcc61f615edd3"
AREA = Path(os.environ["RUNNER_TEMP"]) / "go-deps-standalone"
OUT = AREA / "results"
BAZEL = str(AREA / "bazel")
PREFIX = "@@gazelle++go_deps+"
PROTO = PREFIX + "org_golang_google_protobuf//internal/detrand:rand.go"
POLICIES = {
    "org_golang_google_grpc": ("google.golang.org/grpc", "disable"),
    "com_github_containerd_containerd_v2": ("github.com/containerd/containerd/v2", "disable"),
    "com_github_containerd_cgroups_v3": ("github.com/containerd/cgroups/v3", "disable"),
    "com_github_containerd_errdefs": ("github.com/containerd/errdefs", "disable"),
    "com_github_containerd_log": ("github.com/containerd/log", "disable"),
    "com_github_googleapis_gax_go_v2": ("github.com/googleapis/gax-go/v2", "disable_global"),
    "com_github_googleapis_gnostic": ("github.com/googleapis/gnostic", "disable_global"),
    "io_k8s_api": ("k8s.io/api", "disable_global"),
    "io_k8s_apimachinery": ("k8s.io/apimachinery", "disable_global"),
}
EXPECTED = {module: ["gazelle:proto " + value] for module, value in POLICIES.values()}
REPORT = {"upstream": BASE, "rows": {}, "protobuf": {}, "actual_directives": {}}


def save():
    (OUT / "report.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def command(name, root, kind, *args):
    flags = ["--lockfile_mode=off", "--module_mirrors=https://bcr.cloudflaremirrors.com",
             "--color=no", "--curses=no", "--noshow_progress"]
    cmd = [BAZEL, "--nosystem_rc", "--nohome_rc", kind, *flags, *args]
    files = [OUT / (name + suffix) for suffix in (".stdout", ".stderr")]
    with files[0].open("w") as output, files[1].open("w") as errors:
        proc = subprocess.run(cmd, cwd=root, stdout=output, stderr=errors, timeout=720)
    log = files[1].read_text(errors="replace")
    diagnostics = [line for line in log.splitlines()
                   if line.startswith("ERROR:") or "in a non-root Bazel module" in line
                   or "have a tag class" in line]
    REPORT["rows"][name] = {"exit": proc.returncode, "args": [kind, *args],
                            "root": root.name, "first_diagnostics": diagnostics[:12]}
    save()
    print(name, proc.returncode, *diagnostics[:2], sep="\n", flush=True)
    return files[0].read_text() if proc.returncode == 0 else None


def protobuf(name, root):
    out = command(name + "_protobuf", root, "query", "--output=location", PROTO)
    if out is None:
        return
    loc = re.search(r"^(.+?):\d+:\d+: ", out, re.M)
    assert loc, out
    source = (root / loc[1]).with_name("rand.go")
    data = source.read_bytes()
    REPORT["protobuf"][name] = {"sha256": sha(data), "size": len(data),
        "reads_executable": b"os.Executable()" in data and b"os.Open(" in data,
        "disabled_seed": b"var randSeed uint64" in data}
    (OUT / (name + ".rand.go")).write_bytes(data)
    save()


def directives(name, root):
    out = command(name + "_repository_specs", root, "mod", "show_repo",
                  *(PREFIX + repo for repo in POLICIES))
    observed = {}
    if out is not None:
        for call in re.findall(r"(?ms)^go_repository\(\s*$(.*?)^\)", out):
            attrs = {key: ast.literal_eval(value) for key, value in
                     re.findall(r'(?m)^\s*(importpath|build_directives) = (.*),$', call)}
            if "importpath" in attrs:
                observed[attrs["importpath"]] = attrs.get("build_directives", [])
    # If the mod command also needs an unrelated extension, materialize just
    # these repositories and read the directives Gazelle writes to the BUILDs.
    for repo, (module, _) in POLICIES.items():
        if module in observed:
            continue
        name_root = name + "_" + repo
        listing = command(name_root, root, "query", "--output=location", PREFIX + repo + "//:*")
        if listing is None:
            continue
        matches = re.findall(r"^(.+?/(?:BUILD|BUILD.bazel)):\d+:\d+: ", listing, re.M)
        if matches:
            file = root / matches[0]
            directives = re.findall(r"^# (gazelle:proto \S+)$", file.read_text(), re.M)
            observed[module] = directives
    REPORT["actual_directives"][name] = observed
    save()


def assertions():
    pb = REPORT["protobuf"]
    assert pb["stock_root"] == pb["fixed_root"] and pb["fixed_root"]["disabled_seed"], pb
    assert pb["fixed_consumer"]["reads_executable"] and not pb["fixed_consumer"]["disabled_seed"], pb
    assert pb["fixed_root"]["sha256"] != pb["fixed_consumer"]["sha256"], pb
    assert "stock_consumer" not in pb, pb
    stock = (OUT / "stock_consumer_protobuf.stderr").read_text()
    assert 'tag in a non-root Bazel module' in stock, stock
    for case in ("stock_root", "fixed_root"):
        values = REPORT["actual_directives"][case]
        assert set(values) == set(EXPECTED), values
        assert all(policy[0] in values[module] for module, policy in EXPECTED.items()), values
    assert REPORT["actual_directives"]["stock_root"] == REPORT["actual_directives"]["fixed_root"]
    for case in ("stock", "fixed"):
        for name in ("seqnum", "waiter"):
            row = REPORT["rows"][f"{case}_{name}"]
            if row["exit"]:
                assert any(re.search("ERROR:|Error in fail", line)
                           for line in row["first_diagnostics"]), row
                assert not re.search("out of memory|timed out|403 Forbidden|PKIX|Failed to download",
                                     (OUT / (f"{case}_{name}.stderr")).read_text(), re.I), row


def main():
    patch = AREA / "go_deps.patch"
    assert sha(patch.read_bytes()) == "ebf90e3fb764a452561231145ff285d79877b41a7c332e4269c785832b18d2a1"
    module = SOURCE / "MODULE.bazel"
    consumer = AREA / "consumer"
    consumer.mkdir()
    text = ('module(name="consumer")\nbazel_dep(name="gvisor",version="0.0.1")\n'
            f'local_path_override(module_name="gvisor",path={json.dumps(str(SOURCE))})\n')
    (consumer / "MODULE.bazel").write_text(text)
    (consumer / "BUILD.bazel").write_text("")
    (OUT / "consumer.MODULE.bazel").write_text(text)
    for case in ("stock", "fixed"):
        if case == "fixed":
            subprocess.run(["git", "apply", "--check", str(patch)], check=True)
            subprocess.run(["git", "apply", str(patch)], check=True)
            assert subprocess.check_output(["git", "diff", "--name-only"], text=True).splitlines() == ["MODULE.bazel"]
        (OUT / (case + ".MODULE.bazel")).write_bytes(module.read_bytes())
        REPORT[case + "_module_blob"] = subprocess.check_output(["git", "hash-object", "MODULE.bazel"], text=True).strip()
        for name, label in (("seqnum", "@gvisor//pkg/tcpip/seqnum:seqnum"), ("waiter", "@gvisor//pkg/waiter:waiter")):
            output = command(case + "_" + name, consumer, "build", "--jobs=4", "--show_result=2", label)
            if output is not None:
                files = command(case + "_" + name + "_files", consumer, "cquery", "--output=files", label)
                root = Path(subprocess.check_output([BAZEL, "info", "execution_root"], cwd=consumer, text=True).strip())
                outputs = {path: sha((root / path).read_bytes()) for path in files.splitlines()}
                assert outputs, label
                REPORT["rows"][case + "_" + name]["outputs"] = outputs
        protobuf(case + "_consumer", consumer)
        protobuf(case + "_root", SOURCE)
        directives(case + "_root", SOURCE)
    assertions()
    print("Confirmed separate Gazelle evaluations and all nine root directive sets.", flush=True)
    save()


if __name__ == "__main__":
    main()
