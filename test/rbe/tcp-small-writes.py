"""Collect both pre-read queue lengths and both complete drains for the same socket."""
import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[-1])
sources = json.loads((root / 'sources.json').read_text())
case_prefix = 'AllInetTests/SimpleTcpSocketTest.TwoSmallWriteBurstsWithoutReads/'
family_by_case = {case_prefix+'0': 2, case_prefix+'1': 10}
marker = re.compile(r'TCP-SMALL-BURST family=(2|10) burst=([12]) rcvbuf=(\d+) queued=(\d+) drained=(\d+) remaining=(\d+)')

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')

def text(path):
    return path.read_text(errors='replace').strip() if path.is_file() else ''

def prepare(name):
    source = sources[name]
    git('checkout', '--detach', '--quiet', source['source'])
    assert git('rev-parse', 'HEAD^{tree}') == source['tree']
    assert git('hash-object', 'test/syscalls/linux/tcp_socket.cc') == source['fixture_blob']
    for path, blob in source['tcp_blobs'].items():
        assert git('hash-object', path) == blob
    (root/name/'actual-source').write_text(git('rev-parse', 'HEAD')+'\n')

def one(name):
    d = root/name
    result = dict(source=text(d/'actual-source'), exit=text(d/'exit'), target=text(d/'target'), end=text(d/'end'),
                  invocations=sorted(set(re.findall(r'https://app.buildbuddy.io/invocation/([0-9a-f-]{36})', text(d/'bazel.log')))), cases=[])
    try:
        xml = ET.fromstring(text(d/'test.xml'))
        log_matches = [tuple(map(int, m)) for m in marker.findall(text(d/'test.log'))]
        rows = result['cases']
        for case in xml.iter('testcase'):
            full_name = case.get('classname','')+'.'+case.get('name','')
            family = family_by_case[full_name]
            props = {p.get('name'):p.get('value') for p in case.findall('./properties/property')}
            errs = [e.text or e.get('message','') for e in (*case.findall('failure'),*case.findall('error'))]
            skip = case.get('status')=='notrun' or case.find('skipped') is not None
            row = dict(name=full_name, family=family, errors=errs, skipped=skip, bursts=[])
            rows.append(row)
            for burst in (1,2):
                keys = ['receive_buffer']+[f'burst{burst}_{f}' for f in ('queued','drained','remaining')]
                xmlvalues = tuple(map(int,(props[k] for k in keys))) if all(k in props for k in keys) else ()
                logvalues = {m[2:] for m in log_matches if m[:2]==(family,burst)}
                if len(logvalues)>1 or (xmlvalues and logvalues and xmlvalues not in logvalues):
                    raise ValueError('inconsistent original XML and text measurements')
                values = xmlvalues or next(iter(logvalues), ())
                if len(values)!=4:
                    continue
                rcvbuf,queued,drained,remaining = values
                row['bursts'].append(dict(burst=burst, actual_so_rcvbuf=rcvbuf, written=21000, queued=queued, drained=drained,
                                          remaining=remaining, observed_in_xml=bool(xmlvalues), observed_in_log=bool(logvalues)))
        result['complete'] = (result['source']==sources[name]['source'] and result['exit']=='0' and len(result['invocations'])==1 and len(rows)==2
                and {r['name'] for r in rows}==set(family_by_case) and all(not r['errors'] and not r['skipped'] and len(r['bursts'])==2 for r in rows)
                and all(0<=b['queued']<=b['written'] and b['actual_so_rcvbuf']>0 and b['remaining']==0 and b['drained']==b['written'] for r in rows for b in r['bursts']))
    except Exception as error:
        result.update(complete=False,collection_error=repr(error)[:500])
    result['full_before_both_drains'] = result['complete'] and all(b['queued']==b['written'] for r in result['cases'] for b in r['bursts'])
    return result

if sys.argv[1]=='prepare':
    prepare(sys.argv[2]);sys.exit(0)
results = {name:one(name) for name in sources}
complete = all(r['complete'] for r in results.values())
comparable = complete and all(results[name]['full_before_both_drains'] for name in ('native','coalesced'))
verdict = dict(complete=complete,controls_reproduced=comparable, combined_retained_both=(results['combined']['full_before_both_drains'] if comparable else None),
               sources=sources,measurements=results,verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
write(root/'verdict.json',verdict)
print(json.dumps({k:verdict[k] for k in ('complete','controls_reproduced','combined_retained_both','verified_at')}),flush=True)
sys.exit(0 if complete else 1)
