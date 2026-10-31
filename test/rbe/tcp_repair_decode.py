from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
from zipfile import ZipFile

def decode(raw):
    magic = raw[:4]
    endian = {b"\xd4\xc3\xb2\xa1":"<",b"\xa1\xb2\xc3\xd4":">"}[magic]
    assert struct.unpack_from(endian+"I",raw,20)[0]==1
    pos=24
    packets=[]
    while pos+16<=len(raw):
        sec,usec,cap,size=struct.unpack_from(endian+"IIII",raw,pos)
        frame=raw[pos+16:pos+16+cap];pos+=16+cap
        if frame[12:14]!=b"\x08\x00":continue
        ip=frame[14:]
        if len(ip)<20 or ip[9]!=6:continue
        iph=(ip[0]&15)*4
        tcp=ip[iph:]
        if len(tcp)<20:continue
        sport,dport,sequence,ack=struct.unpack_from("!HHII",tcp)
        th=(tcp[12]>>4)*4
        length=int.from_bytes(ip[2:4],"big")-iph-th
        sack=[]
        opt=20
        while opt<th:
            kind=tcp[opt]
            if kind==0:break
            if kind==1:opt+=1;continue
            n=tcp[opt+1]
            if kind==5:sack=[struct.unpack_from("!II",tcp,i) for i in range(opt+2,opt+n,8)]
            assert n>=2;opt+=n
        packets.append(dict(ts=sec+usec/1e6,sport=sport,dport=dport,seq=sequence,ack=ack,length=length,sack=sack,tag=tcp[th] if length==1 else None))
    return packets
