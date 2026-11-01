"""Reconcile all three syscall executions and the exact tested TCP owners."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

root=Path(sys.argv[1])
expected=['poll-native','poll-stock','poll-candidate','amd64-tests','amd64-nogo','arm64-nogo','arm64-owners']
cases={'AllInetTests/SimpleTcpSocketTest.'+case+'/'+str(family) for case in ['NonBlockingConnectRefused','SynRcvdOnListenerShutdown'] for family in [0,1]}
def stock_cases_match(rows):
    # A refused connection keeps POLLERR set after consuming SO_ERROR.
    # Shutting down a listener is a passing terminal-readiness control.
    if rows.keys() != cases:
        return False
    return all((r['status'] == 'FAIL' and any('poll_fd.revents' in msg for msg in r['examples'])) if '.NonBlockingConnectRefused/' in name else r['status'] == 'OK' for name, r in rows.items())

if len(sys.argv) == 4 and sys.argv[1] == 'replay':
    original = json.loads(Path(sys.argv[2]).read_text())
    rows = original['results']
    stock = rows['poll-stock']
    if stock['source'] != 'aa767e4c43d011121271551fe24da501a56f729d' or stock['exit'] != 3 or list(stock['targets'].values()) != ['FAILED'] or not stock_cases_match(stock['assertions']):
        raise SystemExit('stock behavior is different from the two expected regressions and two passing controls')
    good = all(rows[k]['verified'] and rows[k]['assertions'].keys() == cases and all(a['status'] == 'OK' for a in rows[k]['assertions'].values()) for k in ['poll-native', 'poll-candidate'])
    if not good or len({rows[k]['fixture'] for k in ['poll-stock','poll-native','poll-candidate']}) != 1:
        raise SystemExit('native and corrected fixture controls did not match')
    receipt = {'original_verdict_at': original['verdict_at'], 'original_complete_verdict': original['verified'], 'stock_verifier_before_replay': stock['verified'], 'verified_socket_arms': True, 'socket_results': {k: rows[k] for k in ['poll-native','poll-stock','poll-candidate']}, 'old_arm64_timeout_or_absence': {k:rows[k] for k in ['arm64-nogo','arm64-owners']}}
    Path(sys.argv[3]).write_text(json.dumps(receipt,indent=2)+'\n')
    raise SystemExit(0)

def read(p,fallback=None):
    return p.read_text().strip() if p.is_file() else fallback
def read_bep(p):
    if p.exists():
        for line in p.open():
            try: yield json.loads(line)
            except ValueError: continue
def inspect(name):
    d=root/name
    if not (d/'exit').is_file(): return {'verified':False,'status':'missing'}
    status=int(read(d/'exit'))
    src=read(d/'source');ok=src==os.environ['STOCK' if name=='poll-stock' else 'CANDIDATE']
    actual={}; completed={}; inv=[]; finished=None
    for event in read_bep(d/'bep'):
        if 'started' in event:inv.append(event['started']['uuid'])
        if 'finished' in event:finished=event['finished']['exitCode']
        if 'testSummary' in event:
            actual[event['id']['testSummary']['label']]=event['testSummary']['overallStatus']
        if 'completed' in event and 'targetCompleted' in event['id']:
            completed[event['id']['targetCompleted']['label']]=event['completed'].get('success',False)
    row=dict(source=src,exit=status,start=read(d/'start'),end=read(d/'end'),invocation=inv,finish=finished)
    asserts={};skips=[]; counts=Counter(); files={}
    for xml in d.glob('*.xml'):
        try: xmlroot=ET.parse(xml)
        except ET.ParseError: continue
        n=0
        for case in xmlroot.iter('testcase'):
            n+=1
            test=case.get('classname','')+'.'+case.get('name','')
            fails=case.findall('failure')+case.findall('error')
            state='SKIP' if case.find('skipped') is not None or case.get('status')=='notrun' else 'FAIL' if fails else 'OK'
            counts[state]+=1
            if state=='SKIP':skips.append(test)
            if state=='FAIL' or name.startswith('poll-'):
                msgs=[(node.text or node.get('message',''))[:1100] for node in fails[:2]]
                asserts[test]=dict(status=state,examples=msgs)
        files[xml.name]=n
    log=read(d/'bazel.log','')
    diag=[line[:450] for line in log.splitlines() if line.startswith(('ERROR:', 'Error:', 'error:', 'FAIL:')) and not any(k in line.lower() for k in ('--remote_header','--bes_header','credential','token','api-key'))][:6]
    if name.startswith('poll-'):
        expected_status='FAILED' if name=='poll-stock' else 'PASSED'; target=read(d/'target')
        ok &= set(asserts)==cases and actual.get(target)==expected_status and status==(3 if name=='poll-stock' else 0)
        if name=='poll-stock':
            ok &= stock_cases_match(asserts)
        else: ok &= all(v['status']=='OK' for v in asserts.values())
        row.update(assertions=asserts,fixture=read(d/'fixture-sha256').split()[0],expected=sorted(cases),missing=sorted(cases-asserts.keys()))
    else:
        owners=set(read(d/'labels','').splitlines())
        if name=='arm64-owners':
            ok = False  # A cross-build cannot qualify an ARM64 execution.
            row.update(build_only=True,missing=sorted(owners-{k for k,v in completed.items() if v}))
        else:
            ok &= bool(owners) and status==0 and set(actual)==owners and all(s=='PASSED' for s in actual.values()) and len(files)==len(owners) and counts['FAIL']==0
            row.update(failures=asserts,missing=sorted(owners-actual.keys()),owner_xml=files)
        row.update(owners=sorted(owners))
    row.update(cases=dict(counts),skips=skips,targets=actual,verified=bool(ok),diagnostics=diag)
    return row
results={n:inspect(n) for n in expected};forms={r['fixture'] for r in results.values() if 'fixture' in r}
ok=all(r['verified'] for r in results.values()) and len(forms)==1
report={'expected':expected,'completed':len([v for v in results.values() if 'exit' in v]),'verdict_at':datetime.now(timezone.utc).isoformat(),'verified':ok,'results':results}
(root/'verdict.json').write_text(json.dumps(report,indent=2)+'\n')
if len(sys.argv)==2: raise SystemExit(not ok)
