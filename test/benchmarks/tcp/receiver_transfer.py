"""Connect the unmodified gVisor TCP benchmark proxy to an actual Linux TCP receiver."""
import ctypes
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
WARM = bytes(range(240)) * 256
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
    client=socket.socket()
    log=open(report.with_suffix('.proxy.log'),'wb')
    proxy=subprocess.Popen([binary,'-client','-iface=sender','-addr=192.0.2.2','-mask=24','-sack','-rack','-port=44000','-forward=192.0.2.1:45201'],stdout=log,stderr=log,env={**os.environ,'GOMAXPROCS':'1'})
    try:
        # Socket descriptors stay in the namespace where they were created.
        # Only the relay connects the proxy's veth to this receiver's TAP.
        ns()
        Path('/proc/sys/net/ipv4/tcp_timestamps').write_text('0')
        tap=os.open('/dev/net/tun',os.O_RDWR)
        fcntl.ioctl(tap,0x400454ca,struct.pack('16sH',b'peer0',0x0002|0x1000))
        network('ip','address','add','192.0.2.1/24','dev','peer0')
        network('ip','link','set','peer0','up')
        srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,131072)
        srv.setsockopt(socket.IPPROTO_TCP,socket.TCP_MAXSEG,MSS);srv.bind(('192.0.2.1',PORT));srv.listen()
        target=None;peer=None;phase='handshake';base=None;start=None;opened=None;seen=set();sacked=set();saved={};released=set();history=[];body=bytearray()
        cum=0;wire_ack=0;first=-1;cap=None;warmack=False;retry_start=None
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
        def release():
            i=cum//MSS
            if cum%MSS==0 and i in saved and i not in released and len(body)>=len(WARM)+cum:
                deliver(saved[i]);released.add(i);history.append([started(),'deliver-held',i])
        try:
            while len(body)<len(WARM)+len(DATA):
                if time.monotonic()>deadline:
                    raise TimeoutError((phase,len(body),cum,sorted(seen),sorted(sacked),sorted(released),history[-15:]))
                if proxy.poll() is not None: raise RuntimeError(('proxy exited',proxy.returncode))
                watch=[wire,tap,peer if peer else srv]
                for s in select.select(watch,[],[],.005)[0]:
                    if s is srv:
                        peer,_=srv.accept()
                        continue
                    if s is peer:
                        part=peer.recv(131072)
                        if not part:raise EOFError('premature EOF')
                        body.extend(part)
                        if phase=='recover':release()
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
                            if size and phase in ('collect','recover'):
                                off=(seq-base)&MASK
                                if off>=len(DATA) or off%MSS or size!=MSS:
                                    raise AssertionError(('unexpected data boundary',off,size))
                                i=off//MSS;again=i in seen;seen.add(i)
                                history.append([started(),'send',i,again])
                                if i%2==0:
                                    saved.setdefault(i,frame)
                                    if phase=='recover':release()
                                    continue
                                if phase=='collect' and again:continue
                        elif s==tap and src==PORT and dst==target:
                            wire_ack=ack
                            if ack==base:warmack=True
                            if phase in ('collect','recover'):
                                cum=(ack-base)&MASK
                                if cum>len(DATA):raise AssertionError(('ack outside flight',cum))
                                for a,b in blocks:
                                    first_off=(a-base)&MASK;last_off=(b-base)&MASK
                                    for i in range(1,N,2):
                                        if first_off<=i*MSS and last_off>=(i+1)*MSS:sacked.add(i)
                                history.append([started(),'ack',cum,[[(a-base)&MASK,(b-base)&MASK] for a,b in blocks]])
                    if s is wire:deliver(frame)
                    else:wire.send(frame)
                    if phase=='recover':release()
                if phase=='handshake' and peer is not None:
                    # The proxy binds its native listener after completing its outbound handshake.
                    if 'ready.' in report.with_suffix('.proxy.log').read_text(errors='replace'):
                        client.connect(('127.0.0.1',44000));client.sendall(WARM);phase='warm'
                if phase=='warm' and len(body)==len(WARM) and warmack:
                    if body!=WARM:raise AssertionError('warmup corrupt')
                    start=time.monotonic_ns();client.sendall(DATA);phase='collect'
                if phase=='collect' and len(sacked)==N//2 and len(seen)==N:
                    if shrink:peer.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024)
                    cap=peer.getsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF);opened=started()
                    history.append([opened,'gate',cap]);phase='recover';release()
            done=started()
            if body!=WARM+DATA:raise AssertionError('transfer corrupt')
            assert phase=='recover' and len(released)==N//2 and len(sacked)==N//2,(phase,sacked,released)
            report.write_text(json.dumps(dict(gate_us=opened,complete_us=done,delivery_us=done-opened,rcvbuf=cap,tcp_port=target,received=len(DATA),sha256=hashlib.sha256(body[len(WARM):]).hexdigest(),sacked=sorted(sacked),wire=history)))
        finally:
            if not report.exists():report.write_text(json.dumps(dict(incomplete=True,phase=phase,bytes=len(body),cum=cum,wire=history)))
    finally:
        proxy.kill();proxy.wait();log.close()

def run():
    if len(sys.argv)==5 and sys.argv[1]=='--peer':
        trial(sys.argv[2],sys.argv[3]=='shrink',Path(sys.argv[4]))
        return
    root=Path(os.environ['TEST_SRCDIR'])/os.environ['TEST_WORKSPACE']
    binary=root/'test/benchmarks/tcp/tcp_proxy'
    outputs=Path(os.environ['TEST_UNDECLARED_OUTPUTS_DIR'])
    failed=[]
    for name in ('control1','shrink1','shrink2','control2'):
        case='TestReceiverTransfer/'+name
        print('=== RUN   '+case,flush=True)
        report=outputs/(name+'.json')
        ret=subprocess.run(['sudo','-n','env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin','/usr/bin/python3',str(Path(__file__).resolve()),'--peer',str(binary),name[:-1],str(report)],timeout=25)
        if ret.returncode:
            print('--- FAIL: '+case,flush=True)
            failed.append(name)
            if name=='control1':break
        else:
            print('--- PASS: '+case,flush=True)
    if failed:sys.exit(1)

if __name__=='__main__':
    run()
