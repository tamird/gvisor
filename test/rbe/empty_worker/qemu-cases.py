"""Exercise only the two revised QEMU fixtures on the ordinary AMD64 coordinator."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

sc = '//tools/licensecheck:licensecheck_test'
names = ['TestEnumerateAndFetchQemuLicenseSource', 'TestVerifyLicenseSource']
tmp = Path(os.environ['RUNNER_TEMP'])
folder = tmp / 'package-evidence'
receipt = {'source': os.environ['PRODUCT'], 'scope': sc, 'runtime': 'AMD64 GitHub Linux coordinator; scratch build separately verified', 'expected': names}

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

try:
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == receipt['source']
    args = ['bazel', '--bazelrc=' + str(tmp / 'sidecar.bazelrc'), 'test', '--config=x86_64', '--compilation_mode=fastbuild', '--host_platform=//test/rbe/empty_worker:amd64_musl', '--extra_execution_platforms=//test/rbe/empty_worker:amd64_musl', '--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain', '--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash', '--remote_accept_cached', '--noremote_upload_local_results', '--noremote_local_fallback', '--strategy=TestRunner=local', '--jobs=80', '--nocache_test_results', '--test_output=all', '--test_arg=-test.v', '--test_filter=^(' + '|'.join(names) + ')$', '--build_event_json_file=' + str(tmp / 'tools-test.bep'), sc]
    receipt['start'] = now()
    with (tmp / 'tools-test.log').open('w') as log:
        receipt['exit'] = subprocess.run(['timeout', '16m', *args], stdout=log, stderr=subprocess.STDOUT).returncode
    receipt['finish'] = now()
    log = (tmp / 'tools-test.log').read_text(errors='replace')
    receipt['observed_pass'] = sorted(set(re.findall(r'^--- PASS: (Test\w+) \(', log, re.MULTILINE)))
    receipt['observed_fail'] = sorted(set(re.findall(r'^--- (?:SKIP|FAIL): (Test\w+) \(', log, re.MULTILINE)))
    for line in (tmp / 'tools-test.bep').open():
        event = json.loads(line)
        if 'started' in event:
            receipt['invocation'] = event['started'].get('uuid')
        if 'testSummary' in event and event['id']['testSummary']['label'] == sc:
            receipt['bazel_status'] = event['testSummary'].get('overallStatus')
    # Prefer native XML, but retain real Go's named PASS lines either way.
    xml = Path('bazel-testlogs/tools/licensecheck/licensecheck_test/test.xml')
    if xml.is_file():
        receipt['xml_cases'] = [el.get('name') for el in ET.parse(xml).iter('testcase')][:48]
    receipt['passed'] = (receipt['exit'] == 0 and receipt.get('bazel_status') == 'PASSED' and receipt['observed_pass'] == names and not receipt['observed_fail'])
    if not receipt['passed']:
        receipt['first_errors'] = [line[:600] for line in log.splitlines() if line.startswith(('ERROR:', 'FAIL:', '--- FAIL:')) and not re.search(r'api[-_]?key|authorization|credential|remote_header|bes_header|token=|://[^/]+@',line,re.I)][:5]
finally:
    receipt['verified'] = now()
    (folder / 'qemu-cases.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt), flush=True)
raise SystemExit(0 if receipt.get('passed') else 1)
