"""Resolve leftovers against immutable invocation intents before removal."""
from datetime import datetime
import json
from pathlib import Path
import re


TEMPLATE = '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},"runtime":{{json .HostConfig.Runtime}},"created":{{json .Created}}}'


def matches(value: dict, intent: dict, source: str) -> bool:
    assert intent['source'] == source
    assert re.fullmatch(r'[a-f0-9]{64}', value['id']), 'Invalid container ID'
    created = datetime.fromisoformat(value['created'].replace('Z', '+00:00'))
    assert created.tzinfo is not None, 'Container creation time lacks a timezone'
    return (value['image'] == intent['image']
            and value['runtime'] == intent['runtime']
            and re.fullmatch(intent['namePattern'], value['name']) is not None
            and created.timestamp() >= intent['invocationStartEpoch'])


def intent(results: Path, label: str, source: str, image: str,
           runtime: str, pattern: str, started: float) -> None:
    path = results / 'invocation-intents' / (label + '.json')
    path.parent.mkdir(exist_ok=True)
    assert not path.exists(), 'Invocation already recorded'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({
        'source': source, 'image': image, 'runtime': runtime,
        'namePattern': pattern, 'invocationStartEpoch': started,
    }, indent=2) + '\n')
    temporary.replace(path)


def owned(containers: list[dict], intents: list[dict], source: str) -> list[dict]:
    assert len(containers) <= 1, 'Sequential invocations cannot own multiple leftovers'
    assert len({item['id'] for item in containers}) == len(containers)
    for value in containers:
        selected = [item for item in intents if matches(value, item, source)]
        assert len(selected) == 1, 'Unrelated or ambiguous container: ' + value['id']
    return containers


def complete(results: Path, label: str) -> None:
    """Close an intent only after its caller has verified no containers remain."""
    path = results / 'invocation-complete' / label
    path.parent.mkdir(exist_ok=True)
    assert not path.exists(), 'Invocation completion already recorded'
    path.write_text('Container inventory verified empty\n')
