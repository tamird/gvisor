#!/usr/bin/env python3
"""Build a SHA-bound subset of a prior continuous-build census."""
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
from urllib.parse import unquote, urlsplit

import yaml
from stdlib_provenance import fields, first, number

SOURCE = '9e6e8fa9c55583b3505646d32713e991231ceb47'
TIMER_SOURCE = 'f876b5047538c0bbe2de38ef3f306a2cab985aa7'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'continuous-build'
COMMAND = ['make', 'build', 'OPTIONS=--build_tag_filters=-network_plugins', 'TARGETS=//...']
META = '--build_metadata=CONTINUOUS_CHECK=Native whole graph'
REPRESENTATIVE = '//test/syscalls/linux:proc_net_test'
ROOTS_SHA = 'df9f5550f68668d8657f4804a6572dc45da20f63c6faa81b263bef8ecffcf3ac'
ORIGINAL_CONFIG = 'dc7bccece9cdb4f2afd267c6827b36b64967d224f65fa96cbbc3777bfb26ea2a'
STRICT = ['--experimental_use_hermetic_linux_sandbox', '--spawn_strategy=linux-sandbox',
          '--strategy=CppCompile=linux-sandbox', '--strategy=CppLink=linux-sandbox',
          '--sandbox_default_allow_network=false', '--sandbox_debug']
STARTUP = ['--nosystem_rc', '--nohome_rc', '--host_jvm_args=-Xmx4g']


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
        with (ROOT/(phase+'-docker.jsonl')).open('a') as stream:
            stream.write('unexpected Docker invocation\n')
        return 91
    cmd, *args = argv
    assert cmd in ('build', 'cquery'), cmd
    path = ROOT / (phase + '-' + cmd)
    assert not path.with_suffix('.json').exists(), (phase, cmd)
    row = {'command':cmd, 'arguments':args, 'started_at':now()}
    write(path.with_suffix('.json'), row)
    requested = str(uuid.uuid4())
    row['requested_invocation'] = requested
    flags = ['--jobs=4', '--invocation_id='+requested,
             '--build_event_json_file='+str(path.with_suffix('.bep')),
             '--build_event_json_file_path_conversion=false']
    if cmd == 'build':
        flags += (STRICT if phase == 'strict' else ['--keep_going']) + [
            '--build_event_publish_all_actions',
            '--execution_log_compact_file='+str(path.with_suffix('.compact'))]
    row['placement_flags'] = STRICT if phase == 'strict' and cmd == 'build' else []
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


PROBLEM = re.compile(r'(?i)execvp\(|no such file or directory|error while loading shared libraries|'
                     r'command not found|unable to find|undefined (?:reference|symbol)|fatal error:|'
                     r'cannot open|no usable.*strategy|operation not permitted')


def summarize(phase, cmd, output_base):
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
                    p = Path(unquote(uri.path)).resolve()
                    if (channel=='stderr' and uri.netloc in ('','localhost') and len(failed)<8 and
                            p.is_relative_to(output_base) and re.fullmatch(r'stderr-[0-9]+',p.name) and
                            p.is_file() and p.stat().st_size<=8*1024**2):
                        data=p.read_bytes()
                        lines=data.decode(errors='replace').splitlines()
                        item[channel]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                                       'diagnostics':[diagnostic(l) for l in lines if PROBLEM.search(l)][:6]}

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
        row['action_diagnostics'] = [diagnostic(line) for line in lines if PROBLEM.search(line)][:8]
    return row


def compact(path, compare):
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
            argv_digest = hashlib.sha256(b'\0'.join(spawn.get(1, []))).hexdigest()
            if (mnemonic in ('CppCompile', 'CppLink') and owner == REPRESENTATIVE) or code or status or (owner,mnemonic,argv_digest) in compare:
                argv=spawn.get(1,[])
                compiler=argv[0].decode() if argv else ''
                declared=bool(re.fullmatch(r'external/[A-Za-z0-9_+./-]*llvm[A-Za-z0-9_+./-]*/bin/clang(?:\+\+)?',compiler))
                compilers.append({'argv_sha256':argv_digest,'owner':owner,'mnemonic':mnemonic,'runner':runner,'cached':cached,
                                  'exit':code,'declared_llvm':declared,
                                  'argv0':compiler if re.fullmatch(r'(?:external/|bazel-out/|/bin/|/usr/bin/)[A-Za-z0-9_+./-]+', compiler) and '..' not in compiler.split('/') else None,
                                  'argv0_sha256':hashlib.sha256(compiler.encode()).hexdigest()})
        assert child.wait()==0
    return {'invocation':invocation,'sha256':digest(path),'bytes':path.stat().st_size,
            'counts':[dict(zip(('mnemonic','runner','cached','exit','status_nonempty','count'),(*key,value))) for key,value in sorted(counts.items())],
            'placement_or_execution_errors':violations, 'project_compiler_actions':compilers}



def save(result):
    write(ROOT/'out/verdict.json', result)


def bazel_info(key, *, must_exist=True):
    return Path(subprocess.check_output([os.environ['REAL_BAZEL'], *STARTUP, 'info', key],
                                        text=True).strip()).resolve(strict=must_exist)


def failure(exc):
    frame = next(f for f in reversed(traceback.extract_tb(exc.__traceback__))
                 if Path(f.filename).name==Path(__file__).name)
    error = {'type':type(exc).__name__,'function':frame.name,'line':frame.lineno}
    if isinstance(exc,OSError):
        error['errno']=exc.errno
    if isinstance(exc,subprocess.CalledProcessError):
        error['exit']=exc.returncode
    return error


def namespace_control(result):
    # Same Bazel installation and helper as the real spawn. Change only the root.
    helper = bazel_info('install_base')/'linux-sandbox'
    assert helper.is_file()
    root = ROOT/'visibility'
    work = root/'work'
    work.mkdir(parents=True)
    row = result['namespace_control'] = {'helper_sha256':digest(helper)}
    for mode, extra in (('native', []), ('strict', ['-h', str(root)])):
        command = [str(helper), '-n','-W',str(work), *extra,'--','/bin/true']
        p = subprocess.run(command, cwd=work, capture_output=True, timeout=30)
        row[mode] = {'exit':p.returncode,'stderr_sha256':hashlib.sha256(p.stderr).hexdigest(),
                     'exec_enoent':bool(re.search(rb'execvp\(/bin/true,[^\n]+No such file or directory',p.stderr))}
        if p.returncode:
            row[mode]['diagnostic'] = diagnostic(p.stderr.decode(errors='replace'))
        save(result)
    row['verified'] = row['native']['exit'] == 0 and row['strict']['exit'] != 0 and row['strict']['exec_enoent']
    return row['verified']


def phase(result, name, labels, env, output_base, compare=()):
    row = result['phases'][name] = {'status':'INCOMPLETE','started_at':now(),'requested_labels':labels}
    save(result)
    command = COMMAND[:-1]+['TARGETS='+' '.join(labels),'DOCKER_BUILD=false']
    seconds = 900 if name == 'strict' else 4500
    with (ROOT/(name+'.out')).open('wb') as out, (ROOT/(name+'.err')).open('wb') as err:
        child = subprocess.run(['timeout','-k','20',str(seconds),*command],
                               env=env|{'QUAL_PHASE':name}, stdout=out,stderr=err)
    row.update(native_make_exit=child.returncode,execution_finished_at=now(),
               deadline_exceeded=child.returncode in (124,137))
    save(result)
    row['docker_calls'] = int((ROOT/(name+'-docker.jsonl')).exists())
    for cmd in ('build','cquery'):
        if (ROOT/(name+'-'+cmd+'.json')).exists():
            row[cmd] = summarize(name,cmd,output_base)
            save(result)
    build = row.get('build',{})
    selected = {(l,a,c) for l,a,k,t,c in build.get('configured',[])}
    passed = {(l,a,c) for l,a,c,ok in build.get('completed',[]) if ok}
    failed = {(l,a,c) for l,a,c,ok in build.get('completed',[]) if not ok}
    row['target_coverage'] = {'configured':len(selected),'successful':len(passed&selected),
                          'failed':len(failed&selected),'unaccounted':sorted(selected-passed-failed),
                          'requested_without_configured': sorted(set(labels)-{l for l,a,c in selected})}
    save(result)
    compact_path = ROOT/(name+'-build.compact')
    if compact_path.exists():
        try:
            row['placement'] = compact(compact_path, compare)
        except (AssertionError,ValueError,EOFError,IndexError,KeyError,subprocess.SubprocessError) as error:
            row['incomplete_placement'] = type(error).__name__
    save(result)
    if child.returncode:
        row['status'] = 'INCOMPLETE' if row['deadline_exceeded'] else 'FAIL'
        return
    for cmd in ('build','cquery'):
        report = row[cmd]
        assert report['native_exit'] == 0 and report['observed_invocation'] == report['requested_invocation']
        assert report['bazel_exit']['name'] == 'SUCCESS' and not report.get('incomplete_bep_tail_bytes')
        assert report['test_attempts'] == 0
    args = ['--color=no','--curses=no','--remote_cache=',META]
    assert build['arguments'] == args+['--remote_download_outputs=toplevel','--build_tag_filters=-network_plugins','--',*labels]
    expression = 'set()'+''.join(' union "'+l+'"' for l in labels)
    assert row['cquery']['arguments'] == args+['--output=starlark','--starlark:file=tools/show_paths.bzl',
                                              '--build_tag_filters=-network_plugins','--',expression]
    assert selected == {(l,'',ORIGINAL_CONFIG) for l in labels} and selected == passed and not failed
    assert not build['failed_actions'] and not row['docker_calls'] and not build['aborted']
    assert row['placement']['invocation'] == build['observed_invocation']
    assert not row['placement']['placement_or_execution_errors']
    output = Path('bazel-bin/test/syscalls/linux/proc_net_test').resolve(strict=True)
    assert output.is_file() and os.access(output,os.X_OK)
    assert str(output) in (ROOT/(name+'.out')).read_text().splitlines()
    with output.open('rb') as f:
        header = f.read(20)
    assert header[:6] == b'\x7fELF\x02\x01' and int.from_bytes(header[18:20],'little')==62
    row['representative_elf'] = {'owner':REPRESENTATIVE,'bytes':output.stat().st_size,'sha256':digest(output),'machine':62}
    actions = row['placement']['project_compiler_actions']
    representative = [a for a in actions if a['owner']==REPRESENTATIVE and a['exit']==0]
    row['fresh_declared_compile_and_link'] = all(any(a['mnemonic']==mnemonic and not a['cached'] and
        a['declared_llvm'] and a['runner']=='linux-sandbox' for a in representative)
        for mnemonic in ('CppCompile','CppLink'))
    if name=='strict':
        assert row['fresh_declared_compile_and_link']
    row['status'] = 'PASS'


def executable_evidence(action, execroot):
    name = action['argv0']
    if not name:
        return {}
    path = Path(name)
    if path.is_absolute():
        if name not in ('/bin/sh','/bin/bash','/usr/bin/env'):
            return {}
    else:
        path = execroot/path
        if not path.resolve().is_relative_to(execroot.parent.parent):
            return {}
    if not path.is_file():
        return {'present_in_native_execroot':False}
    with path.open('rb') as f:
        header = f.read(128)
    evidence = {'present_in_native_execroot':True,'sha256':digest(path),'bytes':path.stat().st_size}
    if header.startswith(b'\x7fELF'):
        p = subprocess.run(['readelf','-lW',path],capture_output=True,text=True,timeout=30)
        interp = re.search(r'Requesting program interpreter: (/[A-Za-z0-9_./+-]+)\]',p.stdout)
        if interp:
            evidence['elf_interpreter'] = interp[1]
    elif header.startswith(b'#!'):
        interp = re.match(rb'#!\s*(/[A-Za-z0-9_./+-]+)', header)
        if interp:
            evidence['script_interpreter'] = interp[1].decode()
    return evidence


def main(result):
    assert platform.system()=='Linux' and platform.machine()=='x86_64'
    assert platform.freedesktop_os_release()['VERSION_ID']=='22.04'
    assert git('rev-parse','HEAD')==SOURCE and git('rev-parse','HEAD^{tree}')=='9dcb76213b27e8091809b3c4bcb6710193f1aec6'
    assert git('rev-parse','HEAD:test/syscalls/linux')==git('rev-parse',TIMER_SOURCE+':test/syscalls/linux')
    assert not git('status','--porcelain','--untracked-files=no') and not Path('tools/google.mk').exists()
    assert subprocess.check_output([os.environ['REAL_BAZEL'],'--version'],text=True).strip()=='bazel 9.3.1'
    step, = [s for s in yaml.safe_load(Path('.buildkite/pipeline.yaml').read_text())['steps']
             if isinstance(s,dict) and s.get('label')==':world_map: Build everything']
    assert shlex.split(step['commands'][0]) == COMMAND and step['agents']=={'queue':'default'}
    assert step['if']=='build.env("STAGED_BINARIES") == null && build.branch == "master"'
    assert digest(ROOT/'roots')==ROOTS_SHA
    labels=(ROOT/'roots').read_text().splitlines()
    assert len(labels)==166 and labels==sorted(set(labels)) and REPRESENTATIVE in labels
    assert all(re.fullmatch(r'//test/syscalls/linux:[a-zA-Z0-9_]+',l) for l in labels)
    free=os.statvfs(ROOT)
    result['disk_free_bytes']=free.f_bavail*free.f_frsize
    assert result['disk_free_bytes']>=20*1024**3
    bindir=ROOT/'bin';bindir.mkdir()
    for tool in ('bazel','docker'):
        (bindir/tool).symlink_to(Path(__file__).resolve())
    env=os.environ|{'PATH':str(bindir)+':'+os.environ['PATH'],'BAZEL_OPTIONS':shlex.quote(META),
                    'BAZEL_REMOTE_CACHE':'--remote_cache='}
    result['stage']='locate_bazel_directories'
    save(result)
    output_base=bazel_info('output_base')
    # Bazel reports the execution root before a first analysis creates it.
    execroot=bazel_info('execution_root',must_exist=False)
    assert output_base.is_dir() and execroot.parent==output_base/'execroot'
    result['execution_root']={'under_output_base':True,'exists_before_analysis':execroot.is_dir()}
    result['stage']='qualify_profiles'
    save(result)
    # The ordinary cohort runs even if the isolated diagnostic cannot start.
    try:
        if namespace_control(result):
            phase(result,'strict',[REPRESENTATIVE],env,output_base)
    except Exception as exc:
        result['strict_read_error']=failure(exc)
    save(result)
    strict=result['phases'].get('strict',{})
    failures=[a for a in strict.get('placement',{}).get('project_compiler_actions',[])
              if a['exit'] and a['runner']=='linux-sandbox' and not a['cached']]
    for action in failures[:6]:
        try:
            action['executable']=executable_evidence(action,execroot)
        except (OSError, subprocess.SubprocessError) as exc:
            action['executable_inspection_error']=type(exc).__name__
    save(result)
    def action_key(a):
        return (a['owner'],a['mnemonic'],a['argv_sha256'])
    phase(result,'native',labels,env,output_base,set(map(action_key,failures)))
    native=result['phases']['native']
    save(result)
    if native['status']!='PASS':
        result['status']=native['status']
        return
    matched_actions = native['placement']['project_compiler_actions']+strict.get('placement',{}).get('project_compiler_actions',[])
    assert all(any(a['owner']==REPRESENTATIVE and a['mnemonic']==m and a['declared_llvm'] and
                   not a['cached'] and a['exit']==0 and a['runner']=='linux-sandbox' for a in matched_actions)
               for m in ('CppCompile','CppLink'))
    native_actions=native['placement']['project_compiler_actions']
    native_keys={action_key(a) for a in native_actions if not a['cached'] and not a['exit']}
    originals=strict.get('build',{}).get('failed_actions',[])
    strict_commands = strict.get('build',{}).get('action_diagnostics',[])
    def diagnosed(action):
        matches = [event for event in originals if event['owner']==action['owner'] and
                   event['mnemonic']==action['mnemonic'] and event['exit']==action['exit']]
        if any(e.get('stderr',{}).get('diagnostics') for e in matches):
            return True
        tool = action['argv0']
        if not matches or not tool:
            return False
        return any('execvp('+tool+',' in l['text'] and 'No such file' in l['text'] for l in strict_commands)

    result['matched_strict_failures']=[a for a in failures if action_key(a) in native_keys]
    result['strict_outcome']='INCOMPLETE'
    if strict.get('status')=='PASS':
        result['strict_outcome']='PASS'
    elif strict.get('status')=='FAIL' and any(diagnosed(a) for a in result['matched_strict_failures']):
        build=strict['build']
        assert strict['placement']['invocation']==build['observed_invocation']==build['requested_invocation']
        assert build['test_attempts']==0 and build['placement_flags']==STRICT
        # Records the matched action outcome. Its causal explanation depends on the
        # original stderr, the native control and the recorded executable or loader.
        result['strict_outcome']='MATCHED_ACTION_FAILURE'
    assert result['strict_outcome']!='INCOMPLETE'
    result['status']='PASS'


if __name__=='__main__':
    if Path(sys.argv[0]).name in ('docker','bazel'):
        raise SystemExit(wrapped())
    result={'status':'INCOMPLETE','source':SOURCE,'phases':{},'test_execution_claimed':False,
            'full_graph_status':'INCOMPLETE','root_manifest_sha256':ROOTS_SHA,'stage':'bootstrap',
            'prior_bootstrap':{'artifact':13383533461,'source':SOURCE,'status':'INCOMPLETE','phases':[],
                'sha256':'e83a819f0b4924f6783a9fd4675420a8675f89a400585694ec534cdf400ef344'},
            'original_full_graph':{'source':TIMER_SOURCE,'artifact':13355668355,
                'sha256':'0118c5f6f0df380d17386d88e9726b7a5303b0be2214a9dfa1061ddb91820fdf',
                'configured':7766,'successful':2098,'failed':1,'unaccounted':5667,'selected_package':303},
            'prior_build_artifacts':[13340750893,13347734045],'prior_package_passed':136,
            'additional_timer_artifact':13350348791,'strict_first':True,'remote_cache':False}
    try:
        main(result)
    except Exception as exc:
        result['error']=failure(exc)
    finally:
        result['verified_at']=now();save(result)
    raise SystemExit(int(result['status']!='PASS'))
