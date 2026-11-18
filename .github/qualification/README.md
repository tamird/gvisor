Checkpoint inline-format negative control

SOURCE d2c5c08fa3753bcf14ea6015fad547ee8f3d531c; BASE 1f33fe3003455db178b97f3b43d3a012f1611994. Existing SOURCE owners already passed 159 cases including these six positive Statefile.writeTo fixtures (44230748413). The live paired control was inconclusive because BASE passed.

This plan executes only the six exact positive synthetic fixtures against unchanged BASE production source. It trims later SOURCE tests that refer to new APIs and removes only unused imports, adds only the fixtures' direct test dependencies and the exact narrow protobuf visibility already present in SOURCE, and verifies both transformations before building. It does not patch a production decoder or add a compatibility wrapper. Actual XML must show four legacy/split/generic positives and exactly two inline failures at the BASE writeTo boundary; build failures or unrelated test failures fail the control. Original BASE test/two-BUILD bytes restore before SOURCE checkout restoration. Cleanup ignores repeated TERM/INT, bounds all three copies at5s each, switch at25s and both diffs at6s each (52s total command bounds, below90s grace). No project builds or tests run locally.

Expected remote duration 10–20 minutes; hard qualification-script cap60m plus90s kill grace (existing workflow preparation is separate). Initial44243638873 stopped during Bazel analysis because the first plan omitted protobuf visibility; no tests ran and that failed receipt remains retained.

One forwardcmd owner target, nocache/one attempt/no sharding, three build jobs. No full container suite or full runtime release is rebuilt. This is a synthetic original-defect check, not an exact live-CI reproduction, a performance study, or evidence of a cause for the sampled unreachable CI failures.
