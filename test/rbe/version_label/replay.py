"""Replay the release reporter against the original BuildBuddy event stream."""
from datetime import datetime, timezone
import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from assembly import event_row

IID = "19454a23-5276-4077-bd35-6b2776bcba89"
SOURCE = "14f280cd18a9aa968e2fee3b041d8621d95ff87f"
OWNER = "//debian:release-artifacts"
OUT = Path(os.environ["RUNNER_TEMP"])/"make-read"/"receipt"
OUT.mkdir(parents=True)
record = {"original_build":IID, "original_actions":40904608668, "source":SOURCE}
declared = json.loads(Path(__file__).with_name("release-output-digests.json").read_bytes())


def run():
    con=HTTPSConnection("app.buildbuddy.io",timeout=120)
    try:
        key=os.environ.pop("BUILDBUDDY_API_KEY")
        con.request("GET",f"/file/download?invocation_id={IID}&artifact=raw_json",headers={"x-buildbuddy-api-key":key})
        del key
        with con.getresponse() as response:
            assert response.status==200, ("raw event download",response.status)
            limit=128<<20
            raw=response.read(limit+1)
            assert 0<len(raw)<=limit
    finally:
        con.close()
    record["raw_json"]={"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw)}
    events=json.loads(raw)
    record["total_events"]=len(events)
    # The full original stream, including command and progress, is private;
    # only six complete identities and numerical diagnostics leave this job.
    with tempfile.TemporaryDirectory() as scratch:
        root=Path(scratch)
        p=root/"native"/"build-release"
        p.parent.mkdir()
        (root/"result").mkdir()
        def write(values):
            with p.open("w") as stream:
                for item in values:
                    stream.write(json.dumps(item)+"\n")
        write(events)
        record["named_sets"]=sum("namedSet" in e["id"] for e in events)
        old=subprocess.check_output(["git","show",SOURCE+":test/rbe/version_label/assembly.py"],text=True)
        oldreader={"__name__":"old"}
        exec(compile(old,"original_reporter","exec"),oldreader)
        try:
            oldreader["event_row"](p)
        except KeyError as exc:
            assert exc.args==("0",)
            record["old_named_set_error"]="0"
        else:
            raise ValueError("expected missing named set failure in original reader")
        row=event_row(p)
        assert row["invocation"]==IID and row["exit"]==0 and row["targets"]==[OWNER]
        assert row["outputs"]==declared, "six original output identities"
        record["release"]=row
        # Exercise independence of recorded shell status from an incomplete
        # event stream. This is a deliberately truncated copy of the original.
        write([e for e in events if e["id"].get("namedSet",{}).get("id")!="0"])
        (root/"packages.log").write_text('  File "packages.py", line 43\nAssertionError\n')
        rc=subprocess.run([sys.executable,str(Path(__file__).with_name("assembly.py")),str(root),"23"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        partial=json.loads((root/"result"/"native.json").read_bytes())
        diag=json.loads((root/"result"/"failure-classes.json").read_bytes())
        assert rc.returncode and partial["shell_exit"]==23 and partial["retention_error"]=="KeyError"
        assert diag["packages"]["classes"]==["assertion"] and diag["packages"]["comparison_lines"]==[43]
        record["injected_negative"]={"shell_exit":partial["shell_exit"],"retention_error":partial["retention_error"],"retained_comparison_lines":[43]}


try:
    run()
    record["verdict"]="PASS"
except Exception as exc:
    record["verdict"]="FAIL"
    record["failure"]=type(exc).__name__
    raise
finally:
    record["observed_at"]=datetime.now(timezone.utc).isoformat()
    (OUT/"replay.json").write_text(json.dumps(record,indent=2)+"\n")
