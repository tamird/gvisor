"""Describe only allowlisted error words and source locations from the stock log."""

import re
from original_diagnostics import guest_log


WORDS = set('''test failed failure fail exited exit status signal fatal panic exception error errorf executing execution return child parent outer runner sandbox kernel stdout stderr error string context deadline exceeded timed out timeout killed interrupted abort aborted unexpectedly unexpected EOF no such file directory permission denied invalid argument killed force root cause checkpoint restore save loading saving restoring state stateify unsupported unregistered unexpected type object channel notifier value interface cannot unable failed encode decode read write blocked poll epoll overlay read readiness registration ptrace systrap memory ready result condition assert assertion waited wait process command exited nonzero killed log died closed error returned got want expected pid signal bus segfault stack pop restore runsc checkpointing stopped crash crashlog invalid wait graphics serialization serialize serialize saving namespace missing unreachable unimplemented implemented route stale context unsupported Runtime error package snapshot exiting while to the is but a an of from to has not as by for in on and or with before after must only support supported cannot check filesystem address file filedescriptor function functionpointer data cycle panic nil reference pointer unserializable unsaved serializable struct int object registered field fieldinfo frame container spec tests'''.lower().split())
MARKER = 'OVERLAY_POLL_READY'
text = guest_log('d67e3288-8eb2-480d-b8ad-7d6e47c3a123')
assert len(text.encode()) == 11534
rows = text.splitlines()
starts = [i for i,line in enumerate(rows) if MARKER in line]
print('stock log lines',len(rows),'pre-save marker lines',[n+1 for n in starts]);
for i,line in enumerate(rows):
    low=line.lower()
    if not ('fail' in low or 'error' in low or 'fatal' in low or 'panic' in low or 'exit' in low or 'restore' in low or 'save' in low or (starts and i>=starts[-1])):
        continue
    if re.search(r'api[_-]?key|credential|authorization|bearer|passwd|secret|token|gh[pousr]_',low):
        continue
    words=[w.lower() for w in re.findall(r'[A-Za-z_]+|\d+',line)]
    allowed=[w for w in words if w in WORDS or (w.isdigit() and len(w)<=3)]
    loc=re.findall(r'(?<!\w)([a-z_][a-z0-9_]*\.(?:go|cc|h):[0-9]{1,5})(?!\d)',line)
    types=re.findall(r'(?<!\w)(?:waiter\.ChannelNotifier|gvisor\.dev/gvisor/pkg/[a-z0-9/]{1,100}\.[A-Za-z]{1,70})(?!\w)',line)
    if allowed or loc or types: print(i+1, ' '.join(allowed[:60]), ' '.join(loc[:3]), ' '.join(types[:2]))
