"""Compare the source PR and its actual Copybara export with both formatters."""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


def main():
    old_wheel, new_wheel, config, source, export, candidate = map(Path, sys.argv[1:])
    source, export, candidate = (p.read_bytes() for p in (source, export, candidate))
    assert re.sub(rb"\s", b"", source) == re.sub(rb"\s", b"", export)
    for version, wheel, expected in (
        ("20.1.8", old_wheel, export),
        ("23.1.1", new_wheel, source),
    ):
        binary = Path(os.environ["TEST_TMPDIR"]) / ("clang-format-" + version)
        with zipfile.ZipFile(wheel) as z:
            binary.write_bytes(z.read("clang_format/data/bin/clang-format"))
        binary.chmod(0o755)
        print(subprocess.check_output([binary, "--version"], text=True).strip())
        for label, data, want in (
            ("source", source, expected),
            ("export", export, expected),
            ("candidate", candidate, candidate),
        ):
            output = subprocess.check_output(
                [binary, "--style=file:" + str(config.resolve()), "--assume-filename=errno_safe_allocator_test.cc"],
                input=data,
            )
            assert output == want, (version, label, output.decode())
            print(version, label, len(output), hashlib.sha256(output).hexdigest())


if __name__ == "__main__":
    main()
