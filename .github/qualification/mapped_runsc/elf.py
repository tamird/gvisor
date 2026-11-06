import hashlib
import struct


def sha(contents):
    return hashlib.sha256(contents).hexdigest()


def sections(binary):
    assert binary[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", binary, 18)[0] == 183
    table = struct.unpack_from("<Q", binary, 40)[0]
    size, count, strings = struct.unpack_from("<HHH", binary, 58)
    assert size == 64 and 0 < strings < count <= 256 and table + count * size <= len(binary)
    rows = [struct.unpack_from("<IIQQQQIIQQ", binary, table + i * size) for i in range(count)]
    off, n = rows[strings][4:6]
    assert off + n <= len(binary)
    names = binary[off:off+n]
    result = {}
    for idx, (nameoff, kind, flags, address, offset, length, link, _, _, entsize) in enumerate(rows[1:], 1):
        assert nameoff < n
        end = names.index(0, nameoff)
        name = names[nameoff:end].decode("ascii")
        if kind == 8:  # SHT_NOBITS contributes memory, but has no bytes in the file.
            data = b""
        else:
            assert offset + length <= len(binary)
            data = binary[offset:offset+length]
        assert name not in result
        result[name] = (data, length, flags, address, idx, link, entsize)
    return result


def file_table(parts):
    for name in (".gopclntab", ".data.rel.ro.gopclntab"):
        if name in parts:
            raw = parts[name][0]
            break
    else:
        return []
    if len(raw) < 72 or raw[:4] != b"\xf1\xff\xff\xff" or raw[7] != 8:
        return []
    _, files, _, _, _, start, end, _ = struct.unpack_from("<8Q", raw, 8)
    if not (0 < files < 1000000 and 72 <= start < end <= len(raw)):
        return []
    names = raw[start:end].split(b"\0", files)
    return names[:files] if len(names)>files else []
