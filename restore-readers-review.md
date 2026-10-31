@EtiennePerot, @milantracy: I tried `POSIX_FADV_WILLNEED` on the local
state file with the existing reader. On a cold checkpoint it saved about
3% of kernel load time in paired rounds; buffering saved 41%. Here are
the cold rows from [benchstat][1] (milliseconds, 18 restores per column):

```
             │    direct    │               advice                │                buffer                │
             │ kernel-ms/op │ kernel-ms/op  vs base               │ kernel-ms/op  vs base                │
Restore/cold     777.3 ± 4%     751.0 ± 3%       ~ (p=0.091 n=18)     450.5 ± 3%  -42.04% (p=0.000 n=18)

             │   direct   │              advice               │               buffer               │
             │ cli-ms/op  │ cli-ms/op   vs base               │ cli-ms/op   vs base                │
Restore/cold   809.4 ± 4%   784.1 ± 2%       ~ (p=0.091 n=18)   489.2 ± 3%  -39.56% (p=0.000 n=18)
```

With all three input files **warm**, benchstat didn't distinguish the
readers. Comparing buffered with direct *within each round*, though,
the restore command was slower in 15 of 18 rounds: median +2.2%
(95% interval +0.3% to +5.0%). The kernel-load difference was less
clear: +1.3% (−0.6% to +4.9%). This is consistent with the hot CLI
regression in the PR; it doesn't establish a hot kernel-load regression.

The [108 restores][2] used the [same executable][3] on one 4-vCPU
Linux worker, in balanced order, with a 12.2 MiB state file and a
513 MiB pages file. CLI time ends when the background restore command
returns. Memory and pipe contents were checked afterward. This wasn't a
listening Nginx checkpoint or a CPU-quota sweep, so the measurements
don't pick a buffer count for those configurations.

Posted on behalf of @tamird.

[1]: https://tamird.github.io/gvisor/restore-readers-benchstat.txt
[2]: https://github.com/tamird/gvisor/actions/runs/41186223756/artifacts/13037505941
[3]: https://github.com/tamird/gvisor/commit/d3066814d
