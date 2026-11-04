#!/usr/bin/env python3
"""Qualify the existing full-graph Make command on a native Linux client."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import traceback
import uuid
from urllib.parse import urlsplit

import yaml
from stdlib_provenance import fields, first, number

SOURCE = 'f876b5047538c0bbe2de38ef3f306a2cab985aa7'
FSSTRESS_SOURCE = '588e6f3436b08e9b9a49e656458d9ea6178bfedd'
ANALYZED_SOURCE = '2ae92e53463b06a49d68f8d8e83ba1737bf0ade9'
INTEGRATION = '03b28790330694994421731ae9e9299aa4a3da60'
MASTER = '6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'continuous-build'
COMMAND = ['make', 'build', 'OPTIONS=--build_tag_filters=-network_plugins', 'TARGETS=//...']
META = '--build_metadata=CONTINUOUS_CHECK=Native whole graph'
COMPILER_OWNER = '//test/util:test_util'

# The unchanged selection was analyzed on ANALYZED_SOURCE. The only new main
# repository owner is the fsstress filegroup; compare the remaining semantic
# census to the original without repeating --nobuild or pinning configuration IDs.
ORIGINAL_ANALYSIS = {
    'artifact': 'https://github.com/tamird/gvisor/actions/runs/41857930013/artifacts/13339117542',
    'sha256': '5595c2a7715649e9c79f6c080edfbded0a246b015d366db263beaba91ff22b25',
    'source': ANALYZED_SOURCE,
    'invocation': '0a03c1a5-da07-47b7-8f7e-a7ec16e6d124',
    'status': 'PASS',
    'configured_count': 7765,
    'census_sha256': 'daa71eebe6180e6d5397d8777dcac1ce9bc40fd1a1d0b18172985165380ba9d1',
    'semantic_census_sha256': '82ef559fdf71d0e5646d5c70caeb17a5bf6adf33e1021c77c5f54c5ab608de68',
    'outside_pkg_and_runsc': 4885,
    'verified_at': '2026-11-04T15:38:36.767223+00:00',
    'arguments': {
        'build': ['--color=no', '--curses=no', '--remote_cache=', META,
                  '--remote_download_outputs=toplevel',
                  '--build_tag_filters=-network_plugins', '--', '//...'],
        'cquery': ['--color=no', '--curses=no', '--remote_cache=', META,
                   '--output=starlark', '--starlark:file=tools/show_paths.bzl',
                   '--build_tag_filters=-network_plugins', '--', 'set() union "//..."'],
    },
}


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def normalized(label):
    return label.removeprefix('@@').removeprefix('@')


def events(path, row):
    if path.is_file():
        with path.open('rb') as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if line.endswith(b'\n'):
                        raise
                    row['incomplete_bep_tail_bytes'] = len(line)
                    return


def wrapped():
    phase = os.environ['QUAL_PHASE']
    argv = sys.argv[1:]
    if Path(sys.argv[0]).name == 'docker':
        with (ROOT / (phase + '-docker.jsonl')).open('a') as stream:
            stream.write(json.dumps(argv) + '\n')
        if phase != 'shape-docker':
            return 91
        if argv == ['version', '--format={{.Server.APIVersion}}']:
            print('1.45')
            return 0
        assert argv[:1] == ['exec'] and 'qualification-builder' in argv, argv
        command = argv[argv.index('qualification-builder') + 1:]
        if command == ['echo', 'true']:
            print('true')
            return 0
        if command[:1] == ['xargs']:
            assert not sys.stdin.buffer.read()
            return 0
        assert command[:1] == ['bazel'], command
        argv = command[1:]
    cmd, *args = argv
    assert cmd in ('build', 'cquery'), cmd
    path = ROOT / (phase + '-' + cmd)
    assert not path.with_suffix('.json').exists(), (phase, cmd)
    row = {'command':cmd, 'arguments':args, 'started_at':now()}
    write(path.with_suffix('.json'), row)
    if phase.startswith('shape-'):
        return 0
    requested = str(uuid.uuid4())
    row['requested_invocation'] = requested
    flags = ['--jobs=4', '--invocation_id='+requested,
             '--build_event_json_file='+str(path.with_suffix('.bep')),
             '--build_event_json_file_path_conversion=false']
    if cmd == 'build':
        flags += ['--nobuild'] if phase == 'analysis' else [
            '--keep_going',
            '--build_event_publish_all_actions',
            '--execution_log_compact_file='+str(path.with_suffix('.compact'))]
    row['instrumentation'] = [f.split('=',1)[0] for f in flags]
    write(path.with_suffix('.json'), row)
    with path.with_suffix('.out').open('wb') as out, path.with_suffix('.err').open('wb') as err:
        child = subprocess.run([os.environ['REAL_BAZEL'], '--nosystem_rc', '--nohome_rc',
                                '--host_jvm_args=-Xmx4g',
                                cmd, *flags, *args], stdout=out, stderr=err)
    row.update(native_exit=child.returncode, finished_at=now())
    write(path.with_suffix('.json'), row)
    if cmd == 'cquery':
        with path.with_suffix('.out').open('rb') as stream:
            while chunk := stream.read(1024*1024):
                sys.stdout.buffer.write(chunk)
    return child.returncode


def diagnostic(value):
    """Bound a public build diagnostic from this credential-free worker."""
    raw=value.encode()
    value=re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', value)
    for path, replacement in ((str(Path.cwd()), '$WORKSPACE'), (str(ROOT), '$TEMP'), (str(Path.home()), '$HOME')):
        value=value.replace(path, replacement)
    value=re.sub(r'(?:https?|grpcs?)://[^\s\"\']+', '<URL>', value)
    return {'text':value[:2400], 'truncated':len(value)>2400,
            'sha256':hashlib.sha256(raw).hexdigest(), 'bytes':len(raw)}


def summarize(phase, cmd):
    path = ROOT/(phase+'-'+cmd)
    row = json.loads(path.with_suffix('.json').read_text())
    configured, completed, skipped, failed, configs = [], [], [], [], {}
    attempts = 0
    for event in events(path.with_suffix('.bep'), row):
        key = event.get('id', {})
        if 'started' in event:
            row['observed_invocation'] = event['started']['uuid']
        if 'finished' in event:
            row['bazel_finished'] = event['finished'].get('finishTime')
            row['bazel_exit'] = event['finished'].get('exitCode')
        if 'configuration' in event:
            configs[key['configuration']['id']] = event['configuration'].get('cpu')
        if 'configured' in event and 'targetConfigured' in key:
            owner = normalized(key['targetConfigured']['label'])
            for child in event.get('children', []):
                target = child.get('targetCompleted')
                if target:
                    configured.append([owner, key['targetConfigured'].get('aspect',''),
                                       event['configured'].get('targetKind',''),
                                       sorted(event['configured'].get('tag',[])), target['configuration']['id']])
        if 'targetCompleted' in key and 'completed' in event:
            t=key['targetCompleted']
            completed.append([normalized(t['label']),t.get('aspect',''),t['configuration']['id'],bool(event['completed'].get('success'))])
        if 'aborted' in event:
            t=key.get('targetConfigured',key.get('targetCompleted',{}))
            skipped.append({'label':normalized(t.get('label','')), 'reason':event['aborted'].get('reason'),
                            'aspect':t.get('aspect',''), 'configuration':t.get('configuration',{}).get('id'),
                            'diagnostic':diagnostic(event['aborted'].get('description',''))})
        attempts += int('testResult' in key)
        action=event.get('action')
        if action and not action.get('success'):
            identity=key.get('actionCompleted',{})
            item={'owner':normalized(identity.get('label','')), 'configuration':identity.get('configuration',{}).get('id'),
                  'primary_output':identity.get('primaryOutput'), 'mnemonic':action.get('type'), 'exit':action.get('exitCode')}
            for channel in ('stderr','stdout'):
                uri=urlsplit(action.get(channel,{}).get('uri',''))
                match=re.fullmatch(r'/?(?:.*?/)?blobs/([a-f0-9]{64})/([0-9]+)',uri.path)
                if uri.scheme=='bytestream' and match:
                    item[channel]={'sha256':match[1],'bytes':int(match[2]),'disposition':'CAS_IDENTITY_ONLY'}
                elif uri.scheme=='file':
                    item[channel]={'disposition':'LOCAL_FILE_NOT_READ'}
            failed.append(item)
    row.update(configured=sorted(configured), completed=sorted(completed), aborted=skipped,
               failed_actions=failed, configurations=configs, test_attempts=attempts)
    stderr=path.with_suffix('.err')
    if stderr.is_file():
        row['console']={'sha256':digest(stderr),'bytes':stderr.stat().st_size}
        text=stderr.read_text(errors='replace')
        lines=text.splitlines()
        errors = []
        for index, line in enumerate(lines):
            if line.startswith('ERROR:'):
                if not errors:
                    row['first_console_error']=diagnostic('\n'.join(lines[index:index+12]))
                errors.append(diagnostic(line))
                if len(errors) == 6:
                    break
        row['console_errors'] = errors
        row['error_classes']=[word for word in ('no matching toolchains','no such target','no such package',
            'No space left on device','OUT_OF_MEMORY','Error downloading','undeclared inclusion','not found',
            'Permission denied','undefined reference','missing strict dependencies','Timed out') if word in text]
    return row


def compact(path):
    counts=Counter(); violations=[]; invocation=None; compilers=[]
    with subprocess.Popen(['zstd','-qdc',str(path)],stdout=subprocess.PIPE) as child:
        while (size := number(child.stdout)) is not None:
            assert 0 < size < 64*1024*1024, size
            entry=fields(child.stdout.read(size))
            if 2 in entry:
                invocation=first(fields(first(entry,2)),4).decode()
            if 7 not in entry:
                continue
            spawn=fields(first(entry,7)); mnemonic=first(spawn,8).decode(); runner=first(spawn,11).decode()
            cached=bool(first(spawn,12,0)); code=first(spawn,9,0); status=first(spawn,10).decode()
            counts[(mnemonic,runner,cached,code,bool(status))]+=1
            owner=normalized(first(spawn,7).decode())
            if runner not in ('linux-sandbox','processwrapper-sandbox','local') or code or status:
                violations.append({'owner':owner,'mnemonic':mnemonic,
                                   'runner':runner,'cached':cached,'exit':code,'status_nonempty':bool(status)})
            if mnemonic=='CppCompile' and owner==COMPILER_OWNER:
                argv=spawn.get(1,[])
                compiler=argv[0].decode() if argv else ''
                declared=bool(re.fullmatch(r'external/[A-Za-z0-9_+./-]*llvm[A-Za-z0-9_+./-]*/bin/clang(?:\+\+)?',compiler))
                compilers.append({'owner':owner,'mnemonic':mnemonic,'runner':runner,'cached':cached,
                                  'exit':code,'declared_llvm':declared,
                                  'argv0':compiler if declared else None,
                                  'argv0_sha256':hashlib.sha256(compiler.encode()).hexdigest()})
        assert child.wait()==0
    return {'invocation':invocation,'sha256':digest(path),'bytes':path.stat().st_size,
            'counts':[dict(zip(('mnemonic','runner','cached','exit','status_nonempty','count'),(*key,value))) for key,value in sorted(counts.items())],
            'placement_or_execution_errors':violations, 'project_compiler_actions':compilers}


def main(result):
    assert platform.system()=='Linux' and platform.machine()=='x86_64'
    assert platform.freedesktop_os_release()['VERSION_ID']=='22.04'
    assert git('rev-parse','HEAD')==SOURCE and git('rev-parse','HEAD^')==FSSTRESS_SOURCE
    assert git('rev-parse','HEAD~2')==ANALYZED_SOURCE
    assert git('rev-parse','HEAD~3')==INTEGRATION and git('rev-parse','HEAD~4')==MASTER
    assert git('diff','--name-only',FSSTRESS_SOURCE,SOURCE)=='test/syscalls/linux/timers.cc'
    assert git('rev-parse','HEAD:test/syscalls/linux/timers.cc')=='f9be9a7df51325a66bb5655a94174cec0fe01a97'
    assert git('diff','--name-only',ANALYZED_SOURCE,FSSTRESS_SOURCE).splitlines()==[
        'images/basic/BUILD', 'images/basic/ltp.BUILD']
    assert not git('status','--porcelain','--untracked-files=no')
    assert not Path('tools/google.mk').exists()
    assert subprocess.check_output([os.environ['REAL_BAZEL'],'--version'],text=True).strip()=='bazel 9.3.1'
    result['disk_free_bytes']=os.statvfs(ROOT).f_bavail*os.statvfs(ROOT).f_frsize
    result['host_cpus']=os.cpu_count()
    result['host_memory_bytes']=os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')
    assert result['disk_free_bytes'] >= 20*1024**3
    pipeline=yaml.safe_load(Path('.buildkite/pipeline.yaml').read_text())
    step, = [s for s in pipeline['steps'] if isinstance(s,dict) and s.get('label')==':world_map: Build everything']
    command, = step['commands']
    assert shlex.split(command)==COMMAND
    assert step['if']=='build.env("STAGED_BINARIES") == null && build.branch == "master"'
    assert step['agents']=={'queue':'default'}
    result['public_job']={'build':53478,'job':'01a1a68a-d0e7-4ef7-86c7-9561d699321d','architecture':'amd64',
                          'command':command,'condition':step['if'],'private_bazeldefs_equivalence':False}
    bindir=ROOT/'bin';bindir.mkdir()
    for tool in ('bazel','docker'):
        (bindir/tool).symlink_to(Path(__file__).resolve())
    env=os.environ | {'PATH':str(bindir)+':'+os.environ['PATH'], 'BAZEL_OPTIONS':shlex.quote(META),
                      'BAZEL_REMOTE_CACHE':'--remote_cache='}
    phases=('build',)
    for phase in phases:
        row=result['phases'][phase]={'status':'INCOMPLETE','started_at':now()}
        write(ROOT/'out/verdict.json',result)
        mode='true' if phase=='shape-docker' else 'false'
        with (ROOT/(phase+'.out')).open('wb') as out, (ROOT/(phase+'.err')).open('wb') as err:
            child=subprocess.run(['timeout','-k','20','4500' if phase=='build' else '1200',*COMMAND,
                                  'DOCKER_NAME=qualification-builder'],env=env|{'DOCKER_BUILD':mode,'QUAL_PHASE':phase},stdout=out,stderr=err)
        row.update(native_make_exit=child.returncode,finished_at=now())
        row['deadline_exceeded'] = child.returncode in (124, 137)
        if child.returncode and not row['deadline_exceeded']:
            row['status']='FAIL'
        write(ROOT/'out/verdict.json',result)
        for cmd in ('build','cquery'):
            p=ROOT/(phase+'-'+cmd+'.json')
            if p.is_file():
                row[cmd]=json.loads(p.read_text()) if phase.startswith('shape-') else summarize(phase,cmd)
        row['docker_calls']=sum(1 for _ in (ROOT/(phase+'-docker.jsonl')).open()) if (ROOT/(phase+'-docker.jsonl')).exists() else 0
        write(ROOT/'out/verdict.json',result)
        if phase == 'build' and (ROOT/'build-build.compact').is_file():
            try:
                row['placement'] = compact(ROOT/'build-build.compact')
            except Exception as exc:
                row['placement_incomplete'] = type(exc).__name__
            write(ROOT/'out/verdict.json',result)
        if phase=='analysis':
            owners=row['build']['configured']; assert owners
            assert any('nogo' in tags for _,_,_,tags,_ in owners)
            row['top_level_packages']=dict(sorted(Counter(label.split('/')[2].split(':')[0] for label,*_ in owners).items()))
            row['outside_pkg_and_runsc']=sum(not label.startswith(('//pkg/','//runsc/','//runsc:')) for label,*_ in owners)
            assert row['outside_pkg_and_runsc']>0
        if phase=='build':
            census = json.dumps(row['build']['configured'], sort_keys=True, separators=(',', ':')).encode()
            row['configured_census_sha256'] = hashlib.sha256(census).hexdigest()
            write(ROOT/'out/verdict.json',result)
            added=[r for r in row['build']['configured'] if r[0]=='//images/basic:fsstress_binary']
            prior=[r[:-1] for r in row['build']['configured'] if r[0]!='//images/basic:fsstress_binary']
            row['added_configured_owners']=added
            row['prior_semantic_census_sha256']=hashlib.sha256(json.dumps(
                sorted(prior),sort_keys=True,separators=(',',':')).encode()).hexdigest()
            selected={(label, aspect, configuration) for label,aspect,_,_,configuration in row['build']['configured']}
            passed={tuple(key) for *key,ok in row['build']['completed'] if ok} & selected
            failed={tuple(key) for *key,ok in row['build']['completed'] if not ok} & selected
            aborted={(item['label'],item['aspect'],item['configuration']) for item in row['build']['aborted']} & selected
            row['configured_owner_count']=len(selected)
            row['outside_pkg_and_runsc']=sum(not label.startswith(('//pkg/','//runsc/','//runsc:')) for label,_,_ in selected)
            row['selected_coverage']={'passed':len(passed), 'failed':len(failed), 'aborted':len(aborted),
                                      'unaccounted':len(selected-passed-failed-aborted),
                                      'contradictory':len((passed&failed)|(passed&aborted)|(failed&aborted))}
            write(ROOT/'out/verdict.json',result)
            assert len(added)==1 and added[0][1:4]==['','filegroup rule',[]]
            assert len(prior)==ORIGINAL_ANALYSIS['configured_count']
            assert row['prior_semantic_census_sha256']==ORIGINAL_ANALYSIS['semantic_census_sha256']
            assert row['outside_pkg_and_runsc']==ORIGINAL_ANALYSIS['outside_pkg_and_runsc']+1
            assert '--keep_going' in row['build']['instrumentation']
            assert passed==selected and not failed and not aborted
            assert row['build']['completed'] and all(ok for *_,ok in row['build']['completed'])
            completed={(label, aspect, configuration) for label,aspect,configuration,_ in row['build']['completed']}
            row['completed_owner_count']=len(completed)
            assert not row['build']['failed_actions']
            assert row['placement']['invocation']==row['build']['observed_invocation']
            assert row['placement']['counts'] and not row['placement']['placement_or_execution_errors']
            compilers=row['placement']['project_compiler_actions']
            assert compilers and all(c['declared_llvm'] for c in compilers)
            assert any(not c['cached'] and c['exit']==0 for c in compilers)
            row['materialized_make_paths'] = sum(bool(line.strip()) for line in (ROOT/'build.out').read_text().splitlines())
            assert row['materialized_make_paths']>0
        assert child.returncode==0
        assert row['docker_calls'] if phase=='shape-docker' else not row['docker_calls']
        for cmd in ('build','cquery'):
            assert META in row[cmd]['arguments']
            assert '--build_tag_filters=-network_plugins' in row[cmd]['arguments']
            if phase!='shape-docker':
                assert row[cmd]['arguments']==ORIGINAL_ANALYSIS['arguments'][cmd]
            if not phase.startswith('shape-'):
                assert row[cmd]['native_exit']==0 and row[cmd]['observed_invocation']==row[cmd]['requested_invocation']
                assert row[cmd]['test_attempts']==0
                assert not row[cmd].get('incomplete_bep_tail_bytes')
                assert row[cmd]['bazel_exit'].get('code', 0)==0
                assert row[cmd]['bazel_exit']['name']=='SUCCESS'
        row['status']='PASS';write(ROOT/'out/verdict.json',result)
    assert not git('status','--porcelain','--untracked-files=no')
    result['status']='PASS'


if __name__=='__main__':
    if Path(sys.argv[0]).name in ('docker','bazel'):
        raise SystemExit(wrapped())
    result={'status':'INCOMPLETE','source':SOURCE,'master':MASTER,'phases':{},'test_execution_claimed':False,
            'execution_profile':'Native Ubuntu 22.04 AMD64; default Bazel sandbox; no remote credentials',
            'bounds':{'jobs':4,'build_seconds':4500},
            'analysis_only':False,
            'completed_analysis':ORIGINAL_ANALYSIS,
            'prior_timer_link_failure':{
                'artifact':'https://github.com/tamird/gvisor/actions/runs/41872651951/artifacts/13347734045',
                'sha256':'f83548d8d079b4f49ddaaf57f8af29695b1c64554eaa2575260226cee76503e1',
                'source':FSSTRESS_SOURCE, 'native_make_exit':2, 'native_bazel_exit':1,
                'failed_owner':'//test/syscalls/linux:timers_test', 'failed_mnemonic':'CppLink'},
            'timer_qualification':{
                'artifact':'https://github.com/tamird/gvisor/actions/runs/41883374454/artifacts/13350348791',
                'sha256':'97dbf291cef408baa904ed453d2730c0400506d97f7aadb0f5869253624f7cc8',
                'source':SOURCE, 'master_fix':'626ca2e773c6e35a4948594927ca9a0e3ab3f1dd',
                'case':'IntervalTimerTest.NegativeInterval', 'linux':'PASS', 'systrap':'PASS'},
            'prior_full_build':{
                'artifact':'https://github.com/tamird/gvisor/actions/runs/41861120359/artifacts/13340750893',
                'sha256':'928b4aff1a8e04a3ae546d7aa0ed05fd1f13d925676694167e1da550dec0bd68',
                'source':ANALYZED_SOURCE, 'status':'FAIL', 'native_make_exit':2, 'native_bazel_exit':1,
                'invocation':'98f33d10-6cca-4914-98dd-0b68ef53c140',
                'failed_owner':'@@+http_archive+ltp//:fsstress', 'failed_mnemonic':'CppLink'},
            'fsstress_qualification':{
                'artifact':'https://github.com/tamird/gvisor/actions/runs/41870529908/artifacts/13343778034',
                'sha256':'44f3dd76fc9fbbb0f88432fa55f81d12ce5e3dca023caef8135cdd574766eb41',
                'source':FSSTRESS_SOURCE, 'aggregate_status':'FAIL',
                'root_workspace_builds':'AMD64 and ARM64 binary/archive PASS',
                'runtime':'AMD64 four file creates PASS; ARM64 not executed',
                'independent_gap':'downstream dependency query failed in Gazelle before label resolution'},
            'integration':INTEGRATION,
            'previous_diagnostic':{'artifact':'https://github.com/tamird/gvisor/actions/runs/41856180451/artifacts/13336853558',
                                   'sha256':'ecee3c3a840371641c3ddb471d3f995eaa7b78dff08a5f90b448c3607bb02788',
                                   'source':INTEGRATION,'status':'FAIL','cause':'nested_with_cfg_recursion'},
            'original':{'artifact':'https://github.com/tamird/gvisor/actions/runs/41852660469/artifacts/13335577304',
                        'sha256':'566f417d0bdc59dde7b4e02f2785ce272c43592d24228be245c6e1e68c5a1e06',
                        'status':'FAIL','phase':'analysis','native_bazel_exit':1,'configured_owners':0,
                        'invocation':'5256b80e-d210-477e-b28d-421287c2cab0'}}
    try:
        main(result)
    except Exception as exc:
        result['error']={'type':type(exc).__name__,'line':traceback.extract_tb(exc.__traceback__)[-1].lineno}
        if any(row['status']=='FAIL' for row in result['phases'].values()):
            result['status']='FAIL'
    finally:
        result['verified_at']=now();write(ROOT/'out/verdict.json',result)
    raise SystemExit(int(result['status']!='PASS'))
