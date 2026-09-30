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
  dependency; `blocked_by` records an external blocker. Dashed `includes` edges
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

Merged and closed PRs hide by default; **Show resolved PRs** reveals retained
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

The default **DAG** view lays out active prerequisites from left to right.
Its **Most blocking PRs** ranking counts unique downstream open PRs (including
drafts), over the complete active model, independently of display filters.
The count includes direct and indirect descendants; the direct count is shown
separately. A diamond counts the same descendant once. Only `depends_on` and
`blocked_by` edges contribute; `includes` edges never contribute. Merged and
closed PRs, and explicitly resolved blockers, cut blocking paths. A closed
provider issue with unverified deployment remains unresolved capacity.

External capacity blockers are ranked separately, including a zero open-PR
count when they affect only branches. Branch-only work does not inflate a PR's
score. These counts measure recorded dependency reach, not severity, effort,
or merge readiness. Selecting a ranking focuses its connected dependency chain.
Cycles produce an explicit warning instead of a fabricated topological order.
The grouped **Map** retains integration membership and isolated work, and
**List** offers the complete text view.

Search titles, numbers, refs, or summaries. Filter by workstream and item type,
show connected work only, or use the list view. Select an item to see source
links and incoming/outgoing relationships; **Focus this connected work** follows
the connected component. All visible cards can be reached with the keyboard.

Drag the map to pan; use the arrow keys with the map focused, the zoom controls,
or Control/Command + wheel. `/` focuses search and Escape closes details.
Selection is stored in the URL fragment for sharing. The list and details views
provide text alternatives to the graph's shapes and colors.

## Maintenance checks

No build is required. Before publishing, inspect changed relationships against
the linked sources, ensure IDs are unique and edge endpoints exist, and check
JavaScript syntax. Preview the site over HTTP, then exercise search, filters,
selection, focus, list view, navigation, and unavailable-GitHub fallback. Confirm
the served registry and source files match the published commit.

GitHub Pages is configured for `gh-pages` at `/`. Keep deployments on this
branch. Enabling Pages does not require changing the repository's default
branch or granting additional repository access.
