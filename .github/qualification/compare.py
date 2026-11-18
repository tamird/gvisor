"""Compare complete opaque-release inventories without a performance claim."""

import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1])
    base = json.loads((root / 'base/release-inventory.json').read_text())
    candidate = json.loads((root / 'candidate/release-inventory.json').read_text())
    if not isinstance(base, dict) or not isinstance(candidate, dict):
        raise ValueError('Expected release inventory objects')
    differences = {}
    for path in sorted(base.keys() | candidate.keys()):
        before, after = base.get(path), candidate.get(path)
        if not isinstance(before, dict) or not isinstance(after, dict):
            differences[path] = {'before': before, 'after': after}
            continue
        change = {key: {'before': before.get(key), 'after': after.get(key)}
                  for key in ('sha256', 'size', 'symlink') if before.get(key) != after.get(key)}
        if 'sections' in before or 'sections' in after:
            sections_before = before.get('sections')
            sections_after = after.get('sections')
            if not isinstance(sections_before, list) or not isinstance(sections_after, list):
                raise ValueError(f'Mismatched ELF inventories: {path}')
            if any(not isinstance(section, dict) for section in sections_before + sections_after):
                raise ValueError(f'Malformed section inventories: {path}')
            by_index_before = {section['index']: section for section in sections_before}
            by_index_after = {section['index']: section for section in sections_after}
            changed_sections = {str(index): {'before': by_index_before.get(index),
                                           'after': by_index_after.get(index)}
                                for index in by_index_before.keys() | by_index_after.keys()
                                if by_index_before.get(index) != by_index_after.get(index)}
            if changed_sections:
                change['sections'] = changed_sections
        if change:
            differences[path] = change
    (root / 'release-comparison.json').write_text(json.dumps({
        'differences': differences,
        'conclusion': 'Artifact comparison only; does not establish performance parity or causality.',
        'adverseEvidenceRetained': [
            {'run': '44190929074', 'optimizedGetpidChange': '+3.22%', 'p': 0.008, 'n': 24},
            {'run': '44196112563', 'optimizedGetpidChange': '+1.20%', 'p': 0.003, 'n': 24},
        ],
    }, indent=2) + '\n')


if __name__ == '__main__':
    main()
