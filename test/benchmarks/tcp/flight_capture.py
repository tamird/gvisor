from collections import Counter
import csv
import gzip
from pathlib import Path
import shutil
import subprocess


def capture(d, public, name):
    fields = ("frame.time_epoch", "tcp.srcport", "tcp.dstport", "tcp.seq_raw",
              "tcp.ack_raw", "tcp.len", "tcp.flags", "tcp.options.sack_le",
              "tcp.options.sack_re")
    traces = {}
    for dev in ("client.1", "client2.1", "server.1"):
        path = d / f"{dev}.pcap"
        capture = (d / f"{dev}-capture.txt").read_text()
        assert "0 packets dropped by kernel" in capture, (dev, capture)
        dest = public / f"{name}-{dev}-packets.tsv.gz"
        args = ["tshark", "-n", "-r", str(path), "-T", "fields"]
        for field in fields:
            args.extend(["-e", field])
        p = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
        with gzip.open(dest, "wt") as f:
            f.write("\t".join(fields) + "\n")
            shutil.copyfileobj(p.stdout, f)
        assert p.wait() == 0
        rows = []
        with gzip.open(dest, "rt") as src:
            for row in csv.DictReader(src, delimiter="\t"):
                rows.append((float(row[fields[0]]), int(row[fields[1]]), int(row[fields[2]]),
                             int(row[fields[3]]), int(row[fields[4]]), int(row[fields[5]]),
                             int(row[fields[6]], 0), row[fields[7]], row[fields[8]]))
        assert rows, dev
        traces[dev] = rows
    verdict = {}
    for port, dev, stack in [(44000,"client.1","netstack"),(44001,"client2.1","linux")]:
        tx = traces[dev]; rx = traces["server.1"]
        counts = Counter()
        for r in tx:
            if r[2] == port and r[5] > 0:
                counts[r[1]] += r[5]
        flow, sent = counts.most_common(1)[0]
        assert sent > 2e5 and all(length < sent * .05 for peer,length in counts.items() if peer != flow), counts
        def data(rows):
            return [r for r in rows if r[1:3] == (flow,port) and r[5]>0]
        def ack(rows):
            return [r for r in rows if r[1:3] == (port,flow) and r[6]&16]
        tx_data,rx_data,tx_ack,rx_ack = data(tx),data(rx),ack(tx),ack(rx)
        assert len(tx_ack)>60 and len(rx_ack)>60 and len(rx_data)>80
        def first(v): return sum(c-1 for c in Counter((r[3],r[5]) for r in v).values() if c>1)
        out={"client_port":flow,"wan_port":port,"sender_data":len(tx_data),"receiver_data":len(rx_data),
             "sender_payload_bytes":sum(r[5] for r in tx_data),"receiver_payload_bytes":sum(r[5] for r in rx_data),
             "sender_exact_repeats":first(tx_data),"receiver_exact_repeats":first(rx_data),
             "sender_acks":len(tx_ack),"receiver_acks":len(rx_ack),
             "sender_sack_acks":sum(bool(r[7]) for r in tx_ack),"receiver_sack_acks":sum(bool(r[7]) for r in rx_ack)}
        assert any(r[6] & 2 for r in tx if r[1:3]==(flow,port)) and any(r[6]&2 for r in rx if r[1:3]==(flow,port))
        verdict[stack]=out
    return verdict
