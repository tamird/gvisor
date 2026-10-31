import hashlib
import json
import os
import platform
from pathlib import Path
import struct
import sys
import zipfile

expected_modes = {'containerd-shim-runsc-v1': '0o100555', 'gvisor-bin/checkpointgofer': '0o100555', 'gvisor-bin/gvisor-proc-umounter': '0o100555', 'gvisor-bin/gvisor-sentry-prewarmer': '0o100555', 'gvisor-bin/gvisor_sentry': '0o100555', 'gvisor-bin/runsc-fd-parking': '0o100555', 'gvisor-bin/runsc-metric-server': '0o100555', 'runsc': '0o100555'}
archive = Path(sys.argv[1])
digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
assert digest == os.environ["ARTIFACT_DIGEST"].removeprefix("sha256:"), digest
wanted_machine = {"aarch64":183, "x86_64":62}[platform.machine()]
with zipfile.ZipFile(archive) as z:
    files = {f.filename:f for f in z.infolist() if not f.is_dir()}
    assert set(files)==set(expected_modes), files
    records = {}
    for name,entry in files.items():
        contents = z.read(entry)
        assert contents[:4] == b"\x7fELF" and contents[5] == 1
        assert struct.unpack_from("<H",contents,18)[0] == wanted_machine
        assert oct(entry.external_attr>>16) == expected_modes[name]
        original = (Path("bazel-bin/release")/name).read_bytes()
        assert original == contents, name
        records[name] = {"size":len(contents),"sha256":hashlib.sha256(contents).hexdigest(),"mode":expected_modes[name],"elf_machine":wanted_machine}
Path(sys.argv[2]).write_text(json.dumps({"verified":True,"arch":platform.machine(),"zip_digest":digest,"artifact":os.environ["ARTIFACT_ID"],"members":records},indent=2)+"\n")
print("Verified",len(records),"installed ELF files for",platform.machine())
