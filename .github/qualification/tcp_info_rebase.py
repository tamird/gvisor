"""Verify that both TCP_INFO packet suites exercised each requested case."""

import json
import os
from pathlib import Path
import re
import subprocess

temp = Path(os.environ["RUNNER_TEMP"])
output = temp / "tcp-info"
suites = {
    "tcp_retransmits": [
        "TestActiveHandshakeRetransmits", "TestPassiveHandshakeRetransmits", "TestRetransmits",
    ],
    "tcp_info": [
        "TestTCPInfo", "TestTCPInfoOptions/Neither", "TestTCPInfoOptions/Timestamps",
        "TestTCPInfoOptions/SACK", "TestTCPInfoOptions/Both",
    ],
}
owners = {
    f"{suite}_{stack}_test": cases
    for suite, cases in suites.items()
    for stack in ("native", "netstack")
}
labels = [f"//test/packetimpact/tests:{owner}" for owner in owners]
with (output / "bazel.log").open("w") as log:
    run = subprocess.run(
        [str(temp / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
         "test", "--config=x86_64", "--jobs=3", "--local_test_jobs=1",
         "--strategy=TestRunner=local", "--test_env=GO_TEST_WRAP_TESTV=1",
         "--test_sharding_strategy=disabled", "--nocache_test_results",
         "--flaky_test_attempts=1", "--test_timeout=240", "--test_output=errors", *labels],
        stdout=log, stderr=subprocess.STDOUT,
    )
rows = {}
for owner, expected in owners.items():
    log_file = Path("bazel-testlogs/test/packetimpact/tests") / owner / "test.log"
    text = log_file.read_text(errors="replace") if log_file.exists() else ""
    observed = {name: status for status, name in re.findall(r"--- (PASS|FAIL|SKIP): ([\w/]+)\s", text)}
    rows[owner] = {name: observed.get(name, "MISSING") for name in expected}

diagnostics = [
    line[:500] for line in (output / "bazel.log").read_text(errors="replace").splitlines()
    if line.startswith("ERROR:") or "FAILED" in line
]
source = subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip()
ok = run.returncode == 0 and all(code == "PASS" for cases in rows.values() for code in cases.values())
report = {"source": source, "exit": run.returncode, "cases": rows, "passed": ok, "diagnostics": diagnostics[:15]}
(output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
raise SystemExit(not ok)
