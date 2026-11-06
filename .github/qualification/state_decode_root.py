import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


out = Path(os.environ["RUNNER_TEMP"]) / "decode-roots"
records = {"cases": [], "ownership": []}
def save():
    (out / "results.json").write_text(json.dumps(records, indent=2) + "\n")
def cmd(*args):
    subprocess.run(args, check=True)
def source():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

base = "f51a205a8f4afbc5ab41aded070df74951b081ea"
candidate = "f6fbb1d2c62d486f3e311d2cd481d87a61acf7cb"
donor = "2b51abee8530385431b7225198fdd5cd65e78696"
target = "//pkg/state/tests:tests_test"
bazel = [str(out / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
opts = ["--config=x86_64", "--lockfile_mode=off", "-c", "opt", "--jobs=4", "--local_resources=memory=8192",
        "--remote_cache=", "--remote_executor=", "--bes_backend="]
test = [*bazel, "test", *opts, "--nocache_test_results", "--test_output=errors", "--flaky_test_attempts=1",
        "--test_env=GOMAXPROCS=2", "--test_timeout=180"]
log = Path("bazel-testlogs/pkg/state/tests/tests_test/test.log")
records["ownership_source"] = source()
save()
cmd(*test, "--test_arg=-test.run=^TestDecodeFixtureOwnership$", "--test_arg=-test.v", target)
observations = [line.split("Ownership ", 1)[1] for line in log.read_text().splitlines() if "Ownership codec=" in line]
records["ownership"] = observations
save()
assert len(observations) == 4, observations

for round_no in range(10):
    arms = [("populated", base), ("empty", candidate)]
    for arm, head in arms[::(-1 if round_no % 2 else 1)]:
        cmd("git", "switch", "--detach", head)
        assert source() == head
        cmd(*test, "--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkDecoding$/^(ptr|map|slice)$/^(state|gob)$",
            "--test_arg=-test.benchtime=32x", "--test_arg=-test.benchmem", target)
        data = log.read_bytes()
        rows = [line for line in data.decode().splitlines() if line.startswith("BenchmarkDecoding/") and "allocs/op" in line]
        assert len(rows) == 6 and all(re.search(r"\s32\s", r) for r in rows), rows
        records["cases"].append({"round": round_no, "arm": arm, "source": head,
                                 "sha256": hashlib.sha256(data).hexdigest(), "rows": rows})
        with (out / (arm + ".bench")).open("a") as stream:
            stream.write("\n".join(rows) + "\n")
        save()

cmd("git", "fetch", "--no-tags", "--depth=1", "https://github.com/tamird/gvisor.git", donor)
cmd("git", "switch", "--detach", donor)
assert source() == donor
with (out / "benchstat.txt").open("w") as f:
    subprocess.run([*bazel, "run", *opts, "@org_golang_x_perf//cmd/benchstat", "--",
                    "populated=" + str(out / "populated.bench"), "empty=" + str(out / "empty.bench")], check=True, stdout=f)
print((out / "benchstat.txt").read_text(), flush=True)
