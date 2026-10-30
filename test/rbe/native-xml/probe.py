"""Exercise the actual split XML spawn, including its failure result and content."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

version, binary = sys.argv[1:3]
binary = str(Path(binary).resolve())
temp = Path(os.environ['RUNNER_TEMP'])
work = temp / ('native-xml-consumer-' + version)
receipt = temp / 'native-xml-results'
work.mkdir(exist_ok=True)
receipt.mkdir(exist_ok=True)
(work / 'MODULE.bazel').write_text('module(name="xmlfixture")\nbazel_dep(name="rules_cc", version="0.2.19")\n')
(work / 'BUILD.bazel').write_text('''load("@rules_cc//cc:defs.bzl", "cc_test")
cc_test(name="silent", srcs=["silent.cc"], shard_count=2)
cc_test(name="broken", srcs=["silent.cc"], args=["fail"])
cc_test(name="own_xml", srcs=["silent.cc"], args=["xml"])
''')
(work / 'silent.cc').write_text(r'''#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
int main(int argc, char** argv) {
  // This must be observed by the test itself, rather than inferred from a
  // console line or the execution strategy chosen for an unrelated action.
  if (access("/bin/sh", X_OK) == 0 || access("/usr/bin/bash", X_OK) == 0) {
    fprintf(stderr, "fixture has access to shell\n"); return 42;
  }
  if (const char* shard = getenv("TEST_SHARD_STATUS_FILE")) {
    FILE* f=fopen(shard,"wb"); if(!f) return 44; fclose(f);
  }
  if (argc > 1 && !strcmp(argv[1],"xml")) {
    FILE* f=fopen(getenv("XML_OUTPUT_FILE"),"w");
    if (!f) return 45;
    fputs("<testsuites><testsuite name='mine'><testcase name='already-written'/></testsuite></testsuites>",f);
    return fclose(f) != 0;
  }
  const unsigned char msg[]={'X', 0, 0xf0, 0x9f, 0xa7, 0xaa,']',']','>','\n'};
  fwrite(msg,1,sizeof(msg),stdout);
  return argc>1 ? 7 : 0;
}
''')
# An ordinary Linux sandbox with the shell directories blocked for both test
# spawns. Compiler and repository actions run outside this test sandbox.
args = [binary, '--nosystem_rc', '--noworkspace_rc', '--nohome_rc', 'test',
        '--spawn_strategy=local', '--strategy=TestRunner=linux-sandbox',
        '--sandbox_block_path=/usr/bin', '--test_env=PATH=/no-programs',
        '--nocache_test_results','--test_output=errors',
        '--incompatible_use_native_posix_test_wrapper',
        '--keep_going', '--nobuild_event_json_file_path_conversion',
        f'--build_event_json_file={receipt}/{version}.bep',
        f'--execution_log_json_file={receipt}/{version}.spawns', '--jobs=4',
        '//:silent','//:broken','//:own_xml']
own = subprocess.run([x for x in args[:-3] if not x.startswith(('--execution_log_json_file=','--build_event_json_file='))] + ['//:own_xml'], cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
(receipt / (version+'-own.log')).write_bytes(own.stdout)
if own.returncode != 0:
    print(own.stdout.decode(errors='replace')[-3500:], flush=True)
    raise RuntimeError('the test-owned XML control failed')
r = subprocess.run(args[:-1], cwd=work, stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
(receipt / (version+'-test.log')).write_bytes(r.stdout)
print(r.stdout.decode(errors='replace')[-2800:],flush=True)
logdir=work/'bazel-testlogs'
rows={}
for test in ['silent','broken','own_xml']:
    rows[test]=[]
    for file in sorted((logdir/test).rglob('test.xml')):
        try:
            root=ET.fromstring(file.read_bytes())
        except (OSError,ET.ParseError):
            continue
        case=root.find('.//testcase'); text=root.findtext('.//system-out',default='')
        if case is not None:
            rows[test].append({'name':case.get('name'),'error':case.find('error').get('message') if case.find('error') is not None else None,
                               'message':text[-120:], 'xml':str(file.relative_to(logdir)),
                               'digest':hashlib.sha256(file.read_bytes()).hexdigest()})
out={'binary':binary,'arm':version,'exit':r.returncode,'tests':rows}
(receipt/(version+'-cases.json')).write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out,indent=2))
# The test that supplies its own XML bypasses the fallback on either source.
assert len(rows['own_xml'])==1 and rows['own_xml'][0]['name']=='already-written',out
if version=='parent':
    # Parent's wrapper itself ran; only the post-processing fallback needs Bash.
    assert not rows['silent'] and not rows['broken'] and r.returncode!=0,out
    assert 'generate-xml.sh' in r.stdout.decode(errors='replace') or 'generate-xml.sh' in (receipt/(version+'.spawns')).read_text(),out
else:
    assert r.returncode==3,out
    assert len(rows['silent'])==2 and {x['name'].split('_shard_')[-1] for x in rows['silent']}=={'1/2','2/2'},out
    assert len(rows['broken'])==1 and rows['broken'][0]['error']=='exited with error code 7',out
    assert all('X?🧪]]>\n' in row['message'] for row in rows['silent']+rows['broken']),out
    assert '--generate-xml' in (receipt/(version+'.spawns')).read_text(),out
