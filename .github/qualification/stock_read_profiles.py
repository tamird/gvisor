"""Profile short connected TCP reads and run the original packet-drain benchmark."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import zipfile
from urllib.parse import unquote, urlparse

ROOT = Path(os.environ["RUNNER_TEMP"]) / "tcp-read-profile"
OUT = ROOT / "results"
STOCK = "f51a205a8f4afbc5ab41aded070df74951b081ea"
PR = "e9abe5401c9fa5bc3c96738e52d406b0b1fbc15b"
TEARDOWN = "890afa390aa44699ecbe493699b5260ad50dbaba"
FIXTURE = "cd0b0af723f37718e8405c3db13b574db6a75ab33011006ec31eec7213d98735"
MICRO = "219841d1d9187ef7ec25532f4bcab8a7edb8293d064d58caec1eb9fba16252df"
GONET = Path("pkg/tcpip/adapters/gonet")
STACK = Path("pkg/tcpip/stack")
TEARDOWN_PATHS = [STACK / f for f in ("nic.go", "stack.go", "save_restore.go")]
TEMPORARY = [GONET / "qualification_tcp_read_test.go", GONET / "qualification_tcp_profile_test.go",
             STACK / "qualification_read_drain_test.go"]
BENCH_ROW = re.compile(r"^BenchmarkPacketDataReadTo/headers=(0|4)/fragments=(1|16)-2\s+\d+\s+.+ns/op\s+.+MB/s\s+.+B/op\s+.+allocs/op$")
PROFILE_TARGET = "//pkg/tcpip/adapters/gonet:qualification_tcp_profile"
MICRO_TARGET = "//pkg/tcpip/stack:qualification_read_drain"
PROFILE_RULE = '''
go_test(
    name = "qualification_tcp_profile",
    size = "medium",
    srcs = ["qualification_tcp_read_test.go", "qualification_tcp_profile_test.go"],
    embed = [":gonet"],
    deps = ["//pkg/tcpip", "//pkg/tcpip/header", "//pkg/tcpip/link/fdbased", "//pkg/tcpip/network/ipv4",
            "//pkg/tcpip/stack", "//pkg/tcpip/transport/tcp", "@org_golang_x_sys//unix:go_default_library",
            "@com_github_google_pprof//profile:go_default_library"],
)
'''
MICRO_RULE = '''
go_test(
    name = "qualification_read_drain",
    size = "small",
    srcs = ["qualification_read_drain_test.go"],
    embed = [":stack"],
    deps = ["//pkg/buffer", "//pkg/tcpip"],
)
'''


def git(*args):
    return subprocess.check_output(["git", *map(str, args)], text=True).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(state):
    (OUT / "result.json").write_text(json.dumps(state, sort_keys=True, indent=1) + "\n")


def prepare(source, body):
    git("restore", "--source=HEAD", "--worktree", "--", *TEARDOWN_PATHS, GONET / "BUILD", STACK / "BUILD")
    for path in TEMPORARY:
        path.unlink(missing_ok=True)
    git("checkout", "--quiet", "--detach", source)
    assert git("rev-parse", "HEAD") == source
    git("restore", "--source=" + TEARDOWN, "--worktree", "--", *TEARDOWN_PATHS)
    for path in TEARDOWN_PATHS:
        assert git("hash-object", path) == git("rev-parse", TEARDOWN + ":" + str(path))
    shutil.copyfile(ROOT / "stream_fixture.go", TEMPORARY[0])
    shutil.copyfile(ROOT / "stream_cpu_alloc.go", TEMPORARY[1])
    TEMPORARY[2].write_text('package stack\nimport ("fmt"; "io"; "testing"; "gvisor.dev/gvisor/pkg/buffer"; "gvisor.dev/gvisor/pkg/tcpip")\n' + body)
    for path, contents in ((GONET / "BUILD", PROFILE_RULE), (STACK / "BUILD", MICRO_RULE)):
        with path.open("a") as target:
            target.write(contents)
    changed = set(git("diff", "--name-only", "HEAD").splitlines())
    assert changed == set(map(str, [*TEARDOWN_PATHS, GONET / "BUILD", STACK / "BUILD"])), changed


def local_path(item):
    uri = urlparse(item.get("uri", ""))
    return Path(unquote(uri.path)) if uri.scheme == "file" else None


def preserve_profiles(name, tr, logpath, files):
    raw = {}
    archives = {logpath.parent / "test.outputs" / "outputs.zip"}
    for file in tr["testActionOutput"]:
        path = local_path(file)
        if path is None or not path.is_file():
            continue
        if path.name in files:
            raw[path.name] = path.read_bytes()
        if path.name == "outputs.zip":
            archives.add(path)
    for path in archives:
        if path.is_file():
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    base = Path(entry.filename).name
                    if base in files and entry.file_size < 4 << 20:
                        raw[base] = archive.read(entry)
    for base, digest in files.items():
        assert base in {"cpu.pprof", "before.pprof", "after.pprof"}
        if base not in raw:
            path = logpath.parent / "test.outputs" / base
            if path.is_file():
                raw[base] = path.read_bytes()
        if base in raw:
            assert len(raw[base]) < 4 << 20 and sha(raw[base]) == digest
            (OUT / (name + "-" + base)).write_bytes(raw[base])
    return sorted(raw)


def run(state, source, mode, pair, body):
    name = f"{mode}-{pair:02d}-{source[:9]}"
    prepare(source, body)
    owner = MICRO_TARGET if mode == "micro" else PROFILE_TARGET
    bep, console = ROOT / (name + ".bep"), ROOT / (name + ".console")
    extra = ["--test_arg=-test.run=^TestConnectedReadProfile$", "--test_env=TCP_READ_PROFILE=" + mode]
    if mode == "micro":
        extra = ["--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkPacketDataReadTo$", "--test_arg=-test.benchtime=250ms"]
    if mode == "alloc":
        extra += ["--test_env=GODEBUG=memprofilerate=16384"]
    args = [str(ROOT / "bazelisk"), "--nosystem_rc", "--nohome_rc", "test", "-c", "opt", "--config=x86_64",
            "--jobs=4", "--remote_executor=", "--remote_cache=", "--bes_backend=", "--lockfile_mode=off",
            "--test_env=GOMAXPROCS=2", "--test_env=GOGC=100", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--test_env=TCP_BENCH_CPUS=" + state["cpus"], "--run_under=/usr/bin/taskset -c " + state["cpus"],
            "--test_sharding_strategy=disabled", "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_timeout=180", "--test_arg=-test.timeout=150s", "--test_arg=-test.v", "--test_output=errors",
            "--zip_undeclared_test_outputs", "--build_event_json_file=" + str(bep), *extra, owner]
    row = {"case": name, "source": source, "mode": mode, "pair": pair}
    state["attempts"].append(row)
    save(state)
    with console.open("wb") as out:
        row["exit"] = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT).returncode
    if row["exit"]:
        lines = console.read_text(errors="replace").splitlines()
        first = next((i for i, text in enumerate(lines) if text.startswith("ERROR:") or "--- FAIL:" in text), max(0,len(lines)-15))
        row["diagnostics"] = [text[:450] for text in lines[first:first+22]]
    recs = []
    if bep.is_file():
        with bep.open() as events:
            for line in events:
                e = json.loads(line)
                if e.get("id", {}).get("testResult", {}).get("label") == owner and "testResult" in e:
                    recs.append(e["testResult"])
    assert len(recs) == 1, f"{name} produced {len(recs)} test results"
    tr, = recs
    row["test"] = tr.get("status")
    row["cached"] = bool(tr.get("cachedLocally") or tr.get("executionInfo", {}).get("cachedRemotely"))
    item, = [f for f in tr["testActionOutput"] if f["name"] == "test.log"]
    logpath = local_path(item)
    data = logpath.read_bytes()
    lines = data.decode(errors="replace").splitlines()
    row["log_sha256"] = sha(data)
    assert row["exit"] == 0 and row["test"] == "PASSED" and not row["cached"], name
    if mode == "micro":
        bench = [line.strip() for line in lines if BENCH_ROW.fullmatch(line.strip())]
        assert len(bench) == 4 and len({line.split()[0] for line in bench}) == 4, bench
        row["bench"] = bench
        with (OUT/source[:9]).open("a") as result:
            result.write("\n".join(bench)+"\n")
    else:
        parsed, = [json.loads(line.split("CONNECTED_PROFILE ", 1)[1]) for line in lines if "CONNECTED_PROFILE {" in line]
        assert parsed["Mode"] == mode and parsed["Bytes"] == 256 << 20 and parsed["Reads"] >= parsed["Bytes"] // 64
        assert parsed["Groups"]["all"]["Value"] > 0 and parsed["ProcessCPUNs"] > 0 and parsed["Mallocs"] > 0
        assert mode != "cpu" or parsed["Groups"]["read_total"]["Value"] > 0
        row["profile"] = parsed
        save(state)
        row["raw_files"] = preserve_profiles(name, tr, logpath, parsed["Files"])
    expected = {"cpu.pprof"} if mode == "cpu" else {"before.pprof", "after.pprof"}
    row["verified"] = (mode == "micro" or set(row["raw_files"]) == set(row["profile"]["Files"]) == expected)
    save(state)
    print("PASS" if row["verified"] else "MISSING RAW PROFILE", name, flush=True)


def main():
    assert git("rev-parse", "HEAD^") == PR and git("rev-parse", PR + "^") == git("rev-parse",TEARDOWN + "^") == STOCK
    assert sha((ROOT/"stream_fixture.go").read_bytes()) == FIXTURE
    testsource = subprocess.check_output(["git","show",PR+":pkg/tcpip/stack/packet_buffer_test.go"]).decode()
    body = testsource[testsource.index("func BenchmarkPacketDataReadTo("):]
    assert sha(body.encode()) == MICRO
    assert set(git("diff","--name-only",STOCK,PR).splitlines()) == {
        "pkg/tcpip/stack/packet_buffer.go","pkg/tcpip/stack/packet_buffer_test.go","pkg/buffer/buffer.go","pkg/buffer/buffer_test.go"}
    available = sorted(os.sched_getaffinity(0))
    assert len(available) >= 2 and platform.machine() == "x86_64"
    state = {"status":"INCOMPLETE", "source":git("rev-parse","HEAD"), "baseline":STOCK, "approved":PR,
             "teardown":TEARDOWN, "connected_fixture_sha256":FIXTURE, "microbenchmark_body_sha256":MICRO,
             "cpus":",".join(map(str,available[:2])), "cpu_model":next(line.split(":",1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")),
             "attempts":[]}
    save(state)
    for mode, pairs in (("cpu",4),("alloc",4),("micro",16)):
        for pair in range(1,pairs+1):
            failed = False
            for source in ((STOCK,PR) if pair%2 else (PR,STOCK)):
                try:
                    run(state,source,mode,pair,body)
                except (AssertionError, OSError, ValueError, subprocess.CalledProcessError) as err:
                    state.setdefault("errors",[]).append(f"{mode}/{source[:9]}: {err}"[:500])
                    failed = True
                    save(state)
            if failed:
                break
    state["status"] = "PASS" if len(state["attempts"])==48 and all(r.get("verified") for r in state["attempts"]) else "INCOMPLETE"
    save(state)
    if state["status"]!="PASS":
        raise SystemExit("one or more profile or packet-drain results are incomplete")


if __name__ == "__main__":
    main()
