# Coverage remaining-scope capability pilots

Source: `7eafb7afe2c0255c98392c2e701ec6aecad22b27`; exact parent/control: `1f33fe3003455db178b97f3b43d3a012f1611994`.

Root approved native AMD64 RACE (120 minutes plus 180-second termination grace) and native ARM64 NORMAL/RACE (150 minutes plus the same grace). These are capability and volume pilots, without warmups or statistical claims. The separately approved AMD64 NORMAL n24 study remains separate.

AMD64 exercises StartupEmpty and the seven existing syscallbench leaves. ARM64 exercises portable StartupEmpty and both existing Hackbench modes, with native runc controls before building coverage runtimes. Image architecture and actual worker architecture must match. syscallbench is x86-only and is not cross-compiled as an ARM substitute.

Builds use canonical Bazel targets and complete opaque release filesets. Exactly the two coverage binaries replace their corresponding release slots. BASE old-Go inputs are hash pinned and every intercepted download is audited. The original BASE race interpreter/link behavior is preserved and recorded, rather than forced static. A single static BASE benchmark harness and its complete materialized runfiles are frozen before measurement.

Runtime names are task-specific. Each mode uses the same two active filesystem paths, complete fileset checks, idle Docker checks and a bounded daemon-config plus Dockerinfo readiness predicate. Each result requires the exact expected leaf set, positive iteration counts, finite positive measurements and the native GOARCH header. Missing, failed or skipped cases fail the pilot. Hackbench additionally requires positive execution-time metrics.

Cleanup removes only task runtime containers; native-control cleanup is limited to the two benchmark name prefixes after the native-started marker. It restores the original Docker configuration bytes/ownership/mode or original absence, restarts Docker, restores the exact candidate source with a clean tracked tree, then shuts down Bazel. Cleanup command caps including kill grace total 157 seconds, within the 180-second outer grace; any failed restoration fails the job. Original failed attempts remain evidence. No project build or test was executed locally to prepare these scripts.

Metadata fixtures are recorded outside the plan in `coverage-modes-pilot-fixtures.json`: actual AMD64 log shapes and synthetic ARM/ELF metadata only. They do not establish ARM execution or benchmark performance. Sufficient paired sampling must be proposed after these pilots establish actual native cost.
