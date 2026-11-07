"""Run the same connected socket fixture on the approved header fix and the buffer range removal."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time

BASE='e9abe5401c9fa5bc3c96738e52d406b0b1fbc15b'
CANDIDATE='279ee9dedb484efcf0d67faf5457781356624828'
TEARDOWN='890afa390aa44699ecbe493699b5260ad50dbaba'
FIELDS=('pkg/tcpip/stack/nic.go','pkg/tcpip/stack/stack.go','pkg/tcpip/stack/save_restore.go')
BEFORE,AFTER=BASE[:9],CANDIDATE[:9]
PRODUCTS={BEFORE:BASE, AFTER:CANDIDATE}
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
    git('restore','--worktree','--source=HEAD','--',BUILD,SEG,*FIELDS)
    for p in (FIX,PROFILE):
        if p.exists(): p.unlink()
    git('checkout','-q','--detach', PRODUCTS[arm])
    assert git('rev-parse','HEAD')==PRODUCTS[arm]
    git('restore','--worktree','--source='+TEARDOWN,'--',*FIELDS)
    assert all(git('hash-object',path)==git('rev-parse',f'{TEARDOWN}:{path}') for path in FIELDS)
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
    assert set(git('diff','--name-only','HEAD').splitlines())==set(FIELDS)|({str(BUILD),str(SEG)} if profile else {str(BUILD)})


def run(state,name,arm,profile,micro=False):
    setup(arm,profile)
    events=ROOT/(name+'.bep')
    output=ROOT/(name+'.console')
    case='TestConnectedTCPReadShapes' if profile else 'TestConnectedTCPReadTiming'
    flags=['--test_arg=-test.run=^'+case+'$', '--test_arg=-test.timeout=80s']
    owner = '//pkg/tcpip/stack:stack_test' if micro else TARGET
    if micro:
        flags = ['--test_arg=-test.run=^$', '--test_arg=-test.bench=^BenchmarkPacketDataReadTo$',
                 '--test_arg=-test.benchtime=250ms', '--test_arg=-test.timeout=80s']
    cmd=[str(ROOT/'bazelisk'), '--nosystem_rc','--nohome_rc','test','-c','opt','--config=x86_64',
         '--jobs=4','--remote_executor=','--remote_cache=','--bes_backend=','--lockfile_mode=off',
         '--test_env=GOMAXPROCS=2','--test_env=GOGC=100','--test_env=GO_TEST_WRAP_TESTV=1',
         '--test_env=TCP_BENCH_CPUS='+state['cpus'],'--run_under=/usr/bin/taskset -c '+state['cpus'],
         '--test_sharding_strategy=disabled','--nocache_test_results','--runs_per_test=1','--flaky_test_attempts=1',
         '--test_timeout=120','--test_arg=-test.v','--test_output=errors',
         '--build_event_json_file='+str(events),*flags,owner]
    record={'name':name,'source':PRODUCTS[arm],'profile':profile,'micro':micro,'owner':owner,'start':time.time()}
    state['attempts'].append(record);save(state)
    with output.open('wb') as stream: code=subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT).returncode
    record['exit']=code
    logs=[]
    if events.exists():
        for ln in events.open():
            e=json.loads(ln)
            if e['id'].get('testResult',{}).get('label')!=owner: continue
            if 'testResult' not in e: continue
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
    if micro:
        prefix = 'BenchmarkPacketDataReadTo/'
        lines = [line.strip() for line in text.splitlines() if line.strip().startswith(prefix) and 'allocs/op' in line]
        expected = {f'{prefix}headers={h}/fragments={n}-2' for h in (0,4) for n in (1,16)}
        assert len(lines) == 4 and {line.split()[0] for line in lines} == expected, lines
        with (OUT/('drain-'+arm)).open('a') as dest:
            dest.write('\n'.join(lines)+'\n')
        record['micro_rows'] = len(lines)
    elif profile:
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
        if name.startswith('p'):
            with (OUT/arm).open('a') as dest: dest.write('\n'.join(lines)+'\n')
        assert len(lines) == 2, (name, lines)
    save(state)
    print('PASS',name,flush=True)


def main():
    assert git('rev-parse', CANDIDATE+'^') == BASE
    assert git('rev-parse', TEARDOWN+'^') == git('rev-parse', BASE+'^')
    changed = git('diff','--name-only',BASE,CANDIDATE).splitlines()
    assert changed == ['pkg/buffer/buffer.go', 'pkg/buffer/buffer_test.go',
                       'pkg/tcpip/stack/packet_buffer.go', 'pkg/tcpip/stack/packet_buffer_test.go'], changed
    for product in PRODUCTS.values():
        assert not git('diff','--name-only',BASE,product,'--',*FIELDS)
    for product in PRODUCTS.values():
        data = subprocess.check_output(['git','show',product+':pkg/tcpip/stack/packet_buffer_test.go'])
        body = data[data.index(b'func BenchmarkPacketDataReadTo('):]
        assert hashlib.sha256(body).hexdigest() == '219841d1d9187ef7ec25532f4bcab8a7edb8293d064d58caec1eb9fba16252df'
    assert git('rev-parse','HEAD^')==CANDIDATE
    allowed=sorted(os.sched_getaffinity(0))
    assert len(allowed)>=2 and platform.machine()=='x86_64'
    cpus=','.join(map(str,allowed[:2]))
    state={'status':'INCOMPLETE','cpu_model':next(l.split(':',1)[1].strip() for l in Path('/proc/cpuinfo').read_text().splitlines() if l.startswith('model name')),
           'arch':platform.machine(),'cpus':cpus,'gomaxprocs':2,'mtu':1500,'gso':False,'gro':False,
           'arms':PRODUCTS,'fixture':git('rev-parse','HEAD'),'fixture_sha256':hashlib.sha256((ROOT/'stream_fixture.go').read_bytes()).hexdigest(),
           'pairs':16,'batch_mib':4,'teardown':TEARDOWN,'parent':BASE,'pr_changes':changed,'common_teardown_blobs':{p:git('rev-parse',f'{TEARDOWN}:{p}') for p in FIELDS},'attempts':[]}
    save(state)
    # Run the uninstrumented content and EOF checks for both arms before sampling.
    failures = []
    for arm in PRODUCTS:
        try:
            run(state, 'check-' + arm, arm, False)
        except (AssertionError, RuntimeError) as err:
            failures.append(f'{arm}: {err}')
    if failures:
        state.update(status='INCOMPLETE', preflight_failures=failures)
        save(state)
        raise RuntimeError(failures)
    for name in PRODUCTS: run(state,'shape-'+name,name,True)
    for i in range(1,17):
        for arm in ((BEFORE,AFTER) if i%2 else (AFTER,BEFORE)):
            run(state,f'p{i:02d}-{arm}',arm,False)
    assert len(state['attempts'])==36 and all(r['verified_timing_rows']==2 for r in state['attempts'] if not r['profile'])
    state['connected_verified']=True;save(state)
    for i in range(1,17):
        for arm in ((BEFORE,AFTER) if i%2 else (AFTER,BEFORE)):
            run(state,f'm{i:02d}-{arm}',arm,False,micro=True)
    assert len(state['attempts'])==68 and all(r['micro_rows']==4 for r in state['attempts'] if r['micro'])
    state['status']='PASS';save(state)


if __name__=='__main__': main()
