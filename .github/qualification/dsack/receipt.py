"""Adapt the frozen sender's hidden input and publish only bounded verdict fields."""
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from uuid import UUID
import json
import os
import re
import runpy
import sys


SOURCE = "19b9ac70fff949d3bcf8104efd1ea5e08c3ecfa2"
REQUEST = "c024d269bdb8d93e3aaa7f2bb34826509e2888e133ed6bcf50067ce4475fcb66"
OWNERS = ('//test/packetimpact/tests:tcp_rack_netstack_test', '//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test', '//pkg/tcpip/transport/tcp:tcp_nogo', '//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test_nogo')


def digest(s):
    if not isinstance(s, str) or not re.fullmatch(r"[0-9a-f]{64}", s):
        raise ValueError("Invalid digest")
    return s


def number(n, limit=2**32 - 1):
    if type(n) is not int or not 0 <= n <= limit:
        raise ValueError("Invalid number")
    return n


def enum(s):
    return s if s in ("PASS", "FAIL", "PASSED", "FAILED", "TIMEOUT", "INCOMPLETE", "COMPLETE") else "UNKNOWN"


def pair(x):
    if not isinstance(x, list) or len(x) != 2:
        raise ValueError("Invalid range")
    return [number(n) for n in x]


def public_result(z):
    if z["source"] != SOURCE or z["request"] != REQUEST:
        raise ValueError("Unexpected source")
    result = {"source":SOURCE,"request":REQUEST,"invocation":str(UUID(z["invocation"])),"status":enum(z.get("status")),"producer":enum(z.get("producer")),"collection":enum(z.get("collection")),"owners":{}}
    if z.get("bundle"):
        result["bundle"]={"sha256":digest(z["bundle"]["sha256"]),"bytes":number(z["bundle"]["bytes"])}
    delay=z.get("native_finish_to_verified_s")
    if type(delay) in (int,float) and 0<=delay<86400:
        result["native_finish_to_verified_ms"]=round(delay*1000,3)
    if "native_finished_at" in z:
        from datetime import datetime
        result["native_finished_at"]=datetime.fromisoformat(z["native_finished_at"]).isoformat()
    for label in OWNERS:
        r=z["owners"].get(label,{})
        row=result["owners"][label]={"status":enum(r.get("status"))}
        for k in ("producer_verified","worker_verified","behavior_verified","uncached_remote","named_wire","matches_original_linux","matches_producer"):
            if k in r:row[k]=r[k] is True
        for k in ("cases","failed","skipped","native_exit"):
            if k in r:row[k]=number(r[k],16384)
        if "child" in r:row["child"]=str(UUID(r["child"]))
        for k in ("test.xml","test.log","pcap"):
            if k in r:row[k]={"sha256":digest(r[k]["sha256"]),"bytes":number(r[k]["bytes"])}
        row["markers"]=[a for a in ("amd64","arm64") if a in r.get("markers",[])]
        row["receiver_controls"]=[a for a in ("TestReceiverDSACK/SACK=true","TestReceiverDSACK/SACK=false") if a in r.get("receiver_controls",[])]
        if r.get("observations"):
            if len(r["observations"])!=5:raise ValueError("Unexpected frame census")
            row["observations"]=[{"sent":pair(f["sent"]),"ack":number(f["ack"]),"blocks":[pair(v) for v in f["blocks"]]} for f in r["observations"] if len(f["blocks"])<=4]
    if result["status"]=="PASS":
        rows=result["owners"]
        if not all(r.get("status")=="PASSED" and r.get("native_exit")==0 and r.get("uncached_remote") and r.get("behavior_verified") and r.get("producer_verified") and r.get("cases",0)>0 and r.get("failed")==r.get("skipped")==0 for r in rows.values()):
            raise ValueError("Missing original corrected owner")
        packet,sack,*analyzers=[rows[n] for n in OWNERS]
        if not (packet.get("named_wire") and packet.get("worker_verified") and packet.get("matches_producer") and packet.get("matches_original_linux") and sack.get("worker_verified") and len(sack["receiver_controls"])==2 and all(r["markers"]==["amd64","arm64"] for r in analyzers)):
            raise ValueError("Original corrected evidence does not match the requested contract")
    return result


class SafeOutput:
    """Pass through only the immutable original invocation URL."""
    def write(self, s):
        if re.fullmatch(r"https://app\.buildbuddy\.io/invocation/[0-9a-f-]{36}", s):
            print(s, file=sys.__stdout__, flush=True)
        return len(s)

    def flush(self):
        pass


def collect(key):
    root = Path(os.environ["RUNNER_TEMP"]) / "dsack-comparison"
    task = root / "task"
    sys.path.insert(0, str(task))
    public = {"source": SOURCE, "request": REQUEST, "stage": "submit"}
    rc = 1
    try:
        sender = runpy.run_path(str(task / "submit.py"))
        with redirect_stdout(SafeOutput()), redirect_stderr(SafeOutput()):
            rc = sender["main"](lambda _: key)
        key = None
        public["stage"] = "reconcile"
        with redirect_stdout(SafeOutput()), redirect_stderr(SafeOutput()):
            runpy.run_path(str(task / "reconcile.py"))
        public = public_result(json.loads((task / "outcome.json").read_text()))
    except BaseException as exc:
        rc=1
        public["projection_error"]=type(exc).__name__
        import traceback
        public["projection_frames"]=[(Path(x.filename).name,x.lineno) for x in traceback.extract_tb(exc.__traceback__) if Path(x.filename).name in ("receipt.py","reconcile.py","wire.py","submit.py")]
    finally:
        key = None
        submission = task / "submission.json"
        if submission.is_file():
            public["invocation"] = str(UUID(json.loads(submission.read_text())["invocationId"]))
        (root / "verdict.json").write_text(json.dumps(public, indent=2) + "\n")
    raise SystemExit(rc)
