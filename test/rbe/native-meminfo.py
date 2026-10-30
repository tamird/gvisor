"""Read /proc/meminfo in three native memory cgroups, recording actual membership."""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

root = Path('/sys/fs/cgroup')
result = Path(sys.argv[1])
def mem_total(buf):
    return int(re.search(rb'^MemTotal:\s*([0-9]+) kB$', buf, re.M).group(1)) << 10

ordinary = mem_total(Path('/proc/meminfo').read_bytes())
records = {}
parent = Path(tempfile.mkdtemp(prefix='gvisor-memtotal-', dir=root))
try:
    (parent / 'memory.max').write_text(str(512 << 20))
    (parent / 'cgroup.subtree_control').write_text('+memory')
    middle = parent / 'pod'
    middle.mkdir()
    (middle / 'cgroup.subtree_control').write_text('+memory')
    try:
        for case, limit in [('unlimited', None), ('larger', 768 << 20), ('smaller', 384 << 20)]:
            leaf = middle / case
            leaf.mkdir()
            try:
                if limit is not None:
                    (leaf / 'memory.max').write_text(str(limit))
                limits = [(cg / 'memory.max').read_text().strip() for cg in (parent, middle, leaf)]
                rfd, wfd = os.pipe()
                def join():
                    (leaf / 'cgroup.procs').write_text(str(os.getpid()))
                    os.write(wfd, Path('/proc/self/cgroup').read_bytes())
                try:
                    value = mem_total(subprocess.check_output(['/bin/cat', '/proc/meminfo'], preexec_fn=join, pass_fds=(wfd,), timeout=5))
                    os.close(wfd); wfd = -1
                    member = os.read(rfd, 4096).decode().strip()
                finally:
                    os.close(rfd)
                    if wfd >= 0: os.close(wfd)
                assert member == '0::/' + str(leaf.relative_to(root)), member
                assert limits == [str(512 << 20), 'max', str(limit) if limit else 'max'], limits
                assert value == ordinary and value > (768 << 20), (case, ordinary, value)
                records[case] = dict(memory_max=limits, cgroup=member, MemTotal=value)
            finally:
                leaf.rmdir()
    finally:
        middle.rmdir()
finally:
    parent.rmdir()
result.write_text(json.dumps(dict(kernel=platform.release(),hostMemTotal=ordinary,cases=records),indent=2)+'\n')
print(result.read_text())
