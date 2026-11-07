"""Check the packet reader and its transport callers before measuring throughput."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-read-stream"
PRODUCT = "91d4c6d51f862c64ab3962bfba00b27b66554ad3"
STACK = "//pkg/tcpip/stack:stack_test"
CASES = {"partial_read", "full_read", "fragmented_partial_read",
         "destination_full_at_view_boundary", "empty_destination", "peek"}


def check() -> dict:
    name = "race"
    labels = [STACK]
    extra = ["--config=race", "--symlink_prefix=/",
             "--test_arg=-test.run=^TestPacketDataReadToPreservesHeaders$"]
    bep = WORK / (name + ".bep")
    console = WORK / (name + ".console")
    args = [str(WORK / "bazelisk"), "--nosystem_rc", "--nohome_rc", "test", "-c", "opt",
            "--config=x86_64", "--jobs=4", "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--lockfile_mode=off", "--keep_going", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=300",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v",
            "--build_event_json_file=" + str(bep), *extra, *labels]
    with console.open("wb") as out:
        code = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT).returncode
    rows = {}
    finished = None
    if bep.exists():
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                if "finished" in event:
                    finished = event["finished"].get("exitCode", {}).get("code", 0)
                if (test := event.get("testResult")) is None:
                    continue
                label = event["id"]["testResult"]["label"]
                assert label in labels and label not in rows, label
                row = {"status": test["status"], "cached": test.get("cachedLocally", False),
                       "packet_cases": []}
                rows[label] = row
                logs = [f for f in test.get("testActionOutput", []) if f["name"] == "test.log"]
                if len(logs) != 1:
                    row["log"] = "missing"
                    continue
                uri = urlparse(logs[0].get("uri", ""))
                if uri.scheme not in ("", "file") or not uri.path:
                    row["log"] = "unavailable"
                    continue
                try:
                    data = Path(unquote(uri.path)).read_bytes()
                except OSError:
                    row["log"] = "unavailable"
                    continue
                text = data.decode(errors="replace")
                row.update({
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "packet_cases": sorted(set(re.findall(
                        r"--- PASS: TestPacketDataReadToPreservesHeaders/([\w_]+)", text))),
                    "failed_tests": re.findall(r"--- FAIL: ([\w./-]+)", text)[:20],
                    "passed_top_level": len(re.findall(r"--- PASS: Test\w+ \(", text)),
                })
    ok = (code == 0 and set(rows) == set(labels)
          and all(row["status"] == "PASSED" and not row["cached"] for row in rows.values())
          and set(rows[STACK]["packet_cases"]) == CASES)
    console_text = console.read_text(errors="replace")
    result = {"exit": code, "finished": finished, "owners": rows, "verified": ok}
    if not ok:
        lines = console_text.splitlines()
        starts = [i for i, line in enumerate(lines) if line.startswith("ERROR:")]
        if starts:
            result["diagnostics"] = [line.strip()[:500] for line in lines[starts[0]:starts[0] + 28]]
        print("\n".join(lines[-45:]))
    return result


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == PRODUCT
    path = WORK / "results/correctness.json"
    receipt = {
        "source": PRODUCT, "race": check(),
        "prior_ordinary": {"run": "42328098086", "artifact": "13556900684",
                           "zip_sha256": "bb64ae118904963f66796512a3e3454d6d048d19d0c5a11b8da8f4ee3ee047a3"},
    }
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    if not receipt["race"]["verified"]:
        raise SystemExit("focused race check incomplete")


if __name__ == "__main__":
    main()
