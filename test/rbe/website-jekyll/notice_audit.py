"""Exercise the public license verifier and preserve the five source-bound rows."""
from datetime import datetime,timezone
from pathlib import Path
import json
import os
import re
import shutil
import subprocess

out=Path(os.environ['RUNNER_TEMP'])/'website-notices'
out.mkdir()
source=Path('tools/licensecheck/dependencies.yaml').read_text()
head=subprocess.check_output(['git','rev-parse','HEAD^'],text=True).strip()
changed = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD^', 'HEAD'], text=True).splitlines()
assert changed and all(path == '.github/workflows/website-jekyll-sdk.yml' or path.startswith('test/rbe/website-jekyll/') for path in changed), changed
record={'productionHead':head,'startedAt':datetime.now(timezone.utc).isoformat(),'checks':{}}
secret=os.environ.pop('BUILDBUDDY_API_KEY')
assert '\n' not in secret
rc=Path(os.environ['RUNNER_TEMP'])/'website-notices.bazelrc'
rc.write_text('\n'.join('build '+f for f in [
    '--jobs=4','--noshow_progress','--color=no','--verbose_failures',
    '--remote_cache=grpcs://remote.buildbuddy.io','--remote_executor=grpcs://remote.buildbuddy.io',
    '--remote_header=x-buildbuddy-api-key='+secret,'--bes_backend=grpcs://remote.buildbuddy.io',
    '--bes_results_url=https://app.buildbuddy.io/invocation/','--bes_header=x-buildbuddy-api-key='+secret,
    '--remote_download_outputs=all','--spawn_strategy=sandboxed,local',
])+'\n')
bazel=['bazel','--bazelrc='+str(rc)]
cli=bazel+['run','//tools/licensecheck/main:licensecheck','--']
env=os.environ|{'MAKEFLAGS':'DOCKER_BUILD=false','DOCKER_HOST':'unix:///no-docker-daemon'}
def run(tag,command,expected=0):
    result=subprocess.run(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=45*60)
    log=result.stdout.decode(errors='replace')
    for cred in (secret,env.get('GITHUB_TOKEN','')):
        if cred:log=log.replace(cred,'[redacted]')
    (out/(tag+'.log')).write_text(log)
    invocations=list(dict.fromkeys(re.findall(r'https://app.buildbuddy.io/invocation/[0-9a-f-]+',log)))
    record['checks'][tag]={'exit':result.returncode,'invocations':invocations}
    (out/'result.json').write_text(json.dumps(record,indent=2)+'\n')
    print(tag,result.returncode,invocations,flush=True)
    if expected is None:
        assert result.returncode!=0,tag
    elif result.returncode!=expected:
        print('\n'.join(log.splitlines()[-12:]),flush=True);raise RuntimeError(tag)
    return log
def rows(text):
    chunks=re.split(r'\n(?=- dependency: )','\n'+text)
    return {c.split('\n',1)[0].split(': ',1)[1]:c.rstrip()+'\n' for c in chunks if c.startswith('- dependency: ')}
try:
    original=rows(source)
    gem={k:v for k,v in original.items() if k.startswith('jekyll_gem_')}
    assert len(gem)==5,gem.keys()
    run('catalog-owning-test',bazel+['test','//tools/licensecheck:licensecheck_test','--test_output=errors'])
    run('verify',cli+['--mode=verify'])
    missing=out/'missing-gems.yaml'
    missing.write_text(source.replace(''.join(gem.values()),''))
    assert not any(n in rows(missing.read_text()) for n in gem)
    negative=run('missing-five',cli+['--mode=verify','--yaml='+str(missing)],expected=None)
    problems=sorted(set(re.findall(r'missing entry for (jekyll_gem_\w+)',negative)))
    assert problems==sorted(gem),(problems,sorted(gem))
    record['missingOnControl']=problems
    stale=out/'stale-source.yaml'
    name='jekyll_gem_jekyll_relative_links'
    changed=re.sub(r'(?m)^  sha256: [0-9a-f]{64}$','  sha256: '+'0'*64,gem[name])
    stale.write_text(source.replace(gem[name],changed))
    negative=run('changed-archive-hash',cli+['--mode=verify','--yaml='+str(stale)],expected=None)
    assert 'artifact identity changed or is unpinned' in negative
    canonical=out/'fetched.yaml'
    canonical.write_text(source)
    all_counts=[]
    for i in [1,2]:
        fetch=run('fetch-'+str(i),cli+['--mode=fetch','--yaml='+str(canonical)])
        normal=rows(canonical.read_text())
        assert all(normal.get(k)==v for k,v in gem.items())
        assert 'kept existing entry' in next(x for x in fetch.splitlines() if x.startswith('jekyll_gem_autoprefixer_rails'))
        all_counts.append({'entries':len(normal),'changedUnrelated':[n for n in original if n not in gem and original[n]!=normal.get(n)]})
    assert normal['tar.bzl'] == original['tar.bzl']
    record['fetches']=all_counts
    run('verify-fetched',cli+['--mode=verify','--yaml='+str(canonical)])
    record['verdict']='PASS'
finally:
    (out/'result.json').write_text(json.dumps(record,indent=2)+'\n')
    rc.unlink(missing_ok=True)
