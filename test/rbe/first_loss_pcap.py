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


def pcap_frames(data):
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
    return frames
