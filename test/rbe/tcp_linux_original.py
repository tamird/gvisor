"""Read four synthetic TCP flows from one already completed PacketImpact test."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import textwrap
import traceback
import zipfile

from first_loss_pcap import pcap_frames, sacks


SOURCE = "d66cc79f194a3341766fc69c2822f18c09a3f737"
CHILD = "43474d02-9e00-4aa2-aa14-162c9005ae13"
ZIP = "7814ab1ddc221408962427713dadd1bf311ad50f1c1725fad208faf166f43778"
SIZE = 128386
SOCKETS = "54b8e91a917b132367e0286e46604b164cdc0038f113a3a6a3c2811b0368458f"
TMP = Path(os.environ["RUNNER_TEMP"])


def transfer():
    # The unchanged fixed-origin downloader is verified before the key is set.
    from original_reader import request
    body = bytearray()
    count = request(os.environ.pop("BUILDBUDDY_API_KEY"), "/api/v1/GetFile",
                    {"uri": f"bytestream://remote.buildbuddy.io/blobs/{ZIP}/{SIZE}"},
                    SIZE, body.extend)
    assert count == len(body) == SIZE and hashlib.sha256(body).hexdigest() == ZIP
    (TMP / "original.zip").write_bytes(body)


def records(data):
    # Length of TCP payload on the wire comes from IP, not the captured prefix.
    endian = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
              b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}[data[:4]]
    assert struct.unpack_from(endian + "I", data, 20)[0] == 1  # Ethernet.
    pos = 24
    result = []
    while pos < len(data):
        _, _, captured, original = struct.unpack_from(endian + "IIII", data, pos)
        pos += 16
        assert captured <= original and pos + captured <= len(data)
        frame = data[pos:pos + captured]
        pos += captured
        if len(frame) < 34 or frame[12:14] != b"\x08\x00" or frame[23] != 6:
            continue
        # A fragment or truncated transport header cannot witness a TCP
        # sequence number. Omitting it will leave a gap if the flow needed it.
        if frame[14] >> 4 != 4 or struct.unpack_from("!H", frame, 20)[0] & 0x3fff:
            continue
        ip_header = (frame[14] & 15) * 4
        total = struct.unpack_from("!H", frame, 16)[0]
        start = 14 + ip_header
        if ip_header < 20 or len(frame) < start + 20:
            continue
        tcp_header = (frame[start + 12] >> 4) * 4
        if not 20 <= tcp_header <= min(total - ip_header, captured - start):
            continue
        sp, dp, seq, ack = struct.unpack_from("!HHII", frame, start)
        result.append(((frame[26:30], sp, frame[30:34], dp), seq, ack, frame[start + 13],
                       total-ip_header-tcp_header, min(total+14,captured)-start-tcp_header,
                       sacks(frame[start+20:start+tcp_header])))
    return result


def flow(rows, sample):
    head, mss = sample["RepairSeq"], sample["MSS"]
    def offset(value):
        return ((value-head+(1<<31)) % (1<<32))-(1<<31)
    assert offset(sample["BurstSeq"]) == 37*mss
    [forward] = {key for key,seq,_,_,n,_,_ in rows if seq == head and n > 0}
    reverse = (*forward[2:], *forward[:2])
    # Only normalized sequence ranges, flags and sizes of the synthetic flow
    # can leave the job. Never include IPs, ports, payloads or unrelated flows.
    frames = []
    for i,(key,seq,ack,flags,n,visible,blocks) in enumerate(rows):
        if key == forward:
            frames.append([i,1,offset(seq),n,visible,flags,[]])
        if key == reverse:
            frames.append([i,0,offset(ack),n,visible,flags,[[offset(a),offset(b)] for a,b in blocks]])
    tx = [f for f in frames if f[1] and f[3]]
    rx = [f for f in frames if not f[1] and f[5]&16]
    initial = [f[0] for f in tx if f[2] == 0 and f[3] >= mss]
    reports = [next((f[0] for f in rx if f[2] == 0 and [mss,n*mss] in f[6]), None) for n in (2,3,4)]
    ack = next((f[0] for f in rx if len(initial)>=2 and f[0]>initial[-1] and f[2]==5*mss),None)
    pairs = 0
    last = ack
    for n in range(32):
        if last is None: break
        begin = (5+n)*mss
        seg = next((f for f in tx if f[0]>last and f[2] == begin and f[3] == mss),None)
        last = next((f[0] for f in rx if seg and f[0]>seg[0] and f[2]==begin+mss),None)
        if last is not None: pairs += 1
    burst = []
    cursor = 37*mss
    for f in tx:
        if last is not None and f[0]>last and f[2]==cursor:
            burst.append([f[0],f[3],f[4]])
            cursor += f[3]
            if cursor-37*mss >= sample["BurstBytes"]: break
    advancing = [f[0] for f in rx if last is not None and burst and last < f[0] < burst[-1][0] and f[2] > 37*mss]
    ok = (len(initial)==2 and all(v is not None for v in reports) and
          initial[0]<reports[0]<reports[1]<reports[2] and reports[0]<initial[1] and
          ack is not None and pairs==32 and cursor-37*mss==sample["BurstBytes"] and not advancing)
    value = {"verified":ok,"head_records":initial,"sack_records":reports,"cover":ack,
             "single_data_ack_pairs":pairs,"burst_records":[{"record":i,"ip_payload":n,"captured_payload":v} for i,n,v in burst],
             "burst_tcp_payload_bytes":cursor-37*mss,"advancing_acks_during_burst":advancing}
    if not ok: value["normalized_headers"] = frames
    return value


def summarize():
    [socket_file] = (TMP / "socket").rglob("result.json")
    body = socket_file.read_bytes()
    assert hashlib.sha256(body).hexdigest() == SOCKETS
    socket = json.loads(body)
    assert socket["child"]==CHILD and socket["source"]==SOURCE and socket["native_exit"]==0
    raw = (TMP / "original.zip").read_bytes()
    assert len(raw)==SIZE and hashlib.sha256(raw).hexdigest()==ZIP
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        [entry] = [v for v in archive.infolist() if v.filename.endswith(".pcap")]
        assert 24 <= entry.file_size < 8<<20
        data = archive.read(entry)
    digest = hashlib.sha256(data).hexdigest()
    result = {"source":SOURCE, "child":CHILD, "original_native_exit":0, "original_projection":socket["status"],
              "zip":{"bytes":SIZE,"sha256":ZIP},"pcap":{"bytes":len(data),"sha256":digest},"modes":{}}
    try:
        source = Path("test/rbe/first_loss_result.py").read_text()
        old = source[source.index("    parsed ="):source.index("    assert not code")]
        env = dict(captures={digest:data}, result=copy.deepcopy(socket), pcap_frames=pcap_frames)
        try:
            exec(compile(textwrap.dedent(old), "held_pcap_join", "exec"), env)
        except (AssertionError, KeyError, ValueError, StopIteration) as e:
            loc = traceback.extract_tb(e.__traceback__)
            result["old_decoder"] = {"class":type(e).__name__, "detail":str(e) if str(e) in {
                "min() arg is an empty sequence","short IPv4 TCP frame","invalid TCP data offset","invalid TCP option",
                "invalid SACK option","truncated record","truncated frame"} else None,
                "location":[[Path(v.filename).name,v.lineno] for v in loc if v.filename.endswith("first_loss_pcap.py") or v.filename=="held_pcap_join"]}
        rows = records(data)
        for mode, sample in socket["modes"].items():
            result["modes"][mode] = flow(rows, sample)
        assert len(result["modes"])==4 and all(v["verified"] for v in result["modes"].values())
        result["status"]="PASS"
    except (ValueError, KeyError, AssertionError, struct.error) as e:
        result["status"]="INCOMPLETE"
        result["failure"]={"class":type(e).__name__, "line":traceback.extract_tb(e.__traceback__)[-1].lineno}
    (TMP/"packet-summary.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
    return int(result["status"]!="PASS")


if __name__ == "__main__":
    if sys.argv[1:] == ["transfer"]:
        transfer()
    else:
        assert sys.argv[1:] == ["summarize"]
        sys.exit(summarize())
