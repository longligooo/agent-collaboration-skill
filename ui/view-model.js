/* Pure read-model helpers. No network, HTML interpretation, or synthetic data. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CollabUI = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const array = value => Array.isArray(value) ? value : [];
  const text = value => typeof value === "string" ? value : value == null ? "" : String(value);
  function hashIdentity(identity) {
    let hash = 2166136261;
    for (const char of text(identity)) { hash ^= char.codePointAt(0); hash = Math.imul(hash, 16777619); }
    return hash >>> 0;
  }
  function avatarSpec(identity, human = false) {
    const seed = hashIdentity(identity);
    const palettes = [
      ["#344631", "#9dc665", "#e1c69b", "#334331"],
      ["#4b382d", "#d99362", "#f0d0ad", "#a66c43"],
      ["#2b3b50", "#7597d5", "#d6dbee", "#7697cf"],
      ["#40354e", "#b29acf", "#e2c8bb", "#66527b"],
      ["#254343", "#71beb6", "#e4c49a", "#305d5b"],
      ["#4a3241", "#ce8daa", "#ebc5b9", "#89546c"],
    ];
    return { seed, palette: human ? ["#514123", "#d4b261", "#f0d4ad", "#71522a"] : palettes[seed % palettes.length], accessory: human ? 4 : (seed >>> 4) % 4, accent: (seed >>> 9) % 3 };
  }
  function participants(thread) {
    const explicit = array(thread && thread.participant_details).filter(p => p && typeof p.identity === "string" && typeof p.agent === "string");
    const byAgent = new Map(explicit.map(p => [p.agent, { ...p, actor_type: p.actor_type === "human" ? "human" : "agent", is_coordinator: p.is_coordinator === true }]));
    for (const agent of array(thread && thread.participants)) {
      if (typeof agent === "string" && !byAgent.has(agent)) byAgent.set(agent, { agent, identity: agent, actor_type: "agent", is_coordinator: agent === thread.coordinator });
    }
    if (thread && typeof thread.coordinator === "string" && !byAgent.has(thread.coordinator)) byAgent.set(thread.coordinator, { agent: thread.coordinator, identity: thread.coordinator, actor_type: "agent", is_coordinator: true });
    return [...byAgent.values()];
  }
  function actorFor(thread, author, identity, actorType = "agent") {
    const available = participants(thread);
    const match = identity ? available.find(p => p.identity === identity) : available.find(p => p.agent === author);
    return { agent: text(author) || match?.agent || text(identity) || "未知协作者", identity: text(identity) || match?.identity || text(author) || "unknown", actor_type: actorType === "human" ? "human" : "agent", is_coordinator: !!match?.is_coordinator };
  }
  function isHumanEntry(entry) {
    const source = entry && entry.provenance;
    if (!entry || entry.type !== "human-input" || entry.actor_type !== "human" || typeof entry.identity !== "string" || !entry.identity.trim() || !source || source.supplied_authorized !== true || source.original_text !== true) return false;
    if (source.kind === "manual") return true;
    return source.kind === "authorized-import" && typeof source.source_conversation_id === "string" && !!source.source_conversation_id.trim() && typeof source.source_message_id === "string" && !!source.source_message_id.trim();
  }
  function recipients(entry) {
    if (!entry || !["explicit-human-input", "current-signal"].includes(entry.recipient_source)) return [];
    return array(entry.recipients).filter(p => p && typeof p.agent === "string" && typeof p.identity === "string");
  }
  function history(thread) {
    if (Array.isArray(thread && thread.history)) return thread.history;
    return array(thread && thread.turns).filter(turn => !turn.error).map((turn, index) => ({
      ...turn, id: turn.document || `legacy-${index}`, type: "turn", actor_type: "agent", identity: actorFor(thread, turn.author).identity,
      is_coordinator: turn.author === thread.coordinator, body: "", recipients: [], recipient_source: "unknown", provenance: null, legacy: true,
    }));
  }
  function activityTime(value) {
    // Match server legacy UTC handling; reject Date.parse's date rollover and
    // permissive numeric/locale dates rather than putting malformed rows first.
    if (typeof value !== "string") return NaN;
    const match = value.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$/);
    if (!match) return NaN;
    const [, y, m, d, h = "0", min = "0", sec = "0", zone] = match;
    const year = Number(y), month = Number(m), day = Number(d);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (!year || month < 1 || month > 12 || day < 1 || day > days[month - 1] || Number(h) > 23 || Number(min) > 59 || Number(sec) > 59) return NaN;
    let normalized = value.replace(" ", "T");
    if (match[4] === undefined) normalized = `${y}-${m}-${d}T00:00:00${zone || "Z"}`;
    else if (!zone) normalized += "Z";
    const result = Date.parse(normalized);
    return Number.isFinite(result) ? result : NaN;
  }
  function lastActivityAt(thread) {
    const candidates = [thread?.last_activity_at, thread?.started_at, thread?.updated_at];
    let latest = "", latestTime = -Infinity;
    for (const value of candidates) {
      const timestamp = activityTime(value);
      if (Number.isFinite(timestamp) && timestamp > latestTime) { latest = value; latestTime = timestamp; }
    }
    return latest;
  }
  function sortThreads(threads) {
    const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
    return [...array(threads)].sort((a, b) => {
      const left = activityTime(lastActivityAt(a)), right = activityTime(lastActivityAt(b));
      const l = Number.isFinite(left) ? left : -Infinity, r = Number.isFinite(right) ? right : -Infinity;
      return (l === r ? 0 : l > r ? -1 : 1) || compare(text(a.project_id), text(b.project_id)) || compare(text(a.thread_id), text(b.thread_id));
    });
  }
  function activeRuns(payload, now = Date.now(), humanIdentities = []) {
    const human = new Set(humanIdentities);
    return array(payload && payload.runs).filter(run => run && run.active === true && run.status === "running" && typeof run.identity === "string" && !!run.identity.trim() && !human.has(run.identity) && run.actor_type !== "human" && Number.isFinite(Date.parse(run.expires_at)) && Date.parse(run.expires_at) > now);
  }
  function activeRunFor(payload, identity, now = Date.now(), humanIdentities = []) {
    return activeRuns(payload, now, humanIdentities).find(run => run.identity === identity) || null;
  }
  function manualObservations(payload, now = Date.now()) {
    return array(payload?.manual_observations?.observations).filter(row => row &&
      ["hook-event-observed", "agent-self-reported"].includes(row.evidence_kind) && typeof row.identity === "string" && row.active === false).map(row => {
      const observed = activityTime(row.observed_at), expires = activityTime(row.expires_at);
      const fresh = Number.isFinite(observed) && Number.isFinite(expires) && observed <= now && expires > now && now - observed < 15000;
      return { ...row, evidence_active: fresh && row.evidence_active === true,
        status: fresh || ["revoked", "ended"].includes(row.status) ? row.status : "unknown" };
    });
  }
  function phaseLabel(phase) {
    const labels = { planning: "制定方案", implementing: "实施中", executing: "实施中", verifying: "验证中", reviewing: "审阅中", blocked: "已阻塞", accepted: "已验收", cancelled: "已取消" };
    return Object.hasOwn(labels, phase) ? labels[phase] : text(phase) || "未提供阶段";
  }
  function categoryLabel(category) {
    const labels = { actionable: "可继续处理", "waiting-peer": "等待同伴", "waiting-human": "等待人工" };
    return Object.hasOwn(labels, category) ? labels[category] : "状态未分类";
  }
  function parseRoute(hash) {
    let parts;
    try { parts = text(hash).replace(/^#/, "").split("/").filter(Boolean).map(decodeURIComponent); }
    catch (_) { return { name: "invalid" }; }
    if (parts[0] === "auto" && parts[1] && parts.length <= 3) return { name: "auto", projectId: parts[1], taskId: parts[2] || "" };
    if (parts[0] === "threads" && parts[1] && parts[2]) return { name: "thread", projectId: parts[1], threadId: parts[2] };
    if (["projects", "people"].includes(parts[0]) && parts[1]) return { name: parts[0] === "projects" ? "project" : "person", id: parts[1] };
    if (parts[0] === "tasks") return { name: "tasks", id: parts[1] || "" };
    if (["projects", "threads", "people"].includes(parts[0])) return { name: parts[0] };
    return { name: "overview" };
  }
  return { array, text, hashIdentity, avatarSpec, participants, actorFor, isHumanEntry, recipients, history, activityTime, lastActivityAt, sortThreads, activeRuns, activeRunFor, manualObservations, phaseLabel, categoryLabel, parseRoute };
});
