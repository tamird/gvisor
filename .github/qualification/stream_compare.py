"""Run the same connected socket fixture on both header-preserving sources."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time

OLD='46571d17eb49c7b983ffeb32f3365d2a058983b9'
OLD_PREFIX='b35c34fa430bd3217fbfe85313034ca84b3dc200'
TEARDOWN='890afa390aa44699ecbe493699b5260ad50dbaba'
BASE='7f850daeea35268227e3f95377eaf105671f2018'
PREFIX='e1b1c8606cf78ad082e5af169b261bcfd0ea4779'
PRODUCTS={'author':BASE, 'prefix':PREFIX}
ROOT=Path(os.environ['RUNNER_TEMP'])/'tcp-read-stream'
OUT=ROOT/'results'
PKG=Path('pkg/tcpip/adapters/gonet')
BUILD=PKG/'BUILD'
FIX=PKG/'qualification_tcp_read_test.go'
PROFILE=PKG/'qualification_tcp_shape_test.go'
SEG=Path('pkg/tcpip/transport/tcp/segment.go')
TARGET='//pkg/tcpip/adapters/gonet:qualification_tcp_stream'
DECL="""
go_test(
    name = "qualification_tcp_stream",
    size = "medium",
    srcs = [%s],
    embed = [":gonet"],
    deps = [
        "//pkg/tcpip", "//pkg/tcpip/header", "//pkg/tcpip/link/fdbased",
        "//pkg/tcpip/network/ipv4", "//pkg/tcpip/stack", "//pkg/tcpip/transport/tcp",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
"""
ORIGINAL="""func (s *segment) ReadTo(dst io.Writer, peek bool) (int, error) {
	return s.pkt.Data().ReadTo(dst, peek)
}"""
PROBE="""var QualificationObserveTCPRead func(size, headers, views, used int, first bool)

func (s *segment) ReadTo(dst io.Writer, peek bool) (int, error) {
	if QualificationObserveTCPRead == nil {
		return s.pkt.Data().ReadTo(dst, peek)
	}
	size, headers := s.pkt.Data().Size(), s.pkt.HeaderSize()
	list, skip := s.pkt.AsViewList()
	skip += headers
	views := 0
	for v := list.Front(); v != nil; v = v.Next() {
		if skip >= v.Size() { skip -= v.Size(); continue }
		views++
		skip = 0
	}
	n, err := s.pkt.Data().ReadTo(dst, peek)
	if n > 0 {
		QualificationObserveTCPRead(size, headers, views, n, !s.qualificationRead)
		s.qualificationRead=true
	}
	return n, err
}"""
ROWS=re.compile(r'^BenchmarkConnectedTCPRead/read=(64|65536)-2\s+4\s+[\d.]+ ns/op\s+[\d.]+ MB/s\s+[\d.]+ B/read\s*$',re.M)


def git(*args):
    return subprocess.check_output(['git',*map(str,args)],text=True).strip()


def save(state):
    (OUT/'verdict.json').write_text(json.dumps(state,indent=2)+'\n')


def setup(arm,profile):
    git('restore','--worktree','--source=HEAD','--',BUILD,SEG)
    for p in (FIX,PROFILE):
        if p.exists(): p.unlink()
    git('checkout','-q','--detach', PRODUCTS[arm])
    assert git('rev-parse','HEAD')==PRODUCTS[arm]
    shutil.copyfile(ROOT/'stream_fixture.go',FIX)
    names=[FIX.name]
    if profile:
        shutil.copyfile(ROOT/'stream_profile.go',PROFILE)
        names.append(PROFILE.name)
        old=SEG.read_text()
        assert old.count(ORIGINAL)==old.count('type segment struct {')==1
        SEG.write_text(old.replace('type segment struct {','type segment struct {\n\tqualificationRead bool `state:"nosave"`').replace(ORIGINAL,PROBE))
    else:
        assert git('hash-object',SEG)==git('rev-parse',f'{BASE}:{SEG}')
    with BUILD.open('a') as file: file.write(DECL % ', '.join('"'+n+'"' for n in names))
    assert set(git('diff','--name-only','HEAD').splitlines())==({str(BUILD),str(SEG)} if profile else {str(BUILD)})


def run(state,name,arm,profile):
    setup(arm,profile)
    events=ROOT/(name+'.bep')
    output=ROOT/(name+'.console')
    case='TestConnectedTCPReadShapes' if profile else 'TestConnectedTCPReadTiming'
    flags=['--test_arg=-test.run=^'+case+'$', '--test_arg=-test.timeout=80s']
    cmd=[str(ROOT/'bazelisk'), '--nosystem_rc','--nohome_rc','test','-c','opt','--config=x86_64',
         '--jobs=4','--remote_executor=','--remote_cache=','--bes_backend=','--lockfile_mode=off',
         '--test_env=GOMAXPROCS=2','--test_env=GOGC=100','--test_env=GO_TEST_WRAP_TESTV=1',
         '--test_env=TCP_BENCH_CPUS='+state['cpus'],'--run_under=/usr/bin/taskset -c '+state['cpus'],
         '--test_sharding_strategy=disabled','--nocache_test_results','--runs_per_test=1','--flaky_test_attempts=1',
         '--test_timeout=120','--test_arg=-test.v','--test_output=errors',
         '--build_event_json_file='+str(events),*flags,TARGET]
    record={'name':name,'source':PRODUCTS[arm],'profile':profile,'start':time.time()}
    state['attempts'].append(record);save(state)
    with output.open('wb') as stream: code=subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT).returncode
    record['exit']=code
    logs=[]
    if events.exists():
        for ln in events.open():
            e=json.loads(ln)
            if e['id'].get('testResult',{}).get('label')!=TARGET: continue
            tr=e['testResult']; record['test_result']={k:tr.get(k) for k in ('status','cachedLocally','executionInfo')}
            import urllib.parse
            log,=[x for x in tr['testActionOutput'] if x['name']=='test.log']
            u=urllib.parse.urlparse(log['uri'])
            if u.scheme not in ('','file'): raise ValueError(u.scheme)
            logs.append(Path(urllib.parse.unquote(u.path)).read_bytes())
    if len(logs)==1:
        data,=logs
        assert len(data)<256000
        (OUT/(name+'.log')).write_bytes(data)
    if code or len(logs)!=1:
        record['console_end']=output.read_bytes()[-5500:].decode(errors='replace'); save(state)
        raise RuntimeError(f'{name}: Bazel {code}, {len(logs)} TestRunner logs')
    text=data.decode()
    assert record['test_result']['status']=='PASSED' and not record['test_result']['cachedLocally']
    if profile:
        shapes=[json.loads(line.split('TCP_STREAM_SHAPE ',1)[1]) for line in text.splitlines() if 'TCP_STREAM_SHAPE ' in line]
        assert len(shapes)==2 and {r['read'] for r in shapes}=={64,65536}
        for r in shapes:
            assert sum(int(k)*v for k,v in r['payload_bytes'].items())==r['bytes']==4*(1<<20)
            assert r['complete']>0 and sum(r['payload_views'].values())>0
        record['shapes']=shapes
    else:
        lines=[line.strip() for line in text.splitlines() if ROWS.fullmatch(line.strip())]
        record['verified_timing_rows']=len(lines)
        if len(lines)!=2:
            record['benchmark_lines']=[ln[:400] for ln in text.splitlines() if 'BenchmarkConnectedTCPRead/' in ln][-10:]
        with (OUT/arm).open('a') as dest: dest.write('\n'.join(lines)+'\n')
    save(state)
    print('PASS',name,flush=True)


def main():
    assert git('rev-parse',PREFIX+'^')==BASE
    assert git('rev-parse',BASE+'^')==OLD
    assert git('diff','--binary',BASE,PREFIX)==git('diff','--binary',OLD,OLD_PREFIX)
    fields=('pkg/tcpip/stack/nic.go','pkg/tcpip/stack/stack.go','pkg/tcpip/stack/save_restore.go')
    def ops(parent,head):
        return [line for line in git('diff','--unified=0',parent,head,'--',*fields).splitlines()
                if line.startswith(('+','-')) and not line.startswith(('+++','---'))]
    assert ops(OLD,BASE)==ops(TEARDOWN+'^',TEARDOWN)

    assert set(git('diff','--name-only',BASE,PREFIX).splitlines())=={'pkg/buffer/buffer.go','pkg/buffer/buffer_test.go','pkg/tcpip/stack/packet_buffer.go'}
    assert git('rev-parse','HEAD^')==PREFIX
    allowed=sorted(os.sched_getaffinity(0))
    assert len(allowed)>=2 and platform.machine()=='x86_64'
    cpus=','.join(map(str,allowed[:2]))
    state={'status':'INCOMPLETE','cpu_model':next(l.split(':',1)[1].strip() for l in Path('/proc/cpuinfo').read_text().splitlines() if l.startswith('model name')),
           'arch':platform.machine(),'cpus':cpus,'gomaxprocs':2,'mtu':1500,'gso':False,'gro':False,
           'arms':PRODUCTS,'fixture':git('rev-parse','HEAD'),'fixture_sha256':hashlib.sha256((ROOT/'stream_fixture.go').read_bytes()).hexdigest(),
           'pairs':16,'batch_mib':4,'teardown':TEARDOWN,'original_arms':[OLD,OLD_PREFIX],'attempts':[]}
    save(state)
    for name in PRODUCTS: run(state,'shape-'+name,name,True)
    for i in range(1,17):
        for arm in (('author','prefix') if i%2 else ('prefix','author')):
            run(state,f'p{i:02d}-{arm}',arm,False)
    assert len(state['attempts'])==34 and all(r.get('verified_timing_rows',2)==2 for r in state['attempts'])
    state['status']='PASS';save(state)


if __name__=='__main__': main()
