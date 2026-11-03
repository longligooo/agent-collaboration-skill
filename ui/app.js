"use strict";
const M = window.CollabUI;
const app = document.querySelector("#app");
let snapshot = null;
let error = "";
let loading = false;
let activity = null;
let activityError = "";
let serverClockOffset = 0;
let activityTimer = null;
let expiryTimer = null;
let activityController = null;
let activitySequence = 0;
let messageFilter = "all";
let threadQuery = "";
let mobileThreads = false;
let taskCategory = "all";
let selectedIdentity = "";
const details = new Map();
const detailErrors = new Map();
const detailPending = new Set();
const tasks = new Map();
const taskErrors = new Map();
const taskPending = new Set();
const disclosureState = new Set();
const scrollState = new Map();
let contentLoading = false;
let contentController = null;
let contentSequence = 0;
let contentSuspended = false;
const REQUEST_TIMEOUT_MS = 60000;
const PAGE_CACHE_PREFIX = "collab.page.v1:";
const PAGE_CACHE_LIMIT = 12;
const memoryPages = new Map();
let displayedPageKey = null;
let memoryScope = null;
let reusableMessages = new Map();
try { selectedIdentity = localStorage.getItem("collab.viewIdentity") || ""; } catch (_) { /* Optional preference. */ }

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value != null && (value !== false || key.startsWith("aria-") || key.startsWith("data-"))) node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function svgNode(tag, props) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(props).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}
const iconPaths = {
  thread: "M4 4h16v12H9l-5 4V4Zm4 4h8M8 12h5", tasks: "M5 4h14v17H5ZM9 3h6v4H9Zm-1 9 2 2 5-5M8 18h8",
  people: "M12 8a3 3 0 1 0-6 0 3 3 0 0 0 6 0ZM3 21v-3a6 6 0 0 1 12 0v3M17 4a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 5v2",
  overview: "M3 3h7v7H3ZM14 3h7v7h-7ZM3 14h7v7H3ZM14 14h7v7h-7Z", project: "M3 6h7l2 3h9v12H3Z",
  lock: "M5 10h14v11H5ZM8 10V7a4 4 0 0 1 8 0v3M12 14v3", search: "M16 10a6 6 0 1 0-12 0 6 6 0 0 0 12 0Zm-1 5 5 5",
};
function icon(name) {
  const node = svgNode("svg", { class: "icon", viewBox: "0 0 24 24", "aria-hidden": "true" });
  node.append(svgNode("path", { d: iconPaths[name] || iconPaths.thread }));
  return node;
}
function route() { return M.parseRoute(location.hash.split("?")[0]); }
function pageQuery() { return new URLSearchParams(location.hash.split("?")[1] || ""); }
function currentPage() { const n = Number(pageQuery().get("page") || 1); return Number.isSafeInteger(n) && n > 0 ? n : 1; }
function pageRequest() {
  const current = route(), params = new URLSearchParams({view: current.name, page: String(currentPage())});
  if (current.projectId || current.name === "project") params.set("project_id", current.projectId || current.id);
  if (current.threadId) params.set("thread_id", current.threadId);
  if (["tasks", "person"].includes(current.name)) params.set("identity", current.id || selectedIdentity || "");
  if (pageQuery().get("q")) params.set("q", pageQuery().get("q"));
  return params;
}
function cacheNamespace() { return PAGE_CACHE_PREFIX + (location.origin || "local") + (location.pathname || "/") + ":"; }
function rememberedScope() { if(memoryScope?.namespace===cacheNamespace())return memoryScope.scope;try{return localStorage.getItem(cacheNamespace()+"scope") || "unknown";}catch(_){return "unknown";} }
function pageCacheKey(params = pageRequest()) { return cacheNamespace() + rememberedScope() + ":" + params.toString(); }
function acceptCacheScope(scope) {
  if(typeof scope!=="string" || !scope || scope===rememberedScope())return;
  memoryPages.clear(); memoryScope={namespace:cacheNamespace(),scope};
  try {
    const prefix=cacheNamespace(), old=[];
    for(let i=0;i<localStorage.length;i++){const key=localStorage.key(i);if(key?.startsWith(prefix))old.push(key);}
    for(const key of old)localStorage.removeItem(key);
    localStorage.setItem(prefix+"scope",scope);
  }catch(_){ /* Storage may be unavailable. */ }
}
function pagination() {
  const page = snapshot?.page;
  if (!page || !Number.isInteger(page.total_pages)) return null;
  const change = number => {
    const params = pageQuery(); params.set("page", String(number));
    const hash = `${location.hash.split("?")[0] || "#/"}?${params}`;
    if (hash === location.hash) load(); else location.hash = hash;
  };
  return h("nav", {class:"pagination", "aria-label":"线程分页"},
    h("button", {type:"button", class:"refresh", disabled:page.number <= 1, onclick:()=>change(page.number - 1)}, "上一页"),
    h("span", {class:"meta"}, `第 ${page.number} / ${Math.max(1,page.total_pages)} 页 · 共 ${page.total} 条 · 每页 20 条`),
    h("button", {type:"button", class:"refresh", disabled:page.number >= page.total_pages, onclick:()=>change(page.number + 1)}, "下一页"));
}
function submitThreadQuery(value) {
  const params = pageQuery(); params.set("page", "1");
  if (value.trim()) params.set("q", value.trim()); else params.delete("q");
  const hash = `${location.hash.split("?")[0]}?${params}`;
  if (hash === location.hash) load(); else location.hash = hash;
}
function threadSearch() {
  return h("form", {class:"search", onsubmit:event=>{event.preventDefault();submitThreadQuery(threadQuery);}}, icon("search"),
    h("input", {id:"thread-search", value:threadQuery, placeholder:"搜索线程…", "aria-label":"搜索线程", oninput:event=>{threadQuery=event.target.value;}}),
    h("button", {type:"submit", class:"filter"}, "搜索"));
}
function routeKey(projectId, threadId) { return JSON.stringify([projectId, threadId]); }
function threadHref(projectId, threadId) { return `#/threads/${encodeURIComponent(projectId)}/${encodeURIComponent(threadId)}`; }
function taskHref(identity) { return `#/tasks/${encodeURIComponent(identity)}`; }
function formatTime(value) {
  if (typeof value !== "string" || !value) return "时间未提供";
  const timestamp = M.activityTime(value);
  return Number.isFinite(timestamp) ? new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp)) : value;
}
function clockTime(value) {
  const timestamp = M.activityTime(value);
  return Number.isFinite(timestamp) ? new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp)) : "时间未提供";
}
function formatDuration(start, end) {
  const from = M.activityTime(start), to = end ? M.activityTime(end) : Date.now();
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return "";
  const minutes = Math.floor((to - from) / 60000);
  if (minutes < 1) return "不到 1 分钟";
  return [minutes >= 1440 ? `${Math.floor(minutes / 1440)} 天` : "", minutes >= 60 ? `${Math.floor(minutes % 1440 / 60)} 小时` : "", minutes < 1440 && minutes % 60 ? `${minutes % 60} 分钟` : ""].filter(Boolean).join(" ");
}
function threadTiming(thread) {
  if (!thread.started_at) return "";
  return `开始 ${formatTime(thread.started_at)} · ${thread.terminal ? "持续" : "已持续"} ${formatDuration(thread.started_at, thread.terminal ? thread.updated_at : "")}`;
}
const STATUS_LABELS = { OPEN: "进行中", NEEDS_INPUT: "待决定", NEEDS_CHANGES: "待修改", NO_BLOCKING_FINDINGS: "无阻塞", READY: "就绪", AGREED: "已同意", EXECUTING: "实施中", VERIFYING: "核实中", CLOSED: "已关闭", CANCELLED: "已取消", NONE: "未开始" };
const STATUS_CLASSES = { OPEN: "open", NEEDS_INPUT: "needs-input", NEEDS_CHANGES: "needs-changes", NO_BLOCKING_FINDINGS: "clear", READY: "ready", AGREED: "agreed", EXECUTING: "executing", VERIFYING: "verifying", CLOSED: "closed", CANCELLED: "cancelled", NONE: "none" };
const KIND_LABELS = { HUMAN: "人类", PROPOSAL: "方案", REVIEW: "审阅", REVISION: "修订", VERIFICATION: "验证", DECISION: "决定", RESULT: "结果", ACK: "确认" };
function ownLabel(table, key, fallback) { return Object.hasOwn(table, key) ? table[key] : fallback; }
function statusPill(status) { return h("span", { class: `pill status-${ownLabel(STATUS_CLASSES, status, "none")}`, title: status || "NONE" }, ownLabel(STATUS_LABELS, status, M.text(status) || "未开始")); }
function findProject(id) { return M.array(snapshot?.projects).find(project => project.id === id); }
function findThread(projectId, threadId) { return M.array(findProject(projectId)?.threads).find(thread => thread.thread_id === threadId) || null; }
function allThreads() { return M.sortThreads(M.array(snapshot?.projects).flatMap(project => M.array(project.threads).map(thread => ({ ...thread, project_id: project.id, project_name: project.name })))); }
function people() { return M.array(snapshot?.collaborators); }
function personIdentity(person) { return person.identity || person.stable_identity || person.id; }
function humanIdentities() {
  const result = new Set();
  for (const thread of [...allThreads(), ...details.values()]) {
    for (const person of M.participants(thread)) if (person.actor_type === "human") result.add(person.identity);
    for (const entry of M.history(thread)) if (M.isHumanEntry(entry)) result.add(entry.identity);
  }
  return [...result];
}
function nowOnServer() { return Date.now() + serverClockOffset; }
function runningFor(identity, human = false) { return human || activityError ? null : M.activeRunFor(activity, identity, nowOnServer(), humanIdentities()); }
function activityDescription(run) { return M.text(run.description || run.requested_action || run.action || "执行协作任务"); }
function actorName(identity) { return people().find(p => personIdentity(p) === identity)?.display_name || identity; }
function avatar(identity, name, size = "", human = false) {
  const spec = M.avatarSpec(identity, human), [bg, shirt, skin, hair] = spec.palette;
  const wrap = h("span", { class: `avatar ${size}${human ? " human-avatar" : ""}`, "data-avatar-id": identity, "data-avatar-name": name, "data-actor-type": human ? "human" : "agent", role: "img" });
  const svg = svgNode("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
  const path = (fill, d) => svg.append(svgNode("path", { fill, d }));
  path(bg, "M0 0h24v24H0z"); path(shirt, "M6 18h12v2h3v4H3v-4h3z");
  path(hair, "M7 5h10v2h2v10h-3v2H8v-2H5V7h2z"); path(skin, "M7 9h10v7h-2v3H9v-3H7z");
  path(hair, spec.accent === 0 ? "M7 6h10v4h-3V8h-3v2H7z" : "M7 6h10v3h-5v2H7z");
  path("#25302b", "M9 11h2v2H9zm5 0h2v2h-2z"); path("#ab8067", "M11 15h3v1h-3z");
  if (spec.accessory === 0) { path("#c9f38d", "M6 10h12v2H6z"); path("#667f48", "M8 10h3v2H8z"); }
  else if (spec.accessory === 1) { path("#674f3f", "M7 10h5v4H7zm6 0h5v4h-5z"); path(skin, "M8 11h3v2H8zm6 0h3v2h-3z"); }
  else if (spec.accessory === 2) path("#bad8ef", "M5 8h2v7H4v-5h1zm12 0h2v7h-2zM17 15h2v3h-5v-2h3z");
  else if (spec.accessory === 3) path(shirt, "M6 4h12v3H6zM4 7h16v2H4z");
  else path("#e4cb81", "M6 4h3v3h2V3h3v4h2V4h3v5H6z");
  wrap.append(svg); decorateAvatar(wrap); return wrap;
}
function decorateAvatar(node) {
  const human = node.dataset.actorType === "human";
  const run = runningFor(node.dataset.avatarId, human);
  node.classList.toggle("is-working", !!run);
  node.dataset.activityStatus = run ? "running" : "unknown";
  const evidence = human || activityError ? [] : M.manualObservations(activity, nowOnServer()).filter(row => row.identity === node.dataset.avatarId);
  const sourceInfo = evidence.map(row => `${row.evidence_kind === "hook-event-observed" ? "Hook 事件" : "Agent 自报"}：${row.status === "unknown" ? "未知或已过期" : row.status}；不是独立进程观测。`).join(" ");
  const info = run ? `当前工作中：${activityDescription(run)}（${run.thread_id}）。这是当前跨线程状态，不表示历史消息仍在编写。` : human ? "人类身份，不显示自动执行状态。" : "没有新鲜的已确认执行状态；不显示工作动效。";
  node.title = `${node.dataset.avatarName} · ${info} ${sourceInfo}`;
  node.setAttribute("aria-label", `${node.dataset.avatarName} 的固定像素头像。${info} ${sourceInfo}`);
  const existing = node.querySelector(".avatar-activity");
  if (run && !existing) node.append(h("span", { class: "avatar-activity", "aria-hidden": "true" }, [0, 1, 2, 3].map(() => h("i"))));
  else if (!run && existing) existing.remove();
}
function collaboratorPills(thread) {
  return M.participants(thread).map(person => h("a", { class: "participant", href: taskHref(person.identity), title: `${person.agent} · ${person.identity}` }, avatar(person.identity, person.agent, "sm", person.actor_type === "human"), h("span", {}, person.agent, h("small", {}, person.actor_type === "human" ? "人类" : person.is_coordinator ? "协调者 · 你的替身" : "协作者"))));
}
function avatarStack(thread) { return h("span", { class: "avatar-stack" }, M.participants(thread).map(person => avatar(person.identity, person.agent, "sm", person.actor_type === "human"))); }
function disclosure(key, label, content, className = "") {
  const node = h("details", { class: className, "data-disclosure": key, open: disclosureState.has(key) }, h("summary", {}, label), content);
  node.addEventListener("toggle", () => { if (node.isConnected !== false) node.open ? disclosureState.add(key) : disclosureState.delete(key); });
  return node;
}
function inlineText(value) {
  const fragment = document.createDocumentFragment();
  for (const part of M.text(value).split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g)) {
    if (part.startsWith("`") && part.endsWith("`")) fragment.append(h("code", {}, part.slice(1, -1)));
    else if (part.startsWith("**") && part.endsWith("**")) fragment.append(h("strong", {}, part.slice(2, -2)));
    else fragment.append(document.createTextNode(part));
  }
  return fragment;
}
function readableBody(value) {
  const node = h("div", { class: "record-body" });
  const text = M.text(value), chunks = text.split(/```([^\n]*)\n([\s\S]*?)```/g);
  for (let i = 0; i < chunks.length; i += 3) {
    for (const paragraph of chunks[i].split(/\n\s*\n/).filter(Boolean)) {
      const heading = paragraph.match(/^#{1,6} ([^\n]+)$/);
      node.append(heading ? h("strong", { class: "record-heading" }, heading[1]) : h("p", {}, inlineText(paragraph)));
    }
    if (i + 2 < chunks.length) node.append(h("div", { class: "codebox" }, h("div", { class: "code-label" }, chunks[i + 1] || "代码片段"), h("pre", {}, h("code", {}, chunks[i + 2]))));
  }
  return node;
}
function messageKind(entry) { return M.isHumanEntry(entry) ? "HUMAN" : M.text(entry.kind).toUpperCase(); }
function messageKey(entry, thread, index) { return JSON.stringify([thread.project_id, thread.thread_id, entry.id || entry.document || `legacy-${index}`]); }
function uniqueHistory(thread) {
  const seen = new Set();
  return M.history(thread).filter((entry, index) => {
    const key = messageKey(entry, thread, index);
    if (seen.has(key)) return false;
    seen.add(key); return true;
  });
}
function messageNode(entry, thread, index) {
  const human = M.isHumanEntry(entry), author = M.actorFor(thread, entry.author, entry.identity, human ? "human" : "agent");
  const coordinator = !human && (entry.is_coordinator === true || (!Array.isArray(thread.history) && author.is_coordinator));
  const key = messageKey(entry, thread, index);
  const signature = JSON.stringify([entry, author, coordinator]);
  const previous = reusableMessages.get(key);
  if (previous && previous.messageSignature === signature) return previous;
  const kind = messageKind(entry);
  const recipients = human && entry.recipient_source !== "explicit-human-input" ? [] : M.recipients(entry);
  const bubble = h("div", { class: `bubble${human ? " human-bubble" : ""}` });
  if (human) bubble.append(h("div", { class: "human-rarity" }, h("span", {}, "✦"), h("strong", {}, "HUMAN"), h("span", {}, "MYTHIC"), h("small", {}, entry.provenance.kind === "manual" ? "人工提供" : "授权导入")));
  if (recipients.length) bubble.append(h("div", { class: "mentions" }, recipients.map(person => h("span", { class: human ? "human-mention" : "mention", title: person.identity }, `@${person.agent}`))));
  const fullBody = M.text(entry.body);
  if (human) bubble.append(readableBody(fullBody || entry.summary || "记录未提供正文。"));
  else {
    const summary = M.text(entry.summary);
    if (summary) bubble.append(readableBody(summary));
    else if (fullBody) bubble.append(readableBody(fullBody));
    else bubble.append(h("p", {}, entry.title || "这条记录未提供摘要。"));
    if (summary && fullBody && fullBody.trim() !== summary.trim()) bubble.append(disclosure(`${key}/body`, "查看完整记录与代码", readableBody(fullBody), "body-details"));
  }
  if (human) {
    const source = entry.provenance;
    bubble.append(h("div", { class: "human-source" }, source.source_conversation_id ? `↗ 来源会话：${source.source_conversation_id}` : "人工提供 · 未附外部来源会话"));
    bubble.append(disclosure(`${key}/source`, "来源与归属", h("div", {},
      h("p", { title: entry.attribution || null }, source.kind === "manual" ? "人工录入的人类原话；来源由录入方提供，未独立核实原始会话。" : "用户授权提供的原始记录；来源由提供方声明，未独立核实。不代表界面自行采集外部会话。"),
      h("dl", {}, h("dt", {}, "来源消息"), h("dd", {}, source.source_message_id || "未提供（人工录入）"), h("dt", {}, "来源作者"), h("dd", {}, source.source_author || "未提供"), h("dt", {}, "接收方依据"), h("dd", {}, entry.recipient_source === "explicit-human-input" ? "来源记录明确指定" : "未提供明确目标")),
    ), "human-provenance"));
  } else if (entry.type === "human-input") bubble.append(h("p", { class: "warning" }, "这条记录缺少完整人类来源，未按人类原话呈现。"));
  if (entry.document) bubble.append(disclosure(`${key}/document`, "记录位置", h("code", { class: "path" }, entry.document), "document-details"));
  const node = h("article", { class: `message${human || coordinator ? " own" : ""}${human ? " human-message" : ""}`, "data-message-key": key, "data-author-id": author.identity, "data-message-kind": kind },
    avatar(author.identity, author.agent, "", human), h("div", { class: "message-main" },
      h("div", { class: "message-header" }, h("span", { class: "message-name" }, author.agent), h("span", { class: "message-role" }, human ? "人类" : coordinator ? "协调者 · 你的替身" : entry.role || "协作者")), bubble,
      h("div", { class: "message-footer" }, h("span", { class: `kind ${kind.toLowerCase()}` }, ownLabel(KIND_LABELS, kind, kind || "记录")), h("time", { datetime: entry.created_at || null, title: formatTime(entry.created_at) }, clockTime(entry.created_at)), entry.round != null ? h("span", {}, `R${entry.round}`) : null, entry.legacy ? h("span", {}, "旧版摘要") : null, entry.status ? statusPill(entry.status) : null),
    ));
  node.messageSignature = signature;
  return node;
}
function runningMessages(thread) {
  const runs = activityError ? [] : M.activeRuns(activity, nowOnServer(), humanIdentities()).filter(run => run.project_id === thread.project_id && run.thread_id === thread.thread_id);
  const processMessages = runs.map(run => {
    const person = M.actorFor(thread, run.agent, run.identity);
    return h("article", { class: `message task-message${person.is_coordinator ? " own" : ""}`, "data-current-run": run.run_id }, avatar(person.identity, person.agent), h("div", { class: "message-main" },
      h("div", { class: "message-header" }, person.agent, h("span", { class: "message-role" }, "当前执行状态")),
      h("div", { class: "bubble running-bubble", role: "status" }, h("div", { class: "running-line" }, h("span", { class: "typing-dots", "aria-hidden": "true" }, h("i"), h("i"), h("i")), h("strong", {}, activityDescription(run))), h("p", {}, "由本机执行观测确认，心跳失效后自动停止。"), h("div", { class: "task-sweep", "aria-hidden": "true" }, h("span"))),
      h("div", { class: "message-footer" }, "工作中", h("span", {}, `心跳 ${clockTime(run.heartbeat_at)}`)),
    ));
  });
  const evidence = activityError ? [] : M.manualObservations(activity, nowOnServer()).filter(row => row.project_id === thread.project_id && row.thread_id === thread.thread_id);
  return [...processMessages, ...evidence.map(row => h("article", { class: "message evidence-message", "data-evidence-key": `${row.project_id}/${row.binding_id}/${row.evidence_kind}` }, avatar(row.identity, row.participant || row.identity), h("div", { class: "message-main" }, h("div", { class: "message-header" }, row.participant || row.identity, h("span", { class: "message-role" }, row.evidence_kind === "hook-event-observed" ? "Hook 事件观测" : "Agent 自报")), h("div", { class: "bubble evidence-bubble" }, h("strong", {}, row.evidence_active ? (row.evidence_kind === "hook-event-observed" ? "近期收到工具事件" : "协作者自报工作中") : ({ unknown: "未知 / 证据已过期", ended: "已收到结束事件", revoked: "绑定已撤销", waiting: "自报等待", idle: "自报空闲" }[row.status] || "未确认状态")), h("p", {}, "不是独立进程观测，不显示进程执行动效。")), h("div", { class: "message-footer" }, `来源：${row.evidence_kind}`, `观测 ${clockTime(row.observed_at)}`))))];
}
function threadList(current) {
  const items = allThreads();
  return h("aside", { class: `threadlist${mobileThreads ? " mobile-open" : ""}`, "aria-label": "线程列表" },
    threadSearch(),
    h("div", { class: "section-cap" }, h("span", {}, "项目线程"), `${items.length} 个`),
    items.length ? items.map(thread => h("a", { class: `thread-item${thread.project_id === current.project_id && thread.thread_id === current.thread_id ? " active" : ""}`, href: threadHref(thread.project_id, thread.thread_id) },
      h("div", { class: "thread-meta" }, thread.project_name, h("span", {}, formatTime(M.lastActivityAt(thread)))), h("h3", {}, thread.topic), h("p", { class: "thread-preview" }, thread.objective || thread.thread_id), h("div", { class: "thread-bottom" }, avatarStack(thread), statusPill(thread.status)))) : h("p", { class: "quiet" }, "没有匹配线程。"),
    pagination(), h("p", { class: "list-info" }, "头像下像素条表示当前工作中。跨线程共享同一身份；历史头像反映当前状态。"));
}
function blockerLabel(item) {
  const labels = {"missing-evidence": "缺少验证证据", "stale-evidence": "验证证据不对应当前代码", "failed-evidence": "验证未成功结束", "blocked-evidence": "验证仍有阻塞项", "changed-log": "验证日志缺失或已变化", "missing-review": "缺少独立审阅", "stale-review": "审阅不对应当前代码", "review-not-passed": "审阅尚未通过", "self-review": "审阅必须独立于实现者", "unreviewed-evidence": "当前证据尚未全部审阅", "waiting": "仍在等待同伴或人工", "blocked-task": "检查点处于阻塞状态"};
  return [ownLabel(labels, item.code, item.message || item.code), item.review_kind === "spec" ? "规范" : item.review_kind === "quality" ? "质量" : "", item.criterion_id || item.finding_id || ""].filter(Boolean).join(" · ");
}
function inspector(thread) {
  const task = thread.coding_task, checkpoint = task?.checkpoint || {}, blockers = M.array(task?.acceptance?.blockers);
  const section = (title, content) => h("section", { class: "side-section" }, h("h4", {}, title), content);
  return h("aside", { class: "inspector", "aria-label": "任务进展与验收条件" }, h("h3", { class: "inspector-title" }, "任务上下文", h("span", { class: "small-label" }, task ? "CODING" : "LEGACY")),
    h("div", { class: "state-card" }, h("span", { class: "eyebrow" }, "当前阶段"), h("h3", {}, task ? M.phaseLabel(task.phase) : ownLabel(STATUS_LABELS, thread.status, thread.status)), h("p", {}, task ? "任务阶段不是执行心跳，也不等于当前正在工作。" : "旧版线程未提供编码任务验收模型，不能推断最终验收。")),
    section("目标", h("p", {}, thread.objective || "未提供目标。")),
    task ? section("验收检查点", M.array(task.criteria).map(criterion => { const blocked = blockers.some(item => item.criterion_id === criterion.id); return h("div", { class: "check" }, h("span", { class: `check-dot${blocked ? " pending" : ""}` }, blocked ? "·" : "✓"), h("span", {}, criterion.text || criterion.description || criterion.title || criterion.id)); })) : null,
    section(task?.acceptance?.ready ? "验收条件" : "为什么现在不能验收？", task ? task.acceptance?.ready ? h("div", { class: "success-note" }, "当前版本的证据和审阅条件已满足；最终任务状态以正式记录为准。") : blockers.length ? h("div", { class: "blocker" }, blockers.map(item => h("p", { title: item.message || null }, blockerLabel(item)))) : h("p", {}, "验收信息尚未提供。") : h("div", { class: "blocker" }, "旧版记录缺少验收条件与证据，不自动判定完成。")),
    task?.recovery?.needs_recheck ? section("恢复前需要复核", h("div", { class: "blocker" }, M.array(task.recovery.reasons).map(reason => h("p", {}, ({ "code-changed": "代码版本已变化", "messages-changed": "协作消息已变化" })[reason] || reason)))) : null,
    section("检查点与下一步", h("div", { class: "next-card" }, checkpoint.owner || task?.owner ? h("div", { class: "next-person" }, avatar(M.actorFor(thread, checkpoint.owner || task.owner).identity, checkpoint.owner || task.owner, "sm"), checkpoint.owner || task.owner) : null,
      h("p", {}, checkpoint.next_action || "尚未提供下一步；请查看协作记录。"), checkpoint.waiting_on ? h("p", { class: "quiet" }, `等待：${checkpoint.waiting_on.kind === "human" ? "人工" : "同伴"}${checkpoint.waiting_on.identity ? ` · ${checkpoint.waiting_on.identity}` : ""}`) : null,
      checkpoint.summary ? h("p", { class: "quiet" }, checkpoint.summary) : null,
      task?.revision != null ? h("code", { class: "path" }, `revision ${task.revision}`) : null,
    )),
    task && M.array(task.evidence).length ? section("验证证据", task.evidence.map(evidence => disclosure(`evidence/${thread.project_id}/${thread.thread_id}/${evidence.id}`, `${evidence.id} · ${evidence.exit_code === 0 ? "退出码 0" : evidence.exit_code == null ? "未完成" : `退出码 ${evidence.exit_code}`}`, h("div", {}, h("pre", {}, Array.isArray(evidence.command) ? evidence.command.join(" ") : evidence.command), h("p", {}, `目录：${evidence.cwd}`), h("code", { class: "path" }, evidence.log), M.array(evidence.blocked_items).map(item => h("p", { class: "warning" }, item))), "evidence"))) : null,
    task && M.array(task.reviews).length ? section("审阅记录", task.reviews.map(review => h("p", { class: "quiet" }, `${review.review_kind === "spec" ? "规范" : "质量"} · ${review.author} · ${review.verdict}`))) : null,
    task?.code_version ? section("当前代码版本", h("div", { class: "checkpoint" }, h("p", {}, `HEAD ${(task.code_version.head_sha || "无提交").slice(0, 12)}`), h("p", {}, `指纹 ${(task.code_version.worktree_fingerprint || "未提供").slice(0, 16)}`))) : null,
    M.array(thread.signals).length ? section("收件与通知", disclosure(`signals/${thread.project_id}/${thread.thread_id}`, `${thread.signals.length} 条通知`, h("div", { class: "stack" }, thread.signals.map(signalCard)))) : null,
    h("p", { class: "legend-note" }, "通知已读不等于任务完成；无新鲜执行观测时，不展示工作动效。"));
}
function renderThread(projectId, threadId) {
  const base = findThread(projectId, threadId);
  if (!base && !details.has(routeKey(projectId, threadId))) return pendingEmpty("没有这个线程。");
  const key = routeKey(projectId, threadId), thread = details.get(key) || base;
  const entries = uniqueHistory(thread), visible = entries.filter(entry => messageFilter === "all" || messageKind(entry) === messageFilter);
  const kinds = [...new Set(entries.map(messageKind).filter(Boolean))];
  return h("div", { class: "thread-workspace" }, threadList(thread), h("section", { class: "chat", "aria-label": "协作群聊记录" },
    h("div", { class: "chat-head" }, h("div", { class: "chat-titleline" }, h("h2", {}, thread.topic), statusPill(thread.status)), h("div", { class: "thread-code" }, `${thread.thread_id} · 第 ${thread.round || 0} 轮 · ${thread.coordinator ? "人类与协调者在右侧" : "未指定协调者"}`), h("div", { class: "participants", "aria-label": "群组成员" }, collaboratorPills(thread))),
    h("div", { class: "chat-tools" }, h("div", { class: "filters", "aria-label": "记录类型" }, ["all", ...kinds].map(kind => h("button", { type: "button", class: `filter${messageFilter === kind ? " active" : ""}`, "aria-pressed": messageFilter === kind, onclick: () => { messageFilter = kind; render(); } }, kind === "all" ? "全部" : ownLabel(KIND_LABELS, kind, kind)))), h("span", { class: "messages-count" }, `${visible.length} 条`)),
    detailErrors.has(key) ? h("p", { class: "inline-warning" }, `完整记录读取失败：${detailErrors.get(key)}。保留已加载记录，可能不是最新状态。`) : detailPending.has(key) && !details.has(key) ? h("p", { class: "inline-info" }, "正在读取完整记录…") : null,
    M.array(thread.history_errors).length || M.array(thread.turns).some(turn => turn.error) || thread.projection_error ? h("p", { class: "inline-warning" }, "部分历史或任务信息无法读取；未核实的来源不作为人类原话展示。") : null,
    h("div", { class: "messages", "data-scroll-key": `${key}/${messageFilter}`, "aria-label": "聊天历史" }, h("div", { class: "date-rule" }, "按记录时间排序 · 只读"), visible.length ? visible.map((entry, index) => messageNode(entry, thread, index)) : h("div", { class: "empty" }, "还没有此类记录。"), h("div", { class: "live-messages", "data-thread-key": key }, messageFilter === "all" ? runningMessages(thread) : null)),
    h("div", { class: "read-only" }, h("span", {}, icon("lock"), "只读协作记录 · 不支持在此发送消息"), h("span", { class: "activity-health" }, activityError ? "执行观测暂不可用" : "仅新鲜执行观测显示动效")),
  ), inspector(thread));
}
function signalCard(signal) { return h("div", { class: "card" }, h("div", { class: "card-top" }, `${signal.from || "?"} → ${signal.to || "?"}`, h("span", { class: "meta" }, formatTime(signal.created_at))), h("div", { class: "pills" }, h("span", { class: "pill" }, signal.decision), signal.requested_action ? h("span", { class: "pill" }, signal.requested_action) : null), signal.error ? h("p", { class: "warning" }, signal.error) : null); }
function threadCards(threads) {
  threads = M.sortThreads(threads);
  return threads.length ? h("div", { class: "stack" }, threads.map(thread => h("a", { class: "card thread-card", href: threadHref(thread.project_id, thread.thread_id) }, h("div", { class: "card-top" }, h("span", { class: "title" }, thread.topic), statusPill(thread.status)), h("div", { class: "quiet" }, `${thread.project_name} · ${thread.thread_id}`), h("p", { class: "quiet" }, threadTiming(thread)), h("div", { class: "card-bottom" }, avatarStack(thread), h("span", { class: "meta" }, `第 ${thread.round || 0} 轮`)), M.array(thread.signals).some(signal => signal.decision === "NEW") ? h("span", { class: "pill attention" }, "有待处理通知") : null))) : pendingEmpty("这个范围内还没有线程。");
}
function renderOverview() {
  if (!M.array(snapshot.projects).length) return emptyState();
  const overview = snapshot.overview || {};
  return h("div", {}, h("div", { class: "stats" }, [[overview.projects, "项目"], [overview.threads, "线程"], [overview.collaborators, "协作者"], [overview.pending, "待处理通知"]].map(([value, label]) => h("div", { class: "stat" }, h("b", {}, value == null ? "—" : value), h("span", {}, label)))),
    h("div", { class: "split" }, h("section", { class: "panel" }, h("h2", {}, overview.pending > M.array(overview.attention).length ? `需要处理（显示前 ${M.array(overview.attention).length} 条）` : "需要处理"), M.array(overview.attention).length ? h("div", { class: "stack" }, overview.attention.map(item => h("a", { class: "row", href: threadHref(item.project_id, item.thread_id) }, h("strong", {}, item.topic || item.thread_id), h("div", { class: "quiet" }, `${item.project_name} · ${item.from || "?"} → ${item.to}`), h("span", { class: "pill attention" }, item.requested_action || item.decision)))) : pendingEmpty("没有待处理的通知。")),
      h("section", { class: "panel" }, h("h2", {}, "最近动态"), M.array(overview.recent).length ? h("div", { class: "stack" }, overview.recent.map(item => h("a", { class: "row", href: threadHref(item.project_id, item.thread_id) }, h("strong", {}, item.title || item.topic), h("div", { class: "quiet" }, `${item.project_name} · ${item.author} · ${item.kind}`), item.summary ? h("p", {}, item.summary) : null))) : pendingEmpty("还没有协作记录。"))));
}
function renderProjects() { return M.array(snapshot.projects).length ? h("div", { class: "stack" }, snapshot.projects.map(project => h("a", { class: "card", href: `#/projects/${encodeURIComponent(project.id)}` }, h("div", { class: "card-top" }, h("strong", {}, project.name), h("span", { class: "meta" }, project.available ? `${project.thread_count ?? M.array(project.threads).length} 个线程` : "目录不可用")), h("p", { class: "path" }, project.root)))) : emptyState(); }
function renderProject(id) { const project = findProject(id); return !project ? pendingEmpty("没有这个项目。") : project.available ? h("div", {}, h("p", { class: "path" }, project.root), h("div", { class: "auto-project-entry" }, h("div", {}, h("strong", {}, "自动协作任务"), h("p", { class: "quiet" }, "协调者先规划，确认团队后再执行。手动线程保持不变。")), h("a", { class: "auto-btn primary", href: `#/auto/${encodeURIComponent(id)}` }, "打开自动协作 →")), threadCards(M.array(project.threads)), pagination()) : missing("这个目录已经不在本机上。"); }
function renderPeople() {
  return people().length ? h("div", { class: "people-grid" }, people().map(person => {
    const identity = personIdentity(person), human = person.actor_type === "human" || humanIdentities().includes(identity);
    return h("section", { class: "person-card" }, h("div", { class: "person-head" }, avatar(identity, person.display_name || identity, "lg", human), h("div", {}, h("h3", {}, person.display_name || identity), h("p", { class: "quiet" }, human ? "人类来源身份" : "稳定协作身份"))), h("dl", {}, h("dt", {}, "身份键"), h("dd", {}, identity), h("dt", {}, "参与"), h("dd", {}, `${M.array(person.projects).length} 项目 · ${person.thread_count ?? M.array(person.threads).length} 线程`), h("dt", {}, "本地别名"), h("dd", {}, M.array(person.aliases).map(alias => alias.agent).join("、") || person.id), h("dt", {}, "通知"), h("dd", {}, `${person.pending == null ? "未汇总" : person.pending + " 条待处理"}；通知不代表执行中`)), h("div", { class: "pills" }, M.array(person.tools).map(tool => h("span", { class: "pill" }, tool))), h("a", { class: "text-link", href: taskHref(identity) }, "查看该协作者的任务 →"), h("a", { class: "text-link", href: `#/people/${encodeURIComponent(identity)}` }, "查看参与线程 →"));
  })) : emptyState();
}
function renderPerson(id) { const person = people().find(item => personIdentity(item) === id); return person ? h("div", {}, threadCards(M.array(person.threads)), pagination()) : pendingEmpty("没有这个协作者。"); }
function selectIdentity(identity) { selectedIdentity = identity; try { localStorage.setItem("collab.viewIdentity", identity); } catch (_) { /* Optional preference. */ } location.hash = taskHref(identity); if (route().id === identity) render(); }
function taskCard(item) {
  const task = item.coding_task, checkpoint = task?.checkpoint || {};
  return h("a", { class: "task-card", href: threadHref(item.project_id, item.thread_id) }, h("div", { class: "task-id" }, `${item.project_name} · ${item.thread_id}`), h("h4", {}, item.topic), h("span", { class: `task-tag ${item.classification}` }, task ? M.phaseLabel(task.phase) : "旧版线程"), h("p", {}, item.next_action || "下一步尚未提供，请查看协作记录。"), h("div", { class: "task-context" }, h("b", {}, "已保存的上下文"), h("p", {}, checkpoint.summary || (checkpoint.waiting_on ? `等待 ${checkpoint.waiting_on.identity || checkpoint.waiting_on.kind}` : item.objective || "任务发现不会标记通知已读。")), task?.recovery?.needs_recheck ? h("p", { class: "warning" }, "上下文已变化，恢复前需要复核。") : null), h("span", { class: "task-open" }, item.classification === "actionable" ? "查看恢复点 →" : "查看等待原因 →"));
}
function renderTasks(identity) {
  const options = people();
  if (!identity) return pendingEmpty("还没有可选的协作者身份。");
  const result = tasks.get(identity), failure = taskErrors.get(identity);
  const human = humanIdentities().includes(identity);
  return h("div", { class: "tasks-page" }, h("section", { class: "identity-bar" }, avatar(identity, actorName(identity), "lg", human), h("div", {}, h("h2", {}, `${actorName(identity)} 的工作队列`), h("p", { class: "quiet" }, "稳定身份：", h("code", {}, identity))),
    h("div", { class: "identity-switch" }, h("label", { for: "identity-select" }, "按协作者查看 · 不改变任务归属"), h("select", { id: "identity-select", onchange: event => selectIdentity(event.target.value) }, [...new Set([identity, ...options.map(personIdentity)])].map(id => h("option", { value: id, selected: id === identity }, actorName(id))))),
    h("div", { class: "identity-options", "aria-label": "身份选择" }, options.map(person => { const id = personIdentity(person); return h("button", { class: `identity-option${id === identity ? " selected" : ""}`, type: "button", "aria-pressed": id === identity, onclick: () => selectIdentity(id) }, avatar(id, actorName(id), "sm", humanIdentities().includes(id)), h("span", {}, actorName(id))); }))),
    h("div", { class: "task-filters" }, h("label", { for: "task-category" }, "任务状态"), h("select", { id: "task-category", onchange: event => { taskCategory = event.target.value; render(); } }, ["all", "actionable", "waiting-peer", "waiting-human"].map(category => h("option", { value: category, selected: taskCategory === category }, category === "all" ? "全部状态" : M.categoryLabel(category))))),
    failure ? h("div", { class: "banner" }, `读取任务失败：${failure}`) : null,
    !result ? missing(taskPending.has(identity) ? "正在读取任务…" : "尚未读取任务。") : h("div", { class: "task-columns" }, ["actionable", "waiting-peer", "waiting-human"].filter(category => taskCategory === "all" || taskCategory === category).map(category => { const items = M.array(result.items).filter(item => item.classification === category); return h("section", { class: `task-column ${category}` }, h("h3", {}, h("i", { class: "column-dot" }), M.categoryLabel(category), h("small", {}, `${items.length} 项`)), items.length ? items.map(taskCard) : h("div", { class: "task-empty" }, "当前没有此类任务。")); })),
    M.array(result?.errors).length ? disclosure(`tasks/${identity}/errors`, `${result.errors.length} 个项目或线程读取异常`, h("div", {}, result.errors.map(item => h("p", {}, `${item.project_root || item.thread_id || ""}：${item.error}`))), "task-errors") : null,
    pagination(), h("p", { class: "tasks-note" }, "这里只读任务与检查点。查看任务不会执行命令、发送消息或确认通知；“可继续处理”不代表当前正在执行。"));
}
function pendingEmpty(message) { return missing(message); }
function emptyState() { return h("div", { class: "empty" }, "还没有注册到本机界面的项目。在项目里执行 ", h("code", {}, "python scripts/collab.py ui --root ."), " 后，这里会显示线程和协作者。"); }
function missing(message) { return h("div", { class: "empty" }, message); }
function pageTitle(current) {
  if (current.name === "auto") return window.AutoUI.title(current);
  if (current.name === "projects") return ["项目", "按仓库查看本机协作"];
  if (current.name === "project") { const project = findProject(current.id); return [project?.name || "项目", project?.root || ""]; }
  if (["threads", "thread"].includes(current.name)) return ["协作线程", "在同一个上下文里查看讨论、接力与验收"];
  if (["people", "person"].includes(current.name)) return [current.name === "person" ? current.id : "协作者", "稳定身份、固定像素头像与当前执行状态"];
  if (current.name === "tasks") return ["我的任务", "从检查点继续，而不是从通知重新开始"];
  return ["总览", "本机已注册项目的协作状态"];
}
function nav(label, href, active, symbol) { return h("a", { class: `nav-link${active ? " active" : ""}`, href, title: label, "aria-current": active ? "page" : null }, icon(symbol), h("span", { class: "nav-text" }, label)); }
function documentScrollsMessages(messages) { return window.getComputedStyle?.(messages).overflowY === "visible"; }
function setDocumentScroll(top) { window.scrollTo?.({ left: window.scrollX || 0, top, behavior: "instant" }); }
function rememberView() {
  for (const node of document.querySelectorAll("details[data-disclosure]")) {
    if (node.open) disclosureState.add(node.dataset.disclosure); else disclosureState.delete(node.dataset.disclosure);
  }
  const messages = document.querySelector(".messages");
  if (messages) {
    // At narrow widths the chat grows naturally and the document, not the
    // messages element, scrolls. Preserve the same visible record in both modes.
    const page = documentScrollsMessages(messages), scroller = document.scrollingElement;
    const scrollTop = page ? window.scrollY || scroller?.scrollTop || 0 : messages.scrollTop;
    const top = page ? 0 : messages.getBoundingClientRect?.().top;
    const anchor = Number.isFinite(top) && (!page || scrollTop > 0) ? [...messages.querySelectorAll(".message[data-message-key]")].find(node => {
      const rect = node.getBoundingClientRect();
      return rect.bottom > top && (!page || rect.top < window.innerHeight);
    }) : null;
    const bottom = page ? !!scroller && scroller.scrollHeight - scrollTop - window.innerHeight < 40 : messages.scrollHeight - scrollTop - messages.clientHeight < 40;
    scrollState.set(messages.dataset.scrollKey, { mode: page ? "document" : "element", top: scrollTop, bottom,
      anchor: anchor?.dataset.messageKey, offset: anchor ? anchor.getBoundingClientRect().top - top : null });
  }
}
function restoreMessages() {
  const messages = document.querySelector(".messages");
  if (!messages) return;
  const saved = scrollState.get(messages.dataset.scrollKey), page = documentScrollsMessages(messages);
  if (page) {
    if (!saved || saved.mode !== "document") return;
    setDocumentScroll(saved.bottom ? document.scrollingElement.scrollHeight : saved.top);
  } else {
    if (!saved || saved.mode === "document" || saved.bottom) { messages.scrollTop = messages.scrollHeight; return; }
    messages.scrollTop = saved.top;
  }
  if (saved.bottom) return;
  const anchor = [...messages.querySelectorAll(".message[data-message-key]")].find(node => node.dataset.messageKey === saved.anchor);
  if (anchor && Number.isFinite(saved.offset) && messages.getBoundingClientRect) {
    const delta = anchor.getBoundingClientRect().top - (page ? 0 : messages.getBoundingClientRect().top) - saved.offset;
    if (page) setDocumentScroll((window.scrollY || 0) + delta); else messages.scrollTop += delta;
  }
}
function viewState() {
  const focused = document.activeElement;
  return { focused, focusId: focused?.getAttribute?.("id"), start: focused?.selectionStart, end: focused?.selectionEnd,
    scrolls: [".main", ".threadlist", ".inspector"].map(selector => ({ selector, top: document.querySelector(selector)?.scrollTop || 0 })) };
}
function restoreView(saved) {
  for (const item of saved.scrolls) { const node = document.querySelector(item.selector); if (node) node.scrollTop = item.top; }
  let focused = saved.focused?.isConnected ? saved.focused : null;
  if (!focused && saved.focusId) focused = [...document.querySelectorAll("[id]")].find(node => node.getAttribute("id") === saved.focusId);
  if (focused) {
    focused.focus({ preventScroll: true });
    if (typeof saved.start === "number" && focused.setSelectionRange) focused.setSelectionRange(saved.start, saved.end);
  }
}
function render() {
  rememberView();
  const savedView = viewState();
  reusableMessages = new Map([...document.querySelectorAll(".message[data-message-key]")].map(node => [node.dataset.messageKey, node]));
  const current = route(), [title, subtitle] = pageTitle(current);
  const active = ({ auto: "projects", project: "projects", thread: "threads", person: "people" })[current.name] || current.name;
  let body;
  const identity = current.id || selectedIdentity || (people()[0] && personIdentity(people()[0])) || "";
  if (!snapshot) body = missing(error || "正在读取本机记录…");
  else if (current.name === "auto") body = window.AutoUI.render(current);
  else if (current.name === "overview") body = renderOverview();
  else if (current.name === "projects") body = renderProjects();
  else if (current.name === "project") body = renderProject(current.id);
  else if (current.name === "threads") body = h("div", {}, threadSearch(), threadCards(allThreads()), pagination());
  else if (current.name === "thread") body = renderThread(current.projectId, current.threadId);
  else if (current.name === "people") body = renderPeople();
  else if (current.name === "person") body = renderPerson(current.id);
  else if (current.name === "tasks") body = renderTasks(identity);
  else body = missing("地址格式无效，请从导航重新进入。");
  app.replaceChildren(h("div", { class: "shell" }, h("aside", { class: "sidebar" }, h("a", { class: "brand", href: "#/" }, h("span", { class: "logo", "aria-hidden": "true" }, Array.from({ length: 9 }, () => h("i"))), h("span", { class: "brand-text" }, "协作空间", h("small", {}, "AGENT COLLAB"))),
    h("div", { class: "rail-label" }, "WORKSPACE"), h("nav", { "aria-label": "主导航" }, nav("总览", "#/", active === "overview", "overview"), nav("项目", "#/projects", active === "projects", "project"), nav("协作线程", "#/threads", active === "threads", "thread"), nav("我的任务", taskHref(selectedIdentity || ""), active === "tasks", "tasks"), nav("协作者", "#/people", active === "people", "people")),
    h("div", { class: "sidebar-foot" }, h("p", {}, current.name === "auto" ? "本机自动协作 · 显式授权" : "本机只读界面"), h("code", {}, `127.0.0.1:${snapshot?.port || 23451}`), h("p", {}, "像素条表示当前工作中；等待或状态过期时不显示。"))),
    h("main", { class: `main${current.name === "thread" ? " thread-page" : ""}` }, error ? h("div", { class: "banner" }, error) : null,
      h("header", { class: "top" }, h("div", {}, h("h1", {}, title), h("p", { class: "sub" }, subtitle)), h("div", { class: "top-actions" }, snapshot ? h("span", { class: "meta snapshot-time" }, `更新于 ${formatTime(snapshot.generated_at)}`) : null, h("button", { type: "button", class: "refresh", disabled: loading, onclick: () => current.name === "auto" ? window.AutoUI.refresh(current) : load() }, loading ? "读取中…" : "刷新"), current.name === "thread" ? h("button", { class: "mobile-threads", type: "button", "aria-expanded": mobileThreads, onclick: () => { mobileThreads = !mobileThreads; render(); } }, "切换线程") : null)), body)));
  reusableMessages = new Map();
  restoreMessages();
  restoreView(savedView);
  paintActivity();
}
async function getJSON(url, signal) {
  const response = await fetch(url, {signal, cache:"no-store"});
  if (!response.ok) throw new Error(`读取失败（${response.status}）`);
  return response.json();
}
function contentSignature(payload) {
  if (!payload || typeof payload !== "object") return JSON.stringify(payload);
  const {generated_at, ...content} = payload;
  return JSON.stringify(content);
}
function validPage(payload) {
  const object=value=>value!==null && typeof value==="object" && !Array.isArray(value);
  const thread=value=>object(value) && typeof value.thread_id==="string" && (!value.history || (Array.isArray(value.history) && value.history.every(object))) && (!value.turns || (Array.isArray(value.turns) && value.turns.every(object)));
  const current=route();
  if(!object(payload) || payload.schema_version!==1 || payload.view!==current.name ||
    !Array.isArray(payload.projects) || !payload.projects.every(p=>object(p) && typeof p.id==="string" && Array.isArray(p.threads) && p.threads.every(thread)) ||
    !Array.isArray(payload.collaborators) || !payload.collaborators.every(p=>object(p) && typeof (p.identity || p.id)==="string"))return false;
  if(payload.page && (!object(payload.page) || !Number.isInteger(payload.page.number) || payload.page.number<1 || payload.page.size!==20 || !Number.isInteger(payload.page.total) || payload.page.total<0 || !Number.isInteger(payload.page.total_pages) || payload.page.total_pages<1))return false;
  if(["project","thread","auto"].includes(current.name) && !payload.projects.some(p=>p.id===(current.projectId || current.id)))return false;
  if(current.name==="thread" && (!thread(payload.detail) || payload.detail.project_id!==current.projectId || payload.detail.thread_id!==current.threadId))return false;
  if(payload.tasks && (!object(payload.tasks) || !Array.isArray(payload.tasks.items) || !payload.tasks.items.every(object)))return false;
  return true;
}
function renderPageSafely() {
  try { render();return true; }
  catch (_) { snapshot=null;details.clear();tasks.clear();error="缓存记录格式无效，正在重新读取页面。";render();return false; }
}
function readPageCache(key) {
  if (memoryPages.has(key)) return memoryPages.get(key);
  try {
    const record = JSON.parse(localStorage.getItem(key) || "null");
    if (record?.schema !== 1 || !validPage(record.payload)) return null;
    return record.payload;
  } catch (_) { return null; }
}
function savePageCache(key, payload) {
  memoryPages.delete(key); memoryPages.set(key, payload);
  while (memoryPages.size > PAGE_CACHE_LIMIT) memoryPages.delete(memoryPages.keys().next().value);
  try {
    const value = JSON.stringify({schema:1, saved_at:Date.now(), payload});
    // Large conversations remain available in memory without exhausting origin storage.
    if (value.length > 1500000) return;
    const keys = [];
    for (let i=0;i<localStorage.length;i++) { const k=localStorage.key(i); if(k?.startsWith(PAGE_CACHE_PREFIX) && !k.endsWith(":scope")) keys.push(k); }
    keys.sort((a,b)=>{try{return (JSON.parse(localStorage.getItem(a))?.saved_at||0)-(JSON.parse(localStorage.getItem(b))?.saved_at||0);}catch(_){return 0;}});
    while (keys.filter(k=>k!==key).length >= PAGE_CACHE_LIMIT) {const old=keys.shift();if(old!==key)localStorage.removeItem(old);}
    localStorage.setItem(key,value);
  } catch (_) { /* Quota, blocked storage, or malformed old entries never blocks reads. */ }
}
function applyPage(payload, key) {
  snapshot=payload; displayedPageKey=key;
  details.clear(); tasks.clear(); detailErrors.clear(); taskErrors.clear();
  if(payload.detail?.project_id && payload.detail?.thread_id) details.set(routeKey(payload.detail.project_id,payload.detail.thread_id),payload.detail);
  if(payload.tasks) {
    const identity=payload.tasks.identity || payload.identity || route().id || selectedIdentity || (people()[0] && personIdentity(people()[0]));
    if(identity)tasks.set(identity,payload.tasks);
  }
}
async function load({background=false, reason="refresh"}={}) {
  if(document.hidden || contentSuspended)return;
  const params=pageRequest(), key=pageCacheKey(params), sequence=++contentSequence;
  contentController?.abort();
  const controller=new AbortController();contentController=controller;contentLoading=true;
  const cached=readPageCache(key);
  const oldSignature=contentSignature(snapshot);
  const samePage=displayedPageKey===key;
  if(!samePage){error="";threadQuery=pageQuery().get("q") || "";}
  if(cached)applyPage(cached,key);
  else if(!samePage){snapshot=null;details.clear();tasks.clear();displayedPageKey=key;}
  loading=!snapshot;
  if(!samePage || oldSignature!==contentSignature(snapshot) || loading){if(!renderPageSafely()){memoryPages.delete(key);try{localStorage.removeItem(key);}catch(_){}}}
  const timeout=setTimeout(()=>controller.abort(),REQUEST_TIMEOUT_MS);
  const valid=()=>sequence===contentSequence && pageCacheKey()===key && !controller.signal.aborted;
  try {
    const payload=await getJSON(`/api/page?${params}`,controller.signal);
    if(!valid())return;
    if(!validPage(payload))throw new Error("返回的页面数据格式无效");
    const selected=route();
    if(selected.name==="thread" && (!payload.detail || payload.detail.project_id!==selected.projectId || payload.detail.thread_id!==selected.threadId))throw new Error("返回的线程与当前页面不符");
    const changed=contentSignature(snapshot)!==contentSignature(payload) || !!error;
    const previous=snapshot;applyPage(payload,key);error="";
    if(changed){
      try{render();}catch(_){if(previous)applyPage(previous,key);else{snapshot=null;details.clear();tasks.clear();}throw new Error("返回的页面记录格式无效");}
    }
    acceptCacheScope(payload.cache_scope);const freshKey=pageCacheKey(params);displayedPageKey=freshKey;savePageCache(freshKey,payload);
    if(!changed){const stamp=document.querySelector(".snapshot-time");if(stamp)stamp.textContent=`更新于 ${formatTime(snapshot.generated_at)}`;}
  } catch(exc) {
    if(sequence!==contentSequence || pageCacheKey()!==key)return;
    const previousError=error; error=`${exc.name==="AbortError"?"读取超时":exc.message || "无法读取页面"}。${snapshot?"保留缓存数据，可能不是最新状态。":"请点击刷新重试。"}`;if(error!==previousError)render();
  } finally {
    clearTimeout(timeout);
    if(sequence===contentSequence){loading=false;contentLoading=false;const button=document.querySelector(".refresh");if(button){button.disabled=false;button.textContent="刷新";}}
  }
}
function pauseContent() {
  contentSequence+=1;contentController?.abort();contentLoading=false;loading=false;
}
function paintActivity() {
  window.AutoUI?.paintActivity();
  document.querySelectorAll(".avatar[data-avatar-id]").forEach(decorateAvatar);
  document.querySelectorAll(".activity-health").forEach(node => { node.textContent = activityError ? "执行观测暂不可用" : "仅新鲜执行观测显示动效"; });
  const slot = document.querySelector(".live-messages");
  if (slot) {
    const current = route(), thread = details.get(slot.dataset.threadKey) || findThread(current.projectId, current.threadId);
    const messages = slot.closest(".messages"), atBottom = messages && messages.scrollHeight - messages.scrollTop - messages.clientHeight < 40;
    if (thread) slot.replaceChildren(...(messageFilter === "all" ? runningMessages(thread) : []));
    if (atBottom && messages) messages.scrollTop = messages.scrollHeight;
  }
  clearTimeout(expiryTimer);
  const expiries = [...M.activeRuns(activityError ? null : activity, nowOnServer(), humanIdentities()).map(run => Date.parse(run.expires_at)), ...M.manualObservations(activityError ? null : activity, nowOnServer()).filter(row => row.evidence_active).map(row => Math.min(Date.parse(row.expires_at), Date.parse(row.observed_at) + 15000))];
  if (expiries.length) expiryTimer = setTimeout(paintActivity, Math.max(20, Math.min(...expiries) - nowOnServer() + 20));
}
async function pollActivity() {
  clearTimeout(activityTimer);
  if (document.hidden) return;
  const sequence = ++activitySequence;
  activityController?.abort();
  activityController = new AbortController();
  const controller = activityController;
  const timeout = setTimeout(() => controller.abort(), 6000);
  try {
    const scope = new URLSearchParams();
    const current=route();
    let visibleProjects=[];
    if(["threads","project"].includes(current.name))visibleProjects=M.array(snapshot?.projects).filter(project=>M.array(project.threads).length).map(project=>project.id);
    else if(current.name==="person")visibleProjects=M.array(people().find(person=>personIdentity(person)===current.id)?.threads).map(thread=>thread.project_id);
    else if(current.name==="tasks")visibleProjects=M.array(tasks.get(current.id || selectedIdentity || (people()[0] && personIdentity(people()[0])))?.items).map(item=>item.project_id);
    else if(["thread","auto"].includes(current.name))visibleProjects=[current.projectId];
    else if(current.name==="people")visibleProjects=M.array(snapshot?.projects).map(project=>project.id);
    for (const id of new Set(visibleProjects.filter(Boolean))) scope.append("project_id",id);
    if (!scope.toString()) { activity = {runs:[]}; activityError=""; return; }
    const payload = await getJSON(`/api/activity?${scope}`, controller.signal);
    if (sequence !== activitySequence) return;
    if (!payload || !Array.isArray(payload.runs)) throw new Error("执行观测格式无效");
    activity = payload; activityError = "";
    const generated = Date.parse(payload.generated_at); serverClockOffset = Number.isFinite(generated) ? generated - Date.now() : 0;
  } catch (exc) { if (sequence === activitySequence) { activity = null; activityError = "执行观测不可用"; } }
  finally {
    clearTimeout(timeout);
    if (sequence === activitySequence) { paintActivity(); if (!document.hidden) activityTimer = setTimeout(pollActivity, 2000); }
  }
}
window.AutoUI?.configure({h,avatar,render,route,findProject,messageNode,uniqueHistory,runningMessages,threadHref,identityFor:(projectId,agent)=>{const person=people().find(p=>M.array(p.aliases).some(a=>a.project_id===projectId&&a.agent===agent));return person?personIdentity(person):agent;}});
window.addEventListener("hashchange", () => { window.AutoUI?.navigate(route()); messageFilter = "all"; mobileThreads = false; load({ background: true, reason: "navigation" }); });
document.addEventListener("visibilitychange", () => { if (document.hidden) { clearTimeout(activityTimer); activitySequence += 1; activityController?.abort(); pauseContent(); } else { render(); pollActivity(); } });
window.addEventListener("pagehide", () => { contentSuspended = true; pauseContent(); clearTimeout(activityTimer); clearTimeout(expiryTimer); activitySequence += 1; activityController?.abort(); });
window.addEventListener("pageshow", event => { if (event.persisted) { contentSuspended = false; render(); pollActivity(); } });
load();
pollActivity();
