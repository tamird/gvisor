"""Extract only the termination and case counts of the already-identified XML."""
from pathlib import Path
from xml.etree import ElementTree
import os, re, json
from recover_rack_receipts import resolve
root=Path(os.environ['RUNNER_TEMP']);cache=root/'blobs';cache.mkdir()
uri='bytestream://remote.buildbuddy.io/blobs/785c4ecffa0fd6d46989b99a60f1f24a1cb56f7e129bc9c762073f8536f49fe1/6688953'
path,digest=resolve(uri,cache)
parsed=ElementTree.fromstring(path.read_bytes()); messages=[(f.attrib.get('type'), f.attrib.get('message')) for f in parsed.iter('error')]
output='\n'.join((e.text or '') for e in parsed.iter('system-out'))
ending=[line[:250] for line in output.splitlines()[-18:] if not re.search('(?i)token|authorization|api.key|secret',line)]
(root/'race-exit.json').write_text(json.dumps({'original':uri,'error':messages,'end':ending,'go_passes':len(re.findall(r'^\s*--- PASS:',output,re.M)),'go_failed':len(re.findall(r'^\s*--- FAIL:',output,re.M)),'races':output.count('WARNING: DATA RACE')},indent=2))
