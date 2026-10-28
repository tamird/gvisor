"""Read the original PacketImpact Ethernet capture, retaining the controlled flow."""
import hashlib
import struct


def need(value, reason):
    if not value:
        raise ValueError(reason)


def sacks(opts):
    blocks = []
    while opts:
        kind = opts[0]
        if kind == 0:
            break
        if kind == 1:
            opts = opts[1:]
            continue
        need(len(opts) >= 2 and 2 <= opts[1] <= len(opts), "invalid TCP option")
        size = opts[1]
        if kind == 5:
            need(size >= 10 and (size - 2) % 8 == 0, "invalid SACK option")
            blocks.extend(struct.iter_unpack("!II", opts[2:size]))
        opts = opts[size:]
    return blocks


def inspect_pcap(data):
    need(len(data) >= 24 and len(data) < 8 << 20, "pcap size")
    endians = {b"\xd4\xc3\xb2\xa1":"<", b"\xa1\xb2\xc3\xd4":">", b"\x4d\x3c\xb2\xa1":"<", b"\xa1\xb2\x3c\x4d":">"}
    endian = endians[data[:4]]
    need(struct.unpack_from(endian+"I",data,20)[0] == 1, "expected Ethernet pcap")
    frames = []
    pos = 24
    while pos < len(data):
        need(pos + 16 <= len(data), "truncated record")
        sec, subsec, size, original = struct.unpack_from(endian+"IIII",data,pos)
        pos += 16
        need(size <= original and pos+size <= len(data), "truncated frame")
        fr = data[pos:pos+size]
        pos += size
        if len(fr) < 34 or fr[12:14] != b"\x08\x00" or fr[23] != 6:
            continue
        iplen = struct.unpack_from("!H",fr,16)[0]
        iph = (fr[14] & 15)*4
        start = 14+iph
        need(iph >= 20 and len(fr) >= 14+iplen and iplen >= iph+20, "short IPv4 TCP frame")
        tcp = fr[start:14+iplen]
        off = (tcp[12] >> 4)*4
        need(20 <= off <= len(tcp), "invalid TCP data offset")
        sport,dport,seq,ack = struct.unpack_from("!HHII",tcp)
        frames.append(dict(sport=sport,dport=dport,seq=seq,ack=ack,flags=tcp[13],opts=tcp[20:off],
                           sacks=sacks(tcp[20:off]),payload=tcp[off:],timestamp=[sec,subsec]))
    syns = [f for f in frames if f["flags"] & 0x12 == 2]
    need(len(syns) >= 1 and len({(f["sport"],f["dport"],f["seq"]) for f in syns}) == 1, "one controlled handshake")
    syn = syns[0]
    peer = syn["sport"],syn["dport"]
    base = (syn["seq"]+1) & 0xffffffff
    rel = lambda n: (n-base) & 0xffffffff
    same = lambda f,p: (f["sport"],f["dport"]) == p
    markers = [(i,f) for i,f in enumerate(frames) if same(f,peer) and (f["payload"] or f["flags"] & 1)]
    stream = b"abcdefghijklmnop"
    ranges = [(8,16),(8,16),(10,14),(0,8),(16,16)]
    observed = [(rel(f["seq"]),f["payload"]) for _,f in markers]
    need(observed == [(lo,stream[lo:hi]) for lo,hi in ranges] and markers[-1][1]["flags"] & 1, "original controlled sends including FIN: "+repr(observed))
    out = []
    for n,((i,f),(lo,hi)) in enumerate(zip(markers,ranges)):
        nxt = markers[n+1][0] if n+1 < len(markers) else len(frames)
        want_ack = (0,0,0,16,17)[n]
        acks = [g for g in frames[i+1:nxt] if same(g,peer[::-1]) and g["flags"] == 16 and not g["payload"] and rel(g["ack"]) == want_ack]
        need(bool(acks), "missing cumulative ACK after "+str([lo,hi]))
        a = acks[0]
        out.append(dict(sent=[lo,hi],sent_timestamp=f["timestamp"],ack=rel(a["ack"]),
                        blocks=[[rel(x),rel(y)] for x,y in a["sacks"]],ack_timestamp=a["timestamp"]))
    return {"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data),"isn":syn["seq"],"ports":peer,"frames":out}
