"""Measure six bounded canonical report/consume workloads without changing source."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

mode = sys.argv[1]
work, results, support = map(Path, sys.argv[2:5])
active = work / 'active'
process = None


def command(*args, timeout=20, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


def recover():
    evidence = []
    if active.exists():
        for root in sorted((work / 'cases').glob('*/tmp/containers*')):
            assert root.is_dir() and not root.is_symlink() and root.resolve().is_relative_to(work.resolve())
            listed = command(str(active/'runsc'), '--log=/dev/null', '--root='+str(root), 'list', '--quiet', timeout=10)
            identifiers = listed.stdout.split()
            assert len(identifiers) <= 1 and all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*', x) for x in identifiers)
            for identifier in identifiers:
                command(str(active/'runsc'), '--log=/dev/null', '--root='+str(root), 'delete', '-force=true', identifier, timeout=15)
            evidence.append({'root':str(root), 'deletedIDs':identifiers})
        command('python3', str(support/'quiescent.py'), str(active), timeout=20)
    (results/'runtime-recovery.json').write_text(json.dumps(evidence,indent=2)+'\n')


def interrupted(number, frame):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    raise SystemExit(128+number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
if mode == 'cleanup':
    recover()
    raise SystemExit(0)
assert mode == 'run' and os.getuid() != 0
assert not any(os.environ.get(x) for x in ['COVERAGE_DIR','GOCOVERDIR','GO_TEST_RUN_FROM_BAZEL'])
records = []


def verify(side, label):
    for folder, frozen, name in [(active, results/(side+'-frozen-runtime.json'), 'runtime'),
                                 (work/'harness', results/'frozen-harness.json', 'harness')]:
        command('python3',str(support/'fileset.py'),'verify',str(folder),str(frozen),str(results/(label+'-'+name+'.json')),timeout=60)
    command('python3',str(support/'quiescent.py'),str(active),timeout=20)
    wrapper = work/'tools/report-runtime'
    assert wrapper.is_file() and not wrapper.is_symlink() and os.access(wrapper,os.X_OK)
    assert hashlib.sha256(wrapper.read_bytes()).digest()==hashlib.sha256((support/'report_runtime.py').read_bytes()).digest()


def validate(label, suite, name, output, report_enabled):
    raw = (results/(label+'.log')).read_text()
    full = suite+'.'+name
    counts = {tag:len(re.findall(r'^\[\s*'+tag+r'\s*\]\s+'+re.escape(full)+r'(?:\s|$)',raw,re.M)) for tag in ['RUN','OK']}
    assert counts == {'RUN':100,'OK':100}, counts
    assert not re.search(r'\[\s*(?:FAILED|SKIPPED)\s*\]|--- (?:FAIL|SKIP)',raw), label
    xml = ET.parse(output/'test.xml').getroot()
    cases = list(xml.iter('testcase'))
    assert len(cases)==1 and cases[0].get('classname')==suite and cases[0].get('name')==name
    assert all(cases[0].find(x) is None for x in ['failure','error','skipped'])
    assert cases[0].get('status','run')=='run' and cases[0].get('result','completed')=='completed'
    for section in xml.iter('testsuite'):
        assert all(int(section.get(x,'0'))==0 for x in ['failures','errors','skipped'])
    reports = []
    kernel = False
    for path in sorted(output.rglob('*')):
        if 'coverage' not in path.relative_to(output).parts or path.is_dir():
            continue
        assert path.is_file() and not path.is_symlink(), path
        data = path.read_bytes()
        lines = data.decode().splitlines()
        for line in lines:
            position = re.fullmatch(r'(.+):(\d+)\.(\d+),(\d+)\.(\d+)',line)
            assert position, (path,line)
            start_line,start_column,end_line,end_column = map(int,position.groups()[1:])
            assert min(start_line,start_column,end_line,end_column)>0, (path,line)
            assert (end_line,end_column)>=(start_line,start_column), (path,line)
        kernel |= any('/pkg/sentry/' in line for line in lines)
        reports.append({'path':str(path.relative_to(output)),'bytes':len(data),'positions':len(lines),'sha256':hashlib.sha256(data).hexdigest()})
    if report_enabled:
        assert reports and sum(x['positions'] for x in reports)>0 and kernel, reports
    else:
        assert not reports, reports
    audits = [json.loads(line) for line in (results/(label+'-runtime-argv.jsonl')).read_text().splitlines()]
    assert audits
    redirected = 0
    for audit in audits:
        assert audit['realRuntime']==str(active/'runsc')
        assert audit['sidecarDirectory']==str(active/'gvisor-bin')
        before,after = audit['originalArgs'],audit['finalArgs']
        assert len(before)==len(after) and all(isinstance(x,str) for x in before+after)
        indices = [i for i,x in enumerate(before) if x=='-coverage-report']
        assert len(indices)<=1
        if indices:
            assert report_enabled and audit['reportPathRewritten']
            i = indices[0]+1
            assert i<len(before)
            relative = Path(before[i]).resolve().relative_to(output.resolve())
            assert len(relative.parts)==3 and relative.parts[0]=='main.test'
            assert re.fullmatch(r'runsc[0-9]+',relative.parts[1]) and relative.parts[2]=='coverage'
            assert after[i]==str(output/'coverage')+'/'
            assert before[:i]==after[:i] and before[i+1:]==after[i+1:]
            redirected += 1
        else:
            assert not audit['reportPathRewritten'] and before==after
    assert bool(redirected)==report_enabled
    statistics = (results/(label+'-time.txt')).read_text()
    rss = re.search(r'Maximum resident set size \(kbytes\):\s*(\d+)',statistics)
    assert rss and int(rss[1])>0, statistics
    return {'case':full,'actualIterations':counts,'finalXMLCases':1,'reports':reports,'auditedRuntimeInvocations':len(audits),'redirectedReportPaths':redirected,'maxWaitedCommandRSSKiB':int(rss[1]),'scope':'capability/volume only; whole-command walltime and GNU time waited-descendant RSS; detached Sentry may be absent; not isolated serializer cost; shared wrapper launch included and retained profile deletion omitted'}


try:
    for side in ['base','candidate']:
        recover()
        if active.exists():
            shutil.rmtree(active)
        shutil.copytree(work/side/'runtime',active,symlinks=False)
        for condition,binary,suite,name,report_enabled in [
            ('report-off','uname_test_runsc_systrap_directfs','UnameTest','Sanity',False),
            ('report-on','uname_test_runsc_systrap_directfs','UnameTest','Sanity',True),
            ('kcov-consume','kcov_test_runsc_systrap_directfs','KcovTest','Kcov',False),
        ]:
            label=side+'-'+condition
            verify(side,label+'-before')
            case=work/'cases'/label
            temporary=case/'tmp'
            temporary.mkdir(parents=True)
            output=results/(label+'-output')
            output.mkdir()
            runfiles=work/'harness'/(binary+'.runfiles')
            xml=output/'test.xml'
            args=['/usr/bin/time','-v','-o',str(results/(label+'-time.txt')),
                  'env','LC_ALL=C','GTEST_REPEAT=100','GTEST_FILTER='+suite+'.'+name,'RUNTIME='+str(work/'tools/report-runtime'),
                  'REPORT_CONSUME_WORK='+str(work),'REPORT_CONSUME_OUTPUT='+str(output),
                  'REPORT_CONSUME_REAL_RUNTIME='+str(active/'runsc'),
                  'REPORT_CONSUME_AUDIT='+str(results/(label+'-runtime-argv.jsonl')),
                  'REPORT_CONSUME_REPORT='+str(report_enabled).lower(),
                  'GVISOR_SIDECAR_BINARIES_DIR='+str(active/'gvisor-bin'),
                  'RUNFILES_DIR='+str(runfiles),'TEST_SRCDIR='+str(runfiles),'TEST_WORKSPACE=_main',
                  'TEST_TMPDIR='+str(temporary),'TMPDIR='+str(temporary),
                  'TEST_UNDECLARED_OUTPUTS_DIR='+str(output),'XML_OUTPUT_FILE='+str(xml),
                  'bash',str(work/'harness'/binary),'--collect-coverage='+str(report_enabled).lower()]
            (results/(label+'-argv.json')).write_text(json.dumps(args,indent=2)+'\n')
            start=time.monotonic()
            with (results/(label+'.log')).open('w') as stream:
                process=subprocess.Popen(args,cwd=runfiles/'_main',stdout=stream,stderr=subprocess.STDOUT,start_new_session=True,env=dict(os.environ,LC_ALL='C'))
                try:
                    rc=process.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL)
                        process.wait()
                    rc=124
                process=None
            record={'label':label,'side':side,'reportEnabled':report_enabled,'exit':rc,'wallSeconds':time.monotonic()-start}
            records.append(record)
            (results/'workload-evidence.json').write_text(json.dumps(records,indent=2)+'\n')
            recover()
            try:
                assert rc==0,record
                record.update(validate(label,suite,name,output,report_enabled))
                record['validated'] = True
            except (AssertionError,ValueError,OSError,ET.ParseError) as error:
                # Keep failed observations failed, but collect the other independent
                # conditions when runtime cleanup and identity still pass.
                record['validated'] = False
                record['validationError'] = repr(error)
            (results/'workload-evidence.json').write_text(json.dumps(records,indent=2)+'\n')
            verify(side,label+'-after')
    assert len(records)==6 and all(record['validated'] for record in records),records
    (results/'workloads-complete.json').write_text(json.dumps({'passed':True,'observations':6,'statisticalQualification':False,'reportsAndKcovMutuallyExclusive':True},indent=2)+'\n')
finally:
    recover()
