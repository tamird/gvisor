#!/usr/bin/env python3
"""Measure the Buildkite client change without executing website build actions."""
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
from threading import Thread
import time
from zipfile import ZipFile

BASE = '757a452f9265d02a963203d1ccf7704e7d1ebc57'
SOURCE = 'c75bc99990a381cd8cb8c428a1997d9ee98fa7c7'
ARCHIVE = '001d4d03f0444dfff57a69570cf02e7d935d3c4a5496c937247bcb4f485cc6a3'
TARGET = '//website:site_image'
TMP = Path(os.environ['RUNNER_TEMP']) / 'site-entry'
META = '--build_metadata=SITE_CHECK=Buildkite website'
def now():
    return datetime.now(timezone.utc).isoformat()

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')

def wrapped():
    arm = os.environ['SITE_CHECK_ARM']
    cmd, *args = sys.argv[1:]
    if Path(sys.argv[0]).name == 'docker':
        with (TMP / (arm+'-docker')).open('a') as f:
            print(cmd, file=f)
        return 1
    assert cmd in ('build', 'cquery'), cmd
    event = TMP / f'{arm}-{cmd}.bep'
    # Execution is deliberately disabled on both sides. All other args below
    # come from the unmodified installed pipeline command and native Make.
    flags = ['--build_event_json_file='+str(event)]
    if cmd == 'build':
        flags.append('--nobuild')
    print_file = TMP / f'{arm}-{cmd}.out'
    start = time.monotonic()
    with print_file.open('wb') as f:
        proc = subprocess.run([os.environ['SITE_REAL_BAZEL'], cmd, *flags, *args], stdout=f)
    elapsed = time.monotonic()-start
    if cmd == 'cquery':
        sys.stdout.buffer.write(print_file.read_bytes())
    write(TMP / f'{arm}-{cmd}.json', dict(args=args, elapsed_s=round(elapsed, 6), exit=proc.returncode))
    return proc.returncode

def call(argv, env=None):
    p = subprocess.run(argv, env=env, capture_output=True, text=True)
    if p.returncode:
        # Only public compiler diagnostics, not command lines or environment.
        errors=[s[:380] for s in p.stderr.splitlines() if s.startswith('ERROR:')]
        raise ValueError(f'{argv[0]} exited {p.returncode}: {errors[:3]}')
    return p.stdout

def pipeline():
    import yaml
    steps=yaml.safe_load(Path('.buildkite/pipeline.yaml').read_text())['steps']
    step,=[s for s in steps if isinstance(s,dict) and s.get('label')==':earth_americas: Website tests']
    assert step['env']['DOCKER_BUILD']=='false' and step['agents']['arch']=='amd64'
    return shlex.split(step['command'])

def events(name):
    return [json.loads(line) for line in (TMP/(name+'.bep')).read_text().splitlines()]

def observe(arm, cmd, env, facts):
    call(cmd, env)
    row = facts[arm] = {}
    builds = events(arm+'-build')
    configured=[(e['id']['targetConfigured']['label'], e['configured'].get('targetKind')) for e in builds if 'targetConfigured' in e['id'] and 'configured' in e]
    assert [x[0] for x in configured]==[TARGET],configured
    # --nobuild still reaches target configuration; it must never execute the renderer.
    assert not any('actionCompleted' in e['id'] for e in builds)
    commands = sorted(p.stem[len(arm)+1:] for p in TMP.glob(arm+'-*.json'))
    assert commands==(['build'] if arm=='old' else ['build','cquery']),commands
    for name in commands:
        detail=json.loads((TMP/f'{arm}-{name}.json').read_text())
        assert detail['exit']==0
        opts,=[e['optionsParsed']['explicitCmdLine'] for e in events(arm+'-'+name) if 'optionsParsed' in e]
        cache=env['BAZEL_REMOTE_CACHE']
        present=cache in opts and META in opts
        assert (cache in opts)==(arm=='make') and (META in opts)==(arm=='make'),(name,arm)
        row[name]=dict(seconds=detail['elapsed_s'], got_agent_options=present)
    row['configured']=configured
    row['command']=cmd
    # Compare the actual image producer keys under the respective build flags.
    args=json.loads((TMP/f'{arm}-build.json').read_text())['args']
    assert args[-1]==TARGET
    args=args[:-1]
    if args and args[-1]=='--':
        args.pop()
    graph=json.loads(call([env['SITE_REAL_BAZEL'],'aquery',*args,'--output=jsonproto','--noinclude_artifacts',TARGET],env))
    ids={t['id'] for t in graph['targets'] if t['label'].removeprefix('@@')==TARGET}
    keys=sorted((a['mnemonic'],a.get('actionKey','')) for a in graph['actions'] if a['targetId'] in ids)
    assert keys and all(re.fullmatch('[0-9a-f]{32,64}',key) for _,key in keys), keys
    row['producer_actions']=keys
    if arm=='make':
        raw=(TMP/'make-cquery.out').read_text().splitlines()
        pairs=[tuple(line.split()) for line in raw if line.strip()]
        assert pairs and all(len(p)==2 and p[0].startswith('bazel-out/') and '/website/' in p[0] for p in pairs),pairs
        row['cquery']['output_paths']=[p[0] for p in pairs]
        row['cquery']['count']=len(pairs)
    assert not (TMP/(arm+'-docker')).exists()

class Cache(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_error(404)
    def log_message(self, *args):
        pass

def main(facts):
    assert (platform.system(),platform.machine())==('Linux','x86_64')
    assert git('rev-parse','HEAD')==SOURCE
    assert git('rev-parse','HEAD^')==BASE
    assert set(git('diff','--name-only',BASE,SOURCE).splitlines())=={'.buildkite/pipeline.yaml','tools/bazel.mk'}
    assert git('diff','--name-only','b70c5617a4ee7abe4f899fc1ad913864052f9710',BASE)=='tools/licensecheck/dependencies.yaml'
    archive=Path(os.environ['RUNNER_TEMP'])/'release.zip'
    assert sha256(archive.read_bytes()).hexdigest()==ARCHIVE
    with ZipFile(archive) as z:
        verdict=json.loads(z.read('verdict.json')); normal=json.loads(z.read('normal-build.json')); audit=json.loads(z.read('audit.json'))
        assert verdict['verdict']=='PASS' and verdict['source']=='b70c5617a4ee7abe4f899fc1ad913864052f9710'
        assert Path('tools/licensecheck/dependencies.yaml').read_bytes()==z.read('catalog.yaml')
        facts['previous']=dict(run=41571402230,sha256=ARCHIVE,target=TARGET,source=verdict['source'],catalog_sha256=audit['catalogSHA256'],public_files=normal['public'],raw_identical=normal['raw_identical'],logo=json.loads(z.read('layer-logo.json')),routes=json.loads(z.read('served.json'))['routes'])
    real=shutil.which('bazel'); assert real
    version=call([real,'--version']).strip()
    assert version=='bazel '+Path('.bazelversion').read_text().strip(),version
    facts['bazel']=version
    bindir=TMP/'bin';bindir.mkdir()
    for name in ['bazel','docker']:
        (bindir/name).symlink_to(Path(__file__).resolve())
    server=ThreadingHTTPServer(('127.0.0.1',0),Cache)
    Thread(target=server.serve_forever,daemon=True).start()
    env=os.environ|dict(SITE_REAL_BAZEL=real,PATH=str(bindir)+':'+os.environ['PATH'], DOCKER_BUILD='false',
                         DOCKER_HOST='unix:///absent-buildkite-docker',
                         BAZEL_REMOTE_CACHE='--remote_cache=http://127.0.0.1:'+str(server.server_port),
                         BAZEL_OPTIONS=shlex.quote(META),SITE_CHECK_ARM='make')
    observe('make',pipeline(),env,facts)
    git('switch','--detach',BASE)
    env['SITE_CHECK_ARM']='old'
    observe('old',pipeline(),env,facts)
    assert facts['make']['producer_actions']==facts['old']['producer_actions']
    # The pre-bootstrap precedence on this specific website source must not be
    # assumed to honor an environment variable for the ordinary build goal.
    env['SITE_CHECK_ARM']='old_make_help'
    call(['make','-n','-o','bazel-server-inc','build','TARGETS='+TARGET],env)
    probes=(TMP/'old_make_help-docker').read_text().splitlines()
    assert 'version' in probes and set(probes)<={'version','exec'},probes
    facts['old_make_dry_run_docker']=probes
    facts['execution_finished']=now()
    server.shutdown()
    git('switch','--detach',SOURCE)
    assert git('status','--porcelain','--untracked-files=no')==''
    facts['status']='PASS'

if __name__=='__main__':
    if Path(sys.argv[0]).name in ('bazel','docker'):
        raise SystemExit(wrapped())
    TMP.mkdir(exist_ok=True)
    result=dict(source=SOURCE,old=BASE,status='FAIL')
    try:
        main(result)
    except Exception as exc:
        result['error']=str(exc)
        raise
    finally:
        result['verified_at']=now()
        write(TMP/'result.json',result)
