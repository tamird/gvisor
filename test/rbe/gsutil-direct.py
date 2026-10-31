import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

arch, arm, runtime = sys.argv[1:]
assert platform.machine() == arch
cpu = {'x86_64': 'amd64', 'aarch64': 'arm64'}[arch]
repo = Path.cwd()
tmp = Path(os.environ['RUNNER_TEMP'])
out = tmp / 'results' / (arm + '-' + runtime)
out.mkdir(parents=True, exist_ok=True)
old = 'us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/benchmarks/gsutil_' + arch + ':7eba9c02d11172d4'
source = Path('test/kubernetes/benchmarks/gsutil.go').read_text()
image, = re.findall(r'^\s*gsutilImage\s*= "([^"]+)"', source, re.M)
if arm == 'shipped':
    image = old
subprocess.run(['docker', 'pull', image], check=True)
data = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
assert data['Architecture'] == cpu and data['Os'] == 'linux'
image_info = {k: data[k] for k in ('Architecture','Os','Id','RepoDigests','RootFS','Size')}
image_info['requested'] = image
(out / 'image.json').write_text(json.dumps(image_info, indent=2))
boto = tmp / 'probe.boto'
boto.write_text('[GSUtil]\nsliced_object_download_threshold=1M\nsliced_object_download_component_size=16M\n')
cmd = ['docker','run','--rm','--pull=never','--runtime='+runtime,'--entrypoint=sh','-v',str(boto)+':/probe.boto:ro','-e','BOTO_CONFIG=/probe.boto',image]
version = subprocess.run(cmd+['-c','id -u; uname -m; . /etc/os-release; echo "$PRETTY_NAME"; gcloud version; gsutil version -l'], text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=120)
(out/'version').write_text(version.stdout)
commands = Path('test/rbe/gsutil-pod-commands.json').read_bytes()
assert hashlib.sha256(commands).hexdigest() == '34411413ca17a80532b5d028ebbcff9521cc11836cbf3ab3c40ebef4914d0c0a'
results = []
for mode, argv in sorted(json.loads(commands).items()):
    assert argv[:2] == ['sh','-c'] and len(argv) == 3
    script = argv[2]
    assert script.count('gs://gvisor-benchmark-testdata/bigrandomfile') == 2 and script.count('gsutil ') == 2
    script = script.replace('gs://gvisor-benchmark-testdata/bigrandomfile','gs://gvisor-benchmark-testdata/littlerandomfile').replace('gsutil ', 'gsutil -D ')
    script += " && stat -c 'probe-bytes=%s' /downloads/randombigfile && gsutil hash -c /downloads/randombigfile"
    p = subprocess.run(cmd+['-c',script],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=450)
    log = p.stdout.decode('utf-8',errors='replace')
    ranges = sorted(set(re.findall(r'bytes=(\d+-\d*)',log,re.I)))
    components = sorted(set(re.findall(r'randombigfile component (\d+)',log)))
    starts = {r.split('-')[0] for r in ranges}
    timed = re.findall(r'^::::TIMING::::(\d+)::::(\d+)\s*$',log,re.M)
    row = dict(mode=mode,exit=p.returncode,bytes=52428800 if 'probe-bytes=52428800' in log else 0,crc32c='1BRP5g==' if '1BRP5g==' in log else '',timed=len(timed),clock_valid=bool(timed and int(timed[0][1])>=int(timed[0][0])),reported_failure='::::TIMING::::FAILURE' in log,ranges=ranges,components=components,http_codes=sorted(set(re.findall(r'(?i)(?:status[ =:]\s*|HTTP/1\.[01]"? )(\d\d\d)\b',log))),errors=sorted(set(re.findall(r'\b((?:\w+Error|\w+Exception)(?=:))',log))))
    if 'gsutil requires Python version 2.7 or 3.5-3.11' in log and 'You are currently running Python 3.12' in log:
        row['incompatible_python'] = True
    if arm == 'shipped' and arch == 'aarch64' and p.returncode and row['reported_failure'] and row.get('incompatible_python'):
        row['verdict'] = 'SHIPPED_PYTHON_INCOMPATIBLE'
    elif not p.returncode and row['clock_valid'] and not row['reported_failure'] and row['bytes'] and row['crc32c'] and ((mode=='single' and len(starts)<=1) or (mode=='sliced' and (len(starts)>1 or len(components)>1))):
        row['verdict'] = 'PASS'
    else:
        row['verdict'] = 'FAIL'
    results.append(row)
    (out/'transfers.json').write_text(json.dumps(results,indent=2)+'\n')
    print(arch,arm,runtime,row,flush=True)
assert len(results)==2 and all(r['verdict']!='FAIL' for r in results), results
if any(r['verdict']=='PASS' for r in results):
    assert version.returncode==0 and 'compiled crcmod: True' in version.stdout, version.returncode
