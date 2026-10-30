"""Exercise actual upstream Python programs under the selected supplier bootstrap."""
import json,os,subprocess,sys,tempfile
from pathlib import Path

mode,report=sys.argv[1:]
bazel=['bazelisk','--ignore_all_rc_files']
flags=(['--noenable_bzlmod','--enable_workspace'] if mode=='workspace' else ['--@rules_python//python/config_settings:bootstrap_impl='+('system_python' if mode=='stock' else 'native')])
def bz(*args):
 r=subprocess.run(bazel+[args[0],*flags,*args[1:]],capture_output=True,text=True)
 if r.returncode:print('\n'.join((r.stderr+r.stdout).splitlines()[-25:]),file=sys.stderr)
 return r
def run(*args,env):
 r=subprocess.run(args,capture_output=True,text=True,env=env,timeout=30)
 return dict(exit=r.returncode,stdout=r.stdout[-2400:],stderr=r.stderr[-900:])
out={'mode':mode,'checks':{}}
def main():
 targets=['//:probe','//:multi','//tests/bootstrap_impls/bin_calls_bin:outer','//tests/bootstrap_impls/bin_calls_bin:inner']
 if mode=='workspace':targets=['//:probe']
 r=bz('build',*targets);out['build_exit']=r.returncode
 if r.returncode:return
 programs={t.rsplit(':',1)[-1]:Path('bazel-bin')/(t[2:].split(':')[0])/t.rsplit(':',1)[-1] for t in targets}
 # Resolve the bazel-bin directory, not the binary's own symlinks.
 programs={k:str((v.parent.resolve()/v.name)) for k,v in programs.items()}
 original=dict(os.environ)
 for var in ['RUNFILES_DIR','RUNFILES_MANIFEST_FILE','JAVA_RUNFILES','PYTHONPATH']:original.pop(var,None)
 native=mode=='native'
 restricted=dict(original,PATH='/no-host-tools',RULES_PYTHON_ADDITIONAL_INTERPRETER_ARGS='-X standalone=on')
 ordinary=restricted if native else dict(original,RULES_PYTHON_ADDITIONAL_INTERPRETER_ARGS='-X standalone=on')
 args=['spaced value','--flag','']
 for key,env in [('ordinary',ordinary),('empty_path',restricted)]:
  v=run(programs['probe'],*args,env=env);out['checks'][key]=v
  if not v['exit']:
   payload=json.loads(v['stdout']);assert payload['argv']==args and payload['option']=='on' and payload['prefix'] and payload['executable'] and payload['payload']=='written by upstream compatibility fixture',payload
 if mode=='workspace':return
 tmp=Path(tempfile.mkdtemp())
 l1=tmp/'one';l2=tmp/'two';l1.symlink_to(programs['probe']);l2.symlink_to(l1)
 for key,path in [('symlink_one',l1),('symlink_two',l2)]:
  out['checks'][key]=run(str(path),*args,env=ordinary)
  v=out['checks'][key]
  if not v['exit']:assert json.loads(v['stdout'])['argv']==args
 out['checks']['sibling']=run(programs['outer'],programs['inner'],env=dict(ordinary,RULES_PYTHON_TESTING_TELL_RUNFILES_ROOT='1'))
 joined=out['checks']['sibling']['stdout'];out['checks']['sibling']['inner_import']='inner: import_result=\'success\'' in joined
 out['checks']['declared_args']=run(programs['multi'],env=ordinary)
 # Use upstream's shell test itself, strengthened in the fixture to require
 # successful command substitution (errexit), not just equal stdout.
 t=bz('test','//:symlink_test','--test_output=errors')
 out['checks']['upstream_symlink_test']={'exit':t.returncode,'output':(t.stderr+t.stdout)[-1400:]}
try:main()
finally:Path(report).write_text(json.dumps(out,indent=2)+'\n')
