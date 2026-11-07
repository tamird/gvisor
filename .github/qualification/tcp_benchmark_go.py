"""Qualify the real Go benchmark and its process/aggregate failure handling."""

from decimal import Decimal
import gzip
import io
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile

WORK = Path(os.environ.get("RUNNER_TEMP", ".")) / "tcp-benchmark-go"
PUBLIC = WORK / "public"
SIBLINGS = WORK / "standalone executables"
BAZEL = str(WORK / "bazelisk")
PKG = "//test/benchmarks/tcp:"
OPTIONS = ["--config=x86_64", "--jobs=4", "--lockfile_mode=off"]
SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
SOURCE = "b58c1ff588fe543d296ca5810e4033eb13345767"
PARENT = "9c541c3ae1cd68df1741882480d5bf184396e695"
NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
SUMMARY = re.compile(r"^BenchmarkTCP/role=(client|server)/host-gso=(true|false)/host-gro=(true|false) 1 +("
                     + NUMBER + r") Mb/s +(" + NUMBER + r") cpu-time$", re.M)

# The four error controls replace only the explicitly configured iperf
# executable. Every successful transfer uses the actual installed program.
CONTROL = '''#!/usr/bin/python3
import json, os, pathlib, sys
folder = pathlib.Path(__file__).parent
mode = folder.name
args = sys.argv[1:]
role = "client" if "-c" in args else "server" if "-s" in args else "other"
(folder / (role + "-argv.json")).write_text(json.dumps(args))
if role == "server" and mode == "server-exit":
    print("INTENTIONAL_SERVER_EXIT", file=sys.stderr)
    sys.exit(11)
if role != "client":
    os.execv("/usr/bin/iperf3", ["/usr/bin/iperf3", *args])
if mode == "server-exit":
    print("UNEXPECTED_CLIENT", file=sys.stderr)
    sys.exit(12)
if mode == "client-error":
    print("INTENTIONAL_CLIENT_ERROR", file=sys.stderr)
    sys.exit(7)
if mode == "client-signal":
    print("INTENTIONAL_CLIENT_SIGNAL", file=sys.stderr, flush=True)
    import signal
    os.kill(os.getpid(), signal.SIGTERM)
sample = {"bits_per_second":16800000, "bytes":63000000, "seconds":30}
result = {"end":{"sum_received":sample, "streams":[]}}
if mode == "no-sum":
    result["end"]["streams"] = [{"sender":sample, "receiver":sample}] * 2
print(json.dumps(result))
'''


def read(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def persist(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def need(value, reason):
    if not value:
        raise ValueError(reason)


def collect_transfer(row, out, err, flags):
    records = SUMMARY.findall(out)
    need(row["status"] == 0 and len(records) == 1, "transfer or summary")
    role, hgso, hgro, mbits, cpu = records[0]
    want_role = "client" if "--client" in flags or "--linux-client" in flags else "server"
    need((role, hgso, hgro) == (want_role, str("--disable-linux-gso" not in flags).lower(),
                                str("--disable-linux-gro" not in flags).lower()), "benchmark role or offload")
    need(Decimal(mbits) > 0 and Decimal(cpu) >= 0, "nonpositive transfer or negative CPU")
    row["summary"] = {"role":role, "host_gso":hgso, "host_gro":hgro, "mbps":mbits, "cpu":cpu}
    want_ready = [(str("--client" in flags).lower(), "false"), ("false", str("--server" in flags).lower())]
    need(sorted(row["ready"]) == sorted(want_ready), "actual proxy roles")
    want_stats = {s for s in ("client", "server") if "--" + s in flags}
    need(set(row["stats"]) == want_stats, "netstack statistics roles")
    results = []
    for mark in re.finditer(r'\{\s*"start"\s*:', err):
        try:
            obj, _ = json.JSONDecoder().raw_decode(err[mark.start():])
        except ValueError:
            continue
        if (isinstance(obj.get("start"), dict) and "test_start" in obj["start"] and
                isinstance(obj.get("end"), dict) and "sum_sent" in obj["end"]):
            results.append(obj)
    need(len(results) == 1, "unique original client iperf JSON")
    obj, = results
    start = obj["start"]
    end = obj["end"]
    clients = start["connected"]
    sent, received = end["sum_sent"], end["sum_received"]
    row["iperf"] = {"sent":sent, "received":received, "streams":start["test_start"]["num_streams"],
                    "duration":start["test_start"]["duration"],
                    "address_families":[ipaddress.ip_address(c["remote_host"]).version for c in clients]}
    need(len(clients) == row["iperf"]["streams"] == row["streams"], "stream count")
    need(row["iperf"]["address_families"] == [6 if "--ipv6" in flags else 4] * len(clients), "IP family")
    need(row["iperf"]["duration"] == row["duration"], "duration")
    for phase in (sent, received):
        need(phase["bytes"] > 0 and phase["seconds"] >= row["duration"] - 0.5 and phase["bits_per_second"] > 0, "short/empty transfer")
    # The driver prints the full float calculated from iperf's final sum.
    need(float(mbits) == sent["bits_per_second"] / 1e6, "aggregate rate mismatch")


def arm(name, mode_flags, *, duration=4, streams=1, standalone=False, helpers=False, profile=False, control=False, ordinary=False):
    case = WORK / "inputs with spaces" / name
    case.mkdir(parents=True)
    row = {"name": name, "duration": duration, "streams": streams, "standalone": standalone,
           "explicit_helpers": helpers, "control": control, "verified": False}
    flags = [*mode_flags, "--verbose", "--duration", str(duration)]
    if ordinary:
        need(os.geteuid() != 0, "default case requires ordinary worker UID")
        probe = subprocess.run(["/usr/bin/unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork",
                                "--mount-proc", "/bin/sh", "-ec", "echo entered-default-namespace >&2; unshare --net /bin/true; ip link add probe0 type veth peer name probe1"],
                               capture_output=True, text=True)
        row["namespace_probe"] = {"uid":os.geteuid(), "exit":probe.returncode,"stderr":probe.stderr.strip()[:600]}
    else:
        flags.append("--no-user-ns")
    if streams != 1:
        flags += ["--num-client-threads", str(streams)]
    if helpers:
        flags += ["--helpers", str(SIBLINGS)]
    output_profile = case / "client cpu.pprof"
    if profile:
        flags += ["--cpuprofile", str(output_profile)]
    if control:
        fake = case / "iperf substitute"
        fake.write_text(CONTROL)
        fake.chmod(0o755)
        flags += ["--iperf-binary", str(fake)]
    try:
        if standalone:
            # A bare PATH invocation, from outside the installed directory,
            # crosses the Go driver's self-exec with no injected Bazel flags.
            path = str(SIBLINGS) + ":" + SYSTEM_PATH
            command = ["tcp_benchmark", *flags]
        else:
            path = SYSTEM_PATH
            launcher = WORK / (name + ".run")
            subprocess.run([BAZEL, "run", *OPTIONS, "--script_path=" + str(launcher),
                            PKG + "tcp_benchmark", "--", *flags], check=True)
            command = [str(launcher)]
        as_user = [] if ordinary else ["sudo", "-n"]
        home = os.environ["HOME"] if ordinary else "/root"
        stdout, stderr = case / "stdout", case / "stderr"
        with stdout.open("wb") as output, stderr.open("wb") as errors:
            run = subprocess.run([*as_user, "timeout", "-k", "10", "115", "env", "-i",
                                  "HOME=" + home, "LANG=C", "PATH=" + path, *command],
                                 cwd=WORK, stdout=output, stderr=errors)
        row["status"] = run.returncode
        for label, path in (("stdout", stdout), ("stderr", stderr)):
            blob = path.read_bytes()
            row[label] = {"sha256": sha(blob), "bytes": len(blob)}
            if len(blob) <= 4 << 20:
                (PUBLIC / (name + "." + label + ".log")).write_bytes(blob)
        out, err = stdout.read_text(errors="replace"), stderr.read_text(errors="replace")
        row["ready"] = re.findall(r"client=(true|false), server=(true|false), ready\.", err)
        row["stats"] = re.findall(r"netstack (\S+) Stats:", err)
        if ordinary:
            rejected = ("operation not permitted", "permission denied")
            actual = [line for line in err.splitlines() if any(x in line.lower() for x in rejected)]
            probe = row["namespace_probe"]
            msg = probe["stderr"].lower()
            same_stage = (
                ("unshare failed" in msg and "entered-default-namespace" not in msg and
                 any("fork/exec /proc/self/exe" in line for line in actual)) or
                ("unshare failed" in msg and "entered-default-namespace" in msg and
                 any("network namespace" in line for line in actual)) or
                ("mount" in msg and "proc" in msg and any("mount private proc" in line for line in actual)) or
                ("rtnetlink" in msg and any("rtnetlink" in line.lower() for line in actual))
            )
            row["matched_namespace_denial"] = (run.returncode != 0 and probe["exit"] != 0 and
                                              any(x in msg for x in rejected) and same_stage and "BenchmarkTCP/" not in out)
            if actual:
                row["permission_errors"] = actual[:3]
        if control:
            if name != "server-exit":
                need(sorted(row["ready"]) == [("false", "true"), ("true", "false")], "dual proxy roles")
            seen = {}
            for role in ("client", "server"):
                argv = case / (role + "-argv.json")
                if argv.is_file():
                    seen[role] = json.loads(argv.read_text())
            row["iperf_args"] = seen
            need("server" in seen and "BenchmarkTCP/" not in out, "control did not invoke server or emitted a benchmark")
            if name == "server-exit":
                need(run.returncode == 1 and "client" not in seen and "INTENTIONAL_SERVER_EXIT" in err, "early service exit")
            elif name == "client-signal":
                need(run.returncode == 143 and "client" in seen and "INTENTIONAL_CLIENT_SIGNAL" in err, "signaled iperf exit")
            elif name == "client-error":
                need(run.returncode == 7 and "client" in seen and "INTENTIONAL_CLIENT_ERROR" in err, "iperf client exit")
            else:
                need(run.returncode == 1 and "client" in seen and "iperf did not report an aggregate sender rate" in err,
                     "expected missing aggregate diagnostic")
                need("-J" in seen["client"] and (name != "no-sum" or seen["client"][seen["client"].index("-P")+1] == "2"), "control arguments")
        else:
            collect_transfer(row, out, err, flags)
        if profile:
            blob = output_profile.read_bytes()
            need(len(gzip.decompress(blob)) > 0, "empty CPU profile")
            row["profile"] = {"bytes":len(blob), "sha256":sha(blob)}
        row["verified"] = True
    except (OSError, EOFError, ValueError, ArithmeticError, KeyError, TypeError, subprocess.SubprocessError) as e:
        row["error"] = str(e)[:450]
    persist(PUBLIC / (name + ".json"), row)
    print(name, row.get("status"), row["verified"], row.get("error", ""), flush=True)
    return row["verified"]


def interruption():
    # The shell records its own PID and becomes the real public executable.
    # timeout bounds a broken cleanup; the test signal goes only to that PID.
    folder = WORK / "interrupt inputs"
    folder.mkdir()
    pidfile = folder / "outer.pid"
    profile = folder / "interrupted client.pprof"
    driver = SIBLINGS / "tcp_benchmark"
    cmd = ["sudo", "-n", "timeout", "-k", "5", "50", "env", "-i", "HOME=/root",
           "LANG=C", "PATH=" + SYSTEM_PATH, "/bin/sh", "-c", 'echo "$$" > "$1"; shift; exec "$@"',
           "record-pid", str(pidfile), str(driver), "--no-user-ns", "--client", "--verbose",
           "--duration", "30", "--cpuprofile", str(profile)]
    stdout, stderr = (folder / name for name in ("stdout", "stderr"))
    row = {"verified":False}
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            with subprocess.Popen(cmd, cwd=WORK, stdout=out, stderr=err) as run:
                deadline = time.monotonic() + 24
                while time.monotonic() < deadline and run.poll() is None:
                    # Two proxies both see the control connection. The third
                    # event shows iperf has also opened its data connection.
                    if stderr.read_bytes().count(b"incoming connection established.") >= 3:
                        break
                    time.sleep(0.1)
                row["connections_before_signal"] = stderr.read_bytes().count(b"incoming connection established.")
                need(run.poll() is None and row["connections_before_signal"] >= 3, "no active transfer")
                pid = int(pidfile.read_text())
                need(read("sudo", "-n", "readlink", f"/proc/{pid}/exe") == str(driver), "signal target")
                row["outer_verified"] = True
                subprocess.run(["sudo", "-n", "kill", "-TERM", str(pid)], check=True)
                row["status"] = run.wait(timeout=22)
        need(row["status"] > 0 and row["status"] not in (124, 137), "no orderly nonzero exit")
        need(b"BenchmarkTCP/" not in stdout.read_bytes(), "interrupted transfer reported success")
        blob = profile.read_bytes()
        need(len(gzip.decompress(blob)) > 0, "interrupted profile was not flushed")
        row["profile"] = {"bytes":len(blob), "sha256":sha(blob)}
        row["verified"] = True
    except (OSError, ValueError, subprocess.SubprocessError, EOFError) as exc:
        row["error"] = str(exc)[:450]
    for label, path in (("stdout", stdout), ("stderr", stderr)):
        if path.is_file():
            data = path.read_bytes()
            row[label] = {"bytes":len(data), "sha256":sha(data)}
            if len(data) <= 4 << 20:
                (PUBLIC / ("interrupted." + label + ".log")).write_bytes(data)
    persist(PUBLIC / "interrupted.json", row)
    print("interrupted", row, flush=True)
    return row["verified"]


def shell_verdict(status, out, err):
    errors = [line for line in err.splitlines() if
              ("standalone:" in line and ("No such file or directory" in line or "command not found" in line)) or
              ("export:" in line and "executables:" in line and "not a valid identifier" in line)]
    if status == 0 and SUMMARY.findall(out):
        cause = "reported transfer"
    elif status not in (0, 124, 137) and errors:
        cause = "split PATH export" if "not a valid identifier" in errors[0] else "split helper path"
    else:
        cause = "inconclusive"
    return {"execution":cause, "path_errors":errors[:6]}


def shell_reference():
    # Both historical helper rules have identical source inputs. Install the
    # original driver itself alongside the same two already-built helpers.
    src = "test/benchmarks/tcp/tcp_benchmark.sh"
    old = SIBLINGS / "tcp_benchmark.sh"
    old.write_bytes(subprocess.check_output(["git", "show", PARENT + ":" + src]))
    old.chmod(0o755)
    cmd = ["sudo", "-n", "timeout", "-k", "10", "115", "env", "-i", "HOME=/root", "LANG=C",
           "PATH=" + str(SIBLINGS) + ":" + SYSTEM_PATH, old.name, "--server", "--no-user-ns", "--verbose", "--duration", "4"]
    run = subprocess.run(cmd, cwd=WORK, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    row = {"source":PARENT, "script_sha256":sha(old.read_bytes()), "status":run.returncode}
    for label, data in (("stdout", run.stdout), ("stderr", run.stderr)):
        row[label] = {"sha256":sha(data), "bytes":len(data)}
        if len(data) <= 4 << 20:
            (PUBLIC / ("historical-installed." + label + ".log")).write_bytes(data)
    out, err = run.stdout.decode(errors="replace"), run.stderr.decode(errors="replace")
    row["benchmarks"] = SUMMARY.findall(out)
    row.update(shell_verdict(run.returncode, out, err))
    persist(PUBLIC / "historical-installed.json", row)
    return row


def main():
    report = {"source": read("git", "rev-parse", "HEAD"), "parent": read("git", "rev-parse", "HEAD^"),
              "machine": platform.machine(), "cases": {}, "verified":False,
              "reference":{"run":42372178933, "artifact":13574098943,
                           "sha256":"4351d698c3e9bd0bbde206a69a4f8fee04d1580100078f3d8f4135aa916b7d2e"}}
    persist(PUBLIC / "result.json", report)
    need((report["source"], report["parent"], report["machine"]) == (SOURCE, PARENT, "x86_64"), "source or architecture")
    subprocess.run([BAZEL, "build", *OPTIONS, PKG + "tcp_benchmark"], check=True)
    SIBLINGS.mkdir()
    report["binaries"] = {}
    for name in ("tcp_benchmark", "nsjoin", "tcp_proxy"):
        path = Path(read(BAZEL, "cquery", *OPTIONS, "--output=files", PKG + name)).resolve(strict=True)
        installed = SIBLINGS / name
        shutil.copy2(path, installed)
        blob = installed.read_bytes()
        need(blob[:4] == b"\x7fELF" and int.from_bytes(blob[18:20], "little") == 62, "installed ELF")
        report["binaries"][name] = {"size":len(blob), "sha256":sha(blob)}
    for source in ("test/benchmarks/tcp/nsjoin.c", "test/benchmarks/tcp/tcp_proxy.go"):
        need(read("git", "rev-parse", SOURCE + ":" + source) == read("git", "rev-parse", PARENT + ":" + source), "historical helper source")
    report["historical_installed"] = shell_reference()
    persist(PUBLIC / "result.json", report)
    dual = ["--client", "--server"]
    cases = [("canonical", dual, {"duration":30}), ("dual-parallel", dual, {"duration":30, "streams":2}),
             ("client-ipv6-paths", ["--client", "--ipv6"], {"helpers":True,"profile":True}),
             ("standalone-server", ["--server"], {"standalone":True}),
             ("native-client", ["--linux-client", "--disable-linux-gso", "--disable-linux-gro"], {})]
    for mode in ("server-exit", "client-error", "client-signal", "no-sender", "no-sum"):
        cases.append((mode, dual, {"control":True, "streams":2 if mode == "no-sum" else 1}))
    for name, flags, options in cases:
        report["cases"][name] = arm(name, flags, **options)
        persist(PUBLIC / "result.json", report)
    report["cases"]["interrupted"] = interruption()
    default_pass = arm("default-userns", ["--client"], ordinary=True)
    default = json.loads((PUBLIC / "default-userns.json").read_text())
    report["default_userns"] = {"workload_passed":default_pass, "uid":default["namespace_probe"]["uid"],
                                "matched_host_denial":default.get("matched_namespace_denial", False)}
    report["all_modes_passed"] = all(report["cases"].values()) and default_pass
    report["verified"] = all(report["cases"].values()) and (default_pass or report["default_userns"]["matched_host_denial"])
    persist(PUBLIC / "result.json", report)
    return report["verified"]


def read_original(data):
    digest = "8fdd05aa47061e01d12bd044ca4b4f5020c912fc5c6922a43c937800a514a9d2"
    need(sha(data) == digest, "original GitHub ZIP digest")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        files = {name: z.read(name) for name in z.namelist()}
    original = json.loads(files["result.json"])
    need(original["source"] == SOURCE and original["parent"] == PARENT and original["machine"] == "x86_64", "original source")
    archive = "https://github.com/tamird/gvisor/actions/runs/42374729637/artifacts/13574933620"
    receipt = {"source":SOURCE, "parent":PARENT, "qualifier":"c578f92bbf3368ec8f249d37da93703766753614",
               "archive":archive, "archive_sha256":digest, "original_workflow":"failure",
               "original_observer_error":"Nested iperf intervals contain numeric start/end and were counted alongside the complete client report.",
               "machine":original["machine"], "transfers":{}, "controls":{}}

    def streams(name):
        row = json.loads(files[name + ".json"])
        original_sha = {}
        text = {}
        for key in ("stdout", "stderr"):
            content = files[name + "." + key + ".log"]
            original_sha[key] = sha(content)
            need(original_sha[key] == row[key]["sha256"] and len(content) == row[key]["bytes"], name + " " + key)
            text[key] = content.decode(errors="replace")
        return row, text["stdout"], text["stderr"], original_sha

    modes = {"canonical":["--client","--server"], "dual-parallel":["--client","--server"],
             "client-ipv6-paths":["--client","--ipv6"], "standalone-server":["--server"],
             "native-client":["--linux-client","--disable-linux-gso","--disable-linux-gro"],
             "default-userns":["--client"]}
    for name, flags in modes.items():
        row, out, err, hashes = streams(name)
        need(row["error"] == "unique original client iperf JSON", name + " original verdict")
        need(row["duration"] == (30 if name in ("canonical","dual-parallel") else 4), name + " seconds")
        need(row["streams"] == (2 if name == "dual-parallel" else 1), name + " stream count")
        row["ready"] = re.findall(r"client=(true|false), server=(true|false), ready\.", err)
        row["stats"] = re.findall(r"netstack (\S+) Stats:", err)
        collect_transfer(row, out, err, flags)
        result = receipt["transfers"][name] = {"status":row["status"], "duration_seconds":row["duration"],
                   "streams":row["streams"], "ip_versions":sorted(set(row["iperf"]["address_families"])),
                   "netstack_roles":sorted(set(row["stats"])), "sender_bytes":row["iperf"]["sent"]["bytes"],
                   "receiver_bytes":row["iperf"]["received"]["bytes"], "summary":row["summary"], "sha256":hashes}
        if name == "client-ipv6-paths":
            need("client cpu.pprof" in err, "spaced profile path did not reach proxy")
            result["profile"] = {"sha256":None, "note":"The path reached the proxy. The original observer stopped before reading the file."}
        if name == "standalone-server":
            need(row["standalone"] and not row["explicit_helpers"], "standalone sibling lookup")
        if name == "default-userns":
            probe = row["namespace_probe"]
            need(probe["uid"] > 0 and probe["exit"] == 0, "ordinary default namespace probe")
            result["outer_uid"] = probe["uid"]

    controls = {"server-exit":(1,"INTENTIONAL_SERVER_EXIT"), "client-error":(7,"INTENTIONAL_CLIENT_ERROR"),
                "client-signal":(143,"INTENTIONAL_CLIENT_SIGNAL"), "no-sender":(1,"iperf did not report an aggregate sender rate"),
                "no-sum":(1,"iperf did not report an aggregate sender rate"), "interrupted":(1,"incoming connection established.")}
    for name, (status, marker) in controls.items():
        row, out, err, hashes = streams(name)
        need(row["verified"] and row["status"] == status and not SUMMARY.search(out) and marker in err, name)
        ctl = receipt["controls"][name] = {"status":status, "sha256":hashes}
        if name == "interrupted":
            need(row["outer_verified"] and row["connections_before_signal"] >= 3 and row["profile"]["bytes"] > 0, name)
            ctl.update(profile=row["profile"], connections_before_signal=row["connections_before_signal"], outer_executable_verified=True)
        elif name == "server-exit":
            need("client" not in row["iperf_args"] and "server" in row["iperf_args"], "unexpected iperf client")
        else:
            need("-J" in row["iperf_args"]["client"], "client process not executed")
            if name in ("no-sender", "no-sum"):
                records = [json.loads(line)["end"] for line in err.splitlines() if line.startswith('{"end"')]
                need(len(records) == 1 and "sum_sent" not in records[0], "missing aggregate fixture")
                need(len(records[0]["streams"]) == (2 if name == "no-sum" else 0), "stream fixture")
    row, out, err, hashes = streams("historical-installed")
    old = shell_verdict(row["status"], out, err)
    need(row["source"] == PARENT and row["status"] == 1 and not out and old["execution"] == "split PATH export", "historical PATH comparison")
    receipt["historical_installed"] = {"source":PARENT, "status":row["status"], "sha256":hashes, **old}
    return receipt


if __name__ == "__main__":
    if sys.argv[1:] == ["--read-original"]:
        print(json.dumps(read_original(sys.stdin.buffer.read()), indent=2))
    else:
        need(len(sys.argv) == 1 and "RUNNER_TEMP" in os.environ, "expected --read-original or a native qualification runner")
        sys.exit(not main())
