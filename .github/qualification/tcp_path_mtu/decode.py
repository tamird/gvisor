"""Decode captured ingress frames on the isolated physical peer."""
import base64
from collections import defaultdict
import hashlib
import ipaddress
import struct


def sha(x): return hashlib.sha256(x).hexdigest()


def ones(data):
    if len(data)%2: data+=b"\0"
    value=sum(struct.unpack("!%dH"%(len(data)//2),data))
    while value>>16: value=(value&0xffff)+(value>>16)
    return value==65535


def tcp_packet(data, addr, family, src_port, dst_port):
    if len(data)<20 or struct.unpack("!HH",data[:4])!=(src_port,dst_port): return None
    off=(data[12]>>4)*4
    if not 20<=off<=len(data): return None
    pseudo=addr + (struct.pack("!BBH",0,6,len(data)) if family=="v4" else struct.pack("!I3xB",len(data),6))
    payload=data[off:]
    return dict(seq=int.from_bytes(data[4:8],"big"),flags=data[13],data=len(payload),checksum_valid=ones(pseudo+data),payload_sha256=sha(payload))


def decode(frames, r):
    observed=[];fragments=defaultdict(dict);reassembled=[]
    for f in frames or []:
        wire=base64.b64decode(f["Bytes"],validate=True)
        assert wire[6:12]==bytes.fromhex("020000007101") and f["Kind"]!=4
        ip=wire[14:]
        v=ip[0]>>4;attrs={};offset=0;more=False;ident=None
        if v==4:
            if len(ip)<20 or ip[12:20]!=bytes([192,0,2,1,192,0,2,2]):continue
            end=int.from_bytes(ip[2:4],"big");start=(ip[0]&15)*4
            assert 20<=start<=end<=len(ip)
            flags=int.from_bytes(ip[6:8],"big");offset=8*(flags&0x1fff);more=bool(flags&0x2000)
            ident=int.from_bytes(ip[4:6],"big");proto=ip[9];addr=ip[12:20]
            attrs["ip_checksum_valid"]=ones(ip[:start]);attrs["option"]=0
            i=20
            while i<start:
                kind=ip[i]
                if kind==0:break
                if kind==1:i+=1;continue
                size=ip[i+1];assert size>=2 and i+size<=start
                if kind==30:
                    assert size==4
                    attrs["option"]=int.from_bytes(ip[i+2:i+4],"big")
                i+=size
        elif v==6:
            if len(ip)<40:continue
            addr=ip[8:40]
            if addr!=ipaddress.IPv6Address("2001:db8::1").packed+ipaddress.IPv6Address("2001:db8::2").packed:continue
            end=40+int.from_bytes(ip[4:6],"big");assert end<=len(ip)
            start=40;proto=ip[6];attrs["option"]=0
            while proto in (253,44,0,43,60):
                following=ip[start]
                if proto==44:
                    value=int.from_bytes(ip[start+2:start+4],"big")
                    offset=value&0xfff8;more=bool(value&1);ident=int.from_bytes(ip[start+4:start+8],"big")
                    start+=8;proto=following;break
                length=8*(1+ip[start+1])
                if proto==253:
                    assert length==8
                    attrs["option"]=int.from_bytes(ip[start+2:start+4],"big")
                start+=length;proto=following
        else:continue
        if proto not in (6,253):continue
        rest=ip[start:end]
        row=dict(**attrs,ip_bytes=end,skb_status=f["Status"],if_packet_type=f["Kind"],frag_offset=offset,more=more)
        if offset or more:
            key=(v,ident,addr,proto)
            parts=fragments[key]
            parts[offset]=(rest,more)
            # A later complete datagram can reuse the same identification.
            off=0;buf=bytearray()
            while off in parts:
                part,pending=parts[off]
                buf.extend(part);off+=len(part)
                if not pending:
                    if proto==253 and len(buf)>=8 and buf[0]==6:buf=buf[8:]
                    tc=tcp_packet(bytes(buf),addr,r["Family"],r["SrcPort"],r["DstPort"])
                    if tc is not None:reassembled.append(dict(id=ident,tcp=tc))
                    del fragments[key]
                    break
                if not part:break
        else:
            tc=tcp_packet(rest,addr,r["Family"],r["SrcPort"],r["DstPort"])
            if tc is None:continue
            row["tcp"]=tc
        observed.append(row)
    return {"frames":observed,"reassembled":reassembled}
