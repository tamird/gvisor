import hashlib
import json
from pathlib import Path
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:*") as archive:
    files = {}
    for entry in archive.getmembers():
        if entry.isfile():
            name = entry.name.removeprefix("./")
            with archive.extractfile(entry) as contents:
                digest = hashlib.file_digest(contents, "sha256").hexdigest()
            files[name] = {"bytes": entry.size, "mode": entry.mode, "sha256": digest}
for name in ("server", "etc/ssl/cert.pem", "_site/index.html", "_site/blog/index.xml", "_site/archive.key", "_site/css/main.css", "_site/sitemap.xml"):
    assert name in files and files[name]["bytes"], name
output = Path(sys.argv[2]) / "site-files.json"
output.write_text(json.dumps(files, sort_keys=True, indent=2) + "\n")
print(f"{len(files)} actual files, {sum(item['bytes'] for item in files.values())} bytes")
