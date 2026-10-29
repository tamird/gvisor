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


def records(data):
    # Length of TCP payload on the wire comes from IP, not the captured prefix.
    endian = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
              b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}[data[:4]]
    assert struct.unpack_from(endian + "I", data, 20)[0] == 1  # Ethernet.
    scale = 1 if data[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d") else 1000
    pos = 24
    result = []
    while pos < len(data):
        sec, frac, captured, original = struct.unpack_from(endian + "IIII", data, pos)
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
                       sacks(frame[start+20:start+tcp_header]),sec*1_000_000_000 + frac*scale))
    return result


def wire_result(rows, sample, digest):
    head, mss = sample["RepairSeq"], sample["MSS"]
    def off(value):
        return ((value-head+(1<<31)) % (1<<32))-(1<<31)
    keys = {r[0] for r in rows if r[1]==head and r[4]}
    if not keys:
        return None
    [forward] = keys
    reverse = (*forward[2:], *forward[:2])
    tx = [(i,off(seq),n,visible,ts) for i,(key,seq,ack,flags,n,visible,sacks_,ts) in enumerate(rows) if key==forward and n]
    rx = [(i,off(ack),ts,[(off(a),off(b)) for a,b in sacks_]) for i,(key,seq,ack,flags,n,visible,sacks_,ts) in enumerate(rows) if key==reverse and flags&16]
    repairs = [i for i,s,n,_,_ in tx if s==0 and n==mss]
    reports = [next(i for i,ack,_,blocks in rx if ack==0 and (mss,n*mss) in blocks) for n in (2,3,4)]
    assert len(repairs)==2 and repairs[0] < reports[0] < reports[1] < reports[2] and reports[0] < repairs[1]
    prev = next(i for i,ack,_,_ in rx if i>repairs[1] and ack==5*mss)
    cursor = 5*mss
    seen = []
    for index, f in enumerate([sample["Prime"], *sample["Flights"]]):
        assert off(f["Start"]) == cursor
        packets = []
        before = cursor
        for i,s,n,c,ts in tx:
            if i <= prev:
                continue
            assert s == cursor and n > 0
            packets.append((i,n,c,ts))
            cursor += n
            if cursor >= before + f["Bytes"]:
                break
        assert cursor == before + f["Bytes"] and packets
        assert not any(prev < i < packets[-1][0] and ack > before for i,ack,_,_ in rx)
        previous_ack = next(ts for i,_,ts,_ in rx if i == prev)
        seen.append({"bytes":cursor-before, "captured_payload_bytes":sum(e[2] for e in packets),
                     "frames":len(packets),"ack_to_first_data_ns":packets[0][3]-previous_ack})
        if index < 5:
            prev = next(i for i,ack,_,_ in rx if i>packets[-1][0] and ack==cursor)
    gap = seen[1]["ack_to_first_data_ns"]
    assert gap >= sample["IdleNS"] and (sample["IdleNS"] or gap < sample["PreRTO"]*1000)
    return {"pcap":digest, "overlapping_repair_frames":len(repairs), "sack_reports":len(reports),
            "prime":seen[0], "flights":seen[1:]}
