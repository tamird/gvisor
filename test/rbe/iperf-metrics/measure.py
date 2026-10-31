"""Record one client transfer and the independent receiver's byte count."""
import csv
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

kind, exe, flavor = sys.argv[1:]
root=Path("/proof") / flavor
root.mkdir(exist_ok=True)
version=subprocess.run([exe,"--version" if kind=="3" else "-v"],capture_output=True,text=True)
(root/"version").write_text(version.stdout+version.stderr)
results=[]
trials=[(1,1),(65536,1),(1,4),(1027,4),(65536,4)]
for i,(n,p) in enumerate(trials):
    case=f"n{n}-p{p}"
    case_dir=root/case
    case_dir.mkdir(exist_ok=True)
    port=17921+i
    server_cmd=[exe,"-s","-p",str(port)]
    if kind=="2": server_cmd+=["-y","C"]
    else: server_cmd+=["-1","--json"]
    proc=subprocess.Popen(server_cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        for _ in range(200):
            listening=False
            for f in ("/proc/net/tcp","/proc/net/tcp6"):
                for line in Path(f).read_text().splitlines()[1:]:
                    fields=line.split()
                    if int(fields[1].split(":")[1],16)==port and fields[3]=="0A": listening=True
            if listening: break
            if proc.poll() is not None: raise RuntimeError("server exited before accepting a connection")
            time.sleep(.025)
        else: raise RuntimeError("server did not listen")
        cmd=[exe]
        if kind=="2":
            per=n if p==1 else max(n//p,1)
            cmd+=["--format","K","--realtime","--len","128K","--num",str(per)+"K","--client","127.0.0.1","--port",str(port)]
        else:
            cmd+=["--json","--length","128K","--bytes",str(n)+"K","--client","127.0.0.1","--port",str(port)]
        if p>1: cmd+=["--parallel",str(p)]
        started=time.monotonic()
        client=subprocess.run(cmd,capture_output=True,text=True,timeout=35)
        elapsed=time.monotonic()-started
        (case_dir/"command").write_text(" ".join(cmd)+"\n")
        (case_dir/"client.txt").write_text(client.stdout+client.stderr)
        if client.returncode: raise RuntimeError(f"client exit {client.returncode}")
        if kind=="2":
            # The write shutdown propagates before the server receives SIGINT.
            time.sleep(.35)
            proc.send_signal(signal.SIGINT)
        try: server_stdout,server_stderr=proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.terminate()
            server_stdout,server_stderr=proc.communicate(timeout=5)
        (case_dir/"server.txt").write_text(server_stdout+server_stderr)
        row={"case":case,"iterations":n,"streams":p,"nominal_total":n*1024,"process_seconds":elapsed,"command":cmd}
        if kind=="2":
            row["requested_total"]=per*1024*p
            numeric=r"\[\s*\d+\][^\n]+\s+(\d+\.?\d*)\s+KBytes/sec"
            agg=r"\[\s*SUM\][^\n]+\s+(\d+\.?\d*)\s+KBytes/sec"
            single=re.search(numeric,client.stdout)
            summed=re.search(agg,client.stdout)
            if not single: raise RuntimeError("missing per-stream bandwidth")
            row["first_KiBps"]=float(single[1])
            row["aggregate_KiBps"]=float(summed[1]) if summed else None
            # iperf2 CSV: time, source addr/port, dst addr/port, thread ID,
            # interval, transfer in bytes and bitrate. Filter the SUM row.
            csv_rows=list(csv.reader(server_stdout.splitlines()))
            streams=[r for r in csv_rows if len(r)>=9 and r[5].strip().isdigit() and r[7].strip().isdigit()]
            if len(streams)!=p:
                raise RuntimeError(f"expected {p} receiver stream summaries; got {csv_rows!r}")
            row["receiver_bytes"]=sum(int(r[7]) for r in streams)
            row["receiver_stream_bytes"]=[int(r[7]) for r in streams]
        else:
            data=json.loads(client.stdout)
            if "error" in data: raise RuntimeError(data["error"])
            row["requested_total"]=n*1024
            row["sender_bytes"]=data["end"]["sum_sent"]["bytes"]
            row["receiver_bytes"]=data["end"]["sum_received"]["bytes"]
            row["aggregate_bytes_per_second"]=data["end"]["sum_sent"]["bits_per_second"]/8
        results.append(row)
        print(flavor, row, flush=True)
    except Exception as exc:
        results.append({"case": case, "error": str(exc)})
        print(flavor, case, str(exc), flush=True)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.communicate()
    (root/"observations.json").write_text(json.dumps(results,indent=2)+"\n")
