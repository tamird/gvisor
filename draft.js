// An item can carry multiple replies to the same revision. Include the text
// in its address so an old link cannot open a replacement that needs review.
async function draftKey(draft) {
  const source = JSON.stringify([draft.kind, draft.head, draft.url, draft.title, draft.body, draft.replyBody]);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(source));
  const hex = Array.from(new Uint8Array(digest, 0, 10), byte => byte.toString(16).padStart(2, "0")).join("");
  return draft.kind + "-" + hex;
}

async function draftURL(itemID, draft, currentURL) {
  const url = new URL(currentURL);
  url.pathname = new URL("draft.html", url).pathname;
  url.searchParams.set("draft", await draftKey(draft));
  url.hash = encodeURIComponent(itemID);
  return url;
}

function draftReturnURL(currentURL) {
  const url = new URL(currentURL);
  url.pathname = new URL("./", url).pathname;
  url.searchParams.delete("draft");
  return url;
}

async function findDraft(item, key) {
  if (!item || !key) return null;
  const drafts = [item.contributorReview, ...(item.additionalReviews || []), item.humanAction, ...(item.settledReviews || [])];
  for (const draft of drafts) {
    if (draft?.body && key.startsWith(draft.kind + "-") && await draftKey(draft) === key) return draft;
  }
  return null;
}

function sourcePublication(node, decision, prs, primary, receipts = []) {
  if (!decision || ![undefined, "proposal", "amendment"].includes(decision.kind) || !/^[a-f0-9]{40}$/.test(decision.head || "")) return null;
  const recorded = [...(node.settledReviews || []), ...receipts].find(item => (item.kind || "proposal") === (decision.kind || "proposal")
    && item.head === decision.head && item.outcome === "posted" && item.postedURL);
  return prs.find(pr => {
    const repo = pr.repo || primary;
    if (pr.url !== `https://github.com/${repo}/pull/${pr.number}`) return false;
    if (recorded?.postedURL === pr.url) return true;
    const statuses = decision.kind === "amendment" ? ["open", "draft", "merged", "closed"] : ["open", "draft", "merged"];
    if (pr.head !== decision.head || !statuses.includes(pr.status)) return false;
    let matches;
    if (decision.kind === "amendment") matches = node.type === "pr" && (node.repo || primary) === repo && node.number === pr.number;
    else {
      const commit = /^https:\/\/github\.com\/([\w.-]+\/[\w.-]+)\/commit\/([a-f0-9]{9}|[a-f0-9]{40})$/.exec(decision.url || "");
      const compare = /^https:\/\/github\.com\/([\w.-]+\/[\w.-]+)\/compare\/([a-f0-9]{9})\.\.\.([a-f0-9]{9})$/.exec(decision.url || "");
      let sourceRepo;
      if (commit && [decision.head, decision.head.slice(0, 9)].includes(commit[2])) sourceRepo = commit[1];
      else if (node.type === "branch" && compare && compare[2] === decision.baseHead?.slice(0, 9)
        && compare[3] === decision.head.slice(0, 9)) sourceRepo = compare[1];
      else return false;
      matches = [repo, pr.headRepo].includes(sourceRepo) && repo === (node.repo || primary);
      if (node.type === "branch" && node.repo === sourceRepo)
        matches = repo === primary && pr.headRepo === sourceRepo && pr.ref === node.ref;
    }
    return matches;
  }) || null;
}

function decisionReceipt(node, decision, receipts = []) {
  const issueUpdate = node.type === "issue" && decision.kind === "proposal" && decision.head === undefined;
  return [...(node.settledReviews || []), ...receipts].find(item => (item.kind || "proposal") === (decision.kind || "proposal") && item.head === decision.head
    && (![undefined, "proposal", "amendment"].includes(item.kind) || item.replyBody === decision.replyBody)
    && (!(issueUpdate || ["review", "reply", "issue", "information", "exception", "closure"].includes(item.kind)) || item.body === decision.body)
    && (!(issueUpdate || ["issue", "information", "exception", "closure"].includes(item.kind))
      || item.url === decision.url && item.title === decision.title)) || null;
}
