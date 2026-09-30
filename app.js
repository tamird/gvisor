"use strict";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const CACHE_KEY = "gvisor-work-map-v1";
const CACHE_AGE = 15 * 60 * 1000;
const types = new Set(["pr", "branch", "issue"]);
let registry, model, selected = null, focus = null, view = "map";
let camera = { x: 18, y: 18, scale: 1 }, bounds = { width: 900, height: 600 };
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
  node.addEventListener("click", action);
  return node;
}
function date(value) {
  return new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
}
function label(node) {
  if (node.type === "pr") return `PR #${node.number}`;
  if (node.type === "branch") return "WORKING BRANCH";
  return node.number ? `EXTERNAL #${node.number}` : "EXTERNAL CAPACITY";
}
function status(node) {
  if (node.type === "issue" && node.githubState) {
    if (node.status === "deployment-pending") return `Issue ${node.githubState} · deployment pending`;
    return `Issue ${node.githubState} · capacity unverified`;
  }
  return ({ branch: "Not yet a PR", "deployment-pending": "Deployment pending", unavailable: "Unavailable" })[node.status] || node.status;
}
function resolved(node) { return node.type === "pr" && ["merged", "closed"].includes(node.status); }
function nodeMap() { return new Map(model.nodes.map((node) => [node.id, node])); }
function neighbors(id, recursive = false) {
  const result = new Set([id]), queue = [id];
  while (queue.length) {
    const next = queue.shift();
    for (const edge of model.edges) {
      const other = edge.from === next ? edge.to : edge.to === next ? edge.from : null;
      if (other && !result.has(other)) { result.add(other); if (recursive) queue.push(other); }
    }
  }
  return result;
}
function visibleNodes() {
  const search = $("search").value.toLowerCase().trim();
  const group = $("group-filter").value;
  const connected = new Set(model.edges.flatMap((edge) => [edge.from, edge.to]));
  const focused = focus ? neighbors(focus, true) : null;
  return model.nodes.filter((node) => types.has(node.type)
    && (!resolved(node) || $("show-resolved").checked)
    && (!group || node.group === group)
    && (!$("connected-only").checked || connected.has(node.id))
    && (!focused || focused.has(node.id))
    && (!search || `${node.title} ${node.number || ""} ${node.ref || ""} ${node.summary || ""}`.toLowerCase().includes(search)));
}
function applyCamera() {
  $("graph-content").setAttribute("transform", `translate(${camera.x},${camera.y}) scale(${camera.scale})`);
}
function resetCamera(fit = false) {
  const box = $("graph-stage").getBoundingClientRect();
  const scale = fit ? Math.min((box.width - 36) / bounds.width, (box.height - 70) / bounds.height, 1)
    : Math.min((box.width - 36) / Math.min(bounds.width, 870), 1);
  camera = { x: 18, y: 18, scale: Math.max(.06, scale) };
  applyCamera();
}
function zoom(factor, x, y) {
  const box = $("graph-stage").getBoundingClientRect();
  x ??= box.width / 2; y ??= box.height / 2;
  const scale = Math.max(.06, Math.min(2.2, camera.scale * factor));
  const ratio = scale / camera.scale;
  camera = { x: x - (x - camera.x) * ratio, y: y - (y - camera.y) * ratio, scale };
  applyCamera();
}
function wrap(text, length = 29, maxLines = 3) {
  const words = text.split(/\s+/), lines = [];
  let line = "";
  for (const word of words) {
    if (line && `${line} ${word}`.length > length) { lines.push(line); line = word; }
    else line += `${line ? " " : ""}${word}`;
  }
  if (line) lines.push(line);
  if (lines.length > maxLines) {
    lines.length = maxLines;
    lines[maxLines - 1] = lines[maxLines - 1].slice(0, length - 1) + "…";
  }
  return lines;
}
function drawGraph(nodes) {
  const content = $("graph-content");
  content.replaceChildren();
  const degree = (id) => model.edges.filter((edge) => edge.from === id || edge.to === id).length;
  const groups = model.groups.map((group) => ({ ...group, nodes: nodes.filter((node) => node.group === group.id)
    .sort((a, b) => degree(b.id) - degree(a.id) || a.title.localeCompare(b.title)) })).filter((group) => group.nodes.length);
  const positions = new Map(), cardWidth = 246, cardHeight = 112, laneWidth = 274, gap = 22;
  const columns = Math.min(3, groups.length);
  let rowY = 0;
  for (let index = 0; index < groups.length; index += columns) {
    const row = groups.slice(index, index + columns);
    const rowHeight = Math.max(...row.map((group) => group.nodes.length)) * (cardHeight + 12) + 60;
    row.forEach((group, column) => {
      const x = column * (laneWidth + gap), height = group.nodes.length * (cardHeight + 12) + 60;
      content.append(svg("rect", { class: "group-panel", x, y: rowY, width: laneWidth, height, rx: 12 }));
      content.append(svg("text", { class: "group-heading", x: x + 14, y: rowY + 27 }, group.label.toUpperCase()));
      content.append(svg("text", { class: "group-count", x: x + laneWidth - 16, y: rowY + 27, "text-anchor": "end" }, group.nodes.length));
      group.nodes.forEach((node, i) => positions.set(node.id, { x: x + 14, y: rowY + 44 + i * (cardHeight + 12) }));
    });
    rowY += rowHeight + 26;
  }
  bounds = { width: Math.max(columns * (laneWidth + gap) - gap, 1), height: Math.max(rowY - 26, 1) };
  const related = selected ? neighbors(selected) : null;
  const edges = svg("g", { "aria-hidden": "true" });
  for (const edge of model.edges) {
    const from = positions.get(edge.from), to = positions.get(edge.to);
    if (!from || !to) continue;
    let d;
    if (from.x === to.x) {
      const x = from.x + cardWidth, y1 = from.y + cardHeight / 2, y2 = to.y + cardHeight / 2;
      d = `M ${x},${y1} C ${x + 26},${y1} ${x + 26},${y2} ${x},${y2}`;
    } else {
      const forward = to.x > from.x, x1 = from.x + (forward ? cardWidth : 0), x2 = to.x + (forward ? 0 : cardWidth);
      const y1 = from.y + cardHeight / 2, y2 = to.y + cardHeight / 2, bend = Math.max(Math.abs(x2 - x1) / 2, 42);
      d = `M ${x1},${y1} C ${x1 + (forward ? bend : -bend)},${y1} ${x2 - (forward ? bend : -bend)},${y2} ${x2},${y2}`;
    }
    edges.append(svg("path", { d, class: `edge ${edge.type}${selected ? (edge.from === selected || edge.to === selected ? " active" : " dimmed") : ""}` }));
  }
  content.append(edges);
  for (const node of nodes) {
    const pos = positions.get(node.id);
    const group = svg("g", { class: `node ${node.type}${selected === node.id ? " selected" : ""}${related && !related.has(node.id) ? " dimmed" : ""}`,
      transform: `translate(${pos.x},${pos.y})`, tabindex: "0", role: "button", "aria-label": `${label(node)}: ${node.title}. ${status(node)}. Select for details.`, "aria-pressed": selected === node.id, "data-node": node.id });
    if (node.type === "branch") group.append(svg("path", { class: "node-shape", d: `M 8,0 H ${cardWidth - 18} L ${cardWidth},18 V ${cardHeight - 8} Q ${cardWidth},${cardHeight} ${cardWidth - 8},${cardHeight} H 8 Q 0,${cardHeight} 0,${cardHeight - 8} V 8 Q 0,0 8,0 Z` }));
    else if (node.type === "issue") group.append(svg("path", { class: "node-shape", d: `M 12,0 H ${cardWidth - 12} L ${cardWidth},12 V ${cardHeight - 12} L ${cardWidth - 12},${cardHeight} H 12 L 0,${cardHeight - 12} V 12 Z` }));
    else group.append(svg("rect", { class: "node-shape", width: cardWidth, height: cardHeight, rx: 10 }));
    group.append(svg("text", { class: "node-label", x: 14, y: 22 }, label(node)));
    wrap(node.title).forEach((line, index) => group.append(svg("text", { class: "node-title", x: 14, y: 44 + index * 16 }, line)));
    group.append(svg("circle", { class: `node-dot ${node.status}`, cx: 17, cy: 96, r: 2.5 }));
    group.append(svg("text", { class: "node-status", x: 25, y: 99 }, status(node).slice(0, 37)));
    group.append(svg("title", {}, `${node.title}\n${status(node)}`));
    group.addEventListener("click", () => { if (!moved) select(node.id); });
    group.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); event.stopPropagation(); select(node.id); } });
    content.append(group);
  }
  applyCamera();
}
function drawList(nodes) {
  $("list").replaceChildren();
  for (const group of model.groups) {
    const members = nodes.filter((node) => node.group === group.id);
    if (!members.length) continue;
    const section = element("section", "list-group");
    section.append(element("h2", "", `${group.label} · ${members.length}`));
    for (const node of members) {
      const card = element("div", `list-card${node.id === selected ? " selected" : ""}`), info = element("div", "list-info");
      card.append(element("i", `shape ${node.type}`));
      const choose = button(node.title, () => select(node.id)); choose.dataset.node = node.id;
      info.append(choose, element("p", "", `${label(node)}${node.ref ? " · " + node.ref : ""}`));
      const open = link("↗", node.url, "list-open"); open.setAttribute("aria-label", `Open ${node.title}`);
      card.append(info, element("span", `status-pill ${node.status}`, status(node)), open);
      section.append(card);
    }
    $("list").append(section);
  }
}
function drawDetails() {
  const details = $("details"), nodes = nodeMap(), node = nodes.get(selected);
  details.replaceChildren();
  if (!node) {
    const empty = element("div", "detail-empty"), art = element("div", "detail-art");
    art.setAttribute("aria-hidden", "true"); art.append(element("span"), element("span"), element("span"));
    empty.append(art, element("h2", "", "Every connection has a source."),
      element("p", "", "Select a card to see what it needs, what it unlocks, and the evidence behind each relationship."),
      element("p", "tip", "No recorded dependency does not mean ready to merge. Reviews, tests, and contributor approval still apply."));
    details.append(empty); return;
  }
  const top = element("div", `detail-top ${node.type}`), close = button("×", () => select(null));
  close.setAttribute("aria-label", "Close details"); top.append(element("span", "", label(node)), close);
  details.append(top, element("h2", "item-title", node.title), element("span", `status-pill ${node.status}`, status(node)));
  if (node.summary) details.append(element("p", "detail-description", node.summary));
  if (node.promotedFrom) details.append(element("p", "detail-meta", `Now proposed upstream from ${node.promotedFrom}. Curated branch relationships are preserved.`));
  details.append(link(`Open ${node.type === "pr" ? "pull request" : node.type === "branch" ? "branch" : "source"} ↗`, node.url, "primary-link"));
  if (node.ref) details.append(element("p", "detail-meta", `${node.repo} · ${node.ref}`));
  if (node.updatedAt) details.append(element("p", "detail-meta", `GitHub updated ${date(node.updatedAt)}`));
  if (node.reviewDecision) details.append(element("p", "detail-meta", `Snapshot review: ${node.reviewDecision.toLowerCase().replaceAll("_", " ")}`));
  if (node.type === "issue") details.append(element("p", "detail-meta", "An issue closing does not establish that hosted capacity is deployed or qualified."));
  const relationships = [
    ["REQUIRES", model.edges.filter((edge) => edge.to === node.id && edge.type !== "includes"), "from"],
    ["NEEDED BY", model.edges.filter((edge) => edge.from === node.id && edge.type !== "includes"), "to"],
    ["INCLUDES WORK FROM", model.edges.filter((edge) => edge.to === node.id && edge.type === "includes"), "from"],
    ["INCLUDED IN", model.edges.filter((edge) => edge.from === node.id && edge.type === "includes"), "to"],
  ];
  let count = 0;
  for (const [heading, edges, other] of relationships) {
    if (!edges.length) continue;
    count += edges.length;
    const section = element("section", "detail-section"); section.append(element("h3", "", heading));
    for (const edge of edges) {
      const target = nodes.get(edge[other]); if (!target) continue;
      const relation = element("div", "relation");
      relation.append(button(`${target.type === "pr" ? "#" + target.number + " · " : ""}${target.title}`, () => select(target.id)), element("p", "", edge.reason));
      if (resolved(target)) relation.append(element("p", "", `Prerequisite status: ${target.status}`));
      for (const [index, url] of (Array.isArray(edge.evidence) ? edge.evidence : [edge.evidence]).filter(Boolean).entries()) relation.append(link(`Evidence${index ? " " + (index + 1) : ""} ↗`, url, "evidence-link"));
      section.append(relation);
    }
    details.append(section);
  }
  if (count) details.append(button("Focus this connected work ↗", () => { focus = node.id; $("search").value = ""; $("group-filter").value = ""; render(true, true); }, "focus-button"));
  else details.append(element("p", "detail-description", "No dependency is recorded for this item. This is not a merge-readiness assessment."));
}
function select(id) {
  const focusedNode = document.activeElement?.getAttribute("data-node");
  selected = id;
  history.replaceState(null, "", id ? `#${encodeURIComponent(id)}` : location.pathname + location.search);
  render();
  if (focusedNode) document.querySelector(`${view === "map" ? "#graph" : "#list"} [data-node="${CSS.escape(focusedNode)}"]`)?.focus();
}
function render(reposition = false, fit = false) {
  const nodes = visibleNodes();
  $("visible-count").textContent = `${nodes.length} of ${model.nodes.length} items${focus ? " · focused work" : ""}`;
  $("clear-focus").hidden = !focus;
  $("empty").hidden = nodes.length !== 0;
  drawGraph(nodes); drawList(nodes); drawDetails();
  if (reposition) resetCamera(fit);
  $("summary").replaceChildren();
  for (const [type, title] of [["pr", "Open pull requests"], ["branch", "Working branches"], ["issue", "External items"]]) {
    const stat = element("div", "stat"), description = element("span", "stat-label");
    description.append(element("i", `shape ${type}`), document.createTextNode(title));
    stat.append(element("span", "stat-number", model.nodes.filter((node) => node.type === type && !resolved(node)).length), description);
    $("summary").append(stat);
  }
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
  render();
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
function resetFilters() {
  $("search").value = ""; $("group-filter").value = ""; $("connected-only").checked = false; focus = null;
  for (const type of ["pr", "branch", "issue"]) types.add(type);
  document.querySelectorAll("[data-type]").forEach((node) => node.setAttribute("aria-pressed", "true"));
  render(true);
}
function initialize() {
  $("search").addEventListener("input", () => render(true));
  for (const id of ["group-filter", "connected-only", "show-resolved"]) $(id).addEventListener("change", () => render(true));
  document.querySelectorAll("[data-type]").forEach((node) => node.addEventListener("click", () => {
    const type = node.dataset.type; types.has(type) ? types.delete(type) : types.add(type);
    node.setAttribute("aria-pressed", types.has(type)); render(true);
  }));
  for (const next of ["map", "list"]) $(next === "map" ? "graph-view" : "list-view").addEventListener("click", () => {
    view = next; $("graph-stage").hidden = next !== "map"; $("list").hidden = next !== "list";
    $("graph-view").setAttribute("aria-pressed", next === "map"); $("list-view").setAttribute("aria-pressed", next === "list");
  });
  $("clear-focus").addEventListener("click", () => { focus = null; render(true); });
  $("reset-filters").addEventListener("click", resetFilters);
  $("refresh").addEventListener("click", () => refresh(true));
  $("zoom-in").addEventListener("click", () => zoom(1.2)); $("zoom-out").addEventListener("click", () => zoom(1 / 1.2));
  $("fit").addEventListener("click", () => resetCamera(true));
  $("reset-view").addEventListener("click", () => { camera = { x: 18, y: 18, scale: 1 }; applyCamera(); });
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
    $("registry-age").textContent = `Relationships and saved inventory reviewed ${date(registry.meta.updatedAt)}. ${registry.meta.scope}`;
    selected = decodeURIComponent(location.hash.slice(1)) || null;
    initialize(); render(true); refresh();
  } catch (error) { $("visible-count").textContent = "Map unavailable"; setFreshness(error.message, true); }
}
start();
