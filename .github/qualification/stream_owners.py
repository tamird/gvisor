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
OWNERS = [
    "//pkg/buffer:buffer_test", STACK, "//pkg/tcpip/adapters/gonet:gonet_test",
    "//pkg/tcpip/transport/icmp:icmp_x_test", "//pkg/tcpip/transport/packet:packet_test",
    "//pkg/tcpip/transport/raw:raw_x_test", "//pkg/tcpip/transport/tcp:tcp_test",
    "//pkg/tcpip/transport/udp:udp_x_test",
]
CASES = {"partial_read", "full_read", "fragmented_partial_read",
         "destination_full_at_view_boundary", "empty_destination", "peek"}


def check(name: str, labels: list[str], extra: list[str]) -> dict:
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
    if bep.exists():
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                if (test := event.get("testResult")) is None:
                    continue
                label = event["id"]["testResult"]["label"]
                assert label in labels and label not in rows, label
                log, = (f for f in test["testActionOutput"] if f["name"] == "test.log")
                uri = urlparse(log["uri"])
                assert uri.scheme in ("", "file"), uri.scheme
                data = Path(unquote(uri.path)).read_bytes()
                text = data.decode(errors="replace")
                rows[label] = {
                    "status": test["status"], "cached": test.get("cachedLocally", False),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "packet_cases": sorted(set(re.findall(
                        r"--- PASS: TestPacketDataReadToPreservesHeaders/([\w_]+)", text))),
                    "failed_tests": re.findall(r"--- FAIL: ([\w./-]+)", text)[:20],
                    "passed_top_level": len(re.findall(r"--- PASS: Test\w+ \(", text)),
                }
    ok = (code == 0 and set(rows) == set(labels)
          and all(row["status"] == "PASSED" and not row["cached"] for row in rows.values())
          and set(rows[STACK]["packet_cases"]) == CASES)
    result = {"exit": code, "owners": rows, "verified": ok}
    if not ok:
        diagnostics = [line.strip()[:400] for line in console.read_text(errors="replace").splitlines()
                       if "error" in line.lower() or "fail" in line.lower()]
        result["diagnostics"] = diagnostics[-12:]
    return result


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == PRODUCT
    path = WORK / "results/correctness.json"
    receipt = {"source": PRODUCT}
    for name, labels, extra in [
        ("ordinary", OWNERS, []),
        ("race", [STACK], ["--config=race", "--test_arg=-test.run=^TestPacketDataReadToPreservesHeaders$"]),
    ]:
        receipt[name] = check(name, labels, extra)
        path.write_text(json.dumps(receipt, indent=2) + "\n")
        if not receipt[name]["verified"]:
            raise SystemExit(f"{name} correctness result incomplete")


if __name__ == "__main__":
    main()
