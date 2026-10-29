"""Verify the compiled overlay lock contracts on both architectures."""
from hashlib import sha256
import json
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import unquote, urlparse
import sys
import xml.etree.ElementTree as ET

out, name, source, owner, status = sys.argv[1:]
out=Path(out)
r=dict(source=source, owner=owner, arm=name, command_exit=int(status), verdict="INCOMPLETE")
try:
    bep=[json.loads(s) for s in (out/"events.jsonl").read_text().splitlines()]
    r["invocation"]=next(e["started"]["uuid"] for e in bep if "started" in e)
    finished,=[e["finished"]["exitCode"] for e in bep if "finished" in e]
    r["bazel_exit"]=int(finished.get("code",0))
    r["bazel_exit_name"]=finished.get("name","")
    r["execution_finished"]=next(e["finished"].get("finishTime") or e["finished"].get("finishTimeMillis") for e in bep if "finished" in e)
    test,=[e["testResult"] for e in bep if e.get("id",{}).get("testResult",{}).get("label")==owner]
    r["test_status"]=test["status"]
    r["remote"]=test.get("executionInfo",{}).get("strategy")=="remote"
    r["cached"]=bool(test.get("cachedLocally") or test.get("executionInfo",{}).get("cachedRemotely"))
    assert r["remote"] and not r["cached"]
    r["reports"]={}
    by_name={e["name"]:e for e in test["testActionOutput"]}
    data={}
    for f in ("test.xml","test.log"):
        b=(Path("bazel-testlogs")/owner[2:].replace(":","/")/f).read_bytes()
        h=sha256(b).hexdigest()
        entry=by_name[f]
        if "digest" in entry:
            assert h==entry["digest"] and len(b)==int(entry.get("length",len(b)))
        else:
            uri=urlparse(entry["uri"])
            actual=Path("bazel-testlogs")/owner[2:].replace(":","/")/f
            if uri.scheme=="file":
                assert Path(unquote(uri.path)).resolve()==actual.resolve()
            else:
                assert uri.scheme=="bytestream" and uri.path.endswith("/"+h+"/"+str(len(b)))
        r["reports"][f]={"sha256":h,"bytes":len(b)}
        data[f]=b
    rows=ET.fromstring(data["test.xml"]).findall(".//testcase")
    cases={e.attrib["name"]:next((c.tag for c in e if c.tag in {"skipped","failure","error"}),"pass") for e in rows}
    assert rows and all(mode != "skipped" for mode in cases.values()), cases
    r["cases"] = cases
    log = data["test.log"].decode(errors="replace")
    sections = re.split(r"(?:linux_)?(amd64|arm64)\.\.\.", log)
    by_arch = dict(zip(sections[1::2], sections[2::2], strict=True))
    assert set(by_arch) == {"amd64", "arm64"}, f"Architecture headings: {list(by_arch)}"
    r["diagnostics"] = {
        arch: [line.strip() for line in output.splitlines() if "invalid field access" in line]
        for arch, output in by_arch.items()
    }
    match name:
        case "proposal":
            assert all(not lines for lines in r["diagnostics"].values()), r["diagnostics"]
            assert all(mode == "pass" for mode in cases.values()), cases
            assert (r["test_status"], r["bazel_exit"], int(status)) == ("PASSED", 0, 0)
        case "missing_lock":
            for lines in r["diagnostics"].values():
                assert len(lines) == 1 and "must be locked when accessing cachedFD" in lines[0], lines
                assert "regular_file.go:271:" in lines[0], lines
            assert any(mode in {"failure", "error"} for mode in cases.values()), cases
            assert (r["test_status"], r["bazel_exit"], int(status)) == ("FAILED", 3, 3)
        case _:
            raise ValueError(name)
    r["verdict"]="COMPLETE"
except Exception as exc:
    r["exception"]=type(exc).__name__
    r["line"]=exc.__traceback__.tb_lineno
finally:
    r["verdict_time"] = datetime.now(timezone.utc).isoformat()
    (out/"result.json").write_text(json.dumps(r,sort_keys=True,indent=2)+"\n")
sys.exit(int(r["verdict"]!="COMPLETE"))
