#!/usr/bin/python3
# Fork-only replay of one queue event from the two-sender benchmark.
import bisect
import errno
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time

US = 1000
GUEST = "slotguest"
RECEIVER = "slotpeer"


def run(*command: str) -> None:
    subprocess.run(command, check=True)


def checksum(packet: bytes) -> int:
    padded = packet + bytes(len(packet) % 2)
    value = sum(n for n, in struct.iter_unpack("!H", padded))
    while value >> 16:
        value = (value & 0xffff) + (value >> 16)
    return ~value & 0xffff


def frame(side: str, seq: int, length: int, ident: int) -> bytes:
    # Reconstruct the two TCP flow identities, actual sequence and wire length.
    # The original snaplen does not retain payloads; this is an open-loop queue test.
    if side == "g":
        addr, sport, dport = "10.0.0.2", 62795, 44000
    else:
        addr, sport, dport = "10.0.0.6", 54758, 44001
    src, dest = socket.inet_aton(addr), socket.inet_aton("10.0.0.3")
    tcp = struct.pack("!HHIIHHHH", sport, dport, seq, 1, 0x8010, 65535, 0, 0)
    tcp += struct.pack("!BBBBII", 1, 1, 8, 10, 1, 1) + bytes(length - 66)
    check = checksum(src + dest + struct.pack("!BBH", 0, 6, len(tcp)) + tcp)
    tcp = tcp[:16] + struct.pack("!H", check) + tcp[18:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, length - 14, ident, 0x4000, 64, 6, 0, src, dest)
    mac = bytes.fromhex("0200000000020200000000010800")
    return mac + ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:] + tcp


def attributes(data: bytes):
    offset = 0
    while offset + 4 <= len(data):
        size, kind = struct.unpack_from("=HH", data, offset)
        if size < 4 or offset + size > len(data):
            raise ValueError("invalid rtnetlink attribute")
        yield kind & 0x3fff, data[offset + 4:offset + size]
        offset += (size + 3) & ~3


class Netem:
    """Read the real queue's dequeue, drop and instantaneous depth counters."""
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
        self.sock.settimeout(1)
        self.sock.bind((0, 0))
        self.index = socket.if_nametoindex(GUEST)
        self.seq = 0

    def stats(self) -> dict[str, int]:
        self.seq += 1
        req = struct.pack("=BBHiIII", 0, 0, 0, self.index, 0, 0, 0)
        self.sock.sendto(struct.pack("=IHHII", len(req)+16, 38, 0x301, self.seq, 0)+req, (0,0))
        result = None
        while True:
            data = self.sock.recv(65536)
            offset = 0
            while offset + 16 <= len(data):
                size, kind, _, sequence, _ = struct.unpack_from("=IHHII", data, offset)
                assert sequence == self.seq and size >= 16
                payload = data[offset+16:offset+size]
                offset += (size+3) & ~3
                if kind == 3:
                    assert result is not None, "netem missing from RTM_GETQDISC"
                    return result
                if kind == 2:
                    raise OSError(-struct.unpack_from("=i",payload)[0], "RTM_GETQDISC")
                if kind != 36:
                    continue
                _, _, _, index, handle, _, _ = struct.unpack_from("=BBHiIII", payload)
                if index != self.index or handle != 0x7a910000:
                    continue
                attrs = dict(attributes(payload[20:]))
                assert attrs[1].rstrip(b"\0") == b"netem"
                stats = dict(attributes(attrs[7]))
                _, packets = struct.unpack_from("=QI", stats[1])
                depth, _, drops = struct.unpack_from("=III", stats[3])
                result = dict(dequeued=packets, depth=depth, drops=drops)


def wait_until(when: int) -> None:
    while time.time_ns() < when:
        pass


def receive(sock: socket.socket) -> tuple[int, int]:
    p, ancillary, _, _ = sock.recvmsg(2048, 64)
    assert p[12:14] == bytes.fromhex("0800") and p[26:29] == bytes((10, 0, 0))
    stamp, = [x for level, kind, x in ancillary if level == socket.SOL_SOCKET and kind == 35]
    sec, nsec = struct.unpack("=qq", stamp)
    return int.from_bytes(p[18:20],"big"), sec * 1000000000 + nsec


def send(sock: socket.socket, data: bytes) -> dict[str, int]:
    start = time.time_ns()
    result = 0
    try:
        assert sock.send(data) == len(data)
    except OSError as e:
        if e.errno != errno.ENOBUFS:
            raise
        result = errno.ENOBUFS
    return dict(start=start, end=time.time_ns(), errno=result)


def trial(shape: dict, guest_us: int, spare: bool, linux_us: int = 1540) -> dict:
    run("tc", "qdisc", "replace", "dev", GUEST, "root", "handle", "7a91:", "netem",
        "limit", "100", "delay", "50ms", "rate", "20Mbit")
    q = Netem()
    before = q.stats()
    assert before == dict(dequeued=0,depth=0,drops=0), before
    records = [shape['anchor'], *shape['pending']]
    if not spare:
        # The taps saw 98 data packets, not a packet-by-packet dump of a 100-slot
        # qdisc. Supply the unobserved slot explicitly rather than calling it payload.
        records.append(["g", 0, 1486])
    warm = [frame(*r, ident=i+1) for i,r in enumerate(records)]
    pair = [frame(*r, ident=501+i) for i,r in enumerate(shape['arrivals'])]
    assert len(shape['pending']) == 98 and len(pair) == 4
    obs = {}
    with (socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x0800)) as tx,
          socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x0800)) as rx):
        tx.bind((GUEST,0))
        rx.bind((RECEIVER,0))
        rx.setsockopt(socket.SOL_SOCKET,35,1)  # Linux SO_TIMESTAMPNS_OLD; native 64-bit runner.
        rx.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1 << 20)
        rx.settimeout(1)
        for data in warm:
            assert send(tx,data)["errno"] == 0
        obs['seeded'] = q.stats()
        first, anchor = receive(rx)
        assert first == 1
        received = [(first,anchor)]
        for label, group, at_us, spacing in (("g",pair[:2],guest_us,5), ("linux",pair[2:],linux_us,2)):
            when = anchor + at_us*US
            wait_until(when-100*US)
            obs[label+"_pre"] = q.stats()
            wait_until(when)
            first_send = send(tx,group[0])
            wait_until(first_send['start']+spacing*US)
            second_send = send(tx,group[1])
            obs[label+"_tx"] = [first_send,second_send]
            obs[label+"_post"] = q.stats()
        # The last seeded frame is due around +60ms, followed by the accepted
        # test frames; a quiet 250ms closes reception, including reordered IDs.
        while select.select([rx],[],[],.25)[0]:
            received.append(receive(rx))
    obs['final'] = q.stats()
    q.sock.close()
    run("tc","qdisc","del","dev",GUEST,"root")
    ids = [i for i,_ in received]
    assert len(ids) == len(set(ids)),ids
    assert [i for i in ids if i < 500] == list(range(1,len(warm)+1)),ids
    obs.update(guest_us=guest_us,linux_us=linux_us,spare=spare,anchor_ns=anchor,
               early_rx_ns=[ns-anchor for _,ns in received[:6]],
               experimental_rx={i:ns-anchor for i,ns in received if i>500})
    expected_deqs = 1 if guest_us < 600 else 2
    # With timestamps on the receiving veth and netem's own dequeue counter,
    # separate schedule misses from a real enqueue or delivery mismatch.
    checks = []
    for key,expected in (('g',expected_deqs),('linux',3)):
        start = obs[key+'_tx'][0]['start']; end = obs[key+'_tx'][1]['end']
        expected_time=anchor+(guest_us if key=='g' else linux_us)*US
        checks.extend([0 <= start-expected_time < 70*US,
                       0 <= obs[key+'_tx'][1]['start']-start < 65*US,
                       obs[key+'_pre']['dequeued']==expected,obs[key+'_post']['dequeued']==expected,
                       bisect.bisect_left([ns for _,ns in received],start)==expected,
                       bisect.bisect_right([ns for _,ns in received],end)==expected])
    obs['timed'] = all(checks)
    obs['expected_dequeues'] = dict(g=expected_deqs,linux=3)
    expect = [501,502,503,504] if spare else ([501,503,504] if expected_deqs==1 else [501,502,503])
    assert obs['seeded']==dict(dequeued=0,depth=len(warm),drops=0),obs
    assert obs['final']==dict(dequeued=len(ids),depth=0,drops=len(warm)+4-len(ids)),obs
    obs['expected_rx'] = expect
    obs['correct'] = sorted(obs['experimental_rx']) == expect
    return obs


def main() -> None:
    if len(sys.argv)==1:
        # Same privileged native Linux environment as the existing TCP benchmark.
        out=Path(os.environ['QUEUE_REPLAY_OUTPUT']).resolve()
        fixture=Path("test/benchmarks/tcp/queue_slot_fixture.json").resolve(strict=True)
        run("sudo","-n","unshare","--net",sys.executable,str(Path(__file__).resolve()),str(fixture),str(out))
        return
    shape=json.loads(Path(sys.argv[1]).read_text())
    out=Path(sys.argv[2])
    Path('/proc/sys/net/ipv6/conf/all/disable_ipv6').write_text('1')
    Path('/proc/sys/net/ipv6/conf/default/disable_ipv6').write_text('1')
    run("ip","link","add",GUEST,"type","veth","peer","name",RECEIVER)
    run("ip","link","set",GUEST,"address","02:00:00:00:00:01","up")
    run("ip","link","set",RECEIVER,"address","02:00:00:00:00:02","up")
    os.sched_setaffinity(0,{min(os.sched_getaffinity(0))})
    cases=[("before",320,False),("before_shift",455,False),("after",830,False),("spare",320,True)]
    result=dict(capture=shape['capture'],observations=[],completed=False)
    def save():
        (out/'queue-slot-verdict.json').write_text(json.dumps(result,indent=2)+"\n")
    save()
    for cycle in range(3):
        for name,when,spare in cases[::1 if cycle%2==0 else -1]:
            for attempt in range(3):
                obs=trial(shape,when,spare)
                obs.update(name=name,cycle=cycle,attempt=attempt)
                result["observations"].append(obs)
                save()
                if obs['timed']:
                    assert obs['correct'], obs
                    break
            else:
                raise RuntimeError(f"No timed {name} trial in cycle {cycle}")
    # The original nearest receiver timestamp is only a tap witness. Also try
    # the original offsets literally, retaining scheduler misses separately.
    captured_us = -shape['observed_anchor_offset_us']
    for name,when in [('capture_before',captured_us),('capture_after',captured_us+80)]:
        for attempt in range(3):
            obs=trial(shape,when,False,linux_us=captured_us+shape['shifts_us'][2])
            obs.update(name=name,cycle='original-offset',attempt=attempt)
            result['observations'].append(obs)
            save()
            if obs['timed']:
                assert obs['correct'],obs
                break
    result['completed']=True
    save()
    print([(x['name'],x['timed'],sorted(x['experimental_rx'])) for x in result['observations']])


if __name__=='__main__':
    main()
