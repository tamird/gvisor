"""Record real host/user-namespace access; never run a benchmark."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def read_optional(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError as error:
        return f"errno={error.errno}: {error}"


def execute(arguments: list[str]) -> dict[str, object]:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=5)
        return {"arguments": arguments, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except OSError as error:
        return {"arguments": arguments, "errno": error.errno, "error": str(error)}
    except subprocess.TimeoutExpired:
        return {"arguments": arguments, "timeout": True}


def main() -> None:
    helper = Path(sys.argv[1])
    ancestors = []
    for path in reversed(helper.parents):
        entry = {"path": str(path), "traversable": os.access(path, os.X_OK)}
        try:
            metadata = path.stat()
            entry.update(mode=stat.S_IMODE(metadata.st_mode), uid=metadata.st_uid, gid=metadata.st_gid)
        except OSError as error:
            entry.update(errno=error.errno, error=str(error))
        ancestors.append(entry)
    print(json.dumps({
        "uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(), "groups": os.getgroups(),
        "id": execute(["/usr/bin/id"]),
        "procStatus": read_optional('/proc/self/status'),
        "securityContext": read_optional('/proc/self/attr/current'),
        "uidMap": read_optional('/proc/self/uid_map'),
        "gidMap": read_optional('/proc/self/gid_map'),
        "ancestors": ancestors,
        "true": execute(['/usr/bin/true']),
        "helper": execute([str(helper), '/proc/self/ns/net', '/usr/bin/true']),
    }, indent=2) + '\n')


if __name__ == '__main__':
    main()
