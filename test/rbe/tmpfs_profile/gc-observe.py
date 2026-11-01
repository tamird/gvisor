"""Observe the existing checkpoint benchmark's memory-limit GC trigger."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
from xml.etree import ElementTree

TMP = Path(os.environ["RUNNER_TEMP"])
ROOT = TMP / "gc-observation"
OUT = ROOT / "records"
CAPTURE = ROOT / "capture"
GNU = ["docker", "exec", "tcp-compiler"]
BAZEL = GNU + ["bazel", "--bazelrc=" + str(TMP / "tcp.bazelrc")]
OPTS = ["--config=x86_64", "--spawn_strategy=remote,local", "--noremote_upload_local_results",
        "--strategy=GoStaticAnalysis=local", "--strategy=GoStandardLibraryAnalysis=local",
        "--remote_download_outputs=toplevel", "--jobs=24"]
BENCH = "//test/benchmarks/checkpointfile:checkpointfile_test"
OWNER = "//runsc/container:container_test"
BASE = "aa767e4c43d011121271551fe24da501a56f729d"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(record):
    (OUT / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")


def command(name, args, record):
    start = now()
    with (ROOT / (name + ".log")).open("w") as stream:
        result = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT)
    record[name] = {"exit": result.returncode, "started": start, "finished": now()}
    text = (ROOT / (name + ".log")).read_text(errors="replace")
    record[name]["invocations"] = re.findall(r"Invocation ID: ([0-9a-f-]{36})", text)
    save(record)
    return result


def digests():
    paths = [str(CAPTURE / n) for n in ("checkpoint.img", "pages.img", "pages_meta.img", "spec.json")]
    hashes = subprocess.check_output(GNU + ["sha256sum", *paths], text=True).splitlines()
    sizes = subprocess.check_output(GNU + ["stat", "-c", "%s", *paths], text=True).splitlines()
    assert len(hashes) == len(sizes) == len(paths)
    return {Path(path).name: {"sha256": row.split()[0], "bytes": int(size)}
            for path, row, size in zip(paths, hashes, sizes)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    CAPTURE.mkdir(exist_ok=True)
    CAPTURE.chmod(0o777)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    record = {"status": "INCOMPLETE", "source": head, "product": BASE, "started": now(),
              "scope": "One fresh 50k-file capture, six existing benchmark executions; no performance comparison"}
    save(record)
    try:
        assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == BASE
        source = subprocess.check_output(["git", "diff", "--name-only", BASE, "HEAD"], text=True).splitlines()
        assert set(source) == {".github/workflows/build.yml", "test/rbe/tmpfs_profile/gc-observe.py",
                               "runsc/container/BUILD", "runsc/container/container_save_restore_test.go",
                               "test/benchmarks/checkpointfile/checkpointfile.go"}
        for name, blob in {"runsc/container/BUILD": "34de9f61dbadc6ce68ae3d0128614ac3c2dd94b6",
                           "runsc/container/container_save_restore_test.go": "6818655c7dd2c3a8b9b0557ba12112f1a18b5c73"}.items():
            assert subprocess.check_output(["git", "rev-parse", "HEAD:" + name], text=True).strip() == blob
        record["inherited_environment"] = json.loads(subprocess.check_output(GNU + ["python3", "-c", 'import json,os;print(json.dumps({k:os.environ.get(k) for k in ("GOMEMLIMIT","GOGC","GOMAXPROCS")}))'], text=True))
        save(record)
        result = command("capture", BAZEL + ["test", *OPTS, "--strategy=TestRunner=local",
            "--test_filter=^TestCheckpointTmpfsTreeForProfile$", "--test_env=WORKLOAD_CHECKPOINT_DIR=" + str(CAPTURE),
            "--test_arg=-test.v", "--test_sharding_strategy=disabled", "--nocache_test_results", "--test_timeout=600", OWNER], record)
        if result.returncode:
            record["status"] = "FAIL"
            return 1
        testdir = "bazel-testlogs/runsc/container/container_test/"
        raw = subprocess.check_output(GNU + ["cat", testdir + "test.xml"])
        cases = list(ElementTree.fromstring(raw).iter("testcase"))
        assert len(cases) == 1 and cases[0].get("name") == "TestCheckpointTmpfsTreeForProfile"
        assert not any(cases[0].find(k) is not None for k in ("error", "failure", "skipped"))
        assert cases[0].get("status") not in ("notrun", "suppressed")
        text = subprocess.check_output(GNU + ["cat", testdir + "test.log"], text=True)
        record["capture_case"] = {"name": cases[0].get("name"), "xml_sha256": hashlib.sha256(raw).hexdigest(),
                                  "witnesses": [line.strip() for line in text.splitlines() if "TMPFS-" in line]}
        assert "TMPFS-VERIFIED 50000" in text and "TMPFS-GRAPH kernel=true" in text
        assert all("TMPFS-CAP " + stage + " 2147483648" in text for stage in ("save", "restore"))
        record["input"] = digests()
        save(record)
        result = command("compile", BAZEL + ["build", *OPTS, BENCH], record)
        if result.returncode:
            record["status"] = "FAIL"
            return 1
        result = command("output", BAZEL + ["cquery", *OPTS, "--output=files", BENCH], record)
        assert not result.returncode
        paths = [line.strip() for line in (ROOT / "output.log").read_text().splitlines() if line.startswith("bazel-out/")]
        paths = [p for p in paths if subprocess.run(GNU + ["test", "-f", p, "-a", "-x", p]).returncode == 0]
        assert len(paths) == 1
        executable = str(ROOT / "checkpointfile")
        subprocess.run(GNU + ["cp", paths[0], executable], check=True)
        record["binary_sha256"] = subprocess.check_output(GNU + ["sha256sum", executable], text=True).split()[0]
        record["observations"] = rows = []
        for setting in (None, "2GiB", "64MiB"):
            for mode in ("DecodeStatefile", "EncodeStatefile"):
                label = (setting or "inherited") + "-" + mode
                environment = ["env", "GOMAXPROCS=1", "GO_TEST_WRAP=0"]
                if setting is not None:
                    environment.append("GOMEMLIMIT=" + setting)
                result = command(label, GNU + environment + [executable, "-checkpoint-dir=" + str(CAPTURE),
                    "-test.run=^$", "-test.bench=^BenchmarkCheckpointFile$/^" + mode + "$", "-test.benchtime=1x",
                    "-test.benchmem", "-test.cpu=1", "-test.v"], record)
                text = (ROOT / (label + ".log")).read_text()
                observations = re.findall(r"GC-OBSERVATION limit=(\d+) completed=(\d+) forced=(\d+) pause-ns=(\d+) heap-before=(\d+) heap-after=(\d+)", text)
                benchmarks = [line for line in text.splitlines() if line.startswith("BenchmarkCheckpointFile/") and "ns/op" in line]
                row = {"setting": setting, "mode": mode, "exit": result.returncode, "benchmark_lines": benchmarks,
                       "gc": [{"limit": int(a), "completed": int(b), "forced": int(c), "pause_ns": int(d),
                               "heap_before": int(e), "heap_after": int(f)} for a, b, c, d, e, f in observations]}
                rows.append(row)
                save(record)
                if result.returncode:
                    record["status"] = "FAIL"
                    return 1
                assert len(observations) == len(benchmarks) == 1 and re.search(r"^PASS$", text, re.M)
                assert benchmarks[0].split()[1] == "1"
                if setting is not None:
                    assert row["gc"][0]["limit"] == {"2GiB": 2 << 30, "64MiB": 64 << 20}[setting]
        record["input_unchanged"] = digests() == record["input"]
        record["source_unchanged"] = subprocess.run(["git", "diff", "--quiet", "HEAD"]).returncode == 0
        assert record["input_unchanged"] and record["source_unchanged"]
        record["finding"] = {"limit_trigger_observed": any(row["gc"][0]["completed"] > row["gc"][0]["forced"] for row in rows if row["setting"] == "64MiB"),
                             "inherited_collections": [row["gc"][0]["completed"] for row in rows if row["setting"] is None],
                             "fitting_collections": [row["gc"][0]["completed"] for row in rows if row["setting"] == "2GiB"]}
        record["status"] = "PASS"
        return 0
    except (AssertionError, OSError, ValueError, KeyError, subprocess.SubprocessError) as err:
        record["collection_error"] = {"type": type(err).__name__, "line": traceback.extract_tb(err.__traceback__)[-1].lineno}
        return 1
    finally:
        record["verified_at"] = now()
        save(record)


if __name__ == "__main__":
    raise SystemExit(main())
