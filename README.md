# gVisor and BuildBuddy work map

An interactive map of Tamir's open `google/gvisor` and `buildbuddy-io/buildbuddy` pull requests, selected
working branches, external capacity issues, and branchless investigations.

**Site:** https://tamird.github.io/gvisor/

This orphan `gh-pages` branch contains only the static site. It does not change
upstream source. GitHub Pages publishes the root of this branch with `.nojekyll`.
The fork's default branch owns the scheduled `Refresh work map` workflow;
there is no package install, bundler, database, or authentication in the browser.

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

The everyday views hide inactive history. **Show history** includes delivered
branch records, withdrawn or superseded proposals, merged/closed PRs and
concluded investigations. Its shareable URL is `?view=table&history=1` (also
supported in the DAG and Investigations views); old `resolved=1` links still
work. A direct link always exposes its selected historical record. Dependency
details retain historical relationships without counting them as live blockers.

Use `inactive: true` for a curated branch, external or other record that has no
remaining action of its own. Do not infer inactivity from words such as
"delivered" inside freeform status or follow-up prose: a release or validation
may still be pending. Public open PRs, explicit contributor decisions, changes
requests, human submissions and `Agent work` remain visible even when an old
record carries this flag. Keep ongoing work in its current owning record, and
clear the flag when reactivating a historical record.

Issue visibility follows the provider's open/closed state, including reopening,
just as PR visibility follows its current source state. A closed request is
history even if old notes say "deployment unverified"; its closure reason is
shown without implying that a capability was deployed. Record unfinished
validation in its existing agent-owned investigation or consumer, rather than
keeping the closed provider request as a live blocker. Historical dependencies
remain reachable from details and deep links. A new reply decision can expose
a closed record without reactivating its dependency edges. The collector
refreshes all registered issues in bounded batches. A missing or relocated
issue retains its last observation with an explicit unavailable note while
other records refresh; a partial inventory is rejected. Reconcile the affected
identity before assigning it a new provider state.

Repository policies in `meta.repositories` select the existing Copybara workflow
for gVisor and ordinary direct PR review for BuildBuddy. Every live PR carries
its source repository; non-primary PR IDs include that repository, preserving
existing gVisor deep links and preventing equal PR numbers from colliding.
Repository-qualified identity also scopes review history and conflict refreshes.
Selected third-party prerequisite PRs can be curated like other PRs. Discovery
adds only the configured contributor's open PRs; other repository work is not
imported automatically. Repositories in `meta.registeredOnlyRepositories` use
ordinary direct PR review and fetch only their registered repo/number pairs on
each refresh, including terminal rows so reopening remains visible. They do not
run open-PR discovery. Their inventory scope is explicit, with no claimed
repository-wide open count.

An investigation with a `group` also appears as a dashed blue graph card. It
remains the same research record in the Investigations view, can be an edge
endpoint, and links there until it has a published branch or PR. Do not invent
a branch link before publication. Move relationships to the published node
when that identity is established, retaining findings in the investigation.

### GitHub attribute snapshot

Run this from the Pages checkout with the existing authenticated GitHub CLI:

```sh
python3 update-status.py
```

The scheduled [Refresh work map workflow](https://github.com/tamird/gvisor/actions/workflows/work-map.yml)
is configured for every 10 minutes (GitHub may delay or omit scheduled jobs) and also supports manual
dispatch. It uses the repository's `GITHUB_TOKEN` to read public metadata, then
validates it with the unchanged client validator in `metadata-preflight.mjs`.
Only a complete, valid `github-status.json` is committed. If a site update
lands during collection, the workflow checks out that revision and collects
again, up to three attempts. It never merges a snapshot collected against an
older registry or forces a push. Continued site changes defer collection to
the next scheduled refresh; authentication and unchanged-head push failures
remain errors. Scheduled and manual runs share one concurrency group. During active
owner monitoring, check snapshot age at the same ten-minute cadence; if stale
and no refresh is active, dispatch this workflow once. This uses the existing
collector and does not create a second scheduled monitor. The token never reaches
the browser. Its commit
explicitly requests the existing Pages build because token-authored pushes do
not trigger Pages builds automatically. Six scheduled builds per hour stay
below Pages' soft ten-build limit; manual dispatches and source publications
also consume that allowance. Failed collection/publication keeps the previous
snapshot visible with its original timestamp; workflow failures are visible at
the link above. The collector records actual GraphQL cost and remaining budget.

Manual collection uses the same script and is only needed for development or
recovery; do not run a competing periodic publisher. Run it after registry edits,
then review and commit the resulting
`github-status.json` with those changes. After the final commit, require a clean
tracked tree and run the current browser validator in the same command as the
push:

```sh
git diff --quiet HEAD -- . &&
node metadata-preflight.mjs . &&
git push origin HEAD:gh-pages
```

Keep both checks when using an authorized alternate push transport. An earlier
preflight receipt does not validate later edits; refresh the paired snapshot
after registry changes and repeat this final check. The browser requires
matching registry dates and falls back honestly during a partial deployment.
This is a public-data update, not a build. The script needs Python 3.10+ and
`gh`; it never reads or writes credentials. A complete repository-wide open-PR
inventory discovers new author PRs and reopening of any tracked source or import. Detail
queries refresh open PRs, previously active PRs whose closure needs confirming,
newly tracked PRs, and their active or newly discovered imports. Known active
imports are refreshed even when their source PR is closed. Linked capacity
issues are also refreshed. Terminal history remains in the snapshot with its
original per-record observation times, so a promoted branch does not reappear
and old reviews or checks do not acquire a fresh timestamp. Reopened records
receive full detail queries again. PR detail
queries use batches of eight to limit the combined check, review and timeline
payload. There is a 48-attempt ceiling, including retries, and a 200-open-PR bound.
Failed queries report the GitHub CLI diagnostic and leave the previous snapshot
unchanged.

State ages use their own `observedAt` clock. PR states use that PR's service
observation; an independently recorded terminal-status read may establish a
newer merge or close time. Contributor decisions use the registry observation.
These clocks preserve first-observed dates across metadata-only updates without
advancing the full GitHub snapshot or claiming fresh review-thread evidence.
Older records without `observedAt` retain the full-snapshot clock. Run the clock
and classification checks with `python3 -m unittest work_states_test update_status_test` and
`node --test app_test.mjs`.

The updater owns the complete GitHub status/attribute snapshot. Each PR records its exact
head and base SHAs, observation time, GitHub review decision, labels, merge state, unresolved
review threads, and visible check/status rollup. Nested lists are bounded to 100
entries; incomplete thread counts, label lists, check details and import lookups
are explicitly marked. A null review decision is **not reported**, not approval.
A `ready to pull` label is displayed as a label, not a merge-readiness decision.

The snapshot's `publicImportDisposition` binds the selected public exports to
the current source head and any verified equivalent approval. Exact-head
successors take precedence over older exports. The site and periodic reminder
report use this same decision: a skipped review reminder does not mean an
export is current. An older open export does not replace approval of the current
source head, even when the source rebase preserves its patch or old public checks
pass. The table asks for current-source reapproval and the ordinary alarm asks the
approving maintainer to reapprove so Copybara regenerates its import. A genuinely
fresh current-head approval waits for automatic replacement. Closed, unmerged
exports need re-import, while their unresolved public CI evidence stays visible.
`publicImportHandoff` is diagnostic only. The reminder planner separately
recognizes a previously applied `ready to pull` label that is now missing:
complete label history, a qualified human approval of the current or verified
equivalent patch, closed exports, and no live successor are required. It asks
the approving maintainer to restore the import trigger, not to repeat code
review. Present labels, current successors, source conflicts and incomplete
inventories suppress this reminder. Before posting it refreshes both label
history and export lineage; the same per-PR cooldown covers both reminder kinds.

Fresh-head approval requires a still-effective review of the exact head,
submitted no earlier than its commit time and any observed force-push introducing
that head, with retrieved latest force-push metadata. GitHub can
reassociate old reviews during a rebase; changing the review commit or `updatedAt`
does not constitute a new review. Missing time evidence in an older cached
snapshot remains readable but supplies no approval credit. Approval history shows
submission time and GitHub’s recorded commit association separately.

### gVisor patch-preserving rebases

- Maintain an approved patch's base quietly when its actual master merge-base is over five hours old, a conflict is verified, or a merged prerequisite requires an update; do not repeatedly rebase a recent head or interrupt an active matching Copybara import merely because master advances.
- Before publication, verify the old approved head, complete topic change, latest feedback, explicit holds and import state; establish patch equivalence with stable patch IDs and range-diff, independently reviewing any context-only difference.
- A mechanical rebase does not require another review request or announcement solely because its commit SHA changes; comments solicit human attention and do not trigger Copybara automation.
- Preserve the original approval's author, head, time and review ID separately from verified patch equivalence and exact-source import progress; GitHub reassociating an old review with a new head is not a fresh review.
- Continue review for substantive changes, unresolved findings or an explicit reviewer request; a rebase never clears license, dependency or maintainer holds.
- Let an exact-source active import proceed through its checks, diagnose concrete failures in their owning workflow, and avoid redundant reviewer nudges or rebase churn; import progress is not a guarantee of merge or permission to merge.

The October 19 cohort supplied five merged examples:
[#15500](https://github.com/google/gvisor/pull/15500),
[#15541](https://github.com/google/gvisor/pull/15541),
[#15575](https://github.com/google/gvisor/pull/15575),
[#15935](https://github.com/google/gvisor/pull/15935) and
[#15964](https://github.com/google/gvisor/pull/15964). Their approved source,
rebased source and actual Copybara merge had identical stable patch IDs, without
new public approval. This supports the operational distinction above; it does
not document Copybara's internal policy or establish that a rebase caused every
observed progression.

### Reapproval after amendments

The existing refresh workflow can remind prior human maintainer approvers with
🚨 at most once every three hours, weekdays from 09:00 until 17:00 America/Los_Angeles. It applies
only to open, non-draft `google/gvisor` source PRs. Status, human-feedback and
conflict discovery retain the existing ten-minute refresh cadence. One fresh current-head human
maintainer approval stops this alarm even if the aggregate review decision or
pending review requests still prevent merge readiness. Current-head maintainer
changes requested require source work first. A dismissed review qualifies as a
former approval only when GitHub's
review-dismissal event records its previous state as approved.

Posting also requires fresh `MERGEABLE` status. Conflicts defer to source work;
unknown mergeability gets one bounded, head-checked reread per PR, using the
existing eight-PR batches and request ceiling, and still defers if unresolved.
Every normal sweep reports the full
current conflict/unknown inventory and its changed or resolved observations,
including source/import heads and assigned follow-up. No reminder asks a
maintainer to approve a revision that the fresh provider read reports conflicted.

Delivery owners record exact `reapproval` evidence in the same registry update as
each authorized source publication:
the approved and amended heads, `kind: content-amendment`,
`method: reviewed-source-diff`, the SHA256 of the complete reviewed source diff,
a concise change summary, and its public evidence URL. Compare the complete
topic patches and preserve changed bytes, whitespace, modes and paths; a
different commit SHA or patch ID does not establish an amendment. Verify the
single-commit shape before using parent-to-head patches. The same reviewed
evidence with `kind: patch-preserving-rebase` excludes a proven equivalent
rebase only from an amendment-review alarm when there is no stale open export;
it does not suppress current-source reapproval for an older open export or
import-trigger recovery. It does not restore a dismissed approval or remove
an explicit maintainer re-review request. Unknown changes are reported as `needs-evidence` for
the delivery owner to reconcile, never guessed from stale approval alone.

`python3 update-status.py --reapproval-reminders plan` prints the concrete
selection and comment text without writing to GitHub. The `post` mode verifies
the configured owner credential and refreshes each candidate's head, review
history and comments immediately before posting. The comment links the exact
old and amended commits and retains delegated attribution. Its hidden marker
and GitHub creation time enforce three full hours between reminders, including
across head changes and reminder purposes; no separate reminder database is kept.
Posting is limited to weekdays, 09:00–17:00 America/Los_Angeles with DST.
The ten-minute Actions cron is best-effort scheduling, not a guaranteed
execution interval; a missed runner slot does not reset the cooldown. Mutations are never
automatically retried after an ambiguous response. GitHub has no atomic
expected-head condition for issue comments, so the post-read reports any
observed head or closure race explicitly.

Scheduled posting requires the dedicated `WORK_MAP_COMMENT_TOKEN` secret for
the configured owner with permission to comment on these source PRs. Without
it, the workflow only plans reminders and reports posting disabled. The normal
repository token continues to collect public metadata. Foreground authorized
posting can use the existing local GitHub CLI credential and the same code;
do not start a second periodic monitor or copy that credential into Actions.

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

Each refresh puts a compact `reviewerHandoff` before routine check output and
retains it in the snapshot. It routes new human comments, reviews and replies
on source and linked import PRs to the source's existing `followup.owner`;
a missing owner requires assignment. An
unresolved thread remains in this handoff while its latest message is from
the reviewer; bots do not decide who owes the next reply. An author reply removes that pending-answer signal without
claiming the thread is resolved; a later reviewer reply raises it again.
Edits to earlier human messages surface again even after an author reply.
New top-level comments and review bodies remain in the same snapshot
across refreshes until an exact feedback disposition or explicit assignment
resolution records human triage. Dismissed reviews clear. This does not
backfill every historical top-level message. An assigned `actionableComment`
continues to use the existing explicit assignment mechanism.

After each repository completes, the log also emits `reviewerHandoffProvisional`
with `requiresLiveRecheck`. If later collection or validation fails, recheck
that exact PR head and complete thread live before acting on the provisional
finding. The failed run leaves the published snapshot and its timestamp unchanged.

On each refresh or continuation, handle this handoff before qualifications,
source-CI failures and routine metadata. Read the complete linked discussion,
alert its owner immediately when the existing refresh completes, and update
the existing follow-up with the concrete action.
Do not treat observation as acknowledgement or send automatic replies.
An author reply only changes who owes the next response. Keep the implementation
obligation active until a reviewer-ready fix, necessary substantive question,
justified disagreement or exact disposition resolves it.

After human feedback, drain the same snapshot's `mergeabilityHandoff` before
starting routine qualification work. It retains current-source conflicts,
unknown mergeability and PR/fork-head mismatches on every completed refresh,
even when `changedMergeability` is empty. It also includes current-source
imports; older imports remain in the ordinary diagnostic report. Route each
blocker to its recorded owner, or assign one if absent, and keep the concrete
next action in the active handoff. Do not repeatedly notify an owner already
working on the same evidence. A free-text "waiting for synchronization"
disposition is not resolution: verify the actual PR head, fork head and current
base, and distinguish a source conflict from stale provider metadata. Preserve
the original conflict observation time; a new refresh does not make it new.
The existing collector and its ten-minute fallback own this intake; no separate
poller or automatic public acknowledgement is needed.

Then drain `checkFailures`, retained from the existing check report in the same
snapshot. An empty `changedChecks` list means no transition, not no remaining
work. Check each current source or matching import failure against its exact
head and failing check ID/URL; a named owner or an older free-text disposition
does not establish that this failure has been diagnosed. Keep its concrete
next action in the owner handoff until repaired or explicitly transferred.
Older-source imports, approval-gated checks and internal-only failures retain
their distinct scopes. Do not repeat notifications for an owner already acting
on the same evidence, or publish an empty update merely to acknowledge a sweep.

An assigned top-level comment can be recorded in the existing PR `followup` as
`actionableComment: {head, commentId, url}`, with its assigned `owner`. The exact
head and comment ID/URL must match the complete public feedback snapshot before
it appears under **Review feedback to address**. It also prevents the **Waiting
for merge** label while the assignment remains active, without changing
GitHub's approval. No comment text or follow-up prose creates this assignment. Remove
the record when addressed; a new head requires an explicit carry-forward of
the record's own head, independently of ordinary follow-up metadata updates.

A completed triage can record `followup.attentionDispositions` for a specific
feedback, public-import failure or conflict reason. Each record supplies a title,
next action, public evidence URLs and the exact structured binding produced by
`attentionDispositionEvidence`; a replacement wait also names its explicit
same-repository PR. These records route only the bound reason to **Awaiting
maintainer follow-up**, consistently in groups, filters, counts and copied handoffs.
PR details explain the remaining action. Raw checks, conflicts, review decisions
and unresolved threads are unchanged.

The binding includes complete source review/thread/feedback content and identity,
including actual approval times. Import failures additionally bind current import
heads and exact check identities/results; a replacement wait binds its open state
and head. New replies/findings, changed approvals, runs, imports, source heads or
replacement state invalidate the disposition, as does incomplete or missing
evidence. Observation timestamps alone do not invalidate it. New unrelated reasons
remain actionable, and an explicitly assigned comment always needs author work.
Free-text follow-up notes never create or extend a disposition.

Check Lucas Manning’s nonblocking suggestions daily. With a current-head
approval, record them in the [weekly cleanup queue](https://tamird.github.io/gvisor/?view=investigations#investigation%3Aweekly-review-cleanup),
including the review and original thread. A change can enter the queue
before its import lands. Keep the approved source head in place, batch
the edits in a separate PR, and reply individually on the original threads
after addressing them. `batch-cleanup` routes only the bound feedback to
the queue; other PR actions and new feedback remain visible. A review
that says “LGTM” without approving the change still gets direct triage.

When the next step depends on a public release, record a `releaseWait`
on the owning PR with its source head, the version checked and the next
verification step. The existing refresh reads the latest non-yanked BCR
module version or published GitHub release. The row stays under **Waiting
for an upstream release** until a different version appears. A new version
or a failed lookup is **Check an upstream release** for the agent. Neither
claims that an image has shipped or that the release contains the fix.
New review feedback or a different source head reopens normal triage.

If GitHub reassociates an earlier approval with a rebased head that already
had an export, the agent compares patches before requesting another review.
An export of the verified unchanged patch is shown with its actual outcome:
a closed attempt without a successor needs re-import; an older open or merged
attempt is linked without treating its CI as evidence for the new head.

Copybara import links require a same-repository PR by `copybara-service` whose
body contains the exact `FUTURE_COPYBARA_INTEGRATE_REVIEW` footer for the original
PR URL and source owner/branch. The footer SHA is retained. An older source SHA
is labeled **older source**; its checks do not validate the current original
head. Multiple verified imports are all shown. No matching complete timeline
means **No linked PR found**, not “not imported”: an internal Copybara CL/status
can exist without a public import PR. The existing **Awaiting public export**
filter retains that distinction: its qualifier separates internal import success,
internal check feedback and public validation. An internal failure without a
public log remains maintainer attention, while an actual public check failure
keeps its existing author/agent priority. No service status establishes merge
readiness or a test count. Titles, labels and comments never invent
an import relationship or graph edge.

Every refreshed tracked PR also inspects the latest 100 cross-reference events
and retains public PR sources with their author, body, URL, head and event time.
Private or unavailable sources are omitted from both the snapshot and log. These
links appear in PR details; new or changed references are printed by the existing
collector for owner triage. A missing prior inventory triggers a backfill, and
truncated timelines remain explicitly incomplete. Known terminal records keep
their prior observation until reopened. References do not add tracked PRs or
infer supersession, dependencies, approval or resolution. Those decisions still
require explicit evidence and a registry update; Copybara's exact-footer rule
remains separate.

A diagnosed failing import requires an assigned action until a verified
correction is delivered or a concrete upstream report reaches an identified
owner. Reporting transfers the next action; it does not mark the failure repaired.
Record the failed import head and job, the fix or report link, the responsible
owner and the remaining action in the existing PR follow-up. An older source
marker or import-only drift explains applicability; neither closes the defect.
Retain that follow-up when the source advances, while keeping the old check
separate from current-head validation. Check undiagnosed failures before
claiming that all remaining work is externally held.

Original PR checks and import PR checks are separate. The API's check/status
rollup is **not a test-case result**. Details distinguish success, skipped,
neutral, pending and failure results and retain available HTTPS check links.
The collector identifies the repository's Auto Assign/assign CheckRun by its
GitHub Actions app slug and workflow. Its reviewer-assignment result remains
visible, but an explained administrative failure does not classify contributor
source as failing. Unknown checks and incomplete inventories stay conservative;
merge readiness still requires the raw complete green rollup.

The site does not claim to have read logs or passed tests from a green rollup.
No rollup means unavailable. Both original and import rollups must match their
own returned head SHA before publication.

A curated `ciRecovery` record can identify a diagnosed dependency-download
failure whose retry returned HTTP 403. It binds the source and import heads,
the exact public Actions job and check ID, and any explicitly linked status
IDs. Only those still-failing identities route to **CI configuration or retry**;
other failures remain agent work. Missing IDs, incomplete evidence, changed
heads, or successful/superseding checks invalidate the exception. Raw check
results remain visible. This record never comes from parsing comments or prose.

The same record can identify inspected source-check logs that report a missing
API key. It binds the current source head and each exact Actions check ID and
job URL, routing those failures to **CI configuration or retry** with their
specific authentication need. Multiple jobs may share a name only when every
current same-name job has an exact recorded identity. An extra or changed job
invalidates the exception. The record does not infer why the credential is
absent. Uncovered failures remain agent work. The same identity, completeness
and supersession checks apply.

A `shared-source-fix` recovery binds a diagnosed failure to its remedy PR through
an existing `blocked_by` edge. It records the exact source head and failed source
or matching-import check identities. An open remedy remains a wait. Its merge
releases an inspection action without requiring advance knowledge of the merge
commit. Once ancestry is verified against the actual merge commit, a missing fix
releases a rebase; a contained fix needs no repeat. Missing or mismatched ancestry
remains an inspection, not another wait. Changed heads or superseding/incomplete
check evidence expire this action; ordinary failures and review holds remain visible.
The queue audit lists released actions even when a followup owner is recorded:
an old assignment is not evidence that a newly released action has been handled.

The browser loads this single published snapshot; **Reload snapshot** downloads
both the registry and GitHub snapshot without querying GitHub or changing the
observation time. **Check conflicts on GitHub** in selected PR details makes
one unauthenticated public REST request, bounded to ten seconds, with a ten-second
click cooldown. It updates conflict evidence in that tab without claiming fresh
reviews or checks. Rate-limit/network errors retain the displayed evidence and
link to the PR; no automatic per-PR browser polling occurs (GitHub limits
unauthenticated REST to 60 requests/hour per IP). If the head changed, approval,
checks and import eligibility become unavailable until full collection catches
up. A newer full snapshot supersedes the tab's older response.

Concrete mergeability observations retain head, base and time. GitHub's
`UNKNOWN`/null response means it is computing mergeability, not that a conflict
has cleared. The Conflicts filter retains a same-head prior conflict, explicitly
labeled **last observed conflict** until a concrete response replaces it; details
show the original base and date. A prior result for another head is history only.
Older snapshots may not have recorded the base; it remains unknown. A new
MERGEABLE result clears the conflict. No unknown response grants merge readiness.

A tab open across a deployment adopts the matching pair
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
Inactive records hide by default across all views; **Show history** reveals them.
Direct links still open historical records and their dependency context. Other
external evidence and deployed-worker qualification remain curated. No source or import
check status changes a dependency edge or establishes deployed capacity.

### Interface

The dashboard has three views. **Dependency DAG** opens on all active tracked
work, including independent PRs and branches. Connected chains retain their
left-to-right prerequisite order; independent nodes pack around them across a
canvas shaped for the viewport. Labels start at readable size. Drag to explore
when the graph exceeds the viewport, or use Fit for the complete overview.

Select a node for details; its separate native ↗ link opens the source. The
focus selector and **Focus dependency chain** narrow the graph to one chain.
For an independent item, **Focus item in DAG** shows that item. The action is
labeled as already showing the chain or item when it is focused. Choose
**All work** or Reset to restore the complete graph.

Blocking reach remains on graph cards, in the table and in item details. It
counts unique downstream open PRs over active dependency edges, including
drafts; integration membership does not contribute. This is not a severity,
effort or merge-readiness score. There is no separate ranking section.

The state filters overlap: select several to match any. Counts follow search
and workstream, independently of the other state buttons. The collector owns
these classifications in `work_states.py`, and the UI consumes its snapshot:

- **Contributor review:** an actual pending decision recorded by the task owner
  in the registry's `contributorReview` object (`status: pending`, exact candidate
  `head` and commit `url`) on an unpublished branch node. Independently reviewable
  fixes each have their own branch row, with no inferred dependency between them.
  An amendment to an existing PR may instead use that same PR row with
  `kind: amendment`, the exact proposed `head` and fork commit `url`, and
  `baseHead` identifying the published revision it replaces. Include a specific
  `title`, `summary` and complete reviewable `body`; a different proposed head is
  expected, while a change from `baseHead` prompts reconciliation. Outgoing
  comments and reviews use the owning PR or issue row with
  `kind: reply` or `kind: review`, a specific `title`, `summary`, original discussion
  `url`, and the complete reviewable `body`. PR actions retain the reviewed `head`;
  issue replies do not invent a commit. These explicit decisions use the registry
  observation clock and remain separate from GitHub's incoming review, source
  head, CI and import states. A changed public head prompts reconciliation rather
  than silently deleting the outstanding question. Text and links appear in the
  existing Table/details path and the **Contributor decisions** attention filter.
  When asking Tamir for a decision, record it here and link the request to its
  canonical tracker item with the exact proposed commit or reviewable reply.
  Verify that the item appears in the contributor filter before considering the
  request handed off. When answered, remove the pending object and record the
  disposition and remaining prerequisites on that same item. GitHub review and
  prose alone do not infer a contributor hold.
- **Maintainer review:** an open, nondraft PR without approval or changes
  requested, including GitHub's null review decision (no approval reported).
- **Prepared proposals:** unpublished candidate branches awaiting fresh scope
  reconciliation, without an outstanding contributor decision. The live PR's
  **Proposed updates** links open the exact public candidate; its return link
  opens the live PR. These use the existing nonblocking `includes` edge type
  with `role: proposed_update`; older clients show them as inclusion links.
  Both detail views offer **Copy range-diff**, using the published PR head
  from the GitHub snapshot and the exact proposed head. The command is also
  selectable for manual copying. It disappears when the heads match or
  either head is unavailable; a snapshot refresh updates the published head.
  Refinements without a PR use the same copy control in their detail view.
  Record `refinement.before` and `refinement.after` with exact `head` values;
  the comparison is shown only while `after.head` matches the node head.
  Each revision denotes one commit
  (`head^!`) unless an explicit `base` defines its complete `base..head` range.
  These ranges compare the previously reviewed proposal with its replacement,
  independently of intervening base changes. A PR node suppresses its own
  retained comparison when the observed head differs. A branch proposal keeps
  its earlier-proposal comparison even when the destination PR advances.
  They do not contribute to dependency chains or blocker counts. The proposal
  filter derives from branch status locally, preserving the published state
  enum for already-open clients; proposal age is explicitly unknown.
- **Changes requested**, **Draft** and **Conflicts:** corresponding source PR
  attributes. Drafts do not enter maintainer-review or awaiting-import filters.
- **Awaiting import:** effective approval for the current source head and
  a complete import lookup without an active or merged
  exact-source import. Its check qualifier preserves passing, failing, pending
  or unknown checks. This is waiting work, not an import request or readiness claim.
- **Import PR open:** a verified open or draft Copybara PR for the current source
  revision. Its qualifier states checks and conflict status; UNKNOWN is not
  conflict-free. Older-source imports remain visible only as historical evidence.
- **Waiting for merge:** a source PR approved at its current head and its current
  imports are nondraft, MERGEABLE and have nonempty, complete green visible check
  inventories and a complete import lookup, without changes requested. This observed public state grants no
  merge authority and does not claim internal import progress.
- **Failing checks** and **Checks pending:** current source or active exact-source
  import validation checks, including CI/infrastructure failures but excluding
  identified reviewer-assignment and Copybara import-status results. Copybara
  errors remain maintainer import attention and visible raw statuses; a linked
  import's failing public validation checks remain agent work. An incomplete
  inventory cannot explain away an aggregate failure. Each failure label names
  the source or import PR; details link the failed checks. An old import failure
  stays in details without making the current head fail.

`workStates` stores each current state's scope, `since` and `basis`. Observed
ages use the first retained observation of the same state and revision, not
`updatedAt`, comments or refresh time. `obs` marks the age of the first observation as of the snapshot;
continuity between scans is unknown. A state ending or its scoped revision changing
starts a new interval. The first migration may reuse the immediately preceding
verified PR snapshot, but does not invent historical branch transitions. This deployment also seeds
observation dates from a bounded 96-commit history, stopping at each first
state or revision mismatch and retaining its source snapshot URL.
Legacy PR review ages map only to maintainer review. Legacy branch review ages
carry into contributor review only for a still-pending decision on the same
candidate; the old classifier emitted that branch state only for explicit
contributor requests. A contributor amendment never inherits PR review age.
A branch without a recorded revision has unknown age.
GitHub's explicit merge/close timestamps are marked exact. Older snapshots
without this optional field remain readable, with state filters disabled and
an explicit history-unavailable message. Legacy saved review filters expand to both review authorities; selection and
viewport storage remain unchanged. No status is guessed in the browser.

The table shows every current state and its age; click **State · age** to sort
by the oldest matching state. DAG cards show one compact state/age row (`+N`
indicates additional states); details show all states and dated evidence.

**Table** contains all tracked work, including isolated PRs and branches. It
starts sorted by blocking reach; click the Item, Title, Blocks, Direct, State · age,
Review, PR checks, or Workstream heading to change sort order. Click the item identifier to open
its source, or its title to inspect details. Details consume space only while
an item is selected and preserve all curated relationships, including integration
membership, reasons, and evidence links. The compact Review, PR checks and
Import columns expose the PR snapshot; details include labels, merge conflicts,
review threads, exact revisions and each import’s own checks. DAG cards retain small R/C indicators and hover text for review/check state.

Search titles, numbers, refs, or summaries; filter by workstream. In DAG, matching
chains retain their dependency context, and independent matches appear as nodes. The visible ↗ on every node is a native source anchor,
supporting keyboard activation, new tabs, and normal browser link actions.
The separate node control selects details. Relationship titles navigate the
selection, with an adjacent source link.

Drag the graph to pan; use arrow keys with the graph focused, zoom controls,
or Control/Command + wheel. `/` focuses search and Escape closes details.
Selection is stored in the URL fragment; optional local storage also retains
view, filters, sort, focus and camera across a browser reload. A recorded cycle is reported instead
of inventing a topological order. HTML references versioned JS and CSS URLs so
a redesigned document does not reuse stale assets from an earlier layout.

### Investigations without a branch

**Investigations** is a separate research inventory. It does not add PRs,
GitHub states, dependency edges, or blocker counts. Tasks can begin without
any Git ref and retain their findings after they conclude. A diagnostic branch
or later PR is an evidence link, not the identity of the investigation.

The optional top-level `registry.investigations` array contains records with:

- A unique `id` of the form `investigation:slug`, `title`, `owner`, `summary`,
  `updatedAt`, and `status` (`active` or `concluded`). The date records curation,
  not a GitHub observation or a fabricated completion date.
- `findings`, an array of `{text, evidence}` records. Each evidence entry is
  `{label, url}` with a real HTTPS source or validation URL.
- `evidence`, an array of additional primary links, and an optional `nextStep`.
  Concluded records display that last field as a recommendation. Concluded
  research does not imply an implementation, a performance gain, or a merged PR.
- Optional `comparisons` render before the detailed findings. Each table has
  `title`, `description`, `columns`, `rows`, `note`, and `evidence`. Columns and
  cells are nonempty text; every row matches the column count. State the actual
  source and conditions, distinguish measurement bounds from statistical
  intervals, and keep incompatible profiles or sender roles separate.

No commit, branch, repository, or upstream issue is required. Record observed
results and their limits; do not invent refs to make a task visible. Keep private
communications and unconfirmed incident details out of these public records.
The exact client validator in `metadata-preflight.mjs` checks this collection
with the rest of the registry. Older clients safely ignore the optional array;
new clients also accept older registries without it.
The same compatibility rule applies to optional comparison tables: older
clients retain the prose findings and links; newer clients accept records
without tables. Table text participates in investigation search. Native
captions and row/column headers retain the table relationships on small screens.

Findings and primary links are visible in each card. Search and the
active/concluded selector affect only investigations; PR filters and the graph
camera remain separate. Selecting a card's native title link creates a shareable
URL through the existing URL-state path, for example:
`?view=investigations&investigation-status=active#investigation%3Astate-reflection-design`.
Explicit URL state overrides saved preferences, unknown status values fall back
to all investigations, and stale selections are ignored. Browser back/forward
restores investigation filters and selection; snapshot refresh preserves the
investigation panel scroll position.

## Maintenance checks

No build is required. Before publishing, inspect changed relationships against
the linked sources, ensure IDs are unique and edge endpoints exist, and check
JavaScript syntax. Use the hosted browser check to exercise search, filters,
selection, focus, Table sorting and switching, native links, snapshot fallback, PR attributes and verified import links. Confirm
the served registry, GitHub snapshot and source files match the published commit.

GitHub Pages remains configured for `gh-pages` at `/`. Keep site deployments on
this branch. The scheduled workflow file lives on the fork's existing default
`master` branch because GitHub schedules only workflows there. Its job grants
only repository contents write and Pages write, with no pull-request or access
administration permissions. The default branch and Pages publishing mode stay
unchanged.

Views are shareable: the URL records filters, DAG/table mode, search, workstream,
resolved visibility, selected item and dependency focus. For example,
`?view=dag&state=awaiting-import` opens the Awaiting import filter even when the
recipient saved a different view. Back/forward restores those selections. Exact
pan/zoom stays local and survives reloads and metadata refreshes of the same view.
Unknown filter values and stale item IDs are ignored.

Approval credit requires GitHub's approved decision plus a latest effective
approval whose commit matches the current PR head. Dismissed, older-head or
missing-identity approvals supply no current credit. Review requests are retained;
prior approvals remain available as history. For gVisor, a curated `approvedPatch`
record can separately bind an old approval to a verified equivalent head using
the original review ID, author, time and
approved head, comparison method and patch IDs. Lifecycle routing accepts that
unchanged approved patch without labeling it a fresh review. A changed head,
dismissal, changes request, unresolved thread, incomplete feedback or new external
feedback after verification invalidates this path until reconciled. Author
notices have no effect. Bot feedback also requires reconciliation. Existing
holds and dependency relationships remain.
A ready-to-pull label alone supplies neither kind of approval.

Contributor decisions remain separate from GitHub maintainer reviews.
An answered request for revisions is recorded as
`contributorChangesRequested: {head, url}` on its proposal branch. It is
mutually exclusive with a pending `contributorReview`, and applies only to
the exact branch head. The existing Changes requested filter includes it
with an explicit Contributor qualifier. Older clients accept the optional
field and the unchanged state enum. Fresh-head approval continues to come
only from a maintainer review of the current GitHub head; approval of a verified
unchanged patch is displayed separately.


### Maintainer handoff

The table can group rows by attention needed. Its selectors separate Agent work (conflicts, public source checks, unanswered feedback and public snapshot refreshes), Needs your attention (explicit contributor decisions and separate human-only submissions), and Needs maintainer attention (review or reply follow-up, imports, internal feedback, import checks and unclear internal status). Agent work can be queued; the reason does not imply that it has started. Multiple reasons can apply to a PR. Grouping and explicit reason filters are shareable in the URL; earlier `?view=handoff` links open the grouped table.
An unpublished branch with `status: Agent work` routes to the agent when its existing `followup` names an owner, a nonempty next action and the exact current `sourceHead`. Remove an active `contributorReview` badge while implementation or validation remains with the agent, and retain the prior request in the follow-up description; this does not create a second contributor request. Stale-head assignments do not carry forward.


Copy visible or Copy group exports plain text with a short introduction, snapshot time and plain headings. Maintainer attention uses source PR URLs. Contributor decisions and human-only submissions use canonical tracker links that expose the exact proposed commit or text. Search, workstream, attention and existing state filters determine the copied scope. Native source/import links and details remain in the table. Clipboard denial exposes the same selected text for manual copying, and a scope or snapshot change invalidates stale fallback text and pending copy confirmations.


BuildBuddy status contexts whose exact provider description says
`Check requires approving review` are maintainer approval gates. The raw
`ERROR` status remains visible; these contexts establish neither an executed
test failure nor passing checks. Unknown descriptions and unrelated failures
retain their normal classification.


A human-only submission uses an explicit `humanAction` on its existing item:
`status: pending`, `kind: submission`, exact local `head` matching the item's
head, `title`, `summary`, full actionable `body`, public context `url`, and
`policyURL`. It appears under **Human-only submissions**, separately from
**Contributor decisions**, without implying that an agent may submit it.
This field and `contributorReview` are mutually exclusive. Remove the action
when settled; changing its head requires explicit reconciliation. The same
request-to-visible-item invariant applies, without creating another history or
polling lane.
