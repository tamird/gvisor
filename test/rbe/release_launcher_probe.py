"""Exercise both Buildkite release step environments on a worker without Bazel."""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

def now():
    return datetime.now(timezone.utc).isoformat()

def cmd(args, **kw):
    return subprocess.run(args, text=True, capture_output=True, **kw)

def git(*args):
    return cmd(('git', *args), check=True).stdout

old, new, hook_rev = (os.environ[k] for k in ('CONTROL','SOURCE','HOOK_REV'))
registry = {'control': old, 'candidate': new}
root = Path(os.environ['RUNNER_TEMP'])/'release-launcher'
root.mkdir()
expected = [f'{kind}/{job}' for kind in registry for job in ('ci','release')] + ['python-wrapper']
verdict = dict(source=new, control=old, hook=hook_rev, expected=expected, executed=[], failures={}, skipped=[], observations={})

try:
    assert git('rev-parse','HEAD^').strip() == new
    full_hook = git('show',f'{hook_rev}:.buildkite/hooks/pre-command')
    marker = 'install_lint_bazel() {'
    assert full_hook.count(marker)==1
    bootstrap = root/'bootstrap'
    bootstrap.write_text(full_hook[full_hook.index(marker):])
    pin = Path('.bazelversion').read_text().strip()
    # Mask only Bazel. A preinstalled Bazelisk may itself use node or sh.
    path = []
    for i, directory in enumerate(os.get_exec_path()):
        directory = Path(directory)
        if (directory/'bazel').exists():
            proxy = root/f'path-{i}'
            proxy.mkdir()
            for entry in directory.iterdir():
                if entry.name != 'bazel':
                    (proxy/entry.name).symlink_to(entry.resolve(strict=False))
            directory = proxy
        path.append(str(directory))
    selected_path = os.pathsep.join(path)
    ruby = 'require "yaml";require "json";print JSON.generate(YAML.safe_load(STDIN.read, aliases: true)["steps"].find{|s|s["label"]==ARGV[0]})'
    python_cmd = None
    for kind, ref in registry.items():
        for job, file, label in [('ci','pipeline',':ship: Release tests'),('release','release',':ship: Release')]:
            name = kind+'/'+job
            step = json.loads(cmd(['ruby','-e',ruby,label],input=git('show',ref+':.buildkite/'+file+'.yaml'),check=True).stdout)
            env = dict(os.environ,PATH=selected_path)
            env.pop('DOCKER_BUILD', None)
            env.update(step.get('env',{}))
            # The worker has no bazel executable until the hook exposes one.
            assert not shutil.which('bazel',path=env['PATH'])
            shell = '. "$1"; if ! command -v bazel >/dev/null; then exit 127; fi; bazel --version'
            r=cmd(['/bin/bash','-euc',shell,'probe',str(bootstrap)],env=env)
            verdict['executed'].append(name)
            verdict['observations'][name]={'exit':r.returncode,'stdout':r.stdout.strip(),'stderr_tail':r.stderr[-800:]}
            ok=(r.returncode==127 if kind=='control' else r.returncode==0 and r.stdout.strip()=='bazel '+pin)
            if not ok: verdict['failures'][name]='Unexpected launcher result'
            if kind=='candidate' and job=='release':
                python_cmd, = [c for c in step['commands'] if c.startswith('make ') and c.endswith('artifacts-python')]
                python_env=env.get('DOCKER_BUILD')
    assert python_cmd and python_env
    import shlex
    args=shlex.split(python_cmd)
    attempt=cmd([args[0],'-n','-o','bazel-server-inc','-o','ensure-bazel-server',*args[1:]],env=dict(os.environ,DOCKER_BUILD=python_env))
    selection=[s for s in attempt.stdout.splitlines() if 'tools/make_python_release.sh build' in s]
    verdict['executed'].append('python-wrapper')
    verdict['observations']['python-wrapper']=selection
    if attempt.returncode!=0 or len(selection)!=1 or 'docker exec ' not in selection[0]:
        verdict['failures']['python-wrapper']=attempt.stderr[-800:] or 'Python command left the builder'
except Exception as e:
    verdict['failures']['probe']=repr(e)
verdict['execution_finished']=now()
verdict['missing']=sorted(set(expected)-set(verdict['executed']))
verdict['verdict']='FAIL' if verdict['failures'] or verdict['missing'] else 'PASS'
verdict['verified']=now()
(root/'verdict.json').write_text(json.dumps(verdict,indent=2)+'\n')
print(json.dumps(verdict))
raise SystemExit(verdict['verdict']!='PASS')
