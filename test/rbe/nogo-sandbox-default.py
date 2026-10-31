"""Compare both Nogo policies on one Linux machine, using Bazel's own spawns."""
from pathlib import Path
from xml.etree import ElementTree
import hashlib
import json
import os
import re
import subprocess as sp

tmp = Path(os.environ['RUNNER_TEMP'])
records = tmp / 'records'
source = {'stock': os.environ['BASE'], 'fixed': os.environ['CURRENT']}
bazel = ['docker','exec','tcp-compiler','bazel','--bazelrc='+str(tmp/'tcp.bazelrc')]
container = ['docker','exec','tcp-compiler']
roles = {'bundle','check','filter','render'}
expected = 'fmt.Fprintf format %d has arg "hello world!" of wrong type string'
library = '//tools/nogo/sanity:sanity_nogo'
renderer = '//pkg/goid:goid_asm_render'
tests = ['//tools/nogo/sanity:sanity_test','//pkg/goid:goid_test']
flags = ['--config=x86_64','--noremote_upload_local_results','--incompatible_modify_execution_info_additive',
         '--strategy=GoStdlib=linux-sandbox','--strategy=GoStdlibList=linux-sandbox',
         '--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=+no-remote-cache,^GoStdlib(List)?$=+no-remote-cache',
         '--remote_download_outputs=toplevel','--jobs=3']
errors = []
output_paths = set()
assembly = {}

def log_json(p):
    decoder = json.JSONDecoder()
    with p.open() as src:
        buf = ''
        while True:
            chunk = src.read(131072)
            buf += chunk
            while buf.lstrip():
                buf = buf.lstrip()
                try:
                    row,pos = decoder.raw_decode(buf)
                except ValueError:
                    if not chunk:
                        raise
                    break
                yield row
                buf = buf[pos:]
            if not chunk:
                return

def save(p, obj):
    p.write_text(json.dumps(obj,indent=2)+'\n')

def selected(spawn):
    m = spawn.get('mnemonic')
    args = spawn.get('commandArgs',[])
    label = spawn.get('targetLabel','').partition('//')[2]
    if m == 'GoStandardLibraryAnalysis' and label == 'tools/nogo:stdlib' and 'bundle' in args:
        return 'bundle'
    if m == 'GoStaticAnalysis':
        for op,owner in [('check','tools/nogo/sanity:sanity'),('filter','tools/nogo/sanity:sanity_nogo'),('render','pkg/goid:goid_asm_render')]:
            if label == owner and op in args:
                return op
    return None

def checkout(arm):
    sha = source[arm]
    sp.run(['git','checkout','--quiet','--detach',sha],check=True)
    assert sp.check_output(['git','rev-parse','HEAD'],text=True).strip()==sha
    return sha

def run(arm, label, test=False, control=False, measured=False):
    sha = checkout(arm)
    d = records/label; d.mkdir()
    (d/'source').write_text(sha+'\n')
    strategy = 'linux-sandbox' if arm=='fixed' or control else 'local'
    opts = flags + [f'--strategy={m}={strategy}' for m in ('GoStandardLibraryAnalysis','GoStaticAnalysis')]
    types = ['//tools/nogo:stdlib'] if control else [library,renderer]
    if test:
        opts += ['--strategy=TestRunner=local','--test_output=all','--nocache_test_results','--test_arg=-test.v']
        types = tests + [renderer]
    if measured:
        opts += ['--jobs=1']
        # Invalidate only the four measured action kinds, leaving all compilers,
        # repository data and other cached work on this worker intact.
        assert output_paths
        for path in output_paths:
            assert path.startswith('bazel-out/') and '..' not in Path(path).parts
        execroot=sp.check_output(bazel+['info','execution_root'],text=True).strip().splitlines()[-1]
        assert '/execroot/' in execroot,execroot
        clean = "import os,sys; [os.unlink(p) for p in sys.argv[1:] if os.path.lexists(p)]"
        sp.run(container+['python3','-c',clean,*sorted(execroot+'/'+p for p in output_paths)],check=True)
    execlog = tmp/(label+'.spawns.json')
    bep = tmp/(label+'.bep.json')
    logfile = tmp/(label+'.log')
    with logfile.open('w') as out:
        rc = sp.run(bazel+['test' if test else 'build',*opts,'--execution_log_json_file='+str(execlog),'--build_event_json_file='+str(bep),*types],stdout=out,stderr=sp.STDOUT).returncode
    raw = logfile.read_text(errors='replace')
    extracts = [l for l in raw.splitlines() if any(k in l for k in ('Invocation ID:','Streaming build results','ERROR:','Spawn strategy','usable spawn','viable strategy','PASS','FAILED','processes:','Build completed successfully','Analyzing Go Standard Library'))]
    (d/'diagnostics.txt').write_text('\n'.join(extracts[:40]+raw.splitlines()[-25:])+'\n')
    rows = []
    all_nogo = []
    if execlog.is_file():
        for spawn in log_json(execlog):
            if spawn.get('mnemonic') not in ('GoStaticAnalysis','GoStandardLibraryAnalysis'): continue
            role = selected(spawn)
            row = {k:spawn[k] for k in ('targetLabel','mnemonic','runner','cacheHit','exitCode','status','metrics','listedOutputs','actualOutputs') if k in spawn}
            row['role']=role
            args=spawn.get('commandArgs',[])
            row['subcommand']=[a for a in args if a in roles]
            row['tool']=args[0] if args else None
            if role:
                rows.append(row)
                for p in row['listedOutputs']:
                    output_paths.add(p)
            all_nogo.append(row)
    save(d/'original-nogo-spawns.json',all_nogo)
    if control:
        assert rc!=0 and ('strategy' in raw.lower() or 'sandbox' in raw.lower()) and ('no usable spawn strategy' in raw.lower() or 'can execute' in raw.lower() or 'available strategy' in raw.lower() or ('GoStandardLibraryAnalysis' in raw and 'linux-sandbox' in raw)), raw[-1900:]
        return
    assert rc==0,(label,rc,extracts[-8:])
    assert {r['role'] for r in rows}==roles,(label,[(r['targetLabel'],r['subcommand']) for r in all_nogo])
    assert all(r.get('runner')==strategy and not r.get('cacheHit',False) and r.get('exitCode',0)==0 for r in rows), (label,[(r['role'],r.get('runner'),r.get('cacheHit')) for r in rows])
    if test:
        for tgt in tests:
            prefix=tgt[2:].replace(':','/')
            for ext in ('xml','log'):
                dst=d/(tgt.split(':')[-1]+'.'+ext)
                dst.write_bytes(sp.check_output(container+['cat','bazel-testlogs/'+prefix+'/test.'+ext]))
            xml = ElementTree.parse(d/(tgt.split(':')[-1]+'.xml'))
            assert not any(xml.findall('.//'+tag) for tag in ('failure','error','skipped')), (tgt, ElementTree.tostring(xml.getroot()))
        assert expected in (d/'sanity_test.log').read_text() and 'PASS' in (d/'sanity_test.log').read_text()
        asm=sp.check_output(container+['cat','bazel-bin/pkg/goid/goid_asm_impl.s'])
        assert hashlib.sha256(asm).hexdigest() == 'ad803313492eefd0d410ffa437554255564b90602353646d608dc07e8f008cfd', asm
        (d/'goid_asm_impl.s').write_bytes(asm); assembly[arm]=hashlib.sha256(asm).hexdigest()

def main():
    # Prior raw receipts already prove the rejection and both action strategies.
    # This build warms this worker, verifies the generated bytes and runs the
    # consumer verbosely; rules_go's non-verbose XML has no case records.
    run('fixed','warm-fixed',test=True)
    log = (records/'warm-fixed'/'goid_test.log').read_text()
    assert '--- PASS: TestUniquenessAndConsistency' in log, log
    # The first worker runs above warm repository setup and tool/SDK inputs.
    # Measure each source twice, reversing order in the second pair.
    for i,arm in enumerate(('stock','fixed','fixed','stock')):
        try:
            run(arm,f'measure-{i+1}-{arm}',measured=True)
        except Exception as exc:
            errors.append([f'measure-{i+1}-{arm}',str(exc)])

try:
    main()
except Exception as exc:
    errors.append(['comparison',repr(exc)])
save(records/'verdict.json',{'status':'FAIL' if errors else 'PASS','sources':source,'assembly_sha256':assembly,'errors':errors})
print((records/'verdict.json').read_text())
raise SystemExit(bool(errors))
