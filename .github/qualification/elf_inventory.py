"""Retain complete release identity and ELF section/layout evidence."""

import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inspect_elf(path: Path, output: Path) -> dict[str, object]:
    data = path.read_bytes()
    if data[:6] != b'\x7fELF\x02\x01':
        raise ValueError(f'Expected little-endian ELF64: {path}')
    header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
    section_offset, entry_size, count, names_index = header[6], header[11], header[12], header[13]
    if count == 0 or names_index == 0xffff or entry_size != 64:
        raise ValueError(f'Unsupported extended section table: {path}')
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, section_offset + i * entry_size)
                for i in range(count)]
    names_header = sections[names_index]
    names = data[names_header[4]:names_header[4] + names_header[5]]
    output.mkdir()
    records = []
    for index, section in enumerate(sections):
        name = names[section[0]:].split(b'\0', 1)[0].decode('utf-8')
        offset, size = section[4], section[5]
        # SHT_NOBITS has memory size but no corresponding file bytes.
        contents = b'' if section[1] == 8 else data[offset:offset + size]
        if section[1] != 8 and len(contents) != size:
            raise ValueError(f'Truncated section {name!r}: {path}')
        record = {'index': index, 'name': name, 'type': section[1], 'flags': section[2],
                  'address': section[3], 'offset': offset, 'size': size,
                  'alignment': section[8], 'sha256': sha256(contents)}
        records.append(record)
        if name in ('.text', '.rodata', '.data', '.gopclntab', '.noptrdata'):
            (output / f'{index:03d}{name}.bin').write_bytes(contents)
    for label, command in (
        ('readelf.txt', ['readelf', '-aW', str(path)]),
        ('symbols.txt', ['nm', '-anS', '--demangle', str(path)]),
        ('objdump-layout.txt', ['objdump', '-hp', str(path)]),
    ):
        with (output / label).open('wb') as stream:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=180)
    return {'sha256': sha256(data), 'size': len(data), 'sections': records}


def main() -> None:
    release, output = map(Path, sys.argv[1:3])
    evidence = output / 'elf'
    evidence.mkdir()
    records: dict[str, object] = {}
    elf_count = 0
    # Enumeration is for inspection only; release installation and retention
    # always consume the canonical fileset in full.
    for path in sorted(release.rglob('*')):
        relative = str(path.relative_to(release))
        if path.is_symlink():
            records[relative] = {'symlink': str(path.readlink())}
        elif path.is_file():
            with path.open('rb') as stream:
                magic = stream.read(4)
            record: dict[str, object] = {'size': path.stat().st_size}
            with path.open('rb') as stream:
                record['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
            if magic == b'\x7fELF':
                location = f'{elf_count:03d}'
                record.update(inspect_elf(path, evidence / location))
                record['evidence'] = f'elf/{location}'
                elf_count += 1
            records[relative] = record
    if elf_count == 0:
        raise ValueError('Canonical release contains no ELF files')
    (output / 'release-inventory.json').write_text(json.dumps(records, indent=2) + '\n')


if __name__ == '__main__':
    main()
