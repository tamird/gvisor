"use strict";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const CACHE_KEY = "gvisor-work-map-v1";
const CACHE_AGE = 15 * 60 * 1000;
const CARD = { width: 216, height: 58, column: 264, row: 72 };
const READABLE_SCALE = .9;
let registry, model, selected = null, focus = null, view = "dag";
let sortKey = "impact", sortDirection = -1;
let camera = { x: 20, y: 20, scale: 1 }, bounds = { width: 900, height: 600 };
let refreshing = false, lastAttempt = 0, drag = null, moved = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  if (text !== undefined) node.textContent = text;
  return node;
}
function safeURL(value) {
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : null; }
  catch { return null; }
}
function link(text, url, className) {
  const node = element("a", className, text);
  node.href = safeURL(url) || "#";
  node.target = "_blank";
  node.rel = "noopener noreferrer";
  return node;
}
function button(text, action, className) {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
function date(value) {
  return new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
}
function label(node) {
  if (node.type === "pr") return `#${node.number}`;
  if (node.type === "branch") return "BRANCH";
  return node.number ? `ISSUE #${node.number}` : "CAPACITY";
}
function status(node) {
  if (node.type === "issue" && node.githubState) {
    if (node.status === "deployment-pending") return `Issue ${node.githubState} · deployment pending`;
    return `Issue ${node.githubState} · capacity unverified`;
  }
  return ({ branch: "Working branch", "deployment-pending": "Deployment pending", unavailable: "Unavailable" })[node.status] || node.status;
}
function resolved(node) { return node.type === "pr" && ["merged", "closed"].includes(node.status); }
function nodeMap() { return new Map(model.nodes.map((node) => [node.id, node])); }
function activeDependencyEdges() {
  const active = new Set(model.nodes.filter((node) => !resolved(node) && node.status !== "resolved").map((node) => node.id));
  return model.edges.filter((edge) => ["depends_on", "blocked_by"].includes(edge.type) && active.has(edge.from) && active.has(edge.to));
}
function impact(id) {
  const edges = activeDependencyEdges(), nodes = nodeMap(), seen = new Set([id]), queue = [id];
  const direct = new Set(edges.filter((edge) => edge.from === id && nodes.get(edge.to)?.type === "pr").map((edge) => edge.to));
  for (const current of queue) {
    for (const edge of edges) if (edge.from === current && !seen.has(edge.to)) { seen.add(edge.to); queue.push(edge.to); }
  }
  seen.delete(id);
  return { direct: direct.size, prs: [...seen].filter((other) => nodes.get(other)?.type === "pr").length,
    branches: [...seen].filter((other) => nodes.get(other)?.type === "branch").length };
}
function rankedPRs() {
  return model.nodes.filter((node) => node.type === "pr" && !resolved(node))
    .map((node) => ({ node, reach: impact(node.id) }))
    .sort((a, b) => b.reach.prs - a.reach.prs || b.reach.direct - a.reach.direct || a.node.number - b.node.number);
}
function components() {
  const edges = activeDependencyEdges(), remaining = new Set(edges.flatMap((edge) => [edge.from, edge.to])), result = [];
  const nodes = nodeMap();
  while (remaining.size) {
    const first = remaining.values().next().value, members = [first]; remaining.delete(first);
    for (const id of members) for (const edge of edges) {
      const other = edge.from === id ? edge.to : edge.to === id ? edge.from : null;
      if (remaining.has(other)) { remaining.delete(other); members.push(other); }
    }
    const leaders = members.map((id) => nodes.get(id)).sort((a, b) => impact(b.id).prs - impact(a.id).prs || a.title.localeCompare(b.title));
    const root = leaders[0];
    const sink = members.find((id) => !edges.some((edge) => edge.from === id));
    result.push({ ids: new Set(members), id: root.id, score: impact(root.id).prs,
      title: impact(root.id).prs ? `${label(root)} · ${root.title}` : (nodes.get(sink) || root).title });
  }
  return result.sort((a, b) => b.score - a.score || b.ids.size - a.ids.size);
}
function matches(node) {
  const search = $("search").value.trim().toLowerCase(), group = $("group-filter").value;
  return (!group || node.group === group) && (!search || `${label(node)} ${node.title} ${node.number || ""} ${node.ref || ""} ${node.summary || ""}`.toLowerCase().includes(search));
}
function tableNodes() { return model.nodes.filter((node) => matches(node) && (!resolved(node) || $("show-resolved").checked)); }
function graphNodes() {
  const chains = components(), filtering = $("search").value.trim() || $("group-filter").value;
  const visible = chains.filter((chain) => filtering ? [...chain.ids].some((id) => matches(nodeMap().get(id))) : !focus || chain.ids.has(focus));
  const ids = new Set(visible.flatMap((chain) => [...chain.ids]));
  return model.nodes.filter((node) => ids.has(node.id));
}
function applyCamera() { $("graph-content").setAttribute("transform", `translate(${camera.x},${camera.y}) scale(${camera.scale})`); }
function resetCamera(fit = true, overview = false) {
  if (view !== "dag") return;
  const box = $("graph-stage").getBoundingClientRect();
  const minimum = overview ? .12 : READABLE_SCALE;
  const scale = fit ? Math.max(minimum, Math.min((box.width - 48) / bounds.width, (box.height - 76) / bounds.height, 1.15)) : 1;
  camera = { x: Math.max(24, (box.width - bounds.width * scale) / 2), y: Math.max(24, (box.height - 44 - bounds.height * scale) / 2), scale };
  applyCamera();
}
function zoom(factor, x, y) {
  const box = $("graph-stage").getBoundingClientRect();
  x ??= box.width / 2; y ??= box.height / 2;
  const scale = Math.max(.12, Math.min(2.5, camera.scale * factor)), ratio = scale / camera.scale;
  camera = { x: x - (x - camera.x) * ratio, y: y - (y - camera.y) * ratio, scale };
  applyCamera();
}
function wrap(text, length = 29, maxLines = 2) {
  const words = text.split(/\s+/), lines = []; let line = "";
  for (const word of words) {
    if (line && `${line} ${word}`.length > length) { lines.push(line); line = word; }
    else line += `${line ? " " : ""}${word}`;
  }
  if (line) lines.push(line);
  if (lines.length > maxLines) { lines.length = maxLines; lines[maxLines - 1] = lines[maxLines - 1].slice(0, length - 1) + "…"; }
  return lines;
}
function dagLayout(nodes, content) {
  const ids = new Set(nodes.map((node) => node.id)), byId = nodeMap();
  const edges = activeDependencyEdges().filter((edge) => ids.has(edge.from) && ids.has(edge.to));
  const indegree = new Map(nodes.map((node) => [node.id, 0])), depth = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) indegree.set(edge.to, indegree.get(edge.to) + 1);
  const queue = nodes.filter((node) => !indegree.get(node.id)).map((node) => node.id);
  for (const id of queue) for (const edge of edges) if (edge.from === id) {
    depth.set(edge.to, Math.max(depth.get(edge.to), depth.get(id) + 1));
    indegree.set(edge.to, indegree.get(edge.to) - 1);
    if (!indegree.get(edge.to)) queue.push(edge.to);
  }
  if (queue.length !== nodes.length) throw new Error("A recorded dependency cycle cannot be drawn as a DAG. Inspect its relationships in Table.");
  const chains = components().filter((chain) => ids.has(chain.id)), positions = new Map();
  const padding = chains.length > 1 ? 12 : 0, heading = chains.length > 1 ? 28 : 0;
  const panels = chains.map((chain) => {
    const columns = new Map();
    for (const id of chain.ids) { const level = depth.get(id); if (!columns.has(level)) columns.set(level, []); columns.get(level).push(id); }
    return { chain, columns,
      width: Math.max(...columns.keys()) * CARD.column + CARD.width + 2 * padding,
      height: Math.max(...[...columns.values()].map((column) => column.length)) * CARD.row - (CARD.row - CARD.height) + heading + 2 * padding };
  });
  // Pack whole chains into the available width at a readable scale. Filling
  // gaps beside earlier chains avoids a single tall strip of tiny nodes.
  const available = Math.max(1, ($("graph-stage").clientWidth - 48) / READABLE_SCALE);
  const packingWidth = Math.max(available, ...panels.map((panel) => panel.width));
  const placed = [], gap = 24;
  for (const panel of panels) {
    const xs = [0, ...placed.map((other) => other.x + other.width + gap)].sort((a, b) => a - b);
    const ys = [0, ...placed.map((other) => other.y + other.height + gap)].sort((a, b) => a - b);
    const spot = ys.flatMap((y) => xs.map((x) => ({ x, y }))).find(({ x, y }) =>
      x + panel.width <= packingWidth && placed.every((other) =>
        x + panel.width + gap <= other.x || other.x + other.width + gap <= x ||
        y + panel.height + gap <= other.y || other.y + other.height + gap <= y));
    Object.assign(panel, spot); placed.push(panel);
    if (chains.length > 1) {
      const backdrop = svg("g", { "data-chain": panel.chain.id });
      backdrop.append(svg("rect", { class: "chain-panel", x: panel.x, y: panel.y, width: panel.width, height: panel.height, rx: 6 }),
        svg("text", { class: "chain-heading", x: panel.x + padding, y: panel.y + padding + 12 }, wrap(panel.chain.title, Math.floor((panel.width - 2 * padding) / 7), 1)[0]));
      content.append(backdrop);
    }
    const height = panel.height - heading - 2 * padding;
    for (const [level, column] of panel.columns) {
      column.sort((a, b) => impact(b).prs - impact(a).prs || byId.get(a).title.localeCompare(byId.get(b).title));
      const offset = (height - (column.length * CARD.row - (CARD.row - CARD.height))) / 2;
      column.forEach((id, row) => positions.set(id, { x: panel.x + padding + level * CARD.column,
        y: panel.y + padding + heading + offset + row * CARD.row }));
    }
  }
  return { positions, width: Math.max(1, ...placed.map((panel) => panel.x + panel.width)),
    height: Math.max(CARD.height, ...placed.map((panel) => panel.y + panel.height)) };
}
function drawGraph(nodes) {
  const content = $("graph-content"); content.replaceChildren(); $("dag-warning").hidden = true;
  let layout;
  try { layout = dagLayout(nodes, content); }
  catch (error) { $("dag-warning").textContent = error.message; $("dag-warning").hidden = false; return; }
  bounds = layout;
  const { positions } = layout;
  const edges = svg("g", { "aria-hidden": "true" });
  for (const edge of activeDependencyEdges()) {
    const from = positions.get(edge.from), to = positions.get(edge.to); if (!from || !to) continue;
    const x1 = from.x + CARD.width, x2 = to.x, y1 = from.y + CARD.height / 2, y2 = to.y + CARD.height / 2;
    const bend = Math.max((x2 - x1) / 2, 20);
    edges.append(svg("path", { d: `M ${x1},${y1} C ${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`,
      class: `edge ${edge.type}${edge.from === selected || edge.to === selected ? " active" : ""}`,
      "data-from": edge.from, "data-to": edge.to }));
  }
  content.append(edges);
  const filtering = $("search").value.trim() || $("group-filter").value;
  for (const node of nodes) {
    const pos = positions.get(node.id);
    const group = svg("g", { class: `node ${node.type}${selected === node.id ? " selected" : ""}${filtering && !matches(node) ? " context-node" : ""}`,
      transform: `translate(${pos.x},${pos.y})`, role: "group" });
    const card = svg("g", { tabindex: "0", role: "button", "aria-label": `${label(node)}: ${node.title}. Select details.`, "aria-pressed": selected === node.id, "data-node": node.id });
    card.append(svg("rect", { class: "node-shape", width: CARD.width, height: CARD.height, rx: 5 }));
    card.append(svg("text", { class: "node-label", x: 10, y: 15 }, label(node)));
    const reach = impact(node.id).prs;
    if (reach) card.append(svg("text", { class: "node-reach", x: CARD.width - 33, y: 15, "text-anchor": "end" }, `${reach} downstream`));
    wrap(node.title).forEach((line, index) => card.append(svg("text", { class: "node-title", x: 10, y: 32 + index * 14 }, line)));
    card.append(svg("title", {}, `${node.title}\n${status(node)}`));
    card.addEventListener("click", () => { if (!moved) select(node.id); });
    card.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); event.stopPropagation(); select(node.id); } });
    group.append(card);
    const open = svg("a", { href: safeURL(node.url) || "#", target: "_blank", rel: "noopener noreferrer", tabindex: "0", class: "node-link", "aria-label": `Open ${label(node)}: ${node.title}` });
    open.append(svg("rect", { class: "node-link-hit", x: CARD.width - 29, y: 1, width: 28, height: 26, rx: 4 }),
      svg("text", { class: "node-outlink", x: CARD.width - 21, y: 18, "aria-hidden": "true" }, "↗"));
    open.addEventListener("click", (event) => { event.stopPropagation(); if (moved && event.detail !== 0) event.preventDefault(); });
    open.addEventListener("keydown", (event) => { if (event.key === "Enter") event.stopPropagation(); });
    group.append(open); content.append(group);
  }
  applyCamera();
}
function drawTable(nodes) {
  const table = $("work-table"), head = table.tHead.rows[0], body = table.tBodies[0]; body.replaceChildren();
  for (const th of head.cells) {
    const key = th.querySelector("button")?.dataset.sort;
    if (key) th.setAttribute("aria-sort", key === sortKey ? (sortDirection === -1 ? "descending" : "ascending") : "none");
  }
  const groups = new Map(model.groups.map((group) => [group.id, group.label]));
  const value = (node) => ({ impact: impact(node.id).prs, direct: impact(node.id).direct, title: node.title,
    status: status(node), group: groups.get(node.group) || "", number: node.number || 0 })[sortKey];
  nodes.sort((a, b) => (typeof value(a) === "number" ? value(a) - value(b) : String(value(a)).localeCompare(String(value(b)))) * sortDirection || a.title.localeCompare(b.title));
  for (const node of nodes) {
    const row = element("tr", selected === node.id ? "selected" : ""); row.dataset.nodeId = node.id;
    const reach = impact(node.id), title = element("td", "work-title"), identity = element("td", `identity ${node.type}`);
    identity.append(link(label(node), node.url));
    const choose = button(node.title, () => select(node.id)); choose.dataset.node = node.id;
    title.append(choose);
    row.append(identity, title, element("td", "numeric reach-cell", String(reach.prs)), element("td", "numeric", String(reach.direct)),
      element("td", "status-cell", status(node)), element("td", "group-cell", groups.get(node.group) || ""));
    body.append(row);
  }
}
function drawDetails() {
  const details = $("details"), nodes = nodeMap(), node = nodes.get(selected); details.replaceChildren(); details.hidden = !node;
  if (!node) return;
  const top = element("div", `detail-top ${node.type}`), close = button("×", () => select(null));
  close.setAttribute("aria-label", "Close details"); top.append(link(label(node) + " ↗", node.url), close);
  details.append(top, element("h2", "item-title", node.title), element("span", `status-pill ${node.status}`, status(node)));
  const reach = impact(node.id);
  details.append(element("p", "detail-impact", `${reach.prs} downstream open PRs · ${reach.direct} direct`));
  if (node.summary) details.append(element("p", "detail-description", node.summary));
  if (node.ref) details.append(element("p", "detail-meta", node.ref));
  if (node.type === "issue") details.append(element("p", "detail-meta", "Closed issue ≠ deployed capacity. Qualification remains curated."));
  const relations = [
    ["Requires", model.edges.filter((edge) => edge.to === node.id && edge.type !== "includes"), "from"],
    ["Needed by", model.edges.filter((edge) => edge.from === node.id && edge.type !== "includes"), "to"],
    ["Includes", model.edges.filter((edge) => edge.to === node.id && edge.type === "includes"), "from"],
    ["Included in", model.edges.filter((edge) => edge.from === node.id && edge.type === "includes"), "to"],
  ];
  for (const [heading, edges, other] of relations) {
    if (!edges.length) continue;
    const section = element("section", "detail-section"); section.append(element("h3", "", heading));
    for (const edge of edges) {
      const target = nodes.get(edge[other]); if (!target) continue;
      const relation = element("div", "relation");
      relation.append(button(`${label(target)} · ${target.title}`, () => select(target.id)), link("↗", target.url, "relation-source"), element("p", "", edge.reason));
      if (resolved(target)) relation.append(element("p", "", `Resolved: ${target.status}`));
      for (const [index, url] of (Array.isArray(edge.evidence) ? edge.evidence : [edge.evidence]).filter(Boolean).entries()) relation.append(link(`Evidence${index ? " " + (index + 1) : ""} ↗`, url, "evidence-link"));
      section.append(relation);
    }
    details.append(section);
  }
  if (activeDependencyEdges().some((edge) => edge.from === node.id || edge.to === node.id)) details.append(button("Show dependency chain", () => focusWork(node.id), "focus-button"));
  else details.append(element("p", "detail-description", "No active dependencies recorded. Integration membership is shown above; it is not a blocking relationship."));
}
function select(id) {
  const previous = document.activeElement?.getAttribute("data-node"); selected = id;
  history.replaceState(null, "", id ? `#${encodeURIComponent(id)}` : location.pathname + location.search);
  render();
  if (previous) document.querySelector(`${view === "dag" ? "#graph" : "#work-table"} [data-node="${CSS.escape(previous)}"]`)?.focus();
}
function focusWork(id) {
  focus = id; selected = id; $("search").value = ""; $("group-filter").value = "";
  history.replaceState(null, "", `#${encodeURIComponent(id)}`); setView("dag");
}
function drawRanking() {
  const panel = $("ranking"); panel.replaceChildren();
  panel.append(element("h2", "", "Most blocking PRs"), element("p", "ranking-explainer", "Unique downstream open PRs"));
  const ranked = rankedPRs();
  for (const {node, reach} of ranked.filter((item) => item.reach.prs > 0).slice(0, 5)) {
    const row = element("div", "rank-row"), choose = button("", () => focusWork(node.id), "rank-choice");
    choose.dataset.rank = node.id;
    choose.append(element("span", "rank-number", label(node)), element("span", "rank-title", node.title), element("span", "rank-direct", `${reach.direct} direct`));
    row.append(choose, element("strong", "rank-value", reach.prs)); panel.append(row);
  }
  panel.append(element("p", "ranking-note", "Counts are global. Drafts count; resolved prerequisites cut paths. Integration membership is excluded."));
  const zero = ranked.filter((item) => !item.reach.prs).length;
  panel.append(button(`${zero} PRs block no recorded PRs → Table`, () => { resetFilters(false); setView("table"); }, "table-link"));
  const external = model.nodes.filter((node) => node.type === "issue" && activeDependencyEdges().some((edge) => edge.from === node.id));
  if (external.length) {
    panel.append(element("h3", "", "External capacity"));
    for (const node of external) panel.append(button(node.title, () => focusWork(node.id), "external-rank"));
    panel.append(element("p", "ranking-note", "Issue status does not establish deployment."));
  }
}
function drawChainSelector() {
  const select = $("chain-filter"); select.replaceChildren();
  const all = element("option", "", "All connected work"); all.value = ""; select.append(all);
  for (const chain of components()) { const option = element("option", "", `${chain.title} (${chain.ids.size})`); option.value = chain.id; select.append(option); }
  select.value = components().find((chain) => chain.ids.has(focus))?.id || "";
  select.disabled = Boolean($("search").value.trim() || $("group-filter").value);
}
function render(reposition = false) {
  const dag = view === "dag";
  $("dag-pane").hidden = !dag; $("table-pane").hidden = dag; $("ranking").hidden = !dag;
  $("dag-view").setAttribute("aria-pressed", dag); $("table-view").setAttribute("aria-pressed", !dag);
  $("chain-control").hidden = !dag; $("resolved-control").hidden = dag;
  const nodes = dag ? graphNodes() : tableNodes();
  const connected = new Set(activeDependencyEdges().flatMap((edge) => [edge.from, edge.to]));
  const matched = tableNodes(), isolatedMatches = matched.filter((node) => !connected.has(node.id));
  $("visible-count").textContent = dag ? `${nodes.length} / ${connected.size} connected items · prerequisite → dependent` : `${nodes.length} items · click a title for relationships`;
  $("filter-note").textContent = dag && ($("search").value.trim() || $("group-filter").value) ? "Matching chains with dependency context" : "";
  const notice = $("isolated-notice"); notice.replaceChildren(); notice.hidden = !dag || !($("search").value.trim() || $("group-filter").value) || !isolatedMatches.length;
  if (!notice.hidden) notice.append(document.createTextNode(`${isolatedMatches.length} matching item${isolatedMatches.length === 1 ? " has" : "s have"} no active dependencies. `), button("View in Table", () => setView("table")));
  $("empty").hidden = nodes.length !== 0;
  $("empty-title").textContent = dag && isolatedMatches.length ? "No dependency chain for this search" : "No matching work";
  $("empty-text").textContent = dag && isolatedMatches.length ? "The matching work is available in Table. No dependency is recorded for it." : "Try another search or reset the filters.";
  $("empty-table").hidden = !dag || !isolatedMatches.length;
  if (dag) { drawGraph(nodes); drawRanking(); drawChainSelector(); } else drawTable(nodes);
  drawDetails();
  $("inventory").textContent = `${model.nodes.filter((node) => node.type === "pr" && !resolved(node)).length} open PRs · ${model.nodes.length} tracked items`;
  if (reposition) resetCamera();
}
function setFreshness(text, warning = false, live = false) {
  $("freshness-text").textContent = text;
  $("freshness-dot").className = `status-dot${warning ? " warning" : live ? " live" : ""}`;
}
function applyLive(snapshot) {
  const nodes = registry.nodes.map((node) => ({ ...node })), ids = new Map(nodes.map((node) => [node.id, node])), aliases = new Map();
  for (const pr of [...snapshot.prs, ...snapshot.resolved]) {
    const id = `pr:${pr.number}`, existing = ids.get(id);
    const branch = pr.ref ? nodes.find((node) => node.type === "branch" && node.ref === pr.ref && node.repo === `${registry.meta.owner}/gvisor`) : null;
    const node = { ...existing, id, type: "pr", number: pr.number, repo: registry.meta.repo, title: pr.title, url: pr.url,
      status: pr.status, updatedAt: pr.updatedAt, ref: pr.ref || existing?.ref, group: existing?.group || branch?.group || "new" };
    if (branch && ["open", "draft", "merged"].includes(pr.status)) {
      aliases.set(branch.id, id); node.promotedFrom = branch.ref; node.summary ||= branch.summary;
    }
    if (existing) Object.assign(existing, node); else { nodes.push(node); ids.set(id, node); }
  }
  for (const node of nodes) if (snapshot.issues[node.id]) Object.assign(node, snapshot.issues[node.id]);
  const edges = registry.edges.map((edge) => ({ ...edge, from: aliases.get(edge.from) || edge.from, to: aliases.get(edge.to) || edge.to }))
    .filter((edge, index, all) => edge.from !== edge.to && all.findIndex((other) => other.from === edge.from && other.to === edge.to && other.type === edge.type) === index);
  model = { ...registry, nodes: nodes.filter((node) => !aliases.has(node.id)), edges,
    groups: [...registry.groups, { id: "new", label: "New · not yet grouped" }] };
  if (aliases.has(selected)) selected = aliases.get(selected);
  if (aliases.has(focus)) focus = aliases.get(focus);
  const chains = components();
  if (focus && !chains.some((chain) => chain.ids.has(focus))) focus = chains[0]?.id || null;
  render(true);
  setFreshness(`${snapshot.partial ? "Partial GitHub refresh" : "GitHub status checked"} · ${date(snapshot.checkedAt)}`, snapshot.partial, !snapshot.partial);
  $("freshness-detail").textContent = snapshot.partial
    ? "Some status lookups were unavailable or reached the request limit. Unverified entries retain their saved state; relationships and deployment qualifications remain curated."
    : "Open PRs refresh from public GitHub. New PRs replace matching branches; merged and closed PRs hide by default. Relationships and deployment qualifications remain curated.";
}
async function refresh(force = false) {
  if (refreshing || (force && Date.now() - lastAttempt < 60000)) return;
  let cached;
  try { cached = JSON.parse(localStorage.getItem(CACHE_KEY)); } catch { /* Storage is optional. */ }
  if (cached?.registryDate !== registry.meta.updatedAt) cached = null;
  if (cached && Array.isArray(cached.prs) && Array.isArray(cached.resolved) && cached.issues) {
    applyLive(cached);
    if (!force && Date.now() - cached.checkedAt < CACHE_AGE) return;
  }
  refreshing = true; lastAttempt = Date.now(); $("refresh").disabled = true;
  setFreshness("Checking public GitHub status…");
  let budget = 30;
  const get = async (path) => {
    if (--budget < 0) throw new Error("Request limit reached");
    const response = await fetch(`https://api.github.com${path}`, { credentials: "omit", headers: { Accept: "application/vnd.github+json" }, signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error(`GitHub returned ${response.status}`);
    return response.json();
  };
  try {
    const query = encodeURIComponent(`repo:${registry.meta.repo} is:pr is:open author:${registry.meta.owner}`);
    const first = await get(`/search/issues?q=${query}&per_page=100`);
    if (first.incomplete_results || first.total_count > 200 || !Array.isArray(first.items)) throw new Error("GitHub did not return a complete open-PR inventory");
    let items = first.items;
    if (first.total_count > 100) items = items.concat((await get(`/search/issues?q=${query}&per_page=100&page=2`)).items);
    if (items.length !== first.total_count || new Set(items.map((item) => item.number)).size !== first.total_count) throw new Error("Open-PR inventory changed during refresh");
    const prior = new Map([...registry.nodes.filter((node) => node.type === "pr"), ...(cached?.prs || []), ...(cached?.resolved || [])].map((node) => [node.number, node]));
    const snapshot = { registryDate: registry.meta.updatedAt, checkedAt: Date.now(), prs: [], resolved: [], issues: { ...(cached?.issues || {}) }, partial: false };
    const open = new Set(items.map((item) => item.number));
    // Keep previous identities and states until an individual lookup succeeds.
    snapshot.resolved = [...prior.values()].filter((node) => !open.has(node.number)).map((node) => ({ ...node }));
    for (const item of items) {
      let ref = prior.get(item.number)?.ref;
      if (!ref && budget > 6) {
        try { const detail = await get(`/repos/${registry.meta.repo}/pulls/${item.number}`); if (detail.head?.repo?.owner?.login === registry.meta.owner) ref = detail.head.ref; }
        catch { snapshot.partial = true; }
      } else if (!ref) snapshot.partial = true;
      snapshot.prs.push({ number: item.number, title: item.title, url: item.html_url, status: item.draft ? "draft" : "open", updatedAt: item.updated_at, ref });
    }
    for (const node of prior.values()) {
      if (open.has(node.number)) continue;
      if (budget <= 6) { snapshot.partial = true; continue; }
      try {
        const detail = await get(`/repos/${registry.meta.repo}/pulls/${node.number}`);
        const result = { number: node.number, title: detail.title, url: detail.html_url, status: detail.merged_at ? "merged" : detail.state === "open" && detail.draft ? "draft" : detail.state, updatedAt: detail.updated_at, ref: detail.head?.ref };
        snapshot.resolved[snapshot.resolved.findIndex((item) => item.number === node.number)] = result;
      } catch { snapshot.partial = true; }
    }
    for (const node of registry.nodes.filter((node) => node.type === "issue" && node.repo && node.number)) {
      try { const issue = await get(`/repos/${node.repo}/issues/${node.number}`); snapshot.issues[node.id] = { githubState: issue.state, updatedAt: issue.updated_at }; }
      catch { snapshot.partial = true; }
    }
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(snapshot)); } catch { /* The saved registry works without storage. */ }
    applyLive(snapshot);
  } catch (error) {
    setFreshness(`GitHub refresh unavailable · ${error.message}. Showing ${cached?.registryDate === registry.meta.updatedAt ? "cached status" : "saved snapshot"}.`, true);
  } finally { refreshing = false; $("refresh").disabled = false; }
}
function setView(next) { view = next; render(true); }
function resetFilters(redraw = true) {
  $("search").value = ""; $("group-filter").value = ""; $("show-resolved").checked = false; focus = null;
  if (redraw) render(true);
}
function initialize() {
  $("search").addEventListener("input", () => render(true));
  for (const id of ["group-filter", "show-resolved"]) $(id).addEventListener("change", () => render(true));
  $("chain-filter").addEventListener("change", () => { focus = $("chain-filter").value || null; selected = null; history.replaceState(null, "", location.pathname + location.search); render(true); });
  for (const next of ["dag", "table"]) $(`${next}-view`).addEventListener("click", () => setView(next));
  for (const id of ["reset-filters", "toolbar-reset"]) $(id).addEventListener("click", () => resetFilters());
  $("empty-table").addEventListener("click", () => setView("table"));
  document.querySelectorAll("[data-sort]").forEach((button) => button.addEventListener("click", () => {
    const next = button.dataset.sort; sortDirection = sortKey === next ? -sortDirection : ["impact", "direct", "number"].includes(next) ? -1 : 1; sortKey = next; render();
  }));
  $("refresh").addEventListener("click", () => refresh(true));
  $("zoom-in").addEventListener("click", () => zoom(1.2)); $("zoom-out").addEventListener("click", () => zoom(1 / 1.2));
  $("fit").addEventListener("click", () => resetCamera(true, true)); $("reset-view").addEventListener("click", () => resetCamera(false));
  const graph = $("graph");
  graph.addEventListener("pointerdown", (event) => { if (event.button !== 0) return; moved = false; drag = { x: event.clientX, y: event.clientY, originX: camera.x, originY: camera.y }; });
  graph.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) { moved = true; graph.classList.add("dragging"); graph.setPointerCapture(event.pointerId); }
    if (moved) { camera.x = drag.originX + dx; camera.y = drag.originY + dy; applyCamera(); }
  });
  for (const name of ["pointerup", "pointercancel"]) graph.addEventListener(name, () => { drag = null; graph.classList.remove("dragging"); });
  graph.addEventListener("wheel", (event) => {
    event.preventDefault(); const box = graph.getBoundingClientRect();
    if (event.ctrlKey || event.metaKey) zoom(Math.exp(-event.deltaY * .005), event.clientX - box.left, event.clientY - box.top);
    else { camera.x -= event.deltaX; camera.y -= event.deltaY; applyCamera(); }
  }, { passive: false });
  graph.addEventListener("keydown", (event) => {
    const delta = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] }[event.key];
    if (delta) { event.preventDefault(); camera.x += delta[0]; camera.y += delta[1]; applyCamera(); }
    else if (["+", "="].includes(event.key)) zoom(1.2); else if (event.key === "-") zoom(1 / 1.2);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { event.preventDefault(); $("search").focus(); }
    if (event.key === "Escape") { $("search").blur(); select(null); }
  });
  window.addEventListener("hashchange", () => { selected = decodeURIComponent(location.hash.slice(1)); render(); });
  new ResizeObserver(() => { if (view === "dag" && model) { drawGraph(graphNodes()); resetCamera(); } }).observe($("graph-stage"));
}
async function start() {
  try {
    const response = await fetch("registry.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`Registry returned ${response.status}`);
    registry = await response.json();
    const ids = new Set(registry.nodes.map((node) => node.id));
    if (ids.size !== registry.nodes.length || registry.edges.some((edge) => !ids.has(edge.from) || !ids.has(edge.to))) throw new Error("Registry contains invalid relationships");
    model = { ...registry, groups: [...registry.groups, { id: "new", label: "New · not yet grouped" }] };
    for (const group of model.groups) { const option = element("option", "", group.label); option.value = group.id; $("group-filter").append(option); }
    $("registry-age").textContent = `Registry reviewed ${date(registry.meta.updatedAt)}`;
    selected = decodeURIComponent(location.hash.slice(1)) || null;
    focus = components().find((chain) => chain.ids.has(selected))?.id || components()[0]?.id || null;
    initialize(); render(true); refresh();
  } catch (error) { $("visible-count").textContent = "Registry unavailable"; setFreshness(error.message, true); }
}
start();
