"""Connect the unmodified gVisor TCP benchmark proxy to an actual Linux TCP receiver."""
import ctypes
from collections import deque
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time

MSS = 480
N = 32
WARM = bytes(range(240)) * 2048
DATA = b''.join(bytes([i])*MSS for i in range(N))
PORT = 45201
ETH = 3
MASK = 2**32 - 1

def network(*args):
    subprocess.run(args,check=True,stdout=subprocess.DEVNULL)

def ns():
    if ctypes.CDLL(None,use_errno=True).unshare(0x40000000):
        raise OSError(ctypes.get_errno(),'unshare network namespace')
    network('ip','link','set','lo','up')

def tcp(frame):
    if frame[12:14] != b'\x08\x00' or frame[23] != 6:return
    ip=14; h=ip+(frame[ip]&15)*4; th=(frame[h+12]>>4)*4
    src,dst,seq,ack=struct.unpack_from('!HHII',frame,h)
    n=struct.unpack_from('!H',frame,ip+2)[0]-(h-ip)-th
    opts=memoryview(frame)[h+20:h+th]; blocks=[]
    while opts and opts[0]:
        kind=opts[0]
        if kind==1:opts=opts[1:];continue
        k=opts[1]
        if k<2 or k>len(opts):raise ValueError('bad TCP option')
        if kind==5:blocks.extend(struct.iter_unpack('!II',opts[2:k]))
        opts=opts[k:]
    return src,dst,seq,ack,frame[h+13],n,blocks

def trial(binary,shrink,report):
    os.umask(0o022)
    ns()
    network('ip','link','add','sender','type','veth','peer','name','wire')
    for ifc in ('wire','sender'):
        network('ip','link','set',ifc,'up','promisc','on')
        network('ethtool','-K',ifc,'tx','off','rx','off','tso','off','gso','off','gro','off')
    wire=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(ETH));wire.bind(('wire',0))
    wire.setsockopt(socket.SOL_SOCKET,33,4<<20)  # SO_RCVBUFFORCE on our relay.
    wire.setsockopt(263,23,1)  # SOL_PACKET, PACKET_IGNORE_OUTGOING.
    client=socket.socket()
    log=open(report.with_suffix('.proxy.log'),'wb')
    native=binary=='linux'
    if native:
        network('ip','address','add','192.0.2.2/24','dev','sender')
        Path('/proc/sys/net/ipv4/tcp_timestamps').write_text('0')
        Path('/proc/sys/net/ipv4/tcp_recovery').write_text('1')
        client.setsockopt(socket.IPPROTO_TCP,socket.TCP_CONGESTION,b'reno')
        client.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        assert client.getsockopt(socket.IPPROTO_TCP,socket.TCP_CONGESTION,16).rstrip(b'\x00')==b'reno'
        proxy=None
    else:
        proxy=subprocess.Popen([binary,'-client','-iface=sender','-addr=192.0.2.2','-mask=24','-sack','-rack','-port=44000','-forward=192.0.2.1:45201','-client_tcp_probe_file='+str(report.with_suffix('.probe'))],stdout=log,stderr=log,env={**os.environ,'GOMAXPROCS':'1'})
    try:
        # Socket descriptors stay in the namespace where they were created.
        # Only the relay connects the proxy's veth to this receiver's TAP.
        ns()
        Path('/proc/sys/net/ipv4/tcp_timestamps').write_text('0')
        Path('/proc/sys/net/ipv4/tcp_comp_sack_nr').write_text('0')
        tap=os.open('/dev/net/tun',os.O_RDWR)
        fcntl.ioctl(tap,0x400454ca,struct.pack('16sH',b'peer0',0x0002|0x1000))
        network('ip','address','add','192.0.2.1/24','dev','peer0')
        network('ip','link','set','peer0','up')
        srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,131072)
        # gVisor reserves space for four SACK blocks on send. The option and
        # its padding take 36 bytes when timestamps are not negotiated.
        srv.setsockopt(socket.IPPROTO_TCP,socket.TCP_MAXSEG,MSS if native else MSS+36);srv.bind(('192.0.2.1',PORT));srv.listen()
        if native:
            client.setblocking(False)
            assert client.connect_ex(('192.0.2.1',PORT)) in (0,errno.EINPROGRESS)
        target=None;peer=None;phase='handshake';base=None;start=None;opened=None;seen=set();sacked=set();saved={};released=set();history=[];body=bytearray()
        cum=0;wire_ack=0;cap=None;warmack=False;warm_sizes=set();application_done=None
        warm_pending={};warm_next=0;initial_acks=[];pending=memoryview(b'')
        warm_acks=deque()
        deadline=time.monotonic()+15
        capture=report.with_suffix('.pcap').open('wb',buffering=0)
        capture.write(struct.pack('<IHHIIII',0xa1b23c4d,2,4,0,0,65535,1))
        def record(frame):
            sec,nsec=divmod(time.time_ns(),1_000_000_000)
            capture.write(struct.pack('<IIII',sec,nsec,len(frame),len(frame))+frame)
        def deliver(frame):
            os.write(tap,frame);record(frame)
        def started():
            return (time.monotonic_ns()-start)//1000 if start else 0
        try:
            while application_done is None or cum<len(DATA):
                if time.monotonic()>deadline:
                    raise TimeoutError((phase,len(body),cum,sorted(seen),sorted(sacked),sorted(released),history[-15:]))
                if proxy is not None and proxy.poll() is not None: raise RuntimeError(('proxy exited',proxy.returncode))
                while warm_acks and warm_acks[0][0]<=time.monotonic():
                    _,frame,ack=warm_acks.popleft()
                    wire.send(frame)
                    if ack==base:warmack=True
                watch=[wire,tap,peer if peer else srv]
                readable,writable,_=select.select(watch,[client] if pending else [],[],.005)
                if writable:
                    n=client.send(pending[:65536])
                    if not n:raise RuntimeError('send returned zero')
                    pending=pending[n:]
                for s in readable:
                    if s is srv:
                        peer,_=srv.accept()
                        continue
                    if s is peer:
                        part=peer.recv(131072)
                        if not part:raise EOFError('premature EOF')
                        body.extend(part)
                        if len(body)==len(WARM)+len(DATA) and application_done is None:
                            application_done=started()
                        continue
                    if s is wire:
                        frame,addr=wire.recvfrom(65535)
                        if addr[2]==socket.PACKET_OUTGOING:continue
                    else:frame=os.read(tap,65535);record(frame)
                    t=tcp(frame)
                    if t is not None:
                        src,dst,seq,ack,flags,size,blocks=t
                        if s is wire and dst==PORT:
                            if target is None and flags&2:
                                target=src;base=(seq+1+len(WARM))&MASK
                            if src!=target:continue  # the proxy pre-dials its next connection
                            if size and phase=='warm':
                                warm_sizes.add(size)
                                at=(seq-(base-len(WARM)))&MASK
                                if at>=warm_next:warm_pending[at]=frame
                                while warm_next in warm_pending:
                                    f=warm_pending.pop(warm_next);deliver(f);warm_next+=tcp(f)[5]
                                continue
                            if size and phase in ('collect','recover'):
                                off=(seq-base)&MASK
                                # The last warm-up data can be retransmitted even after
                                # its cumulative ACK has passed through the relay.
                                if 0<((base-seq)&MASK)<=len(WARM) and size<=((base-seq)&MASK):
                                    deliver(frame)
                                    continue
                                if off>=len(DATA) or off%MSS or size!=MSS:
                                    raise AssertionError(('unexpected data boundary',off,size))
                                i=off//MSS;again=i in seen;seen.add(i)
                                history.append([started(),'send',i,again])
                                if len(seen)==N and initial_acks:
                                    for initial in initial_acks:wire.send(initial)
                                    initial_acks.clear()
                                if phase=='collect' and i%2==0:
                                    saved.setdefault(i,frame)
                                    continue
                                if phase=='collect' and again:continue
                        elif s==tap and src==PORT and dst==target:
                            wire_ack=ack
                            if phase=='warm':
                                # Warm up across a nonzero RTT to put at least 32 packets
                                # in flight. Measured ACKs bypass this queue entirely.
                                warm_acks.append((time.monotonic()+.004,frame,ack))
                                continue
                            if phase in ('collect','recover'):
                                if 0<((base-ack)&MASK)<=len(WARM):
                                    wire.send(frame)
                                    continue
                                cum=(ack-base)&MASK
                                if cum>len(DATA):raise AssertionError(('ack outside flight',cum))
                                for a,b in blocks:
                                    first_off=(a-base)&MASK;last_off=(b-base)&MASK
                                    for i in range(1,N,2):
                                        if first_off<=i*MSS and last_off>=(i+1)*MSS:sacked.add(i)
                                history.append([started(),'ack',cum,[[(a-base)&MASK,(b-base)&MASK] for a,b in blocks]])
                                # Deliver the initial flight before returning its SACKs.
                                # Otherwise a fast retransmit can consume the window we
                                # need to put all the odd segments in Linux's receive queue.
                                if phase=='collect' and len(seen)<N:
                                    initial_acks.append(frame)
                                    continue
                    if s is wire:deliver(frame)
                    else:wire.send(frame)
                if phase=='handshake' and peer is not None:
                    # The proxy binds its native listener after completing its outbound handshake.
                    if native or 'ready.' in report.with_suffix('.proxy.log').read_text(errors='replace'):
                        if not native:client.connect(('127.0.0.1',44000))
                        assert client.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)==0
                        client.setblocking(False);pending=memoryview(WARM);phase='warm'
                if phase=='warm' and len(body)==len(WARM) and warmack and not warm_acks:
                    if body!=WARM or max(warm_sizes)!=MSS:
                        raise AssertionError(('warm-up payload or segment size',sorted(warm_sizes)))
                    start=time.monotonic_ns();pending=memoryview(DATA);phase='collect'
                if phase=='collect' and len(sacked)==N//2 and len(seen)==N:
                    if shrink:peer.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024)
                    cap=peer.getsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF);opened=started()
                    history.append([opened,'gate',cap]);phase='recover'
                    # Release every held original; all future frames pass unchanged.
                    for i,frame in sorted(saved.items()):
                        deliver(frame);released.add(i);history.append([started(),'deliver-held',i])
            done=application_done
            if body!=WARM+DATA:raise AssertionError('transfer corrupt')
            assert phase=='recover' and len(released)==N//2 and len(sacked)==N//2,(phase,sacked,released)
            drops=struct.unpack('II',wire.getsockopt(263,6,8))[1]
            assert drops==0,drops
            report.write_text(json.dumps(dict(sender='linux-reno' if native else 'gvisor',warm_bytes=len(WARM),relay_path_drops=drops,gate_us=opened,complete_us=done,delivery_us=done-opened,rcvbuf=cap,tcp_port=target,received=len(DATA),sha256=hashlib.sha256(body[len(WARM):]).hexdigest(),sacked=sorted(sacked),wire=history)))
        finally:
            if report.stat().st_size==0:report.write_text(json.dumps(dict(incomplete=True,phase=phase,bytes=len(body),cum=cum,tcp_port=target,gate_us=opened,sacked=sorted(sacked),relay_path_drops=struct.unpack('II',wire.getsockopt(263,6,8))[1],wire=history)))
    finally:
        if proxy is not None:proxy.kill();proxy.wait()
        log.close()

def run():
    if len(sys.argv)==5 and sys.argv[1]=='--peer':
        trial(sys.argv[2],sys.argv[3]=='shrink',Path(sys.argv[4]))
        return
    root=Path(os.environ['TEST_SRCDIR'])/os.environ['TEST_WORKSPACE']
    binary='linux' if os.getenv('TCP_SOURCE_UNDER_TEST')=='linux' else root/sys.argv[1]
    if binary!='linux' and not os.access(binary,os.X_OK):raise FileNotFoundError(binary)
    outputs=Path(os.environ['TEST_UNDECLARED_OUTPUTS_DIR'])
    failed=[]
    for name in ('control1','shrink1','shrink2','control2'):
        case='TestReceiverTransfer/'+name
        print('=== RUN   '+case,flush=True)
        report=outputs/(name+'.json')
        for ext in ('.json','.proxy.log','.pcap','.probe'):report.with_suffix(ext).touch()
        ret=subprocess.run(['sudo','-n','env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin','/usr/bin/python3',str(Path(__file__).resolve()),'--peer',str(binary),name[:-1],str(report)],timeout=25)
        if ret.returncode:
            print('--- FAIL: '+case,flush=True)
            failed.append(name)
        else:
            print('--- PASS: '+case,flush=True)
    if failed:sys.exit(1)

if __name__=='__main__':
    run()
