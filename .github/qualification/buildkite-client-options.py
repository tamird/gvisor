#!/usr/bin/env python3
"""Build the strict proc_net_test consumer on a native ARM64 host."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
from io import BytesIO
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

SOURCE = '9636c032922a4ead41a7216453240849a9c1fedd'
ORIGINAL = '9e6e8fa9c55583b3505646d32713e991231ceb47'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'continuous-build'
COMMAND = ['make', 'build', 'OPTIONS=--build_tag_filters=-network_plugins', 'TARGETS=//...']
META = '--build_metadata=CONTINUOUS_CHECK=Native whole graph'
REPRESENTATIVE = '//test/syscalls/linux:proc_net_test'
MAP = 'llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-arm64//:module_map'
MAP_OUTPUT = 'bazel-out/aarch64-fastbuild/bin/external/' + MAP.replace('//:', '/') + '.modulemap'
SUPPLIER = 'eb198127bdc4f7b737d08a7ea009869bc7a72e75'
ARM_OPTIONS = ['--build_tag_filters=-network_plugins', '--config=aarch64']
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
        flags += STRICT + ['--keep_going',
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
        identity=key.get('actionCompleted',{})
        if action and normalized(identity.get('label','')) == MAP and action.get('type') == 'CppModuleMap':
            row.setdefault('module_map_events', []).append({
                'configuration':identity.get('configuration',{}).get('id'),
                'output':identity.get('primaryOutput'), 'success':bool(action.get('success')),
                'exit':action.get('exitCode',0)})

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


def repeated_ids(record, field):
    for value in record.get(field, []):
        if isinstance(value, int):
            yield value
        else:
            stream = BytesIO(value)
            while (item := number(stream)) is not None:
                yield item


def compact(path):
    counts=Counter(); violations=[]; invocation=None; compilers=[]
    files, paths, input_sets = {}, {}, {}
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
                if 'module-map-generator' in name or name==MAP_OUTPUT:
                    descriptor=fields(first(file,2))
                    files[entry_id]={'path':name,'sha256':first(descriptor,1).decode(),'bytes':first(descriptor,2,0)}
            if 6 in entry:
                members=fields(first(entry,6))
                ids={i for i in repeated_ids(members,5) if i in files}
                for other in repeated_ids(members,4):
                    ids.update(input_sets.get(other,()))
                if ids:
                    input_sets[entry_id]=ids
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
            if (mnemonic in ('CppCompile', 'CppLink') and owner == REPRESENTATIVE) or code or status or (owner==MAP and mnemonic=='CppModuleMap'):
                argv=spawn.get(1,[])
                compiler=argv[0].decode() if argv else ''
                declared=bool(re.fullmatch(r'external/llvm\+\+llvm_toolchain_minimal\+llvm-toolchain-minimal-linux-arm64/bin/clang(?:\+\+)?',compiler))
                compilers.append({'argv_sha256':argv_digest,'owner':owner,'mnemonic':mnemonic,'runner':runner,'cached':cached,
                                  'exit':code,'declared_llvm':declared,
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
                if owner==MAP and mnemonic=='CppModuleMap':
                    row=compilers[-1]
                    selected=set(input_sets.get(first(spawn,4,0),())) | set(input_sets.get(first(spawn,5,0),()))
                    row['generator_inputs']=[files[i] for i in selected
                                             if files[i]['path']==compiler]
                    row['map_outputs']=[files[i] for o in spawn.get(6,[])
                                        if (i:=first(fields(o),5,0)) in files and files[i]['path']==MAP_OUTPUT]
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



def discover_configuration(result: dict, env: dict[str, str], output_base: Path) -> str:
    """Pin the canonical configuration from this exact owner's real ARM query."""
    projection = ("str(target.label) + ' ' + build_options(target)['//command_line_option:cpu'] + ' ' + "
                  "str(build_options(target)['//command_line_option:platforms'][0]) + ' ' + "
                  "str(len(build_options(target)['//command_line_option:platforms']))")
    command = [str(ROOT/'bin/bazel'), 'cquery', '--color=no', '--curses=no', '--remote_cache=', META,
               '--output=starlark', '--starlark:expr='+projection, *ARM_OPTIONS, '--', REPRESENTATIVE]
    result['stage']='arm_configuration'
    save(result)
    with (ROOT/'configuration.out').open('wb') as out, (ROOT/'configuration.err').open('wb') as err:
        child = subprocess.run(['timeout', '-k', '20', '600', *command],
                               env=env|{'QUAL_PHASE':'configuration'}, stdout=out, stderr=err)
    result['configuration_query_exit']=child.returncode
    save(result)
    row = result['configuration_query'] = summarize('configuration','cquery',output_base)
    save(result)
    output = ROOT/'configuration-cquery.out'
    assert output.stat().st_size < 4096, output.stat().st_size
    observations = [[normalized(x) for x in line.split()] for line in output.read_text().splitlines()]
    row['observations'] = observations
    save(result)
    assert child.returncode == row['native_exit'] == 0, row
    assert row['observed_invocation'] == row['requested_invocation']
    assert row['bazel_exit']['name'] == 'SUCCESS' and not row.get('incomplete_bep_tail_bytes')
    assert row['test_attempts'] == 0 and not row['failed_actions']
    (owner, aspect, kind, tags, config), = row['configured']
    assert [owner, aspect, kind, tags] == [REPRESENTATIVE, '', 'cc_binary rule', []]
    assert re.fullmatch(r'[0-9a-f]{64}',config), config
    assert row['configurations'][config] == 'aarch64', row['configurations']
    # cquery reports its configured owner as NO_BUILD because it only analyzes.
    assert [(a['label'], a['aspect'], a['configuration'], a['reason']) for a in row['aborted']] == [
        (REPRESENTATIVE, '', config, 'NO_BUILD')], row['aborted']
    assert observations == [[REPRESENTATIVE, 'aarch64', '//tools/bazeldefs:linux_arm64', '1']], observations
    row['configuration_pin'] = config
    row['status'] = 'PASS'
    save(result)
    return config


def phase(result, name, labels, env, output_base, target_config):
    row = result['phases'][name] = {'status':'INCOMPLETE','started_at':now(),'requested_labels':labels}
    save(result)
    command = COMMAND[:2]+['OPTIONS='+' '.join(ARM_OPTIONS), 'TARGETS='+' '.join(labels),'DOCKER_BUILD=false']
    seconds = 2400
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
    assert build['arguments'] == args+['--remote_download_outputs=toplevel',*ARM_OPTIONS,'--',*labels]
    expression = 'set()'+''.join(' union "'+l+'"' for l in labels)
    assert row['cquery']['arguments'] == args+['--output=starlark','--starlark:file=tools/show_paths.bzl',
                                              *ARM_OPTIONS,'--',expression]
    assert selected == {(l,'',target_config) for l in labels} and selected == passed and not failed
    assert not build['failed_actions'] and not row['docker_calls'] and not build['aborted']
    assert row['placement']['invocation'] == build['observed_invocation']
    assert not row['placement']['placement_or_execution_errors']
    output = Path('bazel-bin/test/syscalls/linux/proc_net_test').resolve(strict=True)
    assert output.is_file() and os.access(output,os.X_OK)
    assert str(output) in (ROOT/(name+'.out')).read_text().splitlines()
    with output.open('rb') as f:
        header = f.read(20)
    assert header[:6] == b'\x7fELF\x02\x01' and int.from_bytes(header[18:20],'little')==183
    row['representative_elf'] = {'owner':REPRESENTATIVE,'bytes':output.stat().st_size,'sha256':digest(output),'machine':183}
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
    return evidence


def main(result):
    result['host'] = {'system':platform.system(), 'machine':platform.machine(),
                      'os_version':platform.freedesktop_os_release()['VERSION_ID']}
    save(result)
    assert result['host'] == {'system':'Linux', 'machine':'aarch64', 'os_version':'22.04'}, result['host']
    assert git('rev-parse','HEAD')==SOURCE and git('rev-parse','HEAD^{tree}')=='c2230716fd9a882c71070aed8b7caace96b28eb8'
    assert git('rev-parse','HEAD^')==ORIGINAL and git('diff','--name-only','HEAD^','HEAD')=='MODULE.bazel'
    assert git('rev-parse','HEAD:test/syscalls/linux')==git('rev-parse','HEAD^:test/syscalls/linux')
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
    result['stage']='namespace_precondition'
    save(result)
    assert namespace_control(result), result['namespace_control']
    target_config = discover_configuration(result, env, output_base)
    result['stage']='strict_build'
    save(result)
    try:
        phase(result,'strict',[REPRESENTATIVE],env,output_base,target_config)
    except Exception as exc:
        result['strict_read_error']=failure(exc)
    strict=result['phases'].get('strict',{})
    placement=strict.get('placement',{})
    build=strict.get('build',{})
    actions=placement.get('project_compiler_actions',[])
    result['consumer_status']=strict.get('status','INCOMPLETE')
    completed=[ok for l,a,c,ok in build.get('completed',[])
               if l==REPRESENTATIVE and not a and c==target_config]
    result['target_completion']=('PASS' if completed==[True] else 'FAIL' if completed==[False] else 'UNACCOUNTED')
    result['module_map_status']='INCOMPLETE'
    failures=[a for a in actions if a['exit'] and not a['cached'] and a['runner']=='linux-sandbox']
    for action in failures[:8]:
        try:
            action['executable']=executable_evidence(action,execroot)
        except (OSError, subprocess.SubprocessError) as exc:
            action['executable_inspection_error']=type(exc).__name__
    save(result)
    assert placement.get('invocation')==build['observed_invocation']==build['requested_invocation']
    assert build['placement_flags']==STRICT and build['test_attempts']==0 and not strict['deadline_exceeded']
    assert build['arguments']==['--color=no','--curses=no','--remote_cache=',META,'--remote_download_outputs=toplevel',
                                *ARM_OPTIONS,'--',REPRESENTATIVE]
    assert not strict['docker_calls'] and any(l==REPRESENTATIVE and not a and c==target_config
                                              for l,a,k,t,c in build['configured'])
    supplier=output_base/'external/llvm+'/'tools/internal'
    result['supplier_file_sha256']={name:digest(supplier/name) for name in ('BUILD.bazel','module_map_generator.c')}
    save(result)
    assert result['supplier_file_sha256']=={
        'BUILD.bazel':'3363bdf4d39846a7abee7f837b34b5ed4c591dc6bb9f501896eafd751fee8adb',
        'module_map_generator.c':'bc92615d07361f4f939e1d9a0f5a9ed939bc9115a2616c38e5864bf72de58cc0'}
    witness=[r for r in actions if r['owner']==MAP and r['mnemonic']=='CppModuleMap' and r.get('map_outputs')]
    assert len(witness)==1
    action,=witness
    result['module_map_status']='OBSERVED'
    assert not action['cached'] and action['runner']=='linux-sandbox' and action['exit']==0
    action['executable']=executable_evidence(action,execroot)
    save(result)
    elf=action['executable']
    assert elf['machine']==183 and elf['readelf_exit']==0 and not elf['has_interp'] and not elf['has_needed']
    assert action['generator_inputs']==[{'path':action['argv0'],'sha256':elf['sha256'],'bytes':elf['bytes']}]
    module_path=(execroot/MAP_OUTPUT).resolve(strict=True)
    assert module_path.is_file() and module_path.is_relative_to(output_base) and module_path.stat().st_size<8*1024**2
    data=module_path.read_bytes()
    headers=re.findall(rb'^[ \t]*textual header "([^"\r\n]+)" \{ size (\d+) \}$',data,re.M)
    actual={'path':MAP_OUTPUT,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
    result['installed_module_map']=actual|{'headers':len(headers)}
    save(result)
    assert action['map_outputs']==[actual] and len(headers)==len(dict(headers)) and len(headers)>1000
    assert any(e['output']==MAP_OUTPUT and e['configuration'] and e['success'] and e['exit']==0
               for e in build.get('module_map_events',[]))
    result['module_map_status']='PASS'
    save(result)
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
            if (event['owner']!=MAP and event.get('stderr',{}).get('diagnostics') and
                    event['primary_output'] in action.get('outputs',[])):
                joined.append({'owner':event['owner'],'mnemonic':event['mnemonic'],'output':event['primary_output'],
                               'action_stderr_sha256':event['stderr']['sha256'],'argv_sha256':action['argv_sha256']})
                break
    result['next_action_failures']=joined[:8]
    result['qualification_complete']=bool(joined and strict['status']=='FAIL' and
        result['target_completion']=='FAIL' and build.get('native_exit') and
        build.get('bazel_exit',{}).get('code') and build['bazel_exit'].get('name')!='SUCCESS')


if __name__=='__main__':
    if Path(sys.argv[0]).name in ('docker','bazel'):
        raise SystemExit(wrapped())
    result={'status':'INCOMPLETE','source':SOURCE,'phases':{},'test_execution_claimed':False,
            'qualification_complete':False,'full_graph_status':'INCOMPLETE','stage':'bootstrap',
            'prior_result':{'source':ORIGINAL,'artifact':13385085477,'native_owners_passed':166,
                            'strict_failed_owner':'llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-amd64//:module_map','strict_failed_mnemonic':'CppModuleMap',
                            'sha256':'4a61f85bc02faaf11a8c14fcda7998d99ca2ad557055390472a8f0cf34d98727'},
            'supplier':SUPPLIER,'remote_cache':False,
            'execution_architecture':'aarch64','target_architecture':'aarch64',
            'prior_arm_results':[
                {'run':41962820494,'artifact':13386042076,'status':'INCOMPLETE',
                 'sha256':'3f40e7397635e5460f56885ebb3eb6c4a6a05bfaa07bba288c4790a1ee624af9',
                 'scope':'Ubuntu 24.04 namespace mount denied; no analysis or build'},
                {'run':41963157057,'artifact':13386321966,'status':'INCOMPLETE',
                 'sha256':'0821813efc293097e229cb9f893dc0e90c3d18975bdc3952e4f0f44bda531776',
                 'scope':'Ubuntu 22.04 namespace passed; cquery succeeded; collector rejected NO_BUILD; no build'}],
            'prior_amd64_fixed_result':{'source':SOURCE,'run':41961370751,'artifact':13386070096,
                'sha256':'13cbc28aef5e6c3a71640b54b3fde0fc69eda21d749d15c8170ba377cc9ce28d','status':'PASS'}}
    try:
        main(result)
    except Exception as exc:
        result['error']=failure(exc)
    finally:
        result['verified_at']=now();save(result)
    raise SystemExit(int(not result['qualification_complete']))
