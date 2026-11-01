// Kanban: as etapas de cada atividade dos agentes, por projeto (ou todos).
(() => {
  const { $, esc, api, initProjects } = window.GOS;
  let agents = {}; let projects = []; let project = "all"; let agent = ""; let days = 0;
  let tasks = []; let poll = null; let sw = null; let dragId = null; let requestId = 0;

  const COLUMNS = [
    { key: "proposed", title: "Aguardando você", hint: "propostas para aprovar", statuses: ["proposed"] },
    { key: "approved", title: "Na fila", hint: "aprovadas, esperando a vez", statuses: ["approved"] },
    { key: "running", title: "Em execução", hint: "o agente está trabalhando", statuses: ["running"] },
    { key: "waiting", title: "Esperando 2FA", hint: "precisa do seu código", statuses: ["waiting"] },
    { key: "done", title: "Concluídas", hint: "", statuses: ["done"] },
    { key: "closed", title: "Falhou ou rejeitada", hint: "", statuses: ["failed", "rejected"] },
  ];
  const STATUS = { proposed: "aguardando", approved: "na fila", rejected: "rejeitada", running: "executando", waiting: "esperando 2FA", done: "concluída", failed: "falhou" };
  const pinfo = (slug) => projects.find((p) => p.slug === slug) || { name: slug, icon: "◆" };
  const aname = (slug) => slug === "chat" ? "💬 Chat" : agents[slug] ? `${agents[slug].icon} ${agents[slug].name}` : slug;

  function ago(iso) {
    if (!iso) return "";
    const s = (Date.now() - new Date(iso + "Z").getTime()) / 1000;
    if (s < 60) return "agora";
    if (s < 3600) return `há ${Math.round(s / 60)} min`;
    if (s < 86400) return `há ${Math.round(s / 3600)} h`;
    return new Date(iso + "Z").toLocaleDateString("pt-BR", { day: "2-digit", month: "short" });
  }

  function origin(t) {
    if (t.origin === "chat") return '<span class="tag">💬 ação do chat</span>';
    if (t.origin === "automation") return `<span class="tag origin-auto" title="Rotina: ${esc(t.automation || "")}">🔁 rotina</span>`;
    if (t.origin === "handoff") return `<span class="tag handoff">🤝 pedido por ${esc(agents[t.requested_by]?.name || t.requested_by)}</span>`;
    if (t.origin === "plan") return '<span class="tag">☀ plano do dia</span>';
    return '<span class="tag">✋ pedido seu</span>';
  }

  function card(t) {
    const when = t.finished_at || t.decided_at || t.created_at;
    const drag = t.kind !== "command" && ["proposed", "failed"].includes(t.status);
    let actions = "";
    if (t.kind !== "command" && t.status === "proposed") actions = `<button class="btn small danger" data-reject="${t.id}" type="button">Rejeitar</button><button class="btn small primary" data-approve="${t.id}" type="button">Aprovar</button>`;
    else if (t.kind !== "command" && t.status === "failed") actions = `<button class="btn small" data-retry="${t.id}" type="button">${window.GOS.icon("refresh")} Tentar de novo</button>`;
    if (t.conversation_id) actions += `<a class="btn small ghost" href="/?c=${t.conversation_id}">${t.status === "waiting" ? "Dar o 2FA" : "Ver thread"}</a>`;
    return `<article class="kcard s-${esc(t.status)}" data-id="${t.id}" data-kind="${esc(t.kind || "task")}" ${drag ? 'draggable="true"' : ""} tabindex="0">
      <div class="kcard-top"><span class="kcard-agent">${esc(aname(t.agent))}</span>${project === "all" ? `<span class="kcard-proj">${esc(pinfo(t.project).icon)} ${esc(t.project ? pinfo(t.project).name : "Sem projeto")}</span>` : ""}</div>
      <div class="kcard-title">${esc(t.title)}</div>
      <div class="task-tags">${origin(t)}${t.risk !== "baixo" ? `<span class="tag r-${esc(t.risk)}">risco ${esc(t.risk === "medio" ? "médio" : t.risk)}</span>` : ""}${t.status === "rejected" ? '<span class="badge t-rejected">rejeitada</span>' : t.status === "failed" ? '<span class="badge t-failed">falhou</span>' : ""}</div>
      <div class="kcard-foot"><span class="muted">${esc(ago(when))}</span>${actions ? `<div class="kcard-actions">${actions}</div>` : ""}</div>
    </article>`;
  }

  function render() {
    $("#page-crumb").textContent = project === "all" ? "Todos os projetos" : pinfo(project).name;
    const counts = {};
    for (const t of tasks) counts[t.agent] = (counts[t.agent] || 0) + 1;
    const chips = [["", "Todo o time", tasks.length]].concat([...Object.values(agents), {slug: "chat", icon: "💬", name: "Chat"}].filter((a) => counts[a.slug]).map((a) => [a.slug, `${a.icon} ${a.name}`, counts[a.slug]]));
    if (agent && !counts[agent]) agent = "";
    $("#agent-filter").innerHTML = chips.map(([slug, label, n]) => `<button type="button" class="fchip ${slug === agent ? "active" : ""}" data-agent="${esc(slug)}">${esc(label)} <span>${n}</span></button>`).join("");
    const list = tasks.filter((t) => !agent || t.agent === agent);
    $("#board").innerHTML = COLUMNS.map((c) => {
      const items = list.filter((t) => c.statuses.includes(t.status));
      const hint = c.key === "done" || c.key === "closed" ? (days ? `últimos ${days} dias` : "todo o histórico") : c.hint;
      return `<section class="kcol k-${c.key}" data-col="${c.key}">
        <header class="kcol-head"><span class="kdot"></span><h2>${esc(c.title)}</h2><b class="kcount">${items.length}</b></header>
        <p class="kcol-hint muted">${esc(hint)}</p>
        <div class="kcol-body">${items.map(card).join("") || '<div class="kempty">Nada aqui</div>'}</div>
      </section>`;
    }).join("");
  }

  async function load() {
    clearTimeout(poll);
    const current = ++requestId;
    try {
      const data = await api(`/api/board?project=${encodeURIComponent(project)}&days=${days}`);
      if (current !== requestId) return;
      tasks = data.tasks; render();
    } catch (e) {
      if (current === requestId) $("#board").innerHTML = `<div class="alert">${esc(e.message)}</div>`;
    } finally {
      if (current === requestId) poll = setTimeout(load, 15000);
    }
  }

  async function act(fn) {
    try { await fn(); } catch (e) { alert(e.message); }
    await load(); sw?.reload();
  }
  const decide = (id, approve) => act(() => api("/api/tasks/decide", { method: "POST", body: JSON.stringify({ ids: [id], approve }) }));
  const retry = (id) => act(() => api(`/api/tasks/${id}/retry`, { method: "POST" }));

  function openCard(id, kind) {
    const t = tasks.find((x) => x.id === id && (x.kind || "task") === kind); if (!t) return;
    $("#cd-title").textContent = t.title;
    $("#cd-body").innerHTML = `<div class="task-tags"><span class="badge t-${esc(t.status)}">${esc(STATUS[t.status] || t.status)}</span><span class="tag">${esc(aname(t.agent))}</span><span class="tag">${esc(pinfo(t.project).icon)} ${esc(t.project ? pinfo(t.project).name : "Sem projeto")}</span>${origin(t)}</div>
      ${t.automation ? `<h4>Rotina</h4><p><a href="/automations#rotinas">${esc(t.automation)}</a></p>` : ""}
      ${t.command_status === "blocked" ? "<p>Comando bloqueado pela política de segurança.</p>" : ""}
      ${t.rationale ? `<h4>Por quê</h4><p>${esc(t.rationale)}</p>` : ""}
      ${t.steps ? `<h4>Passos</h4><p>${esc(t.steps)}</p>` : ""}
      ${t.result ? `<h4>Resultado</h4><p>${esc(t.result.slice(0, 3000))}</p>` : ""}
      <div class="row">${t.conversation_id ? `<a class="btn primary" href="/?c=${t.conversation_id}">Abrir a thread ${window.GOS.icon("arrow")}</a>` : ""}</div>`;
    $("#card-dialog").showModal();
  }

  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("[data-approve]"); if (a) return decide(Number(a.dataset.approve), true);
    const r = ev.target.closest("[data-reject]"); if (r) return decide(Number(r.dataset.reject), false);
    const y = ev.target.closest("[data-retry]"); if (y) return retry(Number(y.dataset.retry));
    const f = ev.target.closest("[data-agent]"); if (f) { agent = f.dataset.agent; return render(); }
    const d = ev.target.closest("[data-days]");
    if (d) { days = Number(d.dataset.days); document.querySelectorAll("#period button").forEach((b) => b.classList.toggle("on", b === d)); return load(); }
    if (ev.target.closest("a, button")) return;
    const c = ev.target.closest(".kcard"); if (c) openCard(Number(c.dataset.id), c.dataset.kind);
  });
  document.addEventListener("keydown", (ev) => {
    const c = ev.target.closest?.(".kcard"); if (c && ev.key === "Enter") openCard(Number(c.dataset.id), c.dataset.kind);
  });
  $("#cd-close").onclick = () => $("#card-dialog").close();

  // Arrastar: proposta → "Na fila" aprova, → "Falhou ou rejeitada" rejeita; falha → "Na fila" tenta de novo.
  const allowed = (t, col) => t && t.kind !== "command" && ((t.status === "proposed" && (col === "approved" || col === "closed")) || (t.status === "failed" && col === "approved"));
  const board = $("#board");
  board.addEventListener("dragstart", (ev) => {
    const c = ev.target.closest(".kcard"); if (!c || c.dataset.kind === "command") return;
    dragId = Number(c.dataset.id); c.classList.add("dragging");
    ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setData("text/plain", String(dragId));
    const t = tasks.find((x) => x.id === dragId && x.kind !== "command");
    board.querySelectorAll(".kcol").forEach((col) => col.classList.toggle("can-drop", allowed(t, col.dataset.col)));
  });
  board.addEventListener("dragend", () => {
    dragId = null;
    board.querySelectorAll(".dragging, .can-drop, .over").forEach((el) => el.classList.remove("dragging", "can-drop", "over"));
  });
  board.addEventListener("dragover", (ev) => {
    const col = ev.target.closest(".kcol.can-drop"); if (!col) return;
    ev.preventDefault(); board.querySelectorAll(".over").forEach((el) => el !== col && el.classList.remove("over")); col.classList.add("over");
  });
  board.addEventListener("drop", (ev) => {
    const col = ev.target.closest(".kcol.can-drop"); if (!col) return;
    ev.preventDefault();
    const t = tasks.find((x) => x.id === dragId && x.kind !== "command"); if (!t) return;
    if (t.status === "failed") retry(t.id);
    else decide(t.id, col.dataset.col === "approved");
  });

  (async () => {
    for (const a of await api("/api/agents")) agents[a.slug] = a;
    sw = await initProjects((slug, _info, list) => { project = slug || "all"; projects = list; agent = ""; load(); }, { allowAll: true });
  })();
})();
