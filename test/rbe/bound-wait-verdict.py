"""Verify exact Linux and gVisor outcomes for two listener types."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
required = {f"StreamSeqpacket/GoferStreamSeqpacketTest.{name}/{i}" for name in ("EpollBindListenWaitAccept","BindConnectedSocket") for i in range(2)}
out = {}
for lane in ("current-native", "stock-guest", "current-guest"):
    directory = root / lane

    def read(key):
        filename = directory / key
        return filename.read_text().strip() if filename.exists() else None

    cases = {}
    xml = directory / "test.xml"
    errors = []
    if xml.exists():
        try:
            for case in ET.parse(xml).iter("testcase"):
                name = case.get("classname", "") + "." + case.get("name", "")
                fail = [n.text or n.get("message", "") for n in case if n.tag in ("failure", "error")]
                skip = case.find("skipped") is not None or case.get("result") in ("skipped", "suppressed") or case.get("status") == "notrun"
                if name in cases:
                    errors.append("duplicate: " + name)
                cases[name] = {"status": "SKIP" if skip else "FAIL" if fail else "PASS", "failures": fail}
        except ET.ParseError as e:
            errors.append(str(e))

    stock = lane == "stock-guest"
    expected = "6449e2bd668f412903ccb41d12ea7735c2076cec" if stock else "98cd1ab622fd08480b860bab68282158aaccb7fe"
    target = "//test/syscalls:bind_external_test_" + ("native" if lane == "current-native" else "runsc_systrap_directfs")
    valid = set(cases) == required and not errors and read("source") == expected and read("target") == target
    valid &= read("exit") == ("3" if stock else "0")
    if stock:
        for case, result in cases.items():
            fs = result["failures"]
            if ".BindConnectedSocket/" in case:
                # The peer stays readable after its first edge; binding a name
                # neither supplies a second edge nor makes it eligible to listen.
                combined = "\n".join(fs)
                valid &= (result["status"] == "FAIL" and "250" in combined and "Actual: 1" in combined
                          and "listen(socket.get(), 1)" in combined and "Actual: 0" in combined)
            else:
                valid &= result["status"] == "PASS"
    else:
        valid &= all(v["status"] == "PASS" for v in cases.values())
    out[lane] = {"source": read("source"), "target": read("target"), "cases": cases, "missing": sorted(required-cases.keys()), "errors": errors,
                 "exit": read("exit"), "fixture": read("fixture-sha256"), "notifier": read("notifier-sha256"), "finished": read("end"), "verified": bool(valid)}

owners = root / "owners"
exitcode = owners / "exit"
receipt = owners / "receipt"
owner_pass = exitcode.exists() and exitcode.read_text().strip() == "0" and receipt.exists() and any(
    "//pkg/sentry/socket/unix/transport:transport_nogo" in s and "PASSED" in s for s in receipt.read_text().splitlines())
owner_pass &= receipt.exists() and any("//pkg/sentry/socket/unix/transport:transport_test" in s and "PASSED" in s for s in receipt.read_text().splitlines())
owner_pass &= receipt.exists() and all(("PASS: TestHostListenerAdmission/"+case) in receipt.read_text() for case in ("register-after-listen", "listen-after-register"))
same_fixture = len({v["fixture"] for v in out.values()}) == 1 and all(v["fixture"] for v in out.values())
verified = all(v["verified"] for v in out.values()) and same_fixture and owner_pass
verdict = {"verified": bool(verified), "lanes": out, "analyzer": owner_pass, "verified_at": datetime.now(timezone.utc).isoformat()}
(root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
print(json.dumps(verdict))
if not verified:
    sys.exit(1)
