"""One fixed-byte reverse iperf run per recovery policy and link condition."""

from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zipfile

from tcp_mss_forwarder import PRIVATE, PUBLIC, command, digest, put, require
from tcp_sack_stall import FIELDS, headers, retain

from tcp_proxy_source_contrast import BASE, BOUNDARY, FLIGHT, OBSERVER, PRODUCTS

ORIGINAL_SHA = "a5828142f146b7f5f4928c5376a2c70c484ba7e2c96b7b93f0e9c50316c8600d"
AMOUNT = 120 * 61440
MSS = 1424
MASK = (1 << 32) - 1


def ns(value):
    return int(Decimal(value) * 1_000_000_000)


def options(raw):
    data = bytes.fromhex(raw.replace(":", ""))
    out = {}
    while data:
        k = data[0]
        if k == 0:
            break
        n = 1 if k == 1 else data[1]
        require(0 < n <= len(data), "bad TCP option")
        out[k] = data[2:n]
        data = data[n:]
    return out


def reads(directory):
    records = []
    for f in sorted(directory.glob("receiver.read.*")):
        for line in f.read_text().splitlines():
            m = re.match(r"([0-9.]+) connect\((\d+), .*sin_port=htons\(44000\)", line)
            if m:
                records.append({"connect": int(m[2]), "at": ns(m[1])})
            m = re.match(r"([0-9.]+) (?:read|recvfrom)\((0x[\da-f]+|\d+), .*\)\s+= (0x[\da-f]+|\d+) <([0-9.]+)>", line)
            if m and (n := int(m[3], 0)):
                records.append({"fd": int(m[2], 0), "enter": ns(m[1]), "at": ns(m[1])+ns(m[4]), "n": n})
    return sorted(records, key=lambda r:r["at"])


def analyze(row):
    orig = row["originals"]
    server, iperf = orig["server-writes.json"], orig["iperf.json"]
    link = server["link"]
    port, base = link["port"], link["base"]
    require(0 < port < 65536 and link["mode"] == row["mode"] and link["mss"] == MSS, "bulk link association")
    matches = [r for r in server["writes"] if r["to"] == "*gonet.TCPConn" and r["remote"] == f"10.0.0.2:{port}" and r["local"] == "10.0.0.3:44000"]
    require(len(matches) == 1 and matches[0]["accepted"].get("61440",0)>0 and server["buffer"] == 61440, "bulk gonet writer")
    require(server["stack"]["in"]["cc"] == "cubic" and server["stack"]["in"]["sack"] and server["stack"]["in"]["recovery"] == 1 and not any([server["gso_max"],server["swgso"],server["gro"]]) and server["gomaxprocs"] == server["num_cpu"] == 2, "sender setup")
    expected={"net.ipv4.tcp_sack":"1","net.ipv4.tcp_timestamps":"0","net.ipv4.tcp_congestion_control":"cubic"}
    require(list(row["sysctl"].values()) == [expected,expected] and len(row["offloads"])==6 and all(len(k)==3 and set(k.values())=={"off"} for k in row["offloads"].values()), "peer/host settings")
    require(all(orig[d+"-qdisc.json"][0]["kind"]=="netem" and orig[d+"-qdisc.json"][0]["drops"]==0 for d in ["wan.0","wan.1"]),"unexpected competing queue loss")
    rows=[r for r in row["wire"] if {r[3],r[4]}=={"44000",str(port)}]
    require(rows and len(row["wire"])<30000 and row["capture_counts"].get("dropped by kernel")==0, "wire capture incomplete")
    syn=[r for r in rows if int(r[5],16)&2]
    require({r[1] for r in syn}=={"10.0.0.2","10.0.0.3"} and all(4 in options(r[-1]) and 8 not in options(r[-1]) for r in syn),"wire SACK/no TS")
    offer=next(r for r in syn if r[1]=="10.0.0.2")
    scale=options(offer[-1]).get(3,b"\0")[0]
    decisions=link["decisions"]
    off=lambda seq:(int(seq)-base)&MASK
    attempts=[(off(v["Seq"]),v["Size"],v["Drop"]) for v in decisions[:10]]
    rtos=[e for e in (server["events"] or []) if e["Remote"]==port and e["Local"]==44000 and e["Why"]=="rto_before"]
    events=[e for e in (server["events"] or []) if e["Remote"]==port and e["Local"]==44000]
    acks=[]
    for r in rows:
        if r[1]!="10.0.0.2" or not int(r[5],16)&16: continue
        opt=options(r[-1]).get(5,b"")
        require(len(opt)%8==0,"bad SACK block length")
        blocks=[[off(int.from_bytes(opt[i:i+4],"big")),off(int.from_bytes(opt[i+4:i+8],"big"))] for i in range(0,len(opt),8)]
        acks.append({"ns":ns(r[0]),"ack":off(r[7]),"sacks":blocks,"window":int(r[8])<<scale})
    data=[(ns(r[0]),off(r[6]),int(r[9])) for r in rows if r[1]=="10.0.0.3" and int(r[9])>0]
    drop=[(off(v["Seq"]),v["Size"]) for v in decisions if v["Drop"]]
    test=iperf["start"]["test_start"]
    require(test["reverse"]==1 and test["bytes"]==AMOUNT and test["blksize"]==61440, "iperf workload")
    result=row["observation"]={"rto_ns":[e["At"] for e in rtos],"wire_data_before_first_rto":None,"drops":link["drops"],"schedule_matched":False}
    endpoint=iperf["start"]["connected"]
    require(len(endpoint)==1,"iperf streams")
    fd=endpoint[0]["socket"]
    origin=max((r["at"] for r in row["syscalls"] if r.get("connect")==fd),default=None)
    rd=[r for r in row["syscalls"] if r.get("fd")==fd and origin is not None and r["enter"]>=origin]
    total=sum(r["n"] for r in rd)
    result.update(app_read_sum=total, last_positive_app_read_ns=rd[-1]["at"] if rd else None)
    receiver=iperf.get("end",{}).get("sum_received",{})
    result.update(received=receiver, sender=iperf.get("end",{}).get("sum_sent"), data_fd=fd,
                  sampled_terminal_acks=[a for a in acks if a["sacks"]][:32])
    result["completed"]=row["exit"]==0 and "error" not in iperf and receiver.get("bytes")==AMOUNT
    result["last_byte_observed"]=bool(result["completed"] and total==AMOUNT and rd)
    result["last_read_after_first_attempt_ms"]=(rd[-1]["at"]-decisions[0]["At"])/1e6 if result["last_byte_observed"] else None
    result["orderly_data_ack_ns"]=next((a["ns"] for a in acks if AMOUNT<=a["ack"]<=AMOUNT+1),None)
    require(server["events_dropped"]==0 and sum(x["Drop"] for x in decisions)==link["drops"],"incomplete decision evidence")
    spans=sorted([(seq,n) for _,seq,n in data]+drop)
    high=0
    for seq,n in spans:
        require(0<=seq<=high and n>0 and seq+n<=AMOUNT, "missing or out-of-range sender payload")
        high=max(high,seq+n)
    result.update(dropped_ranges=drop, unique_payload=high,
                  transmitted_payload=sum(n for _,n in spans), wire_data_packets=len(data),
                  repeated_ranges=len(spans)-len(set(spans)),
                  retransmitted_bytes=sum(n for _,n in spans)-high)
    if result["completed"]:
        require(high==AMOUNT, "missing completed payload on wire")
    if row["mode"]=="open":
        result["zero_loss"]=not link["drops"] and not rtos
        return
    require(link["drops"]>=9 and any(v["Drop"] for v in decisions) and link["released"], "burst not released")
    if not rtos: return
    rto=rtos[0]
    before = [(seq,n) for when,seq,n in data if when<rto["At"]]
    result["wire_data_before_first_rto"]=before
    tail=[[9*MSS,10*MSS]]
    initial=next((a for a in acks if a["ack"]==0 and a["sacks"]==tail and a["ns"]<rto["At"]),None)
    partial=next((a for a in acks if 0<a["ack"]<9*MSS and a["sacks"]==tail and a["ns"]>rto["At"]),None)
    seen=next((e for e in events if partial and e["Why"]=="partial_rto_ack" and off(e["Ack"])==partial["ack"] and [[off(b["Start"]),off(b["End"])] for b in e["Blocks"] or []]==tail),None)
    decision=next((e for e in events if seen and e["Why"]=="rack_ack_entered" and e["At"]>=seen["At"] and e["Ack"]==seen["Ack"] and e["Blocks"]==seen["Blocks"]),None)
    catch=next((a for a in acks if a["ns"]>rto["At"] and off(rto["Nxt"])<=a["ack"]<=AMOUNT+1),None)
    first_head=next((when for when,seq,n in data if when>=rto["At"] and seq==0 and n==MSS),None)
    next_deadline=rtos[1]["At"] if len(rtos)>1 else row["iperf_end_ns"]
    first_new=next((when for when,seq,n in data if when>=rto["At"] and seq>=off(rto["Nxt"])),None)
    result.update(first_new_after_rto_ms=(first_new-rto["At"])/1e6 if first_new else None,
                  prior_flight_copies_before_catchup=sum(rto["At"]<=when<=catch["ns"] and seq<off(rto["Nxt"]) for when,seq,n in data) if catch else None,
                  first_head_wire_ns=first_head, next_rto_or_end_ns=next_deadline,
                  first_rto_prior_state=rto["State"], recovery_active=rto["Active"], first_rto_flight=off(rto["Nxt"]),
                  original_sack=initial,partial=partial,partial_processed=seen,reentered_on_that_ack=decision,
                  catchup_after_rto_ms=(catch["ns"]-rto["At"])/1e6 if catch else None,
                  last_app_read_after_rto_ms=(rd[-1]["at"]-rto["At"])/1e6 if result["last_byte_observed"] else None)
    result["schedule_matched"]=bool(attempts==[(i*MSS,MSS,i!=9) for i in range(10)] and
        before==[(9*MSS,MSS)] and initial and partial and partial["ack"]==MSS and partial["window"]>0 and seen and
        first_head and first_head<=partial["ns"]<next_deadline and rto["At"]<=seen["At"]<next_deadline and
        seen["Una"]==base and seen["State"]==1 and not seen["Probing"] and seen["AckWindow"]>0 and rto["State"]==3 and
        rto["Active"] and rto["Una"]==base and not rto["Probing"] and off(rto["Nxt"])>=10*MSS and
        next(((seq,n) for when,seq,n in data if when>rto["At"]),None)==(0,MSS))


def run():
    PUBLIC.mkdir(parents=True,exist_ok=True); PRIVATE.mkdir(parents=True,exist_ok=True)
    cpus=sorted(os.sched_getaffinity(0))[:2]
    require(len(cpus)==2,"two CPUs")
    builds={}
    helpers={}
    for name,src in (("flight",FLIGHT),("boundary",BOUNDARY)):
        root=Path(os.environ["RUNNER_TEMP"])/name/"mss-forwarder"
        build=json.loads((root/"public/build.json").read_text())
        require(build["source"]==src and build["verdict"]=="PASS" and build["target_completed"],"build source")
        builds[name]=build
        helpers[name]=root/"private/helpers"
        require(all(digest(helpers[name]/k)==v["sha256"] for k,v in build["binaries"].items()),"binary identity")
    require(builds["flight"]["binaries"]["nsjoin"]["sha256"]==builds["boundary"]["binaries"]["nsjoin"]["sha256"] and
            builds["flight"]["binaries"]["tcp_proxy"]["sha256"]!=builds["boundary"]["binaries"]["tcp_proxy"]["sha256"],"binary contrast")
    original=Path(os.environ["RUNNER_TEMP"])/"original.zip"
    require(digest(original)==ORIGINAL_SHA,"original artifact identity")
    with zipfile.ZipFile(original) as z:
        prior=json.loads(z.read("mss-forwarder/public/result.json"))
    require(prior["source"].startswith("ef6a9e2ef") and prior["verdict"]=="MATCHED" and len(prior["trials"])==4, "original verdict")
    original_observations={}
    for t in prior["trials"]:
        analyze(t)
        v=t["observation"]
        require(v["last_byte_observed"] and (v.get("zero_loss") or v["schedule_matched"]), "original observation")
        original_observations[t["implementation"]+"-"+t["mode"]]=v
    reference=original_observations["stock-held"]
    require(reference["dropped_ranges"]==original_observations["guard-held"]["dropped_ranges"] and reference["first_rto_flight"]==10*MSS, "original actual loss and old flight")
    row={"production":BASE,"observer":OBSERVER,"products":PRODUCTS,"original_sha256":ORIGINAL_SHA,
         "original_observations":original_observations,"source":command(["git","rev-parse","HEAD"]),"builds":builds,"fields":FIELDS,
         "host":command(["uname","-srvmo"]),"cpus":cpus,"verdict":"INCOMPLETE","trials":[]}
    out=PUBLIC/"result.json";put(out,row)
    for name,mode in [("flight","open"),("boundary","held"),("flight","held"),("boundary","open")]:
        d=PRIVATE/(name+"-"+mode);d.mkdir()
        t={"implementation":name,"mode":mode}
        row["trials"].append(t)
        argv=["sudo","-n","env","GOMAXPROCS=2","TERMINAL_BURST_MODE="+mode,"taskset","-c",",".join(map(str,cpus)),
              "bash","test/benchmarks/tcp/tcp_benchmark.sh","--helpers",str(helpers[name]),"--no-user-ns","--ideal","--cubic",
              "--disable-linux-gso","--disable-linux-gro","--mss-study",str(d),"--mss-bytes",str(AMOUNT),"--mss-capture","--server","--sack"]
        try:
            with (d/"command.log").open("w") as f:
                t["exit"]=subprocess.run(argv,stdout=f,stderr=subprocess.STDOUT,timeout=65).returncode
        except subprocess.TimeoutExpired:
            t["timeout"]=True
        retain(d,t);put(out,row)
        try:
            t["syscalls"]=reads(d)
            t["wire"]=headers(d/"capture.pcap")
            t["capture_sha256"]=digest(d/"capture.pcap")
            t["capture_counts"]={label:int(n) for n,label in re.findall(r"(?m)^(\d+) packets? (captured|received by filter|dropped by kernel)$",(d/"capture.log").read_text())}
            put(out,row)
            analyze(t)
            v=t["observation"]
            v["same_drops_as_original"]=v["dropped_ranges"]==(reference["dropped_ranges"] if mode=="held" else [])
        except (KeyError, IndexError, TypeError,ValueError,OSError,subprocess.SubprocessError) as exc:
            t["sensor_error"]=type(exc).__name__+": "+str(exc)[:180]
        put(out,row)
    complete=all("sensor_error" not in t and t.get("observation",{}).get("last_byte_observed") for t in row["trials"])
    if complete:
        controls=[t for t in row["trials"] if t["mode"]=="open"]
        holds=[t for t in row["trials"] if t["mode"]=="held"]
        exact=(all(t["observation"]["zero_loss"] for t in controls) and all(t["observation"]["schedule_matched"] for t in holds)
               and all(t["observation"]["first_rto_flight"]==reference["first_rto_flight"] for t in holds))
        row["verdict"]="COMPLETE_SCHEDULE_NOT_MATCHED"
        if exact:
            row["verdict"]="MATCHED" if all(t["observation"]["same_drops_as_original"] for t in holds) else "MATCHED_RECEIVER_SEQUENCE_DIFFERENT_DROPS"
    row["finished_ns"]=time.time_ns();put(out,row)
    require(complete,"one or more transfer or telemetry phases incomplete; see retained originals")


if __name__ == "__main__":
    run()
