"""Run the reporter's one-fetch experiment serially, rotating senders per seed."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


FLAGS = ["-rack=true", "-loss", "none", "-warmup", "0", "-fetches", "1", "-trials", "1",
         "-timeout", "30s", "-interval", "3s", "-rtt", "3ms", "-rate-mbps", "80", "-queue", "100"]
FETCH = re.compile(r"^trial 1 fetch 1 measured\s+([\d.]+) s \|\s+(\d+) data pkts,\s+(\d+) retransmitted \| RTOs\s+(\d+)(.*)$",re.M)
DROPS = re.compile(r"path drops toward the client: (\d+) by the queue, (\d+) scripted; reordered \(held [^)]*\): (\d+)")
RTOS = re.compile(r"per RTO interval: median (\d+), max (\d+)")


def main():
    paths = dict(zip(("stock","fixed","calibration"),map(lambda p:Path(p).resolve(),sys.argv[1:]),strict=True))
    out = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    rows = []
    paused = 0.0
    last_resume = float("-inf")
    for seed in range(1,11):
        for i in range(len(paths)):
            which=list(paths)[(seed-1+i)%len(paths)]
            # Match the reporter's bounded load gate. The host's scheduler and
            # load metric are recorded for interpreting, not filtering, runs.
            now=time.monotonic()
            if os.getloadavg()[0]>16 and now-last_resume>=45 and paused<600:
                while os.getloadavg()[0]>10 and time.monotonic()-now<min(120,600-paused):
                    time.sleep(3)
                last_resume=time.monotonic()
                paused+=last_resume-now
            load=os.getloadavg()[0]
            start=time.monotonic()
            with subprocess.Popen([paths[which],*FLAGS,"-seed",str(seed)],
                                  env={"PATH":os.defpath,"LANG":"C.UTF-8"},
                                  stdout=subprocess.PIPE,stderr=subprocess.STDOUT) as proc:
                try:
                    raw,_=proc.communicate(timeout=75)
                    code=proc.returncode
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        raw,_=proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        raw,_=proc.communicate()
                    code=124
            wall=time.monotonic()-start
            name=f"{which}-{seed:02}.txt"
            (out/name).write_bytes(raw)
            s=raw.decode(errors="replace")
            fetch=FETCH.search(s)
            drops=DROPS.search(s)
            tail=fetch[5] if fetch else ""
            per=RTOS.search(tail)
            status=("unobserved" if not fetch else "timeout" if "i/o timeout" in tail or "TIMED OUT" in tail
                    else "error" if "ERROR" in tail or code else "complete")
            verified=(code==0 and fetch is not None and drops is not None and drops[2] == "0" and drops[3] == "0"
                      and "RACK on, cubic, host GSO true" in s
                      and "(gVisor connection: TCP timestamps on)" in s
                      and "loss:     none scripted" in s
                      and "1 measured fetches of 2800000 bytes, 3s idle" in s
                      and f"jitter (seed {seed}), 80 Mbit/s toward the client, queue 100 packets (drop-tail), MTU 1280" in s
                      and "measured fetches: 1," in s)
            row={"seed":seed,"position":i+1,"variant":which,"status":status,"verified":verified,
                 "seconds":float(fetch[1]) if fetch else None,"rtos":int(fetch[4]) if fetch else None,
                 "data_packets":int(fetch[2]) if fetch else None,"retransmissions":int(fetch[3]) if fetch else None,
                 "queue_drops":int(drops[1]) if drops else None,"per_rto_median":int(per[1]) if per else None,
                 "bytes_confirmed":2800000 if status=="complete" else None,"exit":code,"load1":load,
                 "wall_seconds":round(wall,3),"raw_sha256":hashlib.sha256(raw).hexdigest()}
            rows.append(row)
            # A killed or timed-out test still leaves every completed row in the
            # original test log and its corresponding raw trace on disk.
            print("QUEUE_SAMPLE "+json.dumps(row,sort_keys=True),flush=True)
            (out/"samples.json").write_text(json.dumps({"flags":FLAGS,"rows":rows,"load_gate_seconds":round(paused,2),
                                                     "logical_cpus":len(os.sched_getaffinity(0))},indent=2))
    assert len(rows)==30 and all(r["verified"] for r in rows),[(r["seed"],r["variant"]) for r in rows if not r["verified"]]


if __name__=="__main__":
    main()
