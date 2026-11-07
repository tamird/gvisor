"""Check the packet reader and its transport callers before measuring throughput."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-read-stream"
PRODUCT = "279ee9dedb484efcf0d67faf5457781356624828"
STACK = "//pkg/tcpip/stack:stack_test"
BUFFER = "//pkg/buffer:buffer_test"
ORDINARY = [BUFFER, STACK, "//pkg/tcpip/adapters/gonet:gonet_test", "//pkg/tcpip/transport/icmp:icmp_x_test",
            "//pkg/tcpip/transport/packet:packet_test", "//pkg/tcpip/transport/raw:raw_x_test",
            "//pkg/tcpip/transport/tcp:tcp_test", "//pkg/tcpip/transport/udp:udp_x_test"]
CASES = {"partial_read", "full_read", "fragmented_partial_read",
         "destination_full_at_view_boundary", "empty_destination", "peek"}


def check(name: str) -> dict:
    assert name in ("ordinary","race")
    labels = ORDINARY if name == "ordinary" else [BUFFER, STACK]
    extra = [] if name == "ordinary" else [
        "--config=race", "--symlink_prefix=/",
        "--test_arg=-test.run=^(TestBufferRemove|TestPacketDataReadToPreservesHeaders)$",
    ]
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
                    "buffer_ranges": len(set(re.findall(r"--- PASS: TestBufferRemove/offset=(\d+)/count=(-?\d+)", text))),
                    "failed_tests": re.findall(r"--- FAIL: ([\w./-]+)", text)[:20],
                    "passed_top_level": len(re.findall(r"--- PASS: Test\w+ \(", text)),
                })
    ok = (code == 0 and set(rows) == set(labels)
          and all(row["status"] == "PASSED" and not row["cached"] for row in rows.values())
          and set(rows[STACK]["packet_cases"]) == CASES and rows[BUFFER].get("buffer_ranges") == 210)
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
    name = sys.argv[1]
    receipt = {"source": PRODUCT, name: check(name)}
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    if not receipt[name]["verified"]:
        raise SystemExit(f"{name} check incomplete")


if __name__ == "__main__":
    main()
