"""Compare the real, unsuppressed, and deliberately unlocked call on each target architecture."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

source = Path('pkg/sentry/kernel/futex/futex.go')
root = Path(os.environ['RUNNER_TEMP'])
out = root / 'records/lock-probe.json'
product = source.read_text()
def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def main():
    assert digest(product) == '347dfabf3564a60f27bd7691d75463e97fbb14ca75df00c502f0f159171c87fd'
    # Exercise the same source without the two return annotations to distinguish
    # an unsupported alias proof from an unguarded wake in the real caller.
    alias = product
    for marker in ['return b1, b1, false // +checklocksforce', 'return // +checklocksforce\n\t}\n\tb2.mu.NestedUnlock']:
        assert alias.count(marker)==1
        alias=alias.replace(marker,marker.replace(' // +checklocksforce',''))
    start = product.index('func (m *Manager) doRequeue')
    call = '\tdone, err := b1.wakeLocked(&k1, ^uint32(0), nwake)'
    before, method = product[:start], product[start:]
    assert method.count(call)==1
    mutant=before + method.replace(call,'\tb1.mu.Unlock()\n'+call+'\n\tb1.mu.Lock()')
    line=1+mutant[:mutant.index(call, start)].count('\n')
    records={}
    try:
        for name, code in [('alias',alias),('unguarded',mutant),('product',product)]:
            source.write_text(code)
            args=['docker','exec','tcp-compiler','timeout','13m','bazel','--bazelrc='+str(root/'tcp.bazelrc'),'test','--config=x86_64','--jobs=32','--nocache_test_results','--build_tag_filters=','--test_tag_filters=','--test_timeout=300','--test_output=all','--remote_download_outputs=toplevel','--remote_download_regex=.*[/]test[.](log|xml)$','//pkg/sentry/kernel/futex:futex_nogo']
            with (root/(name+'.log')).open('wb') as stream:
                rc=subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT).returncode
            stdout=subprocess.run(['docker','exec','tcp-compiler','cat','bazel-testlogs/pkg/sentry/kernel/futex/futex_nogo/test.log'],capture_output=True)
            log=stdout.stdout.decode(errors='replace') if rc in (0,3) and stdout.returncode==0 else ''
            lines=[x[:700] for x in log.splitlines() if 'futex.go:' in x or re.search(r'(amd64|arm64)\.\.\.',x)]
            records[name]={'exit':rc,'source_sha256':digest(code),'finished':time.time(),'diagnostics':lines}
            if not lines:
                records[name]['build_errors']=[x[:500] for x in (root/(name+'.log')).read_text(errors='replace').splitlines() if 'ERROR:' in x or 'error:' in x][:5]
            save(records,line)
    finally:
        source.write_text(product)
    assert save(records,line), records

def save(records,line):
    pos=records.get('product',{});neg=records.get('unguarded',{})
    arch_ok=lambda rec:sorted(re.findall(r'^(amd64|arm64)\.\.\.PASS\s*$', '\n'.join(rec.get('diagnostics',[])), re.M))==['amd64','arm64']
    # Both reports must contain a diagnostic at the unprotected wake.
    diag=[x for x in neg.get('diagnostics',[]) if f'futex.go:{line}:' in x and ('lock' in x.lower() or 'held' in x.lower())]
    valid=(pos.get('exit')==0 and arch_ok(pos) and neg.get('exit')==3 and len(diag)>=2)
    out.write_text(json.dumps({'source':os.environ['CURRENT'],'verified':valid,'unguarded_line':line,'variants':records},indent=2)+'\n')
    return valid

if __name__=='__main__':
    main()
