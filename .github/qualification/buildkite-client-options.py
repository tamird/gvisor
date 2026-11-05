#!/usr/bin/env python3
"""Build and boot the installed nine-file release from the strict Linux sandbox."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import traceback
import uuid
from urllib.parse import unquote, urlsplit

from stdlib_provenance import fields, first, number

SOURCE = '2e7b3cdc26d857428a8a7f5b67bc020a64fb6f56'
PARENT = '82a843a3470a1d779cc800208e720c1c3c97ca54'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'continuous-build'
STAGE = '//:release'
LINKS = {
    '//runsc:runsc': 'runsc',
    '//shim:containerd-shim-runsc-v1': 'containerd-shim-runsc-v1',
    '//runsc/cmd/sentry:gvisor_sentry': 'gvisor-bin/gvisor_sentry',
    '//runsc/checkpointgofer:checkpointgofer_binary': 'gvisor-bin/checkpointgofer',
    '//runsc/cmd/netgofer:runsc-netgofer': 'gvisor-bin/runsc-netgofer',
    '//runsc/cmd/metricserver:runsc-metric-server': 'gvisor-bin/runsc-metric-server',
}
SIDE_LINKS = {
    '//runsc/prewarmer:_linked/gvisor-sentry-prewarmer': 'gvisor-bin/gvisor-sentry-prewarmer',
    '//runsc/fdparking:_linked/runsc-fd-parking': 'gvisor-bin/runsc-fd-parking',
    '//runsc/procumounter:_linked/gvisor-proc-umounter': 'gvisor-bin/gvisor-proc-umounter',
}
STUB = '//pkg/sentry/platform/systrap/sysmsg:'
STUB_DATA = STUB + 'sighandler_data'
EMBED_LINKS = {'//vdso:vdso.so', STUB + 'sighandler.built-in.bin.o'}
EMBED_OUTPUT = re.compile(r'bazel-out/[A-Za-z0-9_+-]+/bin/(?:vdso/vdso\.so|pkg/sentry/platform/systrap/sysmsg/sighandler(?:\.built-in\.bin(?:\.o)?|\.go\.arch))')
MEMBERS = set(LINKS.values()) | set(SIDE_LINKS.values())
RELEASE_FILE = re.compile(r'bazel-out/[a-zA-Z0-9_+-]+/bin/release/(?:' + '|'.join(map(re.escape, sorted(MEMBERS))) + ')')
LINK_OUTPUT = re.compile(r'bazel-out/[A-Za-z0-9_+-]+/bin/(?:runsc|shim)/[A-Za-z0-9_./+-]+')
META = '--build_metadata=CONTINUOUS_CHECK=Native release'
STRICT = ['--experimental_use_hermetic_linux_sandbox', '--spawn_strategy=linux-sandbox',
          '--strategy=GoLink=linux-sandbox', '--sandbox_default_allow_network=false', '--sandbox_debug']
DECLARED = ['--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain',
            '--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash,//test/rbe/protoc_check:static_python']
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
        seconds=3600 if cmd=='build' else 180
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
                     r'cannot open|no usable.*strategy|cannot be executed with any of the available strategies|'
                     r'operation not permitted|protoc version does not match|unable to (?:start|get|create|find|load)|'
                     r'missing import|subcommand failed|unresolved|not defined|build constraints exclude|'
                     r'expected:\s+libprotoc|actual:\s+libprotoc|no input files|failed to initialize build cache|cannot find package|'
                     r'permission denied|read-only file system|cannot create|failed to create|no import for|'
                     r'invalid state conversion type|generated code uses|failed to (?:format|parse|infer)|'
                     r'missing strict dependencies|undeclared inclusion|undefined:|cannot use|not declared by')


def release_action(owner, mnemonic):
    return ((owner == STAGE and mnemonic == 'CopyFile') or
            (owner in LINKS and mnemonic == 'GoLink') or
            ((owner in SIDE_LINKS or owner in EMBED_LINKS) and mnemonic == 'CppLink') or
            owner == STUB_DATA)


def summarize(phase, cmd, output_base):
    path = ROOT/(phase+'-'+cmd)
    row = json.loads(path.with_suffix('.json').read_text())
    configured, completed, skipped, failed, configs = [], [], [], [], {}
    attempts = 0
    action_counts=Counter()
    diagnosed_mnemonics=set()
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
            if release_action(owner, mnemonic):
                row.setdefault('release_actions', []).append(info)

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
                    if (channel=='stderr' and uri.netloc in ('','localhost') and (len(failed)<8 or (item['mnemonic'] not in diagnosed_mnemonics and len(diagnosed_mnemonics)<16) or (item['owner'] in LINKS or item['owner'].startswith(('//vdso:', '//runsc/', '//pkg/sentry/platform/systrap/sysmsg:')))) and
                            p.is_relative_to(output_base) and re.fullmatch(r'stderr-[0-9]+',p.name) and
                            p.is_file() and p.stat().st_size<=8*1024**2):
                        diagnosed_mnemonics.add(item['mnemonic'])
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
                if RELEASE_FILE.fullmatch(name) or LINK_OUTPUT.fullmatch(name) or EMBED_OUTPUT.fullmatch(name):
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
            if release_action(owner, mnemonic) or code or status:
                argv=spawn.get(1,[])
                compiler=argv[0].decode() if argv else ''
                compilers.append({'argv_sha256':argv_digest,'owner':owner,'mnemonic':mnemonic,'runner':runner,'cached':cached,
                                  'exit':code,
                                  'argv0':compiler if re.fullmatch(r'(?:external/|bazel-out/|/bin/|/usr/bin/)[A-Za-z0-9_+./-]+', compiler) and '..' not in compiler.split('/') else None,
                                  'argv0_sha256':hashlib.sha256(compiler.encode()).hexdigest()})
                if code or status:
                    candidates=[]
                    for raw in spawn.get(6,[]):
                        output=fields(raw)
                        name=paths.get(first(output,5,0)) or first(output,4).decode()
                        if re.fullmatch(r'bazel-out/[A-Za-z0-9_+./-]+',name) and '..' not in name.split('/'):
                            candidates.append(name)
                    compilers[-1]['outputs']=candidates
                compilers[-1]['link_outputs'] = [files[i] for o in spawn.get(6, [])
                                                   if (i := first(fields(o),5,0)) in files]
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
    stage = ROOT/'install'
    stage.mkdir()
    command = ['make', 'copy', 'TARGETS='+STAGE, 'DESTINATION='+str(stage), 'DOCKER_BUILD=false']
    row['entrypoint'] = ['make','copy','TARGETS='+STAGE,'DESTINATION=<empty-installation>','DOCKER_BUILD=false']
    seconds = 3950
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
    assert build['arguments'] == args+['--remote_download_outputs=toplevel','--',STAGE]
    assert row['cquery']['arguments'] == args+['--output=starlark','--starlark:file=tools/show_paths.bzl',
                                              '--','set() union "'+STAGE+'"']
    assert len(selected) == 1 and selected == passed and not failed and selected.pop()[0] == STAGE
    assert not build['failed_actions'] and not row['docker_calls'] and not build['aborted']
    assert row['placement']['invocation'] == build['observed_invocation']
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


def inspect_elf(path):
    mode = path.lstat().st_mode
    assert stat.S_ISREG(mode) and mode & 0o111 == 0o111, path.name
    with path.open('rb') as f:
        head = f.read(64)
        assert head[:6] == b'\x7fELF\x02\x01'
        entry, offset = struct.unpack_from('<QQ', head, 24)
        size, count = struct.unpack_from('<HH', head, 54)
        assert size >= 56 and 0 < count < 512 and offset + size*count <= path.stat().st_size
        f.seek(offset)
        types = [struct.unpack_from('<I', f.read(size))[0] for _ in range(count)]
    return {'sha256':digest(path), 'bytes':path.stat().st_size, 'mode':stat.S_IMODE(mode),
            'machine':struct.unpack_from('<H', head, 18)[0], 'entry':entry, 'load':1 in types, 'interp':3 in types}


def verify_install(result, execroot):
    stage = ROOT/'install'
    row = result['layout'] = {'status':'INCOMPLETE','members':{}}
    entries = {str(p.relative_to(stage)): p for p in stage.rglob('*')}
    assert set(entries) == MEMBERS | {'gvisor-bin'} and stat.S_ISDIR(entries['gvisor-bin'].lstat().st_mode)
    phase = result['phases']['strict']
    observed = phase['placement']['project_compiler_actions']
    events = phase['build']['release_actions']
    pairs = [line.split() for line in (ROOT/'strict-cquery.out').read_text().splitlines() if line.strip()]
    assert len(pairs)==len(MEMBERS) and {name for _,name in pairs}==MEMBERS
    for rel, name in pairs:
        assert RELEASE_FILE.fullmatch(rel) and rel.endswith('/bin/release/'+name)
        path = execroot/rel
        assert path.resolve(strict=True).is_relative_to(execroot.parent.parent)
        copy = inspect_elf(entries[name])
        row['members'][name] = copy
        assert copy == inspect_elf(path) and copy['machine']==62 and copy['entry'] and copy['load']
        producers = [(a,o) for a in observed for o in a['link_outputs']
                     if o['sha256']==copy['sha256'] and o['bytes']==copy['bytes'] and
                     a['runner']=='linux-sandbox' and not a['cached'] and not a['exit']]
        copied, = [(a,o) for a,o in producers if a['owner']==STAGE and a['mnemonic']=='CopyFile' and o['path']==rel]
        assert any(e['success'] and e['output']==rel and e['owner']==STAGE and e['mnemonic']=='CopyFile' for e in events)
        copy['stage_action'] = copied[0]['argv_sha256']
        owners, mnemonic = (LINKS,'GoLink') if name in LINKS.values() else (SIDE_LINKS,'CppLink')
        if name in SIDE_LINKS.values():
            dynamic = subprocess.run(['readelf','-dW',entries[name]], capture_output=True, timeout=15)
            copy['needed_library'] = b'(NEEDED)' in dynamic.stdout
            save(result)
            assert dynamic.returncode == 0 and not copy['needed_library'] and not copy['interp']
            if name != 'gvisor-bin/runsc-fd-parking':
                assert copy['bytes'] <= 4096
        matches = [(a,o) for a,o in producers if owners.get(a['owner'])==name and a['mnemonic']==mnemonic
                       and any(e['success'] and e['owner']==a['owner'] and e['mnemonic']==mnemonic
                               and e['output']==o['path'] for e in events)]
        assert matches, name
        copy['link_owner'] = matches[0][0]['owner']
        copy['link_output'] = matches[0][1]['path']
        save(result)
    version = subprocess.run([stage/'runsc','--version'],capture_output=True,timeout=15)
    row['version'] = {'exit':version.returncode,'sha256':hashlib.sha256(version.stdout).hexdigest(),
                      'runsc_line':bool(re.search(rb'^runsc version [^\n]+$',version.stdout,re.M)),
                      'spec_line':bool(re.search(rb'^spec: [0-9][^\n]*$',version.stdout,re.M))}
    save(result)
    assert version.returncode==0 and row['version']['runsc_line'] and row['version']['spec_line']
    row['status']='PASS'
    save(result)


def smoke(result, execroot):
    # Use the public static BusyBox extracted by the existing source-pinned
    # Go bootstrap repo as the only guest file. No host mounts or guest network.
    row = result['smoke'] = {'status':'INCOMPLETE','platform':'systrap','network':'none'}
    output_base = execroot.parent.parent
    payload, = (output_base/'external').glob('*+bootstrap_busybox/payload/usr/bin/busybox')
    assert payload.resolve(strict=True).is_relative_to(output_base/'external')
    assert os.getuid() != 0
    capsule = ROOT/'smoke-root'
    work = capsule/'bundle'
    root = work/'rootfs'
    root.mkdir(parents=True)
    for name in ('proc','sys','dev','tmp','run'):
        (capsule/name).mkdir()
    guest = root/'busybox'
    shutil.copyfile(payload,guest)
    guest.chmod(0o755)
    (capsule/'busybox').hardlink_to(guest)
    shutil.copytree(ROOT/'install', capsule/'release', copy_function=os.link)
    row['guest'] = inspect_elf(guest)
    assert row['guest']['machine']==62 and not row['guest']['interp'] and row['guest']['entry']
    row['installed_inodes_match'] = all((capsule/'release'/n).samefile(ROOT/'install'/n) for n in MEMBERS)
    assert row['installed_inodes_match']
    marker = 'release-running-under-systrap'
    (work/'config.json').write_text(json.dumps({
        'ociVersion':'1.0.2','root':{'path':'rootfs','readonly':True},
        'process':{'args':['/busybox','echo',marker],'cwd':'/','user':{'uid':0,'gid':0},
                   'env':['PATH=/'],'noNewPrivileges':True},
        'linux':{'namespaces':[{'type':n} for n in ('pid','network','ipc','uts','mount')]}}))
    # Only the host's namespace setup tools touch the old root. After pivoting
    # and detaching it, the CLI can see only the release, BusyBox, fresh proc
    # and sysfs, and the explicit character devices below. Root maps solely
    # to the ordinary coordinator UID; neither sudo nor a daemon is involved.
    setup = r'''set -eu
root=$1
test "$(id -u)" = 0
test "$(awk 'NR==1{print $2,$3}' /proc/self/uid_map)" = "$2 1"
test "$(readlink /proc/self/ns/user)" != "$3"
test "$(readlink /proc/self/ns/net)" != "$4"
test "$(readlink /proc/self/ns/pid)" != "$5"
mount --bind "$root" "$root"
mount -t proc proc "$root/proc"
mount -t sysfs -o ro sysfs "$root/sys"
for d in null zero full random urandom; do
    touch "$root/dev/$d"
    mount --bind "/dev/$d" "$root/dev/$d"
done
ln -s /proc/self/fd "$root/dev/fd"
ln -s /proc/self/fd/2 "$root/dev/stderr"
cd "$root"
pivot_root . .
exec /busybox sh -ec '
  /busybox umount -l .
  cd /
  test "$(/busybox id -u)" = 0
  test ! -e /var/run/docker.sock
  echo RELEASE_PRIVATE_ROOT >&2
  exec /release/runsc --rootless --root=/run/state --platform=systrap --network=none --ignore-cgroups=true --debug-log=/dev/stderr run --bundle=/bundle release-smoke
'
'''
    names = [os.readlink('/proc/self/ns/'+ns) for ns in ('user','net','pid')]
    save(result)
    command = ['/usr/bin/setpriv','--no-new-privs','/usr/bin/unshare','--user','--map-root-user',
               '--mount','--net','--pid','--fork','--kill-child','--ipc','--uts','--propagation','private',
               '/bin/sh','-ec',setup,'release',str(capsule),str(os.getuid()),*names]
    try:
        p = subprocess.run(command,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'},
                           cwd=ROOT, stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
        stdout, stderr, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, code = exc.stdout or b'', exc.stderr or b'', None
        row['timeout_seconds'] = 120
    row['private_root_entered'] = b'RELEASE_PRIVATE_ROOT' in stderr.splitlines()
    row['sentry_stdio_witness'] = bool(re.search(rb'Remapped stdio FDs to \[\d+, \d+\] in [^\s]+',stderr))
    row.update(exit=code, stdout_sha256=hashlib.sha256(stdout).hexdigest(),
               stderr_sha256=hashlib.sha256(stderr).hexdigest(), marker=stdout.splitlines()==[marker.encode()],
               diagnostics=[diagnostic(l) for l in stderr.decode(errors='replace').splitlines() if PROBLEM.search(l)][:6])
    save(result)
    assert code==0 and row['marker'] and row['private_root_entered'] and row['sentry_stdio_witness']
    row['status']='PASS'


def supplier_evidence(result, execroot):
    # Compare the actual patched repositories, including the e2e BUILD whose
    # unchanged context is older than the proposed supplier branch.
    expected = {
        'toolchain/runtimes/BUILD.bazel': 'd00146aff7a03491319a465a8e7ebd5082550ceb84c2f0e19d1c5548e1500b75',
        'toolchain/runtimes/cc_runtime_binary.bzl': 'bab38b8e49e0e210987c2ba29bf0dac6af848442b966f4674cb743485c463fad',
        'tools/case_insensitive_vfs/BUILD.bazel': '1ce20f9163c914cd3f2e34fe23852817fcdc63477b62bbf7ac64621d2931c34d',
        'tools/def_file_generator/BUILD.bazel': 'a957cc669867590fef9cb168ed35b48f00e949b517b98d593de62259f42ad72b',
        'tools/internal/BUILD.bazel': '8619752bfda0aef48af74cbee24d40ace3d9e4faa55e307ee1d492770eaa5cc8',
        'e2e/rules_cc/BUILD.bazel': '882c6025cc80dfa76812060e3b92396da64f1f9219f3ace1fe326b1ac9fdc4a3',
        'e2e/rules_cc/freestanding_x86_64.S': '58917139dff2a71003ae96ffc26764b98eee1bef806f989d4f9ebe611bd99039',
        'toolchain/args/linux/BUILD.bazel': 'a863495ff7f38e0052a7bfcfbf9828757953bcf973b0d74a2e0263858a1a88b3',
    }
    row = result['supplier'] = {'files':{}, 'vdso_links':[], 'status':'INCOMPLETE'}
    root = (execroot.parent.parent/'external/llvm+').resolve(strict=True)
    for name, wanted in expected.items():
        path = (root/name).resolve(strict=True)
        assert path.is_relative_to(root) and path.is_file() and path.stat().st_size < 1024**2
        row['files'][name] = digest(path)
    assert row['files'] == expected
    phase = result['phases']['strict']
    build = phase['build']
    for action in phase['placement']['project_compiler_actions']:
        if action['owner'] != '//vdso:vdso.so' or action['mnemonic'] != 'CppLink':
            continue
        if action['cached'] or action['runner'] != 'linux-sandbox' or action['exit']:
            continue
        for output in action['link_outputs']:
            path = output['path']
            if not path.endswith('/bin/vdso/vdso.so') or not EMBED_OUTPUT.fullmatch(path):
                continue
            event, = [e for e in build['release_actions'] if e['success'] and
                      (e['owner'],e['mnemonic'],e['output'])==(action['owner'],action['mnemonic'],path)]
            helper = executable_evidence(action, execroot)
            image = (execroot/path).resolve(strict=True)
            assert image.is_relative_to(execroot.parent.parent) and image.is_file()
            with image.open('rb') as header:
                ident = header.read(20)
            elf = {'sha256':digest(image), 'bytes':image.stat().st_size, 'machine':int.from_bytes(ident[18:20],'little')}
            row['vdso_links'].append({'cpu':build['configurations'][event['configuration']],
                                      'output':output, 'elf':elf, 'exec':action['argv0'], 'helper':helper})
            assert ident[:6]==b'\x7fELF\x02\x01' and ident[16:18]==b'\x03\x00'
            assert (elf['sha256'],elf['bytes'])==(output['sha256'],output['bytes'])
            assert helper['elf64_little_endian'] and helper['readelf_exit']==0 and helper['machine']==62
            assert not helper['has_interp'] and not helper['has_needed']
    assert sorted((x['cpu'],x['elf']['machine']) for x in row['vdso_links'])==[('aarch64',183),('k8',62)]
    row['status']='PASS'


def main(result):
    assert platform.system()=='Linux' and platform.machine()=='x86_64'
    assert platform.freedesktop_os_release()['VERSION_ID']=='22.04'
    assert git('rev-parse','HEAD')==SOURCE and git('rev-parse','HEAD^')==PARENT
    assert git('rev-parse','HEAD^{tree}')=='96e8d0fc55635b75be30899714d9fbb4887aa5dc'
    assert git('rev-parse','HEAD:BUILD')=='358d2249e69ebfdfdfa6234dc4e7520c1c0fdf7a'
    assert git('rev-parse','HEAD:tools/release.bzl')=='849d7f5eaa1fe48312179063ffd2c49288396041'
    assert not git('status','--porcelain','--untracked-files=no') and not Path('tools/google.mk').exists()
    assert 'make copy TARGETS=//:release DESTINATION=bin/' in Path('.github/workflows/build.yml').read_text()
    assert subprocess.check_output([os.environ['REAL_BAZEL'],'--version'],text=True).strip()=='bazel 9.3.1'
    free=shutil.disk_usage(ROOT).free
    result['disk_free_bytes']=free
    assert free >= 30*1024**3
    bindir=ROOT/'bin'
    bindir.mkdir()
    for tool in ('bazel','docker'):
        (bindir/tool).symlink_to(Path(__file__).resolve())
    env=os.environ|{'PATH':str(bindir)+':'+os.environ['PATH'],'BAZEL_OPTIONS':shlex.quote(META),
                    'BAZEL_REMOTE_CACHE':'--remote_cache='}
    output_base=bazel_info('output_base')
    execroot=bazel_info('execution_root',must_exist=False)
    assert output_base.is_dir() and execroot.parent==output_base/'execroot'
    assert namespace_control(result)
    save(result)
    try:
        phase(result, 'strict', [STAGE], env, output_base, execroot)
    except Exception as exc:
        result['phase_inspection_error']=failure(exc)
    strict=result['phases']['strict']
    build=strict.get('build',{})
    result['disk_free_after_build_bytes']=shutil.disk_usage(ROOT).free
    result['build_status']=strict['status']
    result['status']=strict['status'] if strict['status']!='PASS' else 'INCOMPLETE'
    results=[ok for l,a,_,ok in build.get('completed',[]) if l==STAGE and not a]
    result['owner_completion']='PASS' if results==[True] else 'FAIL' if results==[False] else 'UNACCOUNTED'
    actions = strict.get('placement',{}).get('project_compiler_actions',[])
    for item in build.get('failed_actions',[]):
        matches = [a for a in actions if a['exit'] and not a['cached'] and a['runner']=='linux-sandbox' and
                   (a['owner'],a['mnemonic'],a['exit'])==(item['owner'],item['mnemonic'],item['exit']) and
                   item['primary_output'] in a.get('outputs',[])]
        if len(matches)==1:
            a=matches[0]
            item['matched_spawn']={'argv0':a['argv0'],'argv_sha256':a['argv_sha256']}
            if 'executable' not in a:
                try:
                    a['executable']=executable_evidence(a,execroot)
                except (OSError,subprocess.SubprocessError) as exc:
                    a['inspection_error']=failure(exc)
    save(result)
    try:
        supplier_evidence(result,execroot)
    except (OSError,AssertionError,KeyError,ValueError,subprocess.SubprocessError) as exc:
        result['supplier_inspection_error']=failure(exc)
    save(result)
    if strict['status']!='PASS':
        return
    assert result['owner_completion']=='PASS'
    assert build['toolchain_flags']==DECLARED and build['placement_flags']==STRICT
    verify_install(result,execroot)
    smoke(result,execroot)
    assert result['supplier']['status']=='PASS'
    result['status']='PASS'


if __name__=='__main__':
    if Path(sys.argv[0]).name in ('docker','bazel'):
        raise SystemExit(wrapped())
    result={'status':'INCOMPLETE','source':SOURCE,'owner':STAGE,'phases':{},'remote_execution':False,'remote_cache':False,
            'previous_release':{'run':41970371311,'artifact':13389881117,'source':'1668d83dcd713c54dacd019869318254122cb283','matched_bash_enoent':13,
                                'sha256':'b68794e146a2de0828ebcd14dbbb38d67171270b74904579d4afa1c816fb2463'},
            'previous_links':{'run':41971712161,'artifact':13390018761,'source':PARENT,
                              'sha256':'9c0695e32dbeaff530591ac95f0204c42ca92a47e222692f41b95d27e0f6fd74'},
            'previous_cli_only':{'artifact':13388882421,'sha256':'81ebf5776ac4c864d9ae81248a4733e14a5c4616eedf38b7a726457089be80f5'}}
    try:
        main(result)
    except Exception as exc:
        result['error']=failure(exc)
    result['verified_at']=now()
    save(result)
    raise SystemExit(int(result['status']!='PASS'))
