#!/usr/bin/env python3
"""Build the production runsc binary in the strict Linux sandbox."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import signal
import subprocess
import sys
import traceback
import uuid
from urllib.parse import unquote, urlsplit

import yaml
from stdlib_provenance import fields, first, number

SOURCE = 'c4361dee1c37284859a3b176a5e112af3d73c543'
ORIGINAL = '9636c032922a4ead41a7216453240849a9c1fedd'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'continuous-build'
COMMAND = ['make', 'build', 'OPTIONS=--build_tag_filters=-network_plugins', 'TARGETS=//...']
META = '--build_metadata=CONTINUOUS_CHECK=Native whole graph'
REPRESENTATIVE = '//runsc:runsc'
RUNSC_OUTPUT = re.compile(r'bazel-out/[A-Za-z0-9_+-]+/bin/runsc/runsc_/runsc')
SUPPLIER = 'eb198127bdc4f7b737d08a7ea009869bc7a72e75'
ORIGINAL_CONFIG = '76ef89b65a9bb878d6de92da8f60df0763aedc234bd6ee17fd9a7f2a654daac6'
STRICT = ['--experimental_use_hermetic_linux_sandbox', '--spawn_strategy=linux-sandbox',
          '--strategy=GoLink=linux-sandbox',
          '--sandbox_default_allow_network=false', '--sandbox_debug']
PROTO='protobuf+//bazel/private/oss/toolchains/prebuilt:authenticity_validation'
SDK='rules_go++go_sdk+main___download_0_linux_amd64//:builder'
FILTER='//runsc/gofer/fs/filter:filter_precompiled_gen_'
DECLARED=['--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain',
          '--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash,//test/rbe/protoc_check:static_python']
CONTROL='--extra_toolchains=//test/rbe/protoc_check:incompatible_protoc_toolchain'
VERSION_FILE=re.compile(r'bazel-out/[^/]+/bin/external/protobuf[+]/bazel/private/oss/toolchains/prebuilt/validation_output[.]txt')
SDK_FILE=re.compile(r'bazel-out/[^/]+/bin/external/rules_go[+][+]go_sdk[+]main___download_0_linux_amd64/(builder|pack(?:[.]exe)?)')
STUB_FILE=re.compile(r'bazel-out/[^/]+/bin/runsc/gofer/fs/filter/filter_precompiled[.]go[.]gen_stubbed[.]go')
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
    flags = [*DECLARED,'--jobs=4', '--invocation_id='+requested,
             '--build_event_json_file='+str(path.with_suffix('.bep')),
             '--build_event_json_file_path_conversion=false']
    if cmd == 'build':
        flags += STRICT + ['--keep_going',
            '--build_event_publish_all_actions',
            '--execution_log_compact_file='+str(path.with_suffix('.compact'))]
    row['toolchain_flags']=DECLARED
    row['placement_flags'] = STRICT if phase == 'strict' and cmd == 'build' else []
    write(path.with_suffix('.json'), row)
    with path.with_suffix('.out').open('wb') as out, path.with_suffix('.err').open('wb') as err:
        child = subprocess.Popen([os.environ['REAL_BAZEL'], *STARTUP, cmd, *flags, *args], stdout=out, stderr=err)
        seconds=1500 if cmd=='build' else 120
        try:
            child.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            row.update(deadline_exceeded=True,deadline_seconds=seconds)
            write(path.with_suffix('.json'),row)
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    row.update(native_exit=child.returncode, finished_at=now())
    write(path.with_suffix('.json'), row)
    if cmd == 'cquery':
        with path.with_suffix('.out').open('rb') as stream:
            while chunk := stream.read(1024*1024):
                sys.stdout.buffer.write(chunk)
    return 124 if row.get('deadline_exceeded') else child.returncode


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
                     r'cannot open|no usable.*strategy|operation not permitted|protoc version does not match|'
                     r'expected:\s+libprotoc|actual:\s+libprotoc|no input files|failed to initialize build cache|cannot find package|'
                     r'permission denied|read-only file system|cannot create|failed to create')


def summarize(phase, cmd, output_base):
    path = ROOT/(phase+'-'+cmd)
    row = json.loads(path.with_suffix('.json').read_text())
    configured, completed, skipped, failed, configs = [], [], [], [], {}
    attempts = 0
    action_counts=Counter()
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
        identity=key.get('actionCompleted',{})
        if action:
            owner=normalized(identity.get('label',''))
            mnemonic=action.get('type')
            success=bool(action.get('success'))
            action_counts[(mnemonic,success)]+=1
            info={'owner':owner,'mnemonic':mnemonic,'configuration':identity.get('configuration',{}).get('id'),
                  'output':identity.get('primaryOutput'),'success':success,'exit':action.get('exitCode',0)}
            row['last_action']=info
            if owner in (PROTO,SDK,FILTER+'stubbed'):
                row.setdefault('remedy_events',[]).append(info)
            if owner==REPRESENTATIVE and mnemonic=='GoLink':
                row.setdefault('runsc_link_events',[]).append(info)

        if action and not action.get('success'):
            identity=key.get('actionCompleted',{})
            item={'owner':normalized(identity.get('label','')), 'configuration':identity.get('configuration',{}).get('id'),
                  'primary_output':identity.get('primaryOutput'), 'mnemonic':action.get('type'), 'exit':action.get('exitCode')}
            detail=action.get('failureDetail',{})
            item['failure_codes']={k:v.get('code') for k,v in detail.items() if isinstance(v,dict)}
            message=detail.get('message','')
            item['failure_reason']=[diagnostic(l) for l in message.splitlines() if PROBLEM.search(l)][:4]
            for channel in ('stderr','stdout'):
                uri=urlsplit(action.get(channel,{}).get('uri',''))
                match=re.fullmatch(r'/?(?:.*?/)?blobs/([a-f0-9]{64})/([0-9]+)',uri.path)
                if uri.scheme=='bytestream' and match:
                    item[channel]={'sha256':match[1],'bytes':int(match[2]),'disposition':'CAS_IDENTITY_ONLY'}
                elif uri.scheme=='file':
                    item[channel]={'disposition':'LOCAL_FILE_NOT_READ'}
                    p = Path(unquote(uri.path)).resolve()
                    if (channel=='stderr' and uri.netloc in ('','localhost') and (len(failed)<8 or item['owner'] in (PROTO,SDK)) and
                            p.is_relative_to(output_base) and re.fullmatch(r'stderr-[0-9]+',p.name) and
                            p.is_file() and p.stat().st_size<=8*1024**2):
                        data=p.read_bytes()
                        lines=data.decode(errors='replace').splitlines()
                        item[channel]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                                       'diagnostics':[diagnostic(l) for l in lines if PROBLEM.search(l)][:6]}

            failed.append(item)
    row.update(configured=sorted(configured), completed=sorted(completed), aborted=skipped,
               failed_actions=failed, configurations=configs, test_attempts=attempts)
    row['action_counts']=[{'mnemonic':m,'success':ok,'count':n} for (m,ok),n in sorted(action_counts.items())]
    stderr=path.with_suffix('.err')
    if stderr.is_file():
        row['console']={'sha256':digest(stderr),'bytes':stderr.stat().st_size}
        text=stderr.read_text(errors='replace')
        lines=text.splitlines()
        errors = []
        for index, line in enumerate(lines):
            if line.startswith('ERROR:'):
                if not errors:
                    row['first_console_error']=diagnostic(line)
                errors.append(diagnostic(line))
                if len(errors) == 6:
                    break
        row['console_errors'] = errors
        row['error_classes']=[word for word in ('no matching toolchains','no such target','no such package',
            'No space left on device','OUT_OF_MEMORY','Error downloading','undeclared inclusion','not found',
            'Permission denied','undefined reference','missing strict dependencies','Timed out') if word in text]
        row['action_diagnostics'] = [diagnostic(line) for line in lines if PROBLEM.search(line)][:8]
    return row


def compact(path):
    counts=Counter(); violations=[]; invocation=None; compilers=[]
    files, paths = {}, {}
    with subprocess.Popen(['zstd','-qdc',str(path)],stdout=subprocess.PIPE) as child:
        while (size := number(child.stdout)) is not None:
            assert 0 < size < 64*1024*1024, size
            entry=fields(child.stdout.read(size))
            if 2 in entry:
                invocation=first(fields(first(entry,2)),4).decode()
            entry_id=first(entry,1,0)
            if 3 in entry:
                file=fields(first(entry,3))
                name=first(file,1).decode()
                paths[entry_id]=name
                if any(p.fullmatch(name) for p in (RUNSC_OUTPUT,VERSION_FILE,SDK_FILE)):
                    descriptor=fields(first(file,2))
                    files[entry_id]={'path':name,'sha256':first(descriptor,1).decode(),'bytes':first(descriptor,2,0)}
            if 7 not in entry:
                continue
            spawn=fields(first(entry,7)); mnemonic=first(spawn,8).decode(); runner=first(spawn,11).decode()
            cached=bool(first(spawn,12,0)); code=first(spawn,9,0); status=first(spawn,10).decode()
            counts[(mnemonic,runner,cached,code,bool(status))]+=1
            owner=normalized(first(spawn,7).decode())
            if (not cached and runner!='linux-sandbox') or code or status:
                violations.append({'owner':owner,'mnemonic':mnemonic,
                                   'runner':runner,'cached':cached,'exit':code,'status_nonempty':bool(status)})
            argv_digest = hashlib.sha256(b'\0'.join(spawn.get(1, []))).hexdigest()
            if owner in (PROTO,SDK) or (mnemonic=='GoLink' and owner==REPRESENTATIVE) or code or status:
                argv=spawn.get(1,[])
                compiler=argv[0].decode() if argv else ''
                compilers.append({'argv_sha256':argv_digest,'owner':owner,'mnemonic':mnemonic,'runner':runner,'cached':cached,
                                  'exit':code,
                                  'argv0':compiler if re.fullmatch(r'(?:external/|bazel-out/|/bin/|/usr/bin/)[A-Za-z0-9_+./-]+', compiler) and '..' not in compiler.split('/') else None,
                                  'argv0_sha256':hashlib.sha256(compiler.encode()).hexdigest()})
                if owner in (PROTO,SDK):
                    compilers[-1]['wrapper_input']=b'external/rules_go+/go/private/rules/binary_wrapper.sh' in argv
                    compilers[-1]['tool_inputs']=[arg.decode() for arg in argv if
                        re.fullmatch(rb'(?:external/|bazel-out/)[A-Za-z0-9+_./-]+/(?:check_protoc_version.py|protoc)',arg)]
                if code or status:
                    candidates=[]
                    for raw in spawn.get(6,[]):
                        output=fields(raw)
                        name=paths.get(first(output,5,0)) or first(output,4).decode()
                        if re.fullmatch(r'bazel-out/[A-Za-z0-9_+./-]+',name) and '..' not in name.split('/'):
                            candidates.append(name)
                    compilers[-1]['outputs']=candidates
                if owner in (PROTO,SDK) or (owner==REPRESENTATIVE and mnemonic=='GoLink'):
                    compilers[-1]['link_outputs']=[files[i] for o in spawn.get(6,[])
                                                  if (i:=first(fields(o),5,0)) in files]
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


def phase(result, name, labels, env, output_base, execroot):
    row = result['phases'][name] = {'status':'INCOMPLETE','started_at':now(),'requested_labels':labels}
    save(result)
    command = COMMAND[:-1]+['TARGETS='+' '.join(labels),'DOCKER_BUILD=false']
    seconds = 1750
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
    row['deadline_exceeded'] |= any(row.get(cmd,{}).get('deadline_exceeded',False) for cmd in ('build','cquery'))
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
            row['placement'] = compact(compact_path)
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
    assert selected == {(l,'',result['analysis']['runsc_configuration']) for l in labels} and selected == passed and not failed
    assert not build['failed_actions'] and not row['docker_calls'] and not build['aborted']
    assert row['placement']['invocation'] == build['observed_invocation']
    links=[a for a in row['placement']['project_compiler_actions'] if a['mnemonic']=='GoLink' and a['owner']==REPRESENTATIVE]
    link,=links
    assert link['exit']==0 and not link['cached'] and link['runner']=='linux-sandbox'
    linked,=link['link_outputs']
    execpath=linked['path']
    assert RUNSC_OUTPUT.fullmatch(execpath)
    output=(execroot/execpath).resolve(strict=True)
    assert output.is_relative_to(output_base) and output.is_file() and os.access(output,os.X_OK)
    with output.open('rb') as f:
        header=f.read(20)
    row['release_elf']={'path':execpath,'bytes':output.stat().st_size,'sha256':digest(output),
                        'elf64_le':header[:6]==b'\x7fELF\x02\x01','machine':int.from_bytes(header[18:20],'little')}
    save(result)
    assert row['release_elf']['elf64_le'] and row['release_elf']['machine']==62
    assert link['link_outputs']==[{'path':execpath,'bytes':output.stat().st_size,'sha256':row['release_elf']['sha256']}]
    assert any(e['output']==execpath and e['success'] and e['configuration']==result['analysis']['runsc_configuration']
               for e in build['runsc_link_events'])
    assert str(output) in (ROOT/(name+'.out')).read_text().splitlines()
    assert not row['placement']['placement_or_execution_errors']
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
        evidence['elf64_little_endian']=header[:6]==b'\x7fELF\x02\x01'
        if evidence['elf64_little_endian']:
            evidence['machine']=int.from_bytes(header[18:20],'little')
        p = subprocess.run(['readelf','-W','-l','-d',path],capture_output=True,text=True,timeout=30)
        evidence['readelf_exit']=p.returncode
        evidence['needed']=re.findall(r'\(NEEDED\).*?\[([A-Za-z0-9_.+-]+)\]',p.stdout)
        evidence['has_needed']='(NEEDED)' in p.stdout
        evidence['has_interp']=bool(re.search(r'^\s*INTERP\s+',p.stdout,re.M))
        interp = re.search(r'Requesting program interpreter: (/[A-Za-z0-9_./+-]+)\]',p.stdout)
        if interp:
            evidence['elf_interpreter'] = interp[1]
    elif header.startswith(b'#!'):
        interp = re.match(rb'#!\s*(/[A-Za-z0-9_./+-]+)', header)
        if interp:
            evidence['script_interpreter'] = interp[1].decode()
            evidence['env_bash']=header.splitlines()[0]==b'#!/usr/bin/env bash'
    return evidence


def configured_actions(result):
    path=ROOT/'strict-aquery.json'
    err=ROOT/'strict-aquery.err'
    expression='deps('+REPRESENTATIVE+')'
    row=result['analysis']={'expression':expression,'status':'INCOMPLETE'}
    save(result)
    with path.open('wb') as out, err.open('wb') as errors:
        p=subprocess.run(['timeout','-s','INT','-k','20','240',os.environ['REAL_BAZEL'],*STARTUP,
                          'aquery',*STRICT,*DECLARED,'--remote_cache=','--build_tag_filters=-network_plugins',
                          '--include_aspects','--noinclude_commandline','--noinclude_artifacts',
                          '--output=jsonproto',expression],stdout=out,stderr=errors)
    row.update(command_exit=p.returncode,output_bytes=path.stat().st_size,output_sha256=digest(path),
               stderr_sha256=digest(err),errors=[diagnostic(l) for l in err.read_text(errors='replace').splitlines()
                                               if l.startswith('ERROR:')][:4])
    save(result)
    if p.returncode or path.stat().st_size>128*1024**2:
        return
    tree=json.loads(path.read_bytes())
    targets={str(t['id']):normalized(t['label']) for t in tree['targets']}
    configs={str(c['id']):c['checksum'] for c in tree['configuration']}
    classes=Counter()
    constrained={}
    for action in tree['actions']:
        owner=targets[str(action['targetId'])]
        m=action['mnemonic']
        classes[m]+=1
        if any(item['key']=='no-sandbox' for item in action.get('executionInfo',[])):
            constrained.setdefault(m,set()).add(owner)
        if owner in (FILTER+'lib',FILTER+'stubbed',PROTO,SDK) or (m=='GoLink' and owner==REPRESENTATIVE):
            row.setdefault('selected',[]).append({'owner':owner,'mnemonic':m,'key':action.get('actionKey'),
                                                 'configuration':configs[str(action['configurationId'])]})
    row['action_counts']=dict(classes)
    row['no_sandbox_owners']={m:sorted(owners) for m,owners in constrained.items()}
    checksum,={a['configuration'] for a in row['selected'] if a['owner']==REPRESENTATIVE and a['mnemonic']=='GoLink'}
    assert re.fullmatch(r'[a-f0-9]{64}',checksum)
    row['runsc_configuration']=checksum
    row['status']='PASS'
    save(result)


def remedy_observations(result,execroot):
    """Match original owners to their real actions on the corrected source."""
    stage=result['phases']['strict']
    build=stage.get('build',{})
    plan=result['analysis'].get('selected',[])
    events=build.get('remedy_events',[])
    spawns=stage.get('placement',{}).get('project_compiler_actions',[])
    rows=result['remedies']={}
    for owner in (FILTER+'lib',FILTER+'stubbed',PROTO,SDK):
        row=rows[owner]={'status':'UNVERIFIED','events':[e for e in events if e['owner']==owner]}
        row['plan']=[e for e in plan if e['owner']==owner]
        try:
            assert result['analysis']['status']=='PASS'
            if owner==FILTER+'lib':
                assert not row['plan'] and not row['events']
                row['status']='REMOVED_FROM_CONFIGURED_CLOSURE'
                continue
            if owner==FILTER+'stubbed':
                assert {a['mnemonic'] for a in row['plan']}=={'TemplateExpand'}
                row['status']='REPLACED_IN_CONFIGURED_CLOSURE'
                save(result)
                template=Path('pkg/seccomp/precompiledseccomp/load_programs.go.in').read_bytes()
                wanted=template.replace(b'{RULES_IMPORT}',b'').replace(b'{LOAD_PROGRAMS}',b'nil')
                row['expected_sha256']=hashlib.sha256(wanted).hexdigest()
                row['files']=[]
                for f in execroot.glob('bazel-out/*/bin/runsc/gofer/fs/filter/filter_precompiled.go.gen_stubbed.go'):
                    path=f.relative_to(execroot).as_posix()
                    assert STUB_FILE.fullmatch(path) and f.resolve().is_relative_to(execroot.parent.parent)
                    row['files'].append({'path':path,'bytes':f.stat().st_size,'sha256':digest(f)})
                assert any(f['sha256']==row['expected_sha256'] and any(
                    e['success'] and e['mnemonic']=='TemplateExpand' and e['output']==f['path']
                    for e in row['events']) for f in row['files'])
                row['status']='VERIFIED_REPLACEMENT'
                continue
            mnemonic='ProtocAuthenticityCheck' if owner==PROTO else 'GoToolchainBinaryBuild'
            matches=[a for a in spawns if a['owner']==owner and a['mnemonic']==mnemonic]
            row['spawns']=matches
            positive=[a for a in matches if a['exit']==0 and not a['cached'] and a['runner']=='linux-sandbox']
            assert stage['placement']['invocation']==build['observed_invocation']==build['requested_invocation']
            for a in positive:
                outputs=a['link_outputs']
                if owner==PROTO:
                    expected=b'libprotoc 35.1\n'
                    assert a['argv0'].endswith('protoc_check_static_python/bin/python3.11')
                    assert any(t.endswith('/check_protoc_version.py') for t in a['tool_inputs'])
                    output,=outputs
                    assert VERSION_FILE.fullmatch(output['path'])
                    assert output['sha256']==hashlib.sha256(expected).hexdigest() and output['bytes']==len(expected)
                else:
                    assert a['argv0'].endswith('/bin/test/rbe/go_bootstrap/ash') and a['wrapper_input']
                    assert len(outputs)==2 and {Path(o['path']).name for o in outputs}=={'builder','pack.exe'}
                for o in outputs:
                    f=(execroot/o['path']).resolve(strict=True)
                    assert f.is_relative_to(execroot.parent.parent) and f.is_file() and o['bytes']>0
                    assert f.stat().st_size==o['bytes'] and digest(f)==o['sha256']
                assert any(e['success'] and e['mnemonic']==mnemonic and e['output'] in
                           {o['path'] for o in outputs} for e in row['events'])
                row['status']='VERIFIED_EXECUTION'
                break
        except (AssertionError,AttributeError,KeyError,OSError,ValueError) as exc:
            row['inspection_error']=failure(exc)
        finally:
            save(result)


def mismatch_control(result,output_base):
    """Use the original released 36.2 compiler and require its exact version rejection."""
    label='@com_google_protobuf//bazel/private/oss/toolchains/prebuilt:authenticity_validation'
    name='wrong-protoc'
    path=ROOT/(name+'-build')
    requested=str(uuid.uuid4())
    row=result['wrong_protoc']={'status':'INCOMPLETE','owner':PROTO,'expected':'35.1','selected':'36.2'}
    save(result)
    flags=[*STRICT,*DECLARED,CONTROL,'--jobs=4','--remote_cache=','--color=no','--curses=no',
           '--output_groups=_validation','--build_event_publish_all_actions','--invocation_id='+requested,
           '--build_event_json_file='+str(path.with_suffix('.bep')),'--build_event_json_file_path_conversion=false',
           '--execution_log_compact_file='+str(path.with_suffix('.compact'))]
    with path.with_suffix('.out').open('wb') as out,path.with_suffix('.err').open('wb') as err:
        p=subprocess.run(['timeout','-s','INT','-k','20','180',os.environ['REAL_BAZEL'],*STARTUP,'build',
                          *flags,label],stdout=out,stderr=err)
    row['native_exit']=p.returncode
    write(path.with_suffix('.json'),{'native_exit':p.returncode,'requested_invocation':requested})
    save(result)
    b=row['build']=summarize(name,'build',output_base)
    save(result)
    row['placement']=compact(path.with_suffix('.compact'))
    save(result)
    assert row['native_exit']==1 and b['bazel_exit']['name']=='BUILD_FAILURE' and b['test_attempts']==0
    assert b['observed_invocation']==requested==row['placement']['invocation']
    event,=[e for e in b['failed_actions'] if e['owner']==PROTO and e['mnemonic']=='ProtocAuthenticityCheck']
    a,=[a for a in row['placement']['project_compiler_actions'] if a['owner']==PROTO and a['mnemonic']==event['mnemonic']]
    assert a['exit']==event['exit']==1 and not a['cached'] and a['runner']=='linux-sandbox'
    assert event['primary_output'] in a['outputs'] and a['argv0'].endswith('protoc_check_static_python/bin/python3.11')
    assert any(t.endswith('protoc_check_incompatible/bin/protoc') for t in a['tool_inputs'])
    messages={d['text'].strip() for d in event['stderr']['diagnostics']}
    assert 'Expected: libprotoc 35.1' in messages and 'Actual:   libprotoc 36.2' in messages
    assert any(m.startswith('ERROR: protoc version does not match') for m in messages)
    row['status']='VERIFIED_EXPECTED_FAILURE'


def main(result):
    assert platform.system()=='Linux' and platform.machine()=='x86_64'
    assert platform.freedesktop_os_release()['VERSION_ID']=='22.04'
    assert git('rev-parse','HEAD')==SOURCE and git('rev-parse','HEAD^{tree}')=='3431cd7a8ceb8fed3f5e328bef61bb17a9051b36'
    assert git('rev-parse','HEAD^')==ORIGINAL
    assert git('rev-parse','HEAD:runsc/BUILD')=='2c8aed4b00cfc02151f4a68a558bd1d4c7afc507'
    assert git('rev-parse','HEAD:tools/release.bzl')=='849d7f5eaa1fe48312179063ffd2c49288396041'
    assert SUPPLIER in Path('MODULE.bazel').read_text()
    assert not git('status','--porcelain','--untracked-files=no') and not Path('tools/google.mk').exists()
    assert subprocess.check_output([os.environ['REAL_BAZEL'],'--version'],text=True).strip()=='bazel 9.3.1'
    step, = [s for s in yaml.safe_load(Path('.buildkite/pipeline.yaml').read_text())['steps']
             if isinstance(s,dict) and s.get('label')==':world_map: Build everything']
    assert shlex.split(step['commands'][0]) == COMMAND and step['agents']=={'queue':'default'}
    assert step['if']=='build.env("STAGED_BINARIES") == null && build.branch == "master"'
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
    assert namespace_control(result)
    try:
        configured_actions(result)
    except Exception as exc:
        result['analysis_read_error']=failure(exc)
        save(result)
    try:
        phase(result,'strict',[REPRESENTATIVE],env,output_base,execroot)
    except Exception as exc:
        result['strict_read_error']=failure(exc)
    strict=result['phases'].get('strict',{})
    placement=strict.get('placement',{})
    build=strict.get('build',{})
    actions=placement.get('project_compiler_actions',[])
    result['consumer_status']=strict.get('status','INCOMPLETE')
    completed=[ok for l,a,c,ok in build.get('completed',[])
               if l==REPRESENTATIVE and not a and c==result['analysis'].get('runsc_configuration')]
    result['target_completion']=('PASS' if completed==[True] else 'FAIL' if completed==[False] else 'UNACCOUNTED')
    failures=[a for a in actions if a['exit'] and not a['cached'] and a['runner']=='linux-sandbox']
    for action in failures[:8]:
        try:
            action['executable']=executable_evidence(action,execroot)
        except (OSError, subprocess.SubprocessError) as exc:
            action['executable_inspection_error']=type(exc).__name__
    save(result)
    result['status']=strict.get('status','INCOMPLETE')
    save(result)
    remedy_observations(result,execroot)
    save(result)
    assert build['observed_invocation']==build['requested_invocation']
    assert build['toolchain_flags']==DECLARED and build['placement_flags']==STRICT and build['test_attempts']==0
    assert build['arguments']==['--color=no','--curses=no','--remote_cache=',META,'--remote_download_outputs=toplevel',
                                '--build_tag_filters=-network_plugins','--',REPRESENTATIVE]
    assert not strict['docker_calls'] and any(l==REPRESENTATIVE and not a and c==result['analysis']['runsc_configuration']
                                              for l,a,k,t,c in build['configured'])
    if strict['deadline_exceeded']:
        return
    assert result['analysis']['status']=='PASS'
    # A non-sandboxable action can fail strategy selection before any spawn is logged.
    strategy_errors=[]
    for event in build.get('failed_actions',[]):
        owners=result['analysis']['no_sandbox_owners'].get(event['mnemonic'],[])
        if event['owner'] in owners and any(re.search(r'no usable.*strategy',d['text'],re.I)
                                             for d in event['failure_reason']):
            strategy_errors.append({'owner':event['owner'],'mnemonic':event['mnemonic'],
                                    'output':event['primary_output'],'configuration':event['configuration']})
    result['unschedulable_no_sandbox_actions']=strategy_errors
    save(result)
    if not strategy_errors:
        assert placement.get('invocation')==build['observed_invocation']
    if strict['status']=='PASS':
        result['qualification_complete']=True
        result['status']='PASS'
        return
    result['status']=strict.get('status','INCOMPLETE')
    # Report a separate failed producer only when its own fresh execution and
    # action stderr can be joined. Keep both the build and the target red.
    joined=[]
    for action in failures:
        for event in build.get('failed_actions',[]):
            if (event['owner'],event['mnemonic'],event['exit']) != (action['owner'],action['mnemonic'],action['exit']):
                continue
            if (event.get('stderr',{}).get('diagnostics') and
                    event['primary_output'] in action.get('outputs',[])):
                joined.append({'owner':event['owner'],'mnemonic':event['mnemonic'],'output':event['primary_output'],
                               'action_stderr_sha256':event['stderr']['sha256'],'argv_sha256':action['argv_sha256']})
                break
    result['next_action_failures']=joined[:8]
    result['qualification_complete']=bool((joined or strategy_errors) and strict['status']=='FAIL' and
        result['target_completion']=='FAIL' and build.get('native_exit') and
        build.get('bazel_exit',{}).get('code') and build['bazel_exit'].get('name')!='SUCCESS')


if __name__=='__main__':
    if Path(sys.argv[0]).name in ('docker','bazel'):
        raise SystemExit(wrapped())
    result={'status':'INCOMPLETE','source':SOURCE,'phases':{},'test_execution_claimed':False,
            'qualification_complete':False,'full_graph_status':'INCOMPLETE','stage':'bootstrap',
            'prior_runsc':{'source':ORIGINAL,'artifact':13386521630,'configuration':ORIGINAL_CONFIG,
                           'sha256':'d6de5455f410a4a6cc542cc06eb1210320e5e32d658ee9a59188bf5a62e7c39e'},
            'prior_cpp_check':{'source':ORIGINAL,'artifact':13386070096,
                               'sha256':'13cbc28aef5e6c3a71640b54b3fde0fc69eda21d749d15c8170ba377cc9ce28d'},
            'prior_whole_graph':{'artifact':13355668355,'runsc_target_completion':'UNACCOUNTED'},
            'supplier':SUPPLIER,'remote_cache':False}
    try:
        main(result)
    except Exception as exc:
        result['error']=failure(exc)
    if result.get('namespace_control',{}).get('verified') and result['phases'].get('strict'):
        try:
            mismatch_control(result,bazel_info('output_base'))
        except Exception as exc:
            result['control_error']=failure(exc)
    result['build_diagnosis_complete']=result['qualification_complete']
    result['qualification_complete']=(result['build_diagnosis_complete'] and
        result.get('wrong_protoc',{}).get('status')=='VERIFIED_EXPECTED_FAILURE' and
        all(result.get('remedies',{}).get(owner,{}).get('status')==expected for owner,expected in (
            (FILTER+'lib','REMOVED_FROM_CONFIGURED_CLOSURE'),(FILTER+'stubbed','VERIFIED_REPLACEMENT'),
            (PROTO,'VERIFIED_EXECUTION'),(SDK,'VERIFIED_EXECUTION'))))
    result['verified_at']=now()
    save(result)
    raise SystemExit(int(not result['qualification_complete']))
