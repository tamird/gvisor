"""Check the test as committed and confirm it detects an assigned IPv4 address."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import native
import previous

SOURCE = "770112f4ff8d719e03334f627e5cc4d9c0e17203"
PATH = Path("test/syscalls/linux/network_namespace.cc")
PLATFORMS = ["native", "runsc_systrap_directfs"]
PREFIX = "//test/syscalls:network_namespace_test_"
TEST = "NetworkNamespaceTest.LoopbackAddressesAddedOnUp"
NEIGHBOR = "NetworkNamespaceTest.IPv6FlushedOnDown"
OUT = native.OUT
REPORT = {"source": SOURCE, "inputs": {}, "arms": {}, "verified": False}

def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")

def arm(name, platforms, cases, mutated):
    labels = [PREFIX + platform for platform in platforms]
    sub = OUT / name
    sub.mkdir()
    native.OUT = sub
    native.SYSCALLS.update(dict.fromkeys(labels, cases))
    native.REPORT = {"commands": {}, "tests": {}}
    row = REPORT["arms"][name] = {"complete": False, "mutated": mutated, "targets": {},
                                    "source_sha256": hashlib.sha256(PATH.read_bytes()).hexdigest()}
    save()
    try:
        native.run(name, labels, ["--test_env=GTEST_FILTER=" + ":".join(cases)], 55)
        for label in labels:
            target = native.REPORT["tests"][label]
            assert target["strategy"] in ("local", "standalone") and not target["cached"]
            assert target["shard"] == target["attempt"] == 1
            xml = (sub / target["test.xml"]["file"]).read_bytes()
            assert hashlib.sha256(xml).hexdigest() == target["test.xml"]["sha256"]
            got = {}
            for c in ET.fromstring(xml).iter("testcase"):
                key = c.get("classname") + "." + c.get("name")
                assert c.find("skipped") is None and c.get("status") != "notrun" and key not in got
                got[key] = [f.get("message", "")[:1000] for f in c if f.tag in ("failure", "error")]
            assert set(got) == set(cases), got
            row["targets"][label] = {"status": target["status"], "cases": got,
                                      "xml_sha256": target["test.xml"]["sha256"]}
            if mutated:
                assert target["status"] == "FAILED" and any("ifa_addr->sa_family" in f for f in got[TEST]), got
            else:
                assert target["verified"] and not any(got.values()), got
        row["complete"] = True
    except Exception as e:
        row["error"] = repr(e)[:1500]
    save()

assert os.geteuid() != 0 and os.uname().machine == "x86_64"
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
assert not subprocess.check_output(["git", "diff", "--name-only"])
previous.OUT = native.WORK
previous.REPORT["assets"] = REPORT["inputs"]
arm("unmodified", PLATFORMS, [TEST, NEIGHBOR], False)
if REPORT["arms"]["unmodified"]["complete"]:
    text = PATH.read_text()
    needle = "      struct ifaddrs* addrs;"
    assert text.count(needle) == 1
    PATH.write_text(text.replace(needle, "      ASSERT_NO_ERRNO(LinkChangeFlags(lo.index, IFF_UP, IFF_UP));\n"
                                       "      ASSERT_NO_ERRNO(LinkChangeFlags(lo.index, 0, IFF_UP));\n" + needle))
    arm("already-assigned", ["native"], [TEST], True)
REPORT["verified"] = all(REPORT["arms"].get(name, {}).get("complete") for name in ("unmodified", "already-assigned"))
save()
raise SystemExit(not REPORT["verified"])
