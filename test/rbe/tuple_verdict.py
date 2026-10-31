import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from qualification_report import xml_cases

d = Path(sys.argv[1])
source = (d/'source').read_text().strip()
expected = {'linux', 'release-20260721.0'}
xml = d/'test.xml'
counts = xml_cases(xml) if xml.is_file() else {'passed':0, 'failed':0, 'skipped':0}
log = (d/'test.log').read_text() if (d/'test.log').is_file() else ''
rows = [json.loads(line[7:]) for line in log.splitlines() if line.startswith('RESULT ')]
actual = {r['lane'] for r in rows}
rc = int((d/'exit').read_text())
ok = (source == os.environ['GITHUB_SHA'] and rc == 0 and actual == expected and
      len(rows) == 2 and counts['passed'] > 0 and counts['failed'] == counts['skipped'] == 0
      and all(r['ports'] == [43425,43426,8081] and r['steps'] == ['go1','go2','passed','ready1','ready2']
              and len(r['checkpoints']) == (0 if r['lane'] == 'linux' else 2) and all(n>0 for n in r['checkpoints'])
              for r in rows))
report = dict(verdict='PASSED' if ok else 'FAIL_OR_INCOMPLETE', source=source,
              target='//test/rbe/restoretuple:tuple', expected=sorted(expected), completed=sorted(actual),
              missing=sorted(expected-actual), counts=counts, bazel_exit=rc, evidence=rows,
              runtime=[line for line in log.splitlines() if line.startswith('RUNTIME ')],
              execution_finished=(d/'execution-finished').read_text().strip(),
              verified=datetime.now(timezone.utc).isoformat(timespec='seconds'))
(d/'verdict.json').write_text(json.dumps(report,indent=2)+'\n')
print(report)
sys.exit(not ok)
