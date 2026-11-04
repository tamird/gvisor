"""Compare the repository Make entry points with Docker unavailable."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import io
import shutil
import tarfile

BASE = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
PRODUCT = "b79cad51119968aff3e0027237e3d646d00b6a4c"
GOALS = ["list-all-images", "list-all-test-images", "list-cpu-images", "list-gpu-images"]


def variants(goal):
    return [(1,1), (1,2), (2,2)] if goal in GOALS[2:] else [(1,1)]


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    work = Path(os.environ["RUNNER_TEMP"]) / "host-image-inventory"
    work.mkdir()
    docker = work / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$INVENTORY_DOCKER_CALLS"\nexit 77\n')
    docker.chmod(0o755)
    calls = work / "docker-verbs"
    env = dict(os.environ, LC_ALL="C", PATH=str(work) + ":" + os.environ["PATH"], INVENTORY_DOCKER_CALLS=str(calls))
    verdict = {"base": BASE, "product": PRODUCT, "machine": platform.machine(), "cases": {}, "status": "INCOMPLETE"}
    expected = [f"{kind}/{arch}/{goal}/{part}-{total}" for kind in ("stock-explicit", "fixed-default")
                for arch in ("x86_64", "aarch64") for goal in GOALS for part,total in variants(goal)]
    expected += ["stock-default", "fixed-list-and-testlogs", "fixed-mixed-dry-run"]
    verdict["expected_cases"] = expected
    verdict["failed_cases"] = []
    baseline = {}
    current = ""

    def observe(name, args, dry=False):
        nonlocal current
        current = name
        calls.write_text("")
        row = {"execution_started_at": now()}
        verdict["cases"][name] = row
        try:
            proc = subprocess.run(["make", "-s", "--no-print-directory", *(["-n"] if dry else []), *args],
                                  capture_output=True, env=env, timeout=60)
        finally:
            row["execution_finished_at"] = now()
        verbs = calls.read_text().splitlines()
        known = {"version","image","inspect","exec","build","buildx","run","pull","tag","rm","commit","ps","info","create","load","manifest"}
        row.update({"exit": proc.returncode, "docker_calls": len(verbs), "docker_verbs": dict(Counter(v if v in known else "other" for v in verbs)),
               "stdout_sha256": hashlib.sha256(proc.stdout).hexdigest(), "stderr_sha256": hashlib.sha256(proc.stderr).hexdigest(),
               })
        if not dry and proc.returncode == 0:
            row["lines"] = len(proc.stdout.splitlines())
        return proc, verbs

    try:
        assert git("rev-parse", "HEAD^", "HEAD^^").splitlines() == [PRODUCT, BASE]
        assert git("diff", "--name-only", BASE, PRODUCT) == "Makefile\ntools/bazel.mk"
        assert git("rev-parse", PRODUCT + ":tools/images.mk") == git("rev-parse", BASE + ":tools/images.mk")
        for rev in (BASE, PRODUCT):
            git("checkout", "--quiet", "--detach", rev)
            assert git("rev-parse", "HEAD") == rev
            for arch in ("x86_64", "aarch64"):
                for goal in GOALS:
                    for part,total in variants(goal):
                        case = f"{arch}/{goal}/{part}-{total}"
                        opts = ["ARCH="+arch, f"PARTITION={part}", f"TOTAL_PARTITIONS={total}", goal]
                        args = ["DOCKER_BUILD=false", *opts] if rev == BASE else opts
                        name = ("stock-explicit/" if rev == BASE else "fixed-default/") + case
                        proc, verbs = observe(name, args)
                        assert proc.returncode == 0 and not verbs, name
                        names = proc.stdout.decode().splitlines()
                        assert names and all(re.fullmatch("[a-zA-Z0-9_.-]+", n) for n in names), name
                        if rev == BASE:
                            baseline[case] = proc.stdout
                        else:
                            assert proc.stdout == baseline[case], name
                    if goal in GOALS[2:]:
                        all_names, first, second = (Counter(baseline[f"{arch}/{goal}/{p}"].splitlines()) for p in ("1-1","1-2","2-2"))
                        assert first+second == all_names, (arch,goal)
            if rev == BASE:
                proc, verbs = observe("stock-default", ["ARCH=x86_64",GOALS[0]])
                assert "exec" in verbs and "version" in verbs, "stock Docker selection"
            else:
                proc, verbs = observe("fixed-list-and-testlogs", ["ARCH=x86_64",GOALS[0],"testlogs"])
                assert proc.returncode == 0 and not verbs and proc.stdout == baseline["x86_64/list-all-images/1-1"]
                _, verbs = observe("fixed-mixed-dry-run", [GOALS[0],"build","TARGETS=//runsc"], dry=True)
                assert "exec" in verbs and "version" in verbs, "mixed Docker selection"
        current = ""
        assert b"benchmarks_syscallbench" in baseline["x86_64/list-all-images/1-1"].splitlines()
        assert b"benchmarks_syscallbench" not in baseline["aarch64/list-all-images/1-1"].splitlines()
        assert set(verdict["cases"]) == set(expected)
        current = "dispatch"
        dispatch(work, verdict, baseline)
        assert list(verdict["dispatch"]) == verdict["dispatch_expected"]
        verdict["status"] = "PASS"
    except Exception as exc:
        verdict["failed_cases"] = [current] if current else []
        verdict["status"] = "FAIL"
        verdict["failure"] = type(exc).__name__
        verdict["location"] = []
        tb = exc.__traceback__
        while tb:
            if tb.tb_frame.f_code.co_filename == __file__:
                verdict["location"].append(tb.tb_lineno)
            tb = tb.tb_next
        raise
    finally:
        verdict["executed_cases"] = list(verdict["cases"])
        verdict["missing_cases"] = [name for name in expected if name not in verdict["cases"]]
        verdict["missing_dispatch"] = [k for k in verdict.get("dispatch_expected", []) if k not in verdict.get("dispatch", {})]
        verdict["execution_finished_at"] = max([row["execution_finished_at"] for row in verdict["cases"].values() if "execution_finished_at" in row] + [row["finished"] for row in verdict.get("dispatch", {}).values() if "finished" in row], default=None)
        verdict["verified_at"] = now()
        (work/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")


CONTEXTS = {
    "benchmarks_alpine": "//images:benchmark_alpine_context",
    "basic_alpine": "//images/basic:alpine_context",
    "basic_busybox": "//images/basic:busybox_context",
    "basic_fsstress": "//images/basic:fsstress_context",
    "basic_tmpfile": "//images/basic:tmpfile_context",
    "benchmarks_bun-spawn": "//images/benchmarks/bun-spawn:bun_spawn_context",
    "benchmarks_syscallbench": "//images/benchmarks/syscallbench:syscallbench_context",
    **dict.fromkeys(("gpu_cuda-tests", "gpu_cuda-tests-12-8", "gpu_ollama_client"), "//images:source_contexts"),
}

# Record only the external Docker/Bazel boundary. The Make recipes, recursive
# Make processes, copy, tar extraction and content hashing are real.
DOCKER = r'''#!/bin/bash
set -eu
event() { printf '%s\t%s\n' "$1" "${2:-}" >> "$TRACE"; }
case "$1" in
  exec)
    event probe
    while [[ "$1" != gvisor-bazel-* ]]; do shift; done
    shift
    if [[ "$1" == echo && "$2" == true ]]; then [[ "$MODE" != cold ]]; exit; fi
    [[ "$1" == bazel || "$1" == xargs ]]
    export IN_BUILDER=true
    exec "$@" ;;
  version) event version; echo 1.41 ;;
  run)
    if [[ "${2:-}" == --rm && "${3:-}" == gcr.io/go-containerregistry/crane && "${4:-}" == manifest ]]; then
      event manifest "$5"
      if [[ "$MODE" == real ]]; then
        [[ "$5" == 127.0.0.1:5007/check/* ]]
        exec "$REAL_DOCKER" run --network=host --rm "${@:3}"
      fi
      [[ "$MODE" != failed-image ]]
    elif [[ "$*" == *multiarch/qemu-user-static* ]]; then event register
    elif [[ "$MODE" == cold && "$*" == *gvisor.dev/images/* ]]; then event start
    else exit 81; fi ;;
  image) [[ "$MODE" == cold && "$2" == inspect ]]; event inspect ;;
  tag|load|build)
    if [[ "$1" == build ]]; then event docker-build; else event "$1"; fi
    if [[ "$MODE" == real ]]; then
      if [[ "$1" == build ]]; then [[ " $* " == *" --no-cache "* ]]; fi
      exec "$REAL_DOCKER" "$@"
    fi
    if [[ "$MODE" == failed-image && "$1" == build ]]; then exit 49; fi
    [[ "$MODE" == cold ]] ;;
  rm|commit) [[ "$MODE" == cold ]]; event "$1" ;;
  *) exit 82 ;;
esac
'''
BAZEL = r'''#!/bin/bash
set -eu
cmd=$1
shift
[[ "$cmd" == build || "$cmd" == cquery ]]
[[ "$*" == *"--config=$ARCH"* ]]
if [[ "$cmd" == build ]]; then
    target=${!#}
    [[ "$target" == //images* && "$target" != *" "* ]]
    race=default
    [[ "$*" != *"--@io_bazel_rules_go//go/config:race=false"* ]] || race=false
    printf 'build\t%s %s %s\n' "$target" "$race" "${IN_BUILDER:-false}" >> "$TRACE"
    if [[ "$MODE" == failed-copy ]]; then exit 47; fi
else
    printf 'cquery\t%s\n' "${IN_BUILDER:-false}" >> "$TRACE"
fi
if [[ "$MODE" == real ]]; then exec "$REAL_BAZEL" "$cmd" "$@"; fi
if [[ "$cmd" == cquery ]]; then printf '%s context.tar\n' "$CONTEXT_TAR"; fi
'''


def dispatch(work, verdict, baseline):
    results = verdict["dispatch"] = {}
    helper = work / "tools"
    helper.mkdir()
    real_docker = shutil.which("docker")
    real_bazel = str(Path(os.environ["RUNNER_TEMP"]) / "image-check-bazelisk")
    assert os.access(real_bazel, os.X_OK) and real_docker and platform.machine() == "x86_64"
    for name, program in (("docker", DOCKER), ("bazel", BAZEL)):
        (helper / name).write_text(program)
        (helper / name).chmod(0o755)
    trace = work / "route-events"
    archive = work / "route-context.tar"
    with tarfile.open(archive, "w") as tf:
        for image in CONTEXTS:
            name = "images/" + image.replace("_", "/") + "/Dockerfile"
            data = b"FROM scratch\n"
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tf.addfile(member, io.BytesIO(data))
    env = dict(os.environ, LC_ALL="C", PATH=str(helper) + ":" + os.environ["PATH"],
               TRACE=str(trace), CONTEXT_TAR=str(archive), REAL_BAZEL=real_bazel, REAL_DOCKER=real_docker)
    pipeline = Path(".buildkite/pipeline.yaml").read_text()
    hook = Path(".buildkite/hooks/pre-command").read_text()
    assert 'PARTITION=$((${PARTITION}+1))' in hook and 'export TOTAL_PARTITIONS=${BUILDKITE_PARALLEL_JOB_COUNT:-1}' in hook
    steps = re.findall(r'^    command: (make ARCH=(x86_64|aarch64) test-(cpu|gpu)-images)\n    parallelism: ([24])$', pipeline, re.M)
    assert [(a, k, int(n)) for _, a, k, n in steps] == [(a,k,n) for a,n in (("x86_64",4),("aarch64",2)) for k in ("cpu","gpu")]

    verdict["dispatch_expected"] = ["stock-cold-dockerfile-shard", *[f"{arch}/{kind}/{p}-{n}" for _,arch,kind,n in steps for p in range(1,int(n)+1)], "nested-false", "nested-true", "failed-copy", "failed-image", "real-mixed-miss", "real-mixed-hit"]

    def run(name, arch, goal, *args, mode="routes", part=1, total=1):
        trace.write_text("")
        row = results[name] = {"started": now()}
        p = subprocess.run(["make","--no-print-directory","ARCH="+arch,goal,*args], capture_output=True,
                           timeout=1500 if mode == "real" else 120,
                           env=dict(env, MODE=mode, ARCH=arch, PARTITION=str(part), TOTAL_PARTITIONS=str(total)))
        row.update(finished=now(), exit=p.returncode, stderr_sha256=hashlib.sha256(p.stderr).hexdigest())
        ev = [line.split("\t") for line in trace.read_text().splitlines()]
        counts = Counter(e[0] for e in ev)
        row["calls"] = dict(counts)
        row["diagnostics"] = [x for x in ("ERROR:", "Failed to fetch", "no such package", "MANIFEST_UNKNOWN",
            "connection refused", "Error 47", "Error 49", "pull rate limit") if x.lower() in p.stderr.decode(errors="replace").lower()]
        return p.returncode, ev, counts

    def check_routes(ev, names, arch, docker=True, prefix="dispatch.invalid/"):
        tags = [arg for op,arg in ev if op == "manifest"]
        actual = []
        for tag in tags:
            m = re.fullmatch(re.escape(prefix) + r'([a-zA-Z0-9./-]+)_' + arch + r':[0-9a-f]{16}', tag)
            assert m, "unexpected manifest"
            actual.append(m[1].replace("/","_"))
        assert actual == names, "manifest order/selection"
        builds = [(CONTEXTS[n], "false" if CONTEXTS[n] == "//images:source_contexts" else "default",
                   str(docker).lower()) for n in names if n in CONTEXTS]
        assert [v.split() for op,v in ev if op == "build"] == [list(b) for b in builds], "context producer/transport"
        assert [a for op,a in ev if op == "cquery"] == [str(docker).lower()] * len(builds)
        return builds

    prefix = "REMOTE_IMAGE_PREFIX=dispatch.invalid"
    git("checkout","--quiet","--detach",BASE)
    code, ev, ct = run("stock-cold-dockerfile-shard","x86_64","test-gpu-images",prefix,mode="cold",part=3,total=4)
    assert code == 0 and ct["start"] and ct["commit"] and ct["manifest"] == 3 and not ct["build"]
    git("checkout","--quiet","--detach",PRODUCT)
    for _, arch, kind, n in steps:
        total = int(n)
        full = sorted(baseline[f"{arch}/list-{kind}-images/1-1"].decode().splitlines())
        for part in range(1,total+1):
            names = list(dict.fromkeys(full[part-1::total]))
            key = f"{arch}/{kind}/{part}-{total}"
            code, ev, ct = run(key, arch, f"test-{kind}-images", prefix, part=part, total=total)
            builds = check_routes(ev, names, arch)
            assert code == 0 and not ct["start"] and not ct["commit"]
            if not builds:
                assert not ct["probe"] and not ct["version"]
            results[key].update(images=names, contexts=builds)
    sel = "SHARDED_CPU_IMAGES=basic_alpine basic_symlink-resolv"
    for mode in ("false","true"):
        code, ev, _ = run("nested-"+mode,"x86_64","test-cpu-images",sel,prefix,"DOCKER_BUILD="+mode)
        assert code == 0
        check_routes(ev, ["basic_alpine","basic_symlink-resolv"], "x86_64", docker=mode == "true")
    code, ev, ct = run("failed-copy","x86_64","test-cpu-images",sel,prefix,mode="failed-copy")
    assert code == 2 and ct["build"] == 1 and ct["manifest"] == ct["cquery"] == 0 and "Error 47" in results["failed-copy"]["diagnostics"]
    code, ev, ct = run("failed-image","x86_64","test-cpu-images","SHARDED_CPU_IMAGES=basic_symlink-resolv",prefix,mode="failed-image")
    assert code == 2 and ct["manifest"] == ct["docker-build"] == 1 and ct["tag"] == 0 and "Error 49" in results["failed-image"]["diagnostics"]
    real_mixed(real_docker, run, check_routes, results, sel)


def real_mixed(docker, run, check_routes, results, sel):
    local = "127.0.0.1:5007/check/"
    reg = "image-check-registry"
    def cli(*args):
        return subprocess.check_output([docker,*args], stderr=subprocess.STDOUT, timeout=150).decode().strip()
    cli("run","--detach","--publish","127.0.0.1:5007:5000","--name",reg,"registry:2.8.3")
    try:
        cli("pull","gcr.io/go-containerregistry/crane:latest")
        names = ["basic_alpine","basic_symlink-resolv"]
        for key in ("real-mixed-miss","real-mixed-hit"):
            code, ev, ct = run(key,"x86_64","test-cpu-images",sel,"DOCKER_BUILD=false",
                "REMOTE_IMAGE_PREFIX="+local.rstrip("/"),"DOCKER_BUILD_ARGS=--no-cache",mode="real")
            assert code == 0
            check_routes(ev,names,"x86_64",docker=False,prefix=local)
            assert ct["probe"] == ct["start"] == 0
            if key == "real-mixed-miss":
                assert ct["load"] == 1 and ct["docker-build"] == 1 and ct["tag"] >= 4
                ids = {}
                for n,tag in zip(names, [v for op,v in ev if op=="manifest"],strict=True):
                    assert tag.startswith(local)
                    image = cli("image","inspect","--format","{{.Id}}",tag)
                    assert image == cli("image","inspect","--format","{{.Id}}","gvisor.dev/images/"+n.replace("_","/")+":latest")
                    assert re.fullmatch(r"sha256:[a-f0-9]{64}",image)
                    cli("push",tag)
                    ids[n] = image
                results[key]["local_image_ids"] = ids
            else:
                assert not (ct["load"] or ct["docker-build"] or ct["tag"])
    finally:
        cli("rm","--force",reg)


if __name__ == "__main__":
    main()
