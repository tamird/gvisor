"""Require named syscall execution; keep EOF, timeout and elapsed time distinct."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as XML


root = Path(sys.argv[1])
cases = {f"AllInetTests/TcpSocketTest.ReadAfterInitialShutdown/{n}" for n in (0, 1)}
expected = {(family, mode) for family in (2, 10) for mode in range(3)}
observations = {}
native_only = len(sys.argv) > 2
for name in ("poll-native",) if native_only else ("poll-native", "poll-stock", "poll-fixed"):
    directory = root / name
    def read(file):
        path = directory / file
        return path.read_text(errors="replace") if path.exists() else ""
    outcomes = {}
    if xml := read("test.xml"):
        for node in XML.fromstring(xml).iter("testcase"):
            test = node.get("classname", "") + "." + node.get("name", "")
            outcomes[test] = [sub.tag for sub in node if sub.tag in {"failure", "error", "skipped"}]
            if node.get("status") == "notrun":
                outcomes[test].append("notrun")
    log = read("test.log")
    matches = re.findall(r"HOSTREAD family=(\d+) mode=(\d+) initial=(-?\d+) errno=(\d+) us=(\d+) byte=(\d+)", log)
    results = {(int(fam), int(mode)): (int(initial), int(err), int(micros), int(byte))
               for fam, mode, initial, err, micros, byte in matches}
    source = read("source").strip()
    buildlog = read("bazel.log")
    verified = (read("exit").strip() == "0" and set(outcomes) == cases and not any(outcomes.values())
                and set(results) == expected and all(v[3] == 120 for v in results.values())
                and source == os.environ["STOCK" if name == "poll-stock" else "PRODUCT"]
                and read("fixture-sha256").split()[:1] == [os.environ["FIXTURE_SHA256"]])
    if name == "poll-native":
        verified &= all(results.get((fam, mode), ())[:2] == (-1, 11) for fam in (2, 10) for mode in (0, 2))
    elif name in ("poll-fixed", "poll-stock"):
        verified &= read("target").strip().endswith("runsc_systrap_hostnet")
    observations[name] = dict(verified=verified, actual=outcomes, source=source, target=read("target").strip(),
                              observations={f"{f}/{m}": dict(initial=a, errno=b, micros=c, laterByte=d)
                                            for (f, m), (a, b, c, d) in results.items()},
                              start=read("start").strip(), end=read("end").strip(),
                              invocations=list(dict.fromkeys(re.findall(r"app.buildbuddy.io/invocation/([a-f0-9-]{36})",buildlog))),
                              diagnostics=[s.strip()[:350] for s in (buildlog+"\n"+log).splitlines()
                                           if re.search(r"error:|^ERROR:|Failure|Expected:|Actual:|^FAILED:|NoSuch",s)
                                           and not re.search(r"header|credential|bearer|api.?key|token",s,re.I)][-14:])
ok = all(v["verified"] for v in observations.values())
out = dict(nativeFirst=native_only, verified=ok, evidence=observations, verifiedAt=datetime.now(timezone.utc).isoformat())
(root / "verdict.json").write_text(json.dumps(out, indent=2))
raise SystemExit(not ok)
