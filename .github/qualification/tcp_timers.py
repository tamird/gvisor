"""Verify the TCP timer patch on its own source tree and Linux worker."""
import os
import re
import subprocess

import native
import previous


SOURCE = "97e5ddeb668d4730d7fa898ee1ba373a3729aa1f"
TREE = "b92c252a51ead826b893500f254df8a8c24a9533"
BASE = "2a653cea7b2054a2dc9c6419337d3de8031275d6"
CORE = "//pkg/tcpip/transport/tcp:tcp_test"
E2E = "//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test"
PACKET = ["//test/packetimpact/tests:tcp_user_timeout_" + config + "_test"
          for config in ("native", "netstack")]
REQUIRED = {
    CORE: {"TestCleanup", "TestPostponedTimerExpiresOnce", "TestRACKReorderTimerDeclaresLossOnFirstExpiry"},
    E2E: {"TestEifelRetransmissionTimestamps/" + case
          for case in ("wrap/original", "wrap/repair", "two_timeouts/original", "two_timeouts/repair")},
}


def check(label, text, xml, row):
    passed = set(re.findall(r"(?m)^\s*--- PASS: (\S+) \(", text))
    failed = re.findall(r"(?m)^\s*--- FAIL: (\S+) \(", text)
    skipped = re.findall(r"(?m)^\s*--- SKIP: (\S+) \(", text)
    records = list(xml.iter("testcase"))
    row.update(passed=sorted(passed), failed=failed, skipped=skipped,
               xml_names=[c.get("name") for c in records])
    assert not failed
    assert not any(c.find("failure") is not None or c.find("error") is not None for c in records)
    if label in REQUIRED:
        assert REQUIRED[label] <= passed, sorted(REQUIRED[label] - passed)
    else:
        row["keepalive_case"] = "TestKeepaliveReenable" in passed or any(
            c.get("name") == "TestKeepaliveReenable" and c.find("skipped") is None
            and c.get("result") not in ("skipped", "suppressed") and c.get("status") != "notrun"
            for c in records
        )


assert os.uname().machine == "x86_64" and os.geteuid() != 0
for rev, expected in (("HEAD", SOURCE), ("HEAD^{tree}", TREE), ("HEAD^", BASE)):
    assert subprocess.check_output(["git", "rev-parse", rev], text=True).strip() == expected
assert not subprocess.check_output(["git", "diff", "--name-only"])
native.OUT = native.OUT / "timers"
native.OUT.mkdir()
native.REPORT = {"product": SOURCE, "tree": TREE, "base": BASE, "commands": {}, "tests": {},
                 "inputs": {}, "verified": False, "errors": {}}
previous.OUT = native.WORK
previous.REPORT["assets"] = native.REPORT["inputs"]
native.check_case = check
for name, labels, opts, minutes in (
    ("full-timer-and-sack-suites", [CORE, E2E], ["--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1"], 40),
    ("keepalive-packets", PACKET, ["--test_env=GO_TEST_WRAP_TESTV=1", "--test_filter=^TestKeepaliveReenable$"], 45),
):
    try:
        native.run(name, labels, opts, minutes)
        assert all(native.REPORT["tests"][label].get("verified") for label in labels)
    except Exception as err:
        native.REPORT["errors"][name] = repr(err)
        print(name, repr(err), flush=True)
    native.save()
native.REPORT["verified"] = not native.REPORT["errors"]
native.save()
assert native.REPORT["verified"], native.REPORT["errors"]
