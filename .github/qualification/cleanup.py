"""Remove only source-named native consumer leftovers after stage quiescence."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

results = Path(sys.argv[1])
intents = json.loads((results / 'native-intents.json').read_text()) if (results / 'native-intents.json').exists() else []
def docker(*args):
    return subprocess.check_output(['sudo', 'docker', *args], text=True, timeout=5)
template = '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},"runtime":{{json .HostConfig.Runtime}},"created":{{json .Created}}}'
selected = []
for identifier in docker('ps', '-aq', '--no-trunc').split():
    value = json.loads(docker('inspect', '--format', template, identifier))
    matches = []
    for intent in intents:
        assert intent['source'] == os.environ['SOURCE']
        if value['runtime'] != 'runc' or datetime.fromisoformat(value['created'].replace('Z', '+00:00')).timestamp() < intent['started']:
            continue
        if any(value['image'] == image and any(re.fullmatch(re.escape(prefix) + '[A-Z2-7]{32}', value['name']) for prefix in prefixes) for image, prefixes in intent['imagePrefixes'].items()):
            matches.append(intent)
    assert len(matches) == 1, 'Unknown or ambiguous native consumer container'
    selected.append(value)
assert len(selected) <= 3, 'Unexpected simultaneous consumer containers'
# Resolve all identities before any deletion; revalidate immediately before removal.
for value in selected:
    assert json.loads(docker('inspect', '--format', template, value['id'])) == value
    docker('rm', '-f', value['id'])
assert not docker('ps', '-aq').split()
(results / 'cleanup.json').write_text(json.dumps({'ownedRemoved': selected, 'allContainersAbsent': True}, indent=2) + '\n')
