"""Qualify the freestanding runtime opt-out with the supplier's own e2e module."""
import json, os, subprocess, sys, time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT=Path.cwd()
APP=ROOT/'e2e/rules_cc'
D=Path(os.environ['RUNNER_TEMP'])/'sidecar-evidence';D.mkdir(exist_ok=True)
TMP=Path(os.environ['RUNNER_TEMP'])
RC=TMP/'sidecar.bazelrc'
SOURCE=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
REL='toolchain/args/linux/BUILD.bazel'
FILE=ROOT/REL
ORIGINAL=subprocess.check_output(['git','show','HEAD^:'+REL])
CORRECTED=FILE.read_bytes()
BINARY='//:freestanding_link_test'
HOSTED='//:cxx_shared_library_test'
ENV=dict(os.environ)
ENV['USE_BAZEL_VERSION']=(ROOT/'.bazelversion').read_text().strip()
status={}
def invoke(kind,args,out,err):
    start=time.time()
    with out.open('wb') as stdout,err.open('wb') as stderr:
        p=subprocess.run(['bazel',f'--bazelrc={RC}',kind,*args],env=ENV,cwd=APP,stdout=stdout,stderr=stderr)
    text=err.read_text(errors='replace')
    signals=[s[-950:] for s in text.splitlines() if any(k in s.lower() for k in ['error:', 'unable to find library','invocation id','failed to fetch','unknown option','failed to build','executed out of','no tests were found'])]
    return {'exit':p.returncode,'seconds':round(time.time()-start,1),'diagnostics':signals[-15:]}
def result(tag,kind,args):
    out=TMP/(tag+'.out');err=TMP/(tag+'.err');record=invoke(kind,args,out,err);status[tag]=record
    print(tag,json.dumps(record),flush=True);(D/'result.json').write_text(json.dumps({'source':SOURCE,**status},indent=2)+'\n');return out,err,record
def suite(tag,labels):
    bep=TMP/(tag+'.bep')
    common=['--platforms=@llvm//platforms:linux_x86_64','--jobs=110','--strategy=TestRunner=local','--remote_download_outputs=toplevel','--cache_test_results=no','--nobuild_event_json_file_path_conversion','--build_event_json_file='+str(bep),'--test_output=errors','--keep_going',*labels]
    out,err,rec=result(tag,'test',common)
    cases=[]
    for line in bep.read_text().splitlines() if bep.exists() else []:
        event=json.loads(line);ident=event.get('id',{})
        if 'testSummary' in ident:
            t=event['testSummary'];cases.append({'label':ident['testSummary']['label'],'status':t.get('overallStatus'),'totalRuns':t.get('totalRunCount')})
        if 'testResult' in ident:
            art=next((x for x in event['testResult'].get('testActionOutput',[]) if x['name']=='test.xml'),None)
            if art and art.get('uri','').startswith('file://'):
                f=Path(art['uri'][7:])
                if f.exists():
                    xml=ET.parse(f);cases.append({'xmlFor':ident['testResult']['label'],'cases':[{'name':c.get('name'),'skipped':c.find('skipped') is not None,'failed':any(c.find(k) is not None for k in ('failure','error'))} for c in xml.findall('.//testcase')]})
    rec['tests']=cases
    return rec
def flags(tag,platform,targets):
    expression='mnemonic("CppLink", deps(set('+ ' '.join(targets)+')))'
    out,err,rec=result(tag,'aquery',['--platforms='+platform,'--include_param_files','--noinclude_artifacts','--output=jsonproto',expression])
    if rec['exit']:return
    graph=json.loads(out.read_text());labels={t['id']:t['label'] for t in graph.get('targets',[])};rows=[]
    for action in graph.get('actions',[]):
        lbl=labels.get(action['targetId'],'');args=action.get('arguments',[])+[v for p in action.get('paramFiles',[]) for v in p['arguments']]
        if lbl.rpartition(':')[2] in ('freestanding_link_test','cxx_shared_library_test') or 'libunwind.shared' in lbl:
            row={'target':lbl,'tool':args[0], 'pthread':'-lpthread' in args,'dl':'-ldl' in args,'libcSearch':[a[-160:] for a in args if a.startswith('-L') and ('glibc' in a or 'musl' in a)]}
            if row not in rows:rows.append(row)
    rec['actions']=rows
    print(tag+' harvested:',json.dumps(rows),flush=True)
try:
    FILE.write_bytes(ORIGINAL)
    control=suite('stock',[BINARY]);control['expectedFailure']=control['exit']!=0 and all(any('unable to find library '+lib in line for line in control['diagnostics']) for lib in ('-lpthread','-ldl'))
    FILE.write_bytes(CORRECTED)
    fixed=suite('corrected',[BINARY,HOSTED])
    for name,platform in [('gnu','@llvm//platforms:linux_x86_64'),('musl','@llvm//platforms:linux_x86_64_musl')]:
        flags(name,platform,[BINARY,HOSTED,'@llvm//runtimes/libunwind:libunwind.shared'] if name=='gnu' else [BINARY])
    print(json.dumps(status),flush=True)
finally:
    FILE.write_bytes(CORRECTED)
    (D/'result.json').write_text(json.dumps({'source':SOURCE,**status},indent=2)+'\n')
assert control['expectedFailure'] and fixed['exit']==0 and all(any(x.get('label')==label and x.get('status')=='PASSED' for x in fixed['tests']) for label in (BINARY,HOSTED)),status
for name in ['gnu','musl']:
    rows=status[name].get('actions',[])
    assert any(x['target'].endswith(':freestanding_link_test') and not x['pthread'] and not x['dl'] and x['libcSearch'] for x in rows),(name,rows)
rows=status['gnu']['actions']
assert any(x['target'].endswith(':cxx_shared_library_test') and x['pthread'] and x['dl'] for x in rows),rows
assert any('libunwind.shared' in x['target'] and x['pthread'] and x['dl'] for x in rows),rows
