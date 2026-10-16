const app = document.querySelector("#app");
let snapshot = null;
let error = "";
let loading = false;

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value != null && value !== false) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function route() {
  const parts = location.hash.replace(/^#/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "projects" && parts[1]) return { name: "project", id: parts[1] };
  if (parts[0] === "projects") return { name: "projects" };
  if (parts[0] === "threads" && parts[1] && parts[2]) {
    return { name: "thread", projectId: parts[1], threadId: parts[2] };
  }
  if (parts[0] === "threads") return { name: "threads" };
  if (parts[0] === "people" && parts[1]) return { name: "person", id: parts[1] };
  if (parts[0] === "people") return { name: "people" };
  return { name: "overview" };
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function decisionClass(decision) {
  if (decision === "NEW") return "attention";
  if (decision === "TERMINAL") return "info";
  if (decision === "INVALID") return "danger";
  return "";
}

function decisionLabel(decision) {
  return { NEW: "待处理", TERMINAL: "待确认", INVALID: "无效", DUPLICATE: "已处理" }[decision] || decision;
}

function findProject(id) {
  return (snapshot.projects || []).find((project) => project.id === id);
}

function findThread(projectId, threadId) {
  const project = findProject(projectId);
  if (!project) return null;
  return project.threads.find((thread) => thread.thread_id === threadId) || null;
}

function findPerson(id) {
  return (snapshot.collaborators || []).find((person) => person.id === id);
}

function threadHref(projectId, threadId) {
  return `#/threads/${encodeURIComponent(projectId)}/${encodeURIComponent(threadId)}`;
}

function pillsForSignal(signal) {
  return [
    h("span", { class: `pill ${decisionClass(signal.decision)}` }, decisionLabel(signal.decision)),
    signal.requested_action ? h("span", { class: "pill" }, signal.requested_action) : null,
    signal.round ? h("span", { class: "pill" }, `第 ${signal.round} 轮`) : null,
  ];
}

function attentionRow(item) {
  return h(
    "a",
    { class: "row", href: threadHref(item.project_id, item.thread_id) },
    h(
      "div",
      { class: "row-top" },
      h("span", { class: "title" }, item.topic || item.thread_id),
      h("span", { class: "meta" }, formatTime(item.created_at)),
    ),
    h("div", { class: "quiet" }, `${item.project_name} · ${item.from || "?"} → ${item.to}`),
    h("div", { class: "pills" }, pillsForSignal(item)),
  );
}

function recentRow(item) {
  return h(
    "a",
    { class: "row", href: threadHref(item.project_id, item.thread_id) },
    h(
      "div",
      { class: "row-top" },
      h("span", { class: "title" }, item.title || item.topic),
      h("span", { class: "meta" }, formatTime(item.created_at)),
    ),
    h("div", { class: "quiet" }, `${item.project_name} · ${item.author} · ${item.kind}`),
    item.summary ? h("p", { class: "summary" }, item.summary) : null,
  );
}

function renderOverview() {
  if (!snapshot.projects.length) return emptyState();
  const overview = snapshot.overview;
  return h(
    "div",
    {},
    h("div", { class: "stats" },
      stat(overview.projects, "项目"),
      stat(overview.threads, "线程"),
      stat(overview.collaborators, "协作者"),
      stat(overview.pending, "待处理", overview.pending > 0),
    ),
    h(
      "div",
      { class: "split" },
      h("section", { class: "panel" },
        h("h2", {}, "需要处理"),
        overview.attention.length
          ? h("div", { class: "list" }, overview.attention.map(attentionRow))
          : h("div", { class: "empty" }, "没有待处理的消息。"),
      ),
      h("section", { class: "panel" },
        h("h2", {}, "最近动态"),
        overview.recent.length
          ? h("div", { class: "list" }, overview.recent.map(recentRow))
          : h("div", { class: "empty" }, "还没有协作记录。"),
      ),
    ),
  );
}

function stat(value, label, waiting) {
  return h("div", { class: waiting ? "stat waiting" : "stat" }, h("b", {}, value), h("span", {}, label));
}

function renderProjects() {
  if (!snapshot.projects.length) return emptyState();
  return h(
    "div",
    { class: "stack" },
    snapshot.projects.map((project) =>
      h(
        "a",
        { class: "card", href: `#/projects/${encodeURIComponent(project.id)}` },
        h(
          "div",
          { class: "card-top" },
          h("span", { class: "title" }, project.name),
          h("span", { class: "meta" }, project.available ? `${project.threads.length} 个线程` : "目录不可用"),
        ),
        h("div", { class: "path" }, project.root),
      ),
    ),
  );
}

function renderProject(id) {
  const project = findProject(id);
  if (!project) return missing("没有这个项目。");
  return h(
    "div",
    { class: "stack" },
    h("div", { class: "path" }, project.root),
    project.available
      ? threadCards(project.threads)
      : h("div", { class: "empty" }, "这个目录已经不在本机上。"),
  );
}

function renderThreads() {
  const threads = snapshot.projects.flatMap((project) =>
    project.threads.map((thread) => ({ ...thread, project_name: project.name, project_id: project.id })),
  );
  if (!threads.length) return emptyState();
  return threadCards(threads);
}

function threadCards(threads) {
  if (!threads.length) return h("div", { class: "empty" }, "这个范围内还没有线程。");
  return h(
    "div",
    { class: "stack" },
    threads.map((thread) =>
      h(
        "a",
        { class: "card", href: threadHref(thread.project_id, thread.thread_id) },
        h(
          "div",
          { class: "card-top" },
          h("span", { class: "title" }, thread.topic),
          h("span", { class: "meta" }, formatTime(thread.updated_at)),
        ),
        h("div", { class: "quiet" }, `${thread.project_name} · ${thread.thread_id}`),
        h(
          "div",
          { class: "pills" },
          h("span", { class: "pill" }, thread.status),
          h("span", { class: "pill" }, `第 ${thread.round || 0} 轮`),
          thread.coordinator ? h("span", { class: "pill info" }, `协调者 ${thread.coordinator}`) : null,
          thread.signals.some((signal) => signal.decision === "NEW")
            ? h("span", { class: "pill attention" }, "有待处理")
            : null,
        ),
      ),
    ),
  );
}

function renderThread(projectId, threadId) {
  const thread = findThread(projectId, threadId);
  if (!thread) return missing("没有这个线程。");
  return h(
    "div",
    { class: "stack" },
    h("p", { class: "sub" }, thread.objective || "没有目标说明。"),
    h(
      "div",
      { class: "pills" },
      h("span", { class: "pill" }, thread.project_name),
      h("span", { class: "pill" }, thread.status),
          thread.coordinator
            ? h("span", { class: "pill info" }, `协调者 ${thread.coordinator}`)
            : h("span", { class: "pill" }, "未指定协调者"),
          ...thread.participants
            .filter((name) => name !== thread.coordinator)
            .map((name) => h("span", { class: "pill" }, name)),
    ),
    thread.signals.length
      ? h("section", {}, h("h2", { class: "section-title" }, "收件"), h("div", { class: "stack" }, thread.signals.map(signalCard)))
      : null,
    h("section", {}, h("h2", { class: "section-title" }, "轮次"), turnList(thread.turns)),
  );
}

function signalCard(signal) {
  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "card-top" },
      h("span", { class: "title" }, `${signal.from || "?"} → ${signal.to}`),
      h("span", { class: "meta" }, formatTime(signal.created_at)),
    ),
    h("div", { class: "pills" }, pillsForSignal(signal)),
    signal.error ? h("p", { class: "summary" }, signal.error) : null,
  );
}

function turnList(turns) {
  const visible = turns.filter((turn) => !turn.error);
  if (!visible.length) return h("div", { class: "empty" }, "还没有轮次。");
  return h(
    "div",
    { class: "stack" },
    visible.map((turn) =>
      h(
        "article",
        { class: "turn" },
        h(
          "div",
          { class: "turn-top" },
          h("span", { class: "title" }, turn.title || `${turn.author} · ${turn.kind}`),
          h("span", { class: "meta" }, formatTime(turn.created_at)),
        ),
        h(
          "div",
          { class: "pills" },
          h("span", { class: "pill" }, `第 ${turn.round} 轮`),
          h("span", { class: "pill" }, turn.author),
          h("span", { class: "pill" }, turn.kind),
          h("span", { class: "pill" }, turn.status),
          turn.requested_action ? h("span", { class: "pill" }, turn.requested_action) : null,
        ),
        turn.summary ? h("p", { class: "summary" }, turn.summary) : null,
      ),
    ),
  );
}

function renderPeople() {
  if (!snapshot.collaborators.length) return emptyState();
  return h(
    "div",
    { class: "stack" },
    snapshot.collaborators.map((person) =>
      h(
        "a",
        { class: "card", href: `#/people/${encodeURIComponent(person.id)}` },
        h(
          "div",
          { class: "card-top" },
          h("span", { class: "title" }, person.id),
          h("span", { class: "meta" }, person.pending ? `${person.pending} 条待处理` : formatTime(person.last_at)),
        ),
        h(
          "div",
          { class: "quiet" },
          `${person.projects.length} 个项目 · ${person.threads.length} 个线程 · ${person.turns} 条发言`,
        ),
        h(
          "div",
          { class: "pills" },
          person.pending ? h("span", { class: "pill attention" }, "待处理") : h("span", { class: "pill ok" }, "没有待办"),
          ...person.tools.map((tool) => h("span", { class: "pill" }, tool)),
        ),
      ),
    ),
  );
}

function renderPerson(id) {
  const person = findPerson(id);
  if (!person) return missing("没有这个协作者。");
  const byProject = new Map();
  for (const thread of person.threads) {
    if (!byProject.has(thread.project_id)) byProject.set(thread.project_id, []);
    byProject.get(thread.project_id).push(thread);
  }
  const sections = [...byProject.entries()].map(([projectId, threads]) =>
    h(
      "section",
      { class: "stack" },
      h("h2", { class: "section-title" }, threads[0].project_name),
      ...threads.map((thread) =>
        h(
          "a",
          { class: "card", href: threadHref(projectId, thread.thread_id) },
          h(
            "div",
            { class: "card-top" },
            h("span", { class: "title" }, thread.topic),
            h("span", { class: "meta" }, formatTime(thread.updated_at)),
          ),
          h(
            "div",
            { class: "pills" },
            h("span", { class: "pill" }, thread.status),
            thread.coordinator ? h("span", { class: "pill info" }, "协调者") : null,
            thread.pending ? h("span", { class: "pill attention" }, `${thread.pending} 条待处理`) : null,
          ),
        ),
      ),
    ),
  );
  return h("div", { class: "stack" }, sections.length ? sections : h("div", { class: "empty" }, "还没有参与记录。"));
}

function emptyState() {
  return h(
    "div",
    { class: "empty" },
    "还没有注册到本机界面的项目。在项目里执行 ",
    h("code", {}, "python scripts/collab.py ui --root ."),
    " 后，这里会显示它的线程和协作者。",
  );
}

function missing(message) {
  return h("div", { class: "empty" }, message);
}

function pageTitle(current) {
  if (current.name === "projects") return ["项目", "按仓库查看本机协作"];
  if (current.name === "project") {
    const project = findProject(current.id);
    return [project ? project.name : "项目", project ? project.root : ""];
  }
  if (current.name === "threads") return ["线程", "跨项目的全部协作线程"];
  if (current.name === "thread") {
    const thread = findThread(current.projectId, current.threadId);
    return [thread ? thread.topic : "线程", thread ? `${thread.project_name} / ${thread.thread_id}` : ""];
  }
  if (current.name === "people") return ["协作者", "同一个人可以出现在多个项目和线程里"];
  if (current.name === "person") return [current.id, "这个协作者参与过的项目和线程"];
  return ["总览", "本机已注册项目的协作状态"];
}

function render() {
  const current = route();
  const [title, subtitle] = snapshot ? pageTitle(current) : ["协作", "正在读取本机记录"];
  const active = current.name === "project" ? "projects" : current.name === "thread" ? "threads" : current.name === "person" ? "people" : current.name;
  let body = null;
  if (!snapshot) body = h("div", { class: "empty" }, error || "正在读取…");
  else if (current.name === "overview") body = renderOverview();
  else if (current.name === "projects") body = renderProjects();
  else if (current.name === "project") body = renderProject(current.id);
  else if (current.name === "threads") body = renderThreads();
  else if (current.name === "thread") body = renderThread(current.projectId, current.threadId);
  else if (current.name === "people") body = renderPeople();
  else if (current.name === "person") body = renderPerson(current.id);

  app.replaceChildren(
    h(
      "div",
      { class: "shell" },
      h(
        "aside",
        { class: "sidebar" },
        h("div", { class: "brand" }, h("div", { class: "mark" }, h("span")), "协作"),
        h(
          "nav",
          {},
          nav("总览", "#/", active === "overview"),
          nav("项目", "#/projects", active === "projects"),
          nav("线程", "#/threads", active === "threads"),
          nav("协作者", "#/people", active === "people"),
        ),
        h("div", { class: "sidebar-foot" }, `127.0.0.1:${snapshot && snapshot.port ? snapshot.port : 23451}`),
      ),
      h(
        "main",
        { class: "main" },
        error ? h("div", { class: "banner" }, error) : null,
        h(
          "div",
          { class: "top" },
          h("div", {}, h("h1", {}, title), subtitle ? h("p", { class: "sub" }, subtitle) : null),
          h(
            "div",
            { class: "top-actions" },
            snapshot
              ? h("span", { class: "meta" }, `更新于 ${formatTime(snapshot.generated_at)}`)
              : null,
            h(
              "button",
              {
                type: "button",
                class: "refresh",
                disabled: loading,
                onclick: () => load(),
              },
              "刷新",
            ),
          ),
        ),
        body,
      ),
    ),
  );
}

function nav(label, href, isActive) {
  return h("a", { class: isActive ? "nav-link active" : "nav-link", href }, label);
}

async function load() {
  if (loading) return;
  loading = true;
  render();
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`读取失败（${response.status}）`);
    snapshot = await response.json();
    error = "";
  } catch (exc) {
    error = exc.message || "无法读取协作数据";
  } finally {
    loading = false;
    render();
  }
}

window.addEventListener("hashchange", render);
load();
