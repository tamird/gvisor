"""Compare wheel namespace generation on unmodified Bazel."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat()

tmp = Path(os.environ['RUNNER_TEMP'])
repo = Path(os.environ['GITHUB_WORKSPACE'])
base = repo / 'supplier'
patch = repo / 'consumer/test/rbe/supplier-namespace/bazel-lib-copy.patch'
fixture = tmp / 'consumer'
stage = tmp / 'copy-results'
fixture.mkdir()
stage.mkdir()
baseline = '674ce0745dea93e54865d9cfb846510b30475969'
receipt = dict(source=baseline, patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest(), expected=['stock','candidate','s390x'], observed={}, commands=[])
bazel = shutil.which('bazelisk') or shutil.which('bazel')

def run(args, cwd=fixture, name='cmd', ok=True):
    r = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (stage / (name+'.log')).write_bytes(r.stdout)
    receipt['commands'].append(dict(name=name, argv=list(map(str,args)), exit=r.returncode))
    if ok and r.returncode:
        print(r.stdout[-3500:].decode(errors='replace'), flush=True)
        raise RuntimeError(f'{name}: exit {r.returncode}')
    return r

def bz(*args, **kwargs):
    return run([bazel, *args], **kwargs)

def info(key):
    return Path(bz('info', key, name='info-'+key).stdout.decode().strip().splitlines()[-1])

def objpath(root, rel):
    p = Path(rel)
    assert not p.is_absolute() and '..' not in p.parts, p
    return root / p

def main():
    assert run(['git','rev-parse','HEAD'],base,name='source').stdout.decode().strip()==baseline
    (fixture / 'MODULE.bazel').write_text(f'''module(name = "copy_witness")
bazel_dep(name = "rules_python", version = "2.4.5")
local_path_override(module_name = "rules_python", path = "{base}")
''')
    (fixture / 'BUILD.bazel').write_text('''load("@rules_python//python/private/pypi:namespace_pkgs.bzl", "create_inits")
create_inits(srcs = ["alpha/beta/example.py"])
platform(name = "linux_s390x", constraint_values = ["@platforms//os:linux", "@platforms//cpu:s390x"])
''')
    templates = ('//:_cp_0_namespace','//:_cp_1_namespace')
    # Exercise the same generator the supplier uses, not a copy rule instantiated by hand.
    version=bz('--version', name='version').stdout.decode().strip()
    assert '8.8.1' in version, version
    opts = ('--action_env=PATH=/no-host-programs', '--spawn_strategy=linux-sandbox')
    stock = bz('build',*opts,*templates,ok=False,name='stock')
    assert stock.returncode and b'cp: command not found' in stock.stdout, stock.stdout[-3600:]
    receipt['observed']['stock']={'missing':'cp','exit':stock.returncode}
    run(['git','apply','--check',patch],base,name='patch-check')
    run(['git','apply',patch],base,name='patch')
    bz('build',*opts,*templates,name='candidate')
    outputs=info('bazel-bin'); execution=info('execution_root')
    src=(base/'python/private/pypi/namespace_pkg_tmpl.py').read_bytes()
    import stat
    files={}
    for file in ('alpha/__init__.py','alpha/beta/__init__.py'):
        dest=outputs/file
        assert stat.S_ISREG(dest.lstat().st_mode) and dest.read_bytes()==src
        files[file]=hashlib.sha256(src).hexdigest()
    # aquery's diagnostics are on stderr; keep the JSON separate.
    with (stage/'aquery.json').open('wb') as out, (stage/'aquery.log').open('wb') as err:
        subprocess.run([bazel,'aquery','--output=jsonproto',*templates[:1]],cwd=fixture,stdout=out,stderr=err,check=True)
    actions=json.loads((stage/'aquery.json').read_bytes())['actions']
    copy,=[a for a in actions if a['mnemonic']=='CopyFile']
    argv=copy['arguments']
    assert len(argv)==4 and argv[1]=='cp',argv
    # Replay the original executable and arguments with no shell, libc, loader, or other files.
    empty=tmp/'empty-copy'; empty.mkdir()
    for path in (argv[0],argv[2]):
        dest=objpath(empty,path); dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(objpath(execution,path),dest)
    out=objpath(empty,argv[3]);out.parent.mkdir(parents=True,exist_ok=True)
    run(['sudo','chroot',empty,'/'+argv[0],*argv[1:]],name='empty-root')
    assert out.read_bytes()==src
    receipt['observed']['candidate']={'outputs':files,'size':len(src),'argv':argv,'empty_root':'PASS'}
    opt=['--host_platform=//:linux_s390x','--platforms=//:linux_s390x']
    other=bz('build','--nobuild',*opt,*templates,ok=False,name='s390x')
    assert other.returncode and b'coreutils_toolchain_type' in other.stdout and b'No matching toolchains' in other.stdout,other.stdout[-3800:]
    receipt['observed']['s390x']={'configuration':'no default coreutils toolchain','exit':other.returncode}
    receipt['execution_finished_at']=now()

try:
    main()
    assert sorted(receipt['observed'])==sorted(receipt['expected'])
    receipt['status']='PASS'
except Exception as e:
    receipt['status']='FAIL'
    receipt['error']=str(e)
    raise
finally:
    receipt['verdict_available_at']=now()
    (stage/'verdict.json').write_text(json.dumps(receipt,indent=2)+'\n')
