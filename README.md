# gVisor work map

An interactive map of Tamir's open `google/gvisor` pull requests, selected
working branches, and external capacity issues.

**Site:** https://tamird.github.io/gvisor/

This orphan `gh-pages` branch contains only the static site. It does not change
upstream source, the fork's default branch, or its build workflows. GitHub Pages
publishes the root of this branch with `.nojekyll`; there is no package install,
bundler, scheduled job, database, or authentication in the browser.

## Keeping the map current

Edit [`registry.json`](registry.json) when a dependency, pending branch, or
qualification blocker changes, update `meta.updatedAt`, and push this branch.
Pages republishes it automatically. The registry is both the dependency source
and an offline snapshot; keep source links and the distinction between observed
facts and unresolved causes in every relationship.

- `nodes` have a stable `id`, a `type` (`pr`, `branch`, or `issue`), a title,
  HTTPS source URL, workstream `group`, and status. PR and GitHub issue records
  also carry `repo` and `number`; branch and PR records carry `ref`.
- `edges` point **from prerequisite to dependent**. `depends_on` is an explicit
  dependency; `blocked_by` records an external blocker. `includes` relationships
  only identify work included in an integration branch, not a required PR stack.
  Include a plain-language `reason` and a public `evidence` URL (or URL array).
- Grouping is for navigation. Sharing a group or Git ancestry does not create an
  edge. No recorded dependency is not a claim that an item is ready to merge.
- Keep deployment qualification separate from issue state. A provider closing
  an issue does not establish that a fix is deployed or that a workload passed.
- Remove obsolete branches manually. Do not infer retirement from a missing
  branch lookup or silently replace a dependency with a similarly named item.

### GitHub attribute snapshot

Run this from the Pages checkout with the existing authenticated GitHub CLI:

```sh
python3 update-status.py
```

Run it after registry edits, then review and commit the resulting
`github-status.json` with those changes. The browser requires matching registry
dates and falls back honestly during a partial deployment. This is
a public-data update, not a build. The script needs Python 3.10+ and `gh`; it
never reads or writes credentials. Its GraphQL queries fetch the author's open
PRs, previously discovered/tracked closed PRs, and linked capacity issues.
Previously discovered PR identities stay in the snapshot after closure, so a
promoted branch does not reappear. One measured update fetched
81 original PRs and ten verified import PRs in 21 requests. PR detail
queries use batches of five to limit the combined check, review and timeline
payload. There is a 32-request ceiling and a 200-open-PR bound.
Failed queries report the GitHub CLI diagnostic and leave the previous snapshot
unchanged.

The updater is the sole GitHub status/attribute owner. Each PR records its exact
head SHA, observation time, GitHub review decision, labels, merge state, unresolved
review threads, and visible check/status rollup. Nested lists are bounded to 100
entries; incomplete thread counts, label lists, check details and import lookups
are explicitly marked. A null review decision is **not reported**, not approval.
A `ready to pull` label is displayed as a label, not a merge-readiness decision.

The snapshot also retains public top-level comments, review bodies and inline
replies by their GitHub IDs, with authors, timestamps, reviewed commits and
thread resolution state. The updater prints new or changed feedback from other
authors by comparing these records, including feedback inside approvals. An
older snapshot without feedback records triggers a backfill; its observation
time never implies that earlier feedback was read or addressed. Unchanged
records do not repeat, and incomplete feedback connections are explicitly
reported. Review/comment lists retain the latest 100 entries per connection;
the existing 100-thread bound still applies. These records do not imply that a
reviewer's concern has been resolved.

Copybara import links require a same-repository PR by `copybara-service` whose
body contains the exact `FUTURE_COPYBARA_INTEGRATE_REVIEW` footer for the original
PR URL and source owner/branch. The footer SHA is retained. An older source SHA
is labeled **older source**; its checks do not validate the current original
head. Multiple verified imports are all shown. No matching complete timeline
means **No linked PR found**, not “not imported”: an internal Copybara CL/status
can exist without a public import PR. Titles, labels and comments never invent
an import relationship or graph edge.

Original PR checks and import PR checks are separate. The API's check/status
rollup is **not a test-case result**. Details distinguish success, skipped,
neutral, pending and failure results and retain available HTTPS check links.
The site does not claim to have read logs or passed tests from a green rollup.
No rollup means unavailable. Both original and import rollups must match their
own returned head SHA before publication.

The browser loads this single published snapshot; **Reload snapshot** downloads
both the registry and GitHub snapshot without querying GitHub or changing the
observation time. A tab open across a deployment adopts the matching pair
together. Mismatched published revisions leave the last good view and cache
intact, with an explicit warning; a later reload can recover. The footer
always shows the snapshot time and warns when it is over two hours old. A local
cache can preserve that same snapshot during an outage. If neither published
nor cached attributes are available, the curated registry remains usable and
attributes say they are unavailable. Refreshing the page never makes old data
fresh. There are no API keys, analytics, third-party scripts or remote fonts in
the browser.

Reloading preserves graph pan/zoom, table and details scroll, expanded check
lists, selection, focus, filters and sorting. A removed item clears its selection;
promoted branches retain it under the PR identity. Initial display, filter/focus
changes and explicit Fit/Reset controls still position the graph.

New PRs replace matching curated branches and inherit their relationships.
Merged/closed PRs hide by default; **Show resolved** reveals them. Other external
evidence and deployed-worker qualification remain curated. No source or import
check status changes a dependency edge or establishes deployed capacity.

### Interface

The dashboard has two views. **Dependency DAG** opens on all active tracked
work, including independent PRs and branches. Connected chains retain their
left-to-right prerequisite order; independent nodes pack around them across a
canvas shaped for the viewport. Labels start at readable size. Drag to explore
when the graph exceeds the viewport, or use Fit for the complete overview.

Select a node for details; its separate native ↗ link opens the source. The
focus selector and **Focus dependency chain** narrow the graph to one chain.
For an independent item, **Focus item in DAG** shows that item. The action is
labeled as already showing the chain or item when it is focused. Choose
**All work** or Reset to restore the complete graph.

The compact **Most blocking PRs** rail counts unique downstream open PRs,
including drafts, over the complete active model. Filters and chain selection
do not alter these counts. A diamond counts a descendant only once. The direct
count is separate; `includes` membership never contributes. Merged and closed
PRs, and explicitly resolved blockers, cut paths. A closed provider issue whose
deployment is unverified remains a blocker. External capacity is listed
separately; branch-only work does not inflate PR counts. Reach is not severity,
effort, or merge readiness.

**Table** contains all tracked work, including isolated PRs and branches. It
starts sorted by blocking reach; click the Item, Title, Blocks, Direct, Status,
Review, PR checks, or Workstream heading to change sort order. Click the item identifier to open
its source, or its title to inspect details. Details consume space only while
an item is selected and preserve all curated relationships, including integration
membership, reasons, and evidence links. The compact Review, PR checks and
Import columns expose the PR snapshot; details include labels, merge conflicts,
review threads, exact revisions and each import’s own checks. DAG cards keep
their height: small R/C indicators and hover text summarize review/check state.

Search titles, numbers, refs, or summaries; filter by workstream. In DAG, matching
chains retain their dependency context, and independent matches appear as nodes. The visible ↗ on every node is a native source anchor,
supporting keyboard activation, new tabs, and normal browser link actions.
The separate node control selects details. Relationship titles navigate the
selection, with an adjacent source link.

Drag the graph to pan; use arrow keys with the graph focused, zoom controls,
or Control/Command + wheel. `/` focuses search and Escape closes details.
Selection is stored in the URL fragment. A recorded cycle is reported instead
of inventing a topological order. HTML references versioned JS and CSS URLs so
a redesigned document does not reuse stale assets from an earlier layout.

## Maintenance checks

No build is required. Before publishing, inspect changed relationships against
the linked sources, ensure IDs are unique and edge endpoints exist, and check
JavaScript syntax. Use the hosted browser check to exercise search, filters,
selection, focus, Table sorting and switching, native links, snapshot fallback, PR attributes and verified import links. Confirm
the served registry, GitHub snapshot and source files match the published commit.

GitHub Pages is configured for `gh-pages` at `/`. Keep deployments on this
branch. Enabling Pages does not require changing the repository's default
branch or granting additional repository access.
