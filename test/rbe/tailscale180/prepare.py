from pathlib import Path
import hashlib
import json
import tarfile

SOURCE = 'ad90750cf030c4e56a83cdba8ae1d4f3c1ff75c8'
def replace(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old,new)

def adapt(archive, dest):
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == 'b4769385629eb98c2ac55cf62f3035fa1450c6e04ea035e8e7e9a8d8ea6ec435'
    dest.mkdir(parents=True,exist_ok=True)
    sha = {}
    with tarfile.open(archive) as t:
        for n in ('gso-split.go','gvisor-stacks.go','link-emulator.go','linux-sender-other.go','main.go','sender-state.go','strip-timestamps.go','wire-trace.go'):
            orig=t.extractfile(f'netstack-rack-repro-{SOURCE}/{n}').read()
            s=orig.decode()
            if n=='gvisor-stacks.go':
                s=replace(s,'tcp.TCPProbeFunc','stack.TCPProbeFunc')
                s=replace(s,'tcp.NewProtocolProbe(probe)','tcp.NewProtocol')
                s=replace(s,'\t\tClockResolution:    cfg.clockRes,\n','')
                s=replace(s, 's := stack.New(opts)', '''if cfg.clockRes != 0 || cfg.hostGSO || cfg.cc != "reno" { panic("wrong FreeBSD sender configuration") }
    s := stack.New(opts)
    s.AddTCPProbe(probe)
    sack := tcpip.TCPSACKEnabled(true)
    if err := s.SetTransportProtocolOption(tcp.ProtocolNumber, &sack); err != nil { panic(err) }''')
                s=replace(s,'return attachNIC(name, s, link, addr)','return attachNIC(name, s, link, addr)') if s.count('return attachNIC(name, s, link, addr)')==1 else s
                s=replace(s,'if cfg.hostGSO {', '''var gotCC tcpip.CongestionControlOption
    var gotRecovery tcpip.TCPRecovery
    var gotSACK tcpip.TCPSACKEnabled
    if s.TransportProtocolOption(tcp.ProtocolNumber, &gotCC)!=nil || s.TransportProtocolOption(tcp.ProtocolNumber, &gotRecovery)!=nil || s.TransportProtocolOption(tcp.ProtocolNumber, &gotSACK)!=nil { panic("cannot read options") }
    if gotCC!="reno" || (gotRecovery&tcpip.TCPRACKLossDetection != 0)!=cfg.rack || !gotSACK || link.SupportedGSO()!=stack.GSONotSupported { panic("incorrect sender options") }
    fmt.Printf("ACTUAL cc=%s sack=%t rack=%t gso=off\\n", gotCC, gotSACK, gotRecovery&tcpip.TCPRACKLossDetection!=0)
    if cfg.hostGSO {''')
                s=replace(s,'gso := pkt.GSOOptions','gso := pkt.GSOOptions\n        if gso.Type != stack.GSONone { panic("unexpected GSO on wire") }')
            if n=='sender-state.go':
                s=replace(s,'\n\t"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"','')
                s=replace(s,'*tcp.TCPEndpointState','*stack.TCPEndpointState')
            if n=='linux-sender-other.go':
                s=replace(s,'//go:build !linux\n','')
            if n=='link-emulator.go':
                s=replace(s,'info := parsePacket(b)','info := parsePacket(b)\n    reportHandshake(b)')
            if n=='gso-split.go':
                s+='''
// reportHandshake sees the IP packet passed to the path, after optional
// timestamp stripping. Both legs are independent real gVisor endpoints.
func reportHandshake(b []byte) {
    p := parsePacket(b)
    if !p.isTCP || p.flags & header.TCPFlagSyn == 0 { return }
    ack := p.flags & header.TCPFlagAck != 0
    ip := header.IPv4(b)
    tcp := header.TCP(b[ip.HeaderLength():])
    o := header.ParseSynOptions(tcp.Options(),ack)
    fmt.Printf("WIRE SYN ack=%t ts=%t sack=%t mss=%d\\n",ack,o.TS,o.SACKPermitted,o.MSS)
}
'''
            if n=='main.go':
                s=replace(s,'dataPkts  int','received int\n dataPkts  int')
                s=replace(s,'} else if _, err := io.ReadFull(conn, buf); err != nil {\n\t\t\t\tr.err = err\n\t\t\t}', '''} else {
                r.received, r.err = io.ReadFull(conn, buf)
                for i,b := range buf[:r.received] {
                    if b != byte(i) { panic("payload mismatch") }
                }
            }''')
                s=replace(s,'printFetch(r)','printFetch(r)\n            fmt.Printf("PROGRESS measured=%t received=%d want=%d\\n",r.measured,r.received,*sizeFlag)')
            (dest/n).write_text(s);sha[n]=[hashlib.sha256(orig).hexdigest(),hashlib.sha256(s.encode()).hexdigest()]
    pkgs=('buffer','tcpip','tcpip/adapters/gonet','tcpip/checksum','tcpip/header','tcpip/link/channel','tcpip/network/ipv4','tcpip/network/ipv6','tcpip/stack','tcpip/transport/tcp','tcpip/transport/udp')
    (dest/'BUILD').write_text('load("@io_bazel_rules_go//go:def.bzl", "go_binary")\ngo_binary(name="replay",srcs='+str(sorted(sha))+',deps='+str(['//pkg/'+s for s in pkgs])+')\n')
    return sha

if __name__=='__main__':
    import sys
    print(json.dumps(adapt(Path(sys.argv[1]),Path(sys.argv[2])),indent=2))
