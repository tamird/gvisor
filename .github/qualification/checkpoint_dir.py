"""Verify the existing checkpoint reader on a real checkpoint with Docker absent."""
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import public_assets as assets
import tldk_public_inputs as inputs

BASE = "451b92800b9cb82262dc8a735388f7cf4d17d895"
FIX = "cba5d1b3f0a94c5ac55c10fc8f29af4fd8d6978e"
PREVIOUS = "0bd1d5810f10b22eb30389046a8139f265c5a475"
CARRIER = os.environ["GITHUB_SHA"]
PACKAGE = Path("runsc/container")
BENCH = "//test/benchmarks/checkpointfile:checkpointfile_test"
APP = "//test/cmd/test_app:test_app"
OWNER = "//runsc/container:checkpoint_dir_without_docker"
WORK = Path(os.environ["RUNNER_TEMP"]) / "checkpoint-dir"
DEADLINE = time.monotonic() + 72 * 60
DIRS = {assets.DIST}
RECORD = {"base": BASE, "product": FIX, "carrier": CARRIER, "previous_artifact": 13615413609,
          "previous_zip_sha256": "c1f656b5f70b9af4335419ebbc4afbcef481a91d173596d035fcdcc43e733f06",
          "public_inputs": {}, "phases": {}}
KNOWN = {}


def save():
    (WORK / "receipt.json").write_text(json.dumps(RECORD, indent=2) + "\n")


def sha(path):
    with path.open("rb") as f:
        digest = hashlib.sha256()
        for part in iter(lambda: f.read(1 << 20), b""):
            digest.update(part)
    return digest.hexdigest()


def git(*args):
    return subprocess.check_output(["git", *map(str, args)])


def recover(log):
    pairs = re.findall(r"Error downloading \[(https://github[.]com/[^\]\s,]+)\] to ([^\r\n ]+)", log)
    direct = dict(re.findall(r'^bazel_dep\(name = "([\w.-]+)", version = "([\w.+-]+)"', Path("MODULE.bazel").read_text(), re.M))
    before = set(DIRS)
    for url, dest in sorted(set(pairs)):
        if url in RECORD["public_inputs"] or len(RECORD["public_inputs"]) >= 64:
            continue
        room = inputs.LIMIT - sum(e.get("bytes", 0) for e in RECORD["public_inputs"].values())
        try:
            if url in KNOWN:
                directory, row = inputs.materialize(*KNOWN[url], room)
            elif (archive := inputs.ARCHIVE.fullmatch(url)) and (owner := assets.DIR.search(dest)):
                module = owner[1]
                tag = archive[3].split("/")[-1].removeprefix("v")
                directory, row = inputs.materialize(module, direct.get(module, tag), url, None, None, None, room)
            else:
                directory, row = assets.release_distfile(url, assets.DIST, remaining_bytes=room)
        except (AssertionError, ValueError, OSError, KeyError) as exc:
            row = {"unavailable": str(exc)[:180]}
        else:
            DIRS.add(directory)
        RECORD["public_inputs"][url] = row
    return before != DIRS


def bazel(phase, command, *args):
    row = RECORD["phases"][phase] = {"command": command, "args": list(args), "attempts": []}
    for attempt in range(10):
        remaining = DEADLINE - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("checkpoint qualification time budget")
        bep = WORK / (phase + ".bep")
        bep.unlink(missing_ok=True)
        argv = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", command,
                "--config=x86_64", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=", "--bes_backend=",
                "--lockfile_mode=off", "--spawn_strategy=processwrapper-sandbox,local",
                "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
                *["--distdir=" + str(d) for d in sorted(DIRS)], "--build_event_json_file=" + str(bep), *args]
        with (WORK / (phase + ".log")).open("w+") as log:
            save()
            result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=min(remaining, 3600))
            log.seek(0)
            output = log.read()
        if result.returncode == 0:
            row["exit"] = 0
            save()
            return output, bep
        row["attempts"].append({"exit": result.returncode, "errors": [s[:450] for s in output.splitlines() if s.startswith("ERROR:") or "Error downloading" in s][:8]})
        save()
        if command == "test" or not recover(output):
            raise RuntimeError("Bazel failed during " + phase)
    raise RuntimeError("public input retry limit")


def default(phase, target):
    text, _ = bazel(phase, "cquery", "--output=files", target)
    paths = [Path(s) for s in text.splitlines() if s.startswith("bazel-out/") and Path(s).is_file()]
    return paths


def preserve_failure(bep):
    if not bep.exists():
        return
    for e in (json.loads(s) for s in bep.read_text().splitlines()):
        if e["id"].get("testResult", {}).get("label") != OWNER or "testResult" not in e:
            continue
        RECORD["failed_test"] = {"status": e["testResult"].get("status"), "configuration": e["id"]["testResult"]["configuration"]["id"]}
        for out in e["testResult"].get("testActionOutput", []):
            uri = urlsplit(out.get("uri", ""))
            if out["name"] in ("test.log", "test.xml") and uri.scheme == "file" and not uri.netloc:
                path = Path(unquote(uri.path))
                if path.is_file():
                    shutil.copy2(path, WORK / out["name"])
                    if out["name"] == "test.log":
                        lines = path.read_text(errors="replace").splitlines()
                        RECORD["failed_test"]["observations"] = [
                            s[:350] for s in lines if s.startswith(("CHECKPOINT_CONTROL ", "VERIFIED_", "--- PASS: TestCheckpointFileRoundTrip ",
                                                                 "BenchmarkCheckpointFile/EncodeStatefile")) or "State file sizes: original " in s
                        ][:24]


def test_result():
    _, bep = bazel("native", "test", "--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1",
                   "--test_sharding_strategy=disabled", "--test_output=errors", OWNER)
    events = [json.loads(s) for s in bep.read_text().splitlines()]
    entry, = [e for e in events if "testResult" in e]
    key, result = entry["id"]["testResult"], entry["testResult"]
    info = result["executionInfo"]
    assert key["label"] == OWNER and result["status"] == "PASSED" and not result.get("cachedLocally")
    assert not info.get("cachedRemotely") and info["strategy"] == "local"
    assert any(e.get("completed", {}).get("success") and e["id"].get("targetCompleted", {}).get("label") == OWNER
               and e["id"]["targetCompleted"]["configuration"] == key["configuration"] for e in events)
    record = {"owner": OWNER, "configuration": key["configuration"]["id"], "status": result["status"],
              "strategy": info["strategy"], "outputs": {}, "invocation": next(e["started"]["uuid"] for e in events if "started" in e)}
    RECORD["physical"] = record
    for output in result["testActionOutput"]:
        name = output["name"]
        if name not in ("test.log", "test.xml"):
            continue
        uri = urlsplit(output["uri"])
        assert uri.scheme == "file" and not uri.netloc
        path = Path(unquote(uri.path))
        record["outputs"][name] = sha(path)
        if name == "test.xml":
            root = ET.parse(path).getroot()
            assert root.findall(".//testcase") and all(not root.findall(".//" + tag) for tag in ("failure", "error", "skipped"))
        else:
            text = path.read_text(errors="replace")
            markers = ["NATIVE_GUEST_PRIVATE_ROOT", "VERIFIED_REAL_CAPTURE", "VERIFIED_REAL_RESTORE", "VERIFIED_HELP_BOTH",
                       "VERIFIED_DOCKER_ERROR stock-directory", "VERIFIED_DOCKER_ERROR fixed-live", "VERIFIED_CHECKPOINT_DIRECTORY"]
            assert all(text.splitlines().count(s) == 1 for s in markers)
            controls = [s for s in text.splitlines() if s.startswith("CHECKPOINT_CONTROL ")]
            assert controls == [f"CHECKPOINT_CONTROL {arm} PASS" for arm in ("help", "stock-directory", "fixed-live", "fixed-directory")]
            assert text.splitlines().index(controls[-1]) < text.splitlines().index("VERIFIED_REAL_RESTORE")
            sizes, = re.findall(r'State file sizes: original (\d+), re-encoded \[(\d+) (\d+) (\d+)\]', text)
            row, = [s for s in text.splitlines() if re.match(r'^BenchmarkCheckpointFile/EncodeStatefile(?:-\d+)?\s+1\s', s)]
            assert all(int(n) > 0 for n in sizes) and re.search(r'\s[1-9]\d*\s+encoded-kernel-B', row)
            assert len(re.findall(r'(?m)^--- PASS: TestCheckpointFileRoundTrip ', text)) == 1
            media = dict((name, digest) for digest, name in re.findall(r'(?m)^([0-9a-f]{64})  (checkpoint.img|pages_meta.img|pages.img|spec.json)$', text))
            assert len(media) == 4
            assert all(re.search(r'^' + digest + r'  .+$', text, re.M) for digest in RECORD["binaries"].values())
            record.update(markers=markers, sizes=[int(n) for n in sizes], media=media, benchmark=row,
                          docker_errors=[s[-380:] for s in text.splitlines() if "executable file not found" in s and "docker version" in s])
            assert len(record["docker_errors"]) == 2
        shutil.copy2(path, WORK / name)
    assert set(record["outputs"]) == {"test.log", "test.xml"}


def main():
    assert platform.machine() == "x86_64" and os.getuid() != 0
    assert git("rev-parse", CARRIER + "^").decode().strip() == PREVIOUS
    assert git("rev-parse", PREVIOUS + "^").decode().strip() == FIX
    assert git("rev-parse", FIX + "^{tree}").decode().strip() == "6e81d267bbee82ffe68f23ee2e5724c47863686e"
    assert git("rev-parse", FIX + "^").decode().strip() == BASE
    allowed = {".github/workflows/build.yml", ".github/qualification/checkpoint_dir.py", "runsc/container/BUILD", "runsc/container/checkpoint_dir.sh"}
    assert set(git("diff", "--name-only", FIX, CARRIER).decode().splitlines()) == allowed
    assets.DIST.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((WORK / "inputs.json").read_bytes())
    seeds = [v for v in manifest if v[0] is not None and v[0] != "llvm"]
    pgv = ["protoc-gen-validate", "1.3.0", "https://github.com/bufbuild/protoc-gen-validate/archive/refs/tags/v1.3.0.tar.gz",
           "26dc6db523ee943af51fdfc292658a2dbe7046735654f1dc839745988f0fe26b", 190514, None]
    coral = [None, None, "https://github.com/google-coral/crosstool/archive/8e885509123395299bed6a5f9529fdc1b9751599.tar.gz",
             "f86d488ca353c5ee99187579fe408adb73e9f2bb1d69c6e3a42ffb904ce3ba01", 17312, None]
    for declaration in (coral[2], coral[3]):
        assert declaration in Path("tools/bazeldefs/extensions/coral_crosstool.bzl").read_text()
    KNOWN.update((v[2], v) for v in [*manifest, pgv, coral])
    RECORD["unavailable_seeds"] = []
    for directory, row in inputs.seed([*seeds, pgv, coral]):
        if directory:
            DIRS.add(directory)
            RECORD["public_inputs"][row["source"]] = row
        else:
            RECORD["unavailable_seeds"].append(row)
    git("checkout", "-q", "--detach", BASE)
    bazel("stock-build", "build", BENCH)
    stock, = default("stock-default", BENCH)
    stock_sha = sha(stock)
    shutil.copy2(stock, WORK / "stock")
    git("checkout", "-q", "--detach", FIX)
    for path in (PACKAGE / "BUILD", PACKAGE / "checkpoint_dir.sh"):
        path.write_bytes(git("show", CARRIER + ":" + str(path)))
    (PACKAGE / "checkpoint_dir.sh").chmod(0o755)
    shutil.copy2(WORK / "stock", PACKAGE / "checkpoint_dir_stock")
    shutil.copy2(WORK / "busybox", PACKAGE / "checkpoint_dir_busybox")
    # Reuse the proven non-root namespace initializer, choosing its guest command.
    tree = ast.parse((WORK / "runtime_guest.py").read_text())
    launch, = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "launch"]
    setup, = [n.value.value for n in launch.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
              and any(isinstance(t, ast.Name) and t.id == "setup" for t in n.targets)]
    assert setup.count("COMMAND") == 1
    (PACKAGE / "checkpoint_dir_namespace.sh").write_text(setup.replace("COMMAND", "exec /busybox sh /case.sh guest"))
    bazel("candidate-build", "build", OWNER)
    candidate, = default("candidate-default", BENCH)
    app, = default("app-default", APP)
    release = default("release-default", "//:release")
    runsc, = [p for p in release if p.as_posix().endswith("/release/runsc")]
    sentry, = [p for p in release if p.as_posix().endswith("/release/gvisor-bin/gvisor_sentry")]
    RECORD["binaries"] = {"stock": stock_sha, "candidate": sha(candidate), "test_app": sha(app), "runsc": sha(runsc), "sentry": sha(sentry)}
    assert RECORD["binaries"]["stock"] != RECORD["binaries"]["candidate"]
    save()
    test_result()
    RECORD["verified"] = True


try:
    main()
finally:
    if not RECORD.get("verified"):
        preserve_failure(WORK / "native.bep")
    save()
