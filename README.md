# gVisor work map

An interactive map of Tamir's open `google/gvisor` pull requests, selected
working branches, and external capacity issues.

**Site:** https://tamird.github.io/gvisor/

This orphan `gh-pages` branch contains only the static site. It does not change
upstream source, the fork's default branch, or its build workflows. GitHub Pages
publishes the root of this branch with `.nojekyll`; there is no package install,
bundler, scheduled job, database, or authentication in the site.

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

### Public status refresh

On load, the browser checks a 15-minute local cache. When stale, it requests
Tamir's open upstream PRs from the public GitHub search API, including new PRs.
Known PRs missing from that list are individually checked before being marked
merged or closed. New PR head refs are retrieved to match curated fork branches;
matching open, draft, or merged PRs replace their branch nodes and inherit their
curated relationships. Closed, unmerged proposals leave the working branch in
the registry. Newly discovered work without a matching branch is shown in
**New · not yet grouped** until someone curates it.

Merged and closed PRs hide by default; **Show resolved** reveals retained
resolved entries. Prerequisite details still show their resolved status. Draft
status is preserved. Review decisions, where present, are explicitly snapshot
values rather than a live review assessment.

The same refresh checks linked GitHub issues. Other external evidence, such as
worker invocation results, remains the curated snapshot. Relationships are
never generated from titles, shared groups, or branch ancestry.

A refresh permits at most 30 anonymous requests, each with a 10-second timeout,
and at most 200 open PRs. It rejects incomplete search inventories instead of
marking missing PRs closed. Individual lookup failures preserve saved states and
show a partial-refresh warning. A failed top-level request leaves the last
snapshot usable. Manual refresh has a one-minute cooldown. There is no polling,
credential, API key, analytics, third-party JavaScript, or remote font. Rate
limits therefore degrade freshness rather than access to the map. Public API
status is cached in this browser only and can be removed by clearing site data.

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
or Workstream heading to change sort order. Click the item identifier to open
its source, or its title to inspect details. Details consume space only while
an item is selected and preserve all curated relationships, including integration
membership, reasons, and evidence links.

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
selection, focus, Table sorting and switching, native links, and unavailable-GitHub fallback. Confirm
the served registry and source files match the published commit.

GitHub Pages is configured for `gh-pages` at `/`. Keep deployments on this
branch. Enabling Pages does not require changing the repository's default
branch or granting additional repository access.
