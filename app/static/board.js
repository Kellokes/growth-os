// Kanban: as etapas de cada atividade dos agentes, por projeto (ou todos).
(() => {
  const { $, esc, api, initProjects } = window.GOS;
  let agents = {}; let projects = []; let project = "all"; let agent = ""; let days = 14;
  let tasks = []; let poll = null; let sw = null; let dragId = null;
  const TYPES = [["", "Tudo"], ["task", "Tarefas"], ["content", "Conteúdo"], ["action", "Ações externas"]];
  const TYPE_ALIAS = { conteudo: "content", tarefas: "task", acoes: "action" };
  let type = TYPE_ALIAS[new URLSearchParams(location.search).get("tipo")] || "";
  const key = (t) => `${t.type || "task"}-${t.id}`;
  const byKey = (k) => tasks.find((x) => key(x) === k);
  const decideUrl = (t) => `/approvals#${key(t)}`;

  const COLUMNS = [
    { key: "proposed", title: "Aguardando você", hint: "propostas para aprovar", statuses: ["proposed"] },
    { key: "approved", title: "Na fila", hint: "aprovadas, esperando a vez", statuses: ["approved"] },
    { key: "running", title: "Em execução", hint: "o agente está trabalhando", statuses: ["running"] },
    { key: "waiting", title: "Aguardando", hint: "seu 2FA, uma ação externa ou outro agente", statuses: ["waiting"] },
    { key: "done", title: "Concluídas", hint: "", statuses: ["done"] },
    { key: "closed", title: "Falhou ou rejeitada", hint: "", statuses: ["failed", "rejected"] },
  ];
  const STATUS = { proposed: "aguardando", approved: "na fila", rejected: "rejeitada", running: "executando", waiting: "aguardando", done: "concluída", failed: "falhou" };
  const pinfo = (slug) => projects.find((p) => p.slug === slug) || { name: slug, icon: "◆" };
  const aname = (slug) => agents[slug] ? `${agents[slug].icon} ${agents[slug].name}` : slug;

  function ago(iso) {
    if (!iso) return "";
    const s = (Date.now() - new Date(iso + "Z").getTime()) / 1000;
    if (s < 60) return "agora";
    if (s < 3600) return `há ${Math.round(s / 60)} min`;
    if (s < 86400) return `há ${Math.round(s / 3600)} h`;
    return new Date(iso + "Z").toLocaleDateString("pt-BR", { day: "2-digit", month: "short" });
  }

  const WAIT = { "2fa": "esperando o seu 2FA", acao: "esperando aprovar uma ação externa", agente: "esperando outro agente", horario: "esperando o horário" };

  function origin(t) {
    if (t.type === "content") return `<span class="tag kind-content">✎ ${esc(t.channel || "conteúdo")}</span>`;
    if (t.type === "action") return `<span class="tag kind-action">↗ ${esc(t.channel || "integração")}</span>`;
    if (t.origin === "automation") return `<span class="tag origin-auto" title="Rotina: ${esc(t.automation || "")}">🔁 rotina</span>`;
    if (t.origin === "handoff") return `<span class="tag handoff">🤝 pedido por ${esc(agents[t.requested_by]?.name || t.requested_by)}</span>`;
    if (t.origin === "plan") return '<span class="tag">☀ plano do dia</span>';
    return '<span class="tag">✋ pedido seu</span>';
  }

  function card(t) {
    const when = t.finished_at || t.decided_at || t.created_at;
    const isTask = (t.type || "task") === "task";
    const drag = isTask && ["proposed", "failed"].includes(t.status);
    let actions = "";
    if (t.status === "proposed" && isTask) actions = `<button class="btn small danger" data-reject="${t.id}" type="button">Rejeitar</button><button class="btn small primary" data-approve="${t.id}" type="button">Aprovar</button>`;
    else if (t.status === "proposed") actions = `<a class="btn small primary" href="${decideUrl(t)}">Decidir em Aprovações</a>`;
    else if (t.status === "failed" && isTask) actions = `<button class="btn small" data-retry="${t.id}" type="button">${window.GOS.icon("refresh")} Tentar de novo</button>`;
    if (t.conversation_id) actions += `<a class="btn small ghost" href="/?c=${t.conversation_id}">${t.status === "waiting" ? "Dar o 2FA" : "Ver thread"}</a>`;
    return `<article class="kcard s-${esc(t.status)}" data-id="${esc(key(t))}" ${drag ? 'draggable="true"' : ""} tabindex="0">
      <div class="kcard-top"><span class="kcard-agent">${esc(aname(t.agent))}</span>${project === "all" ? `<span class="kcard-proj">${esc(pinfo(t.project).icon)} ${esc(pinfo(t.project).name)}</span>` : ""}</div>
      <div class="kcard-title">${esc(t.title)}</div>
      ${t.status === "waiting" && t.wait_reason ? `<div class="muted small-print">${esc(WAIT[t.wait_reason] || t.wait_reason)}</div>` : ""}
      ${t.scheduled_at ? `<div class="muted small-print">📅 ${esc(new Date(t.scheduled_at).toLocaleString("pt-BR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }))}</div>` : ""}
      <div class="task-tags">${origin(t)}${t.risk !== "baixo" ? `<span class="tag r-${esc(t.risk)}">risco ${esc(t.risk === "medio" ? "médio" : t.risk)}</span>` : ""}${t.status === "rejected" ? '<span class="badge t-rejected">rejeitada</span>' : t.status === "failed" ? '<span class="badge t-failed">falhou</span>' : ""}</div>
      <div class="kcard-foot"><span class="muted">${esc(ago(when))}</span>${actions ? `<div class="kcard-actions">${actions}</div>` : ""}</div>
    </article>`;
  }

  function render() {
    $("#page-crumb").textContent = project === "all" ? "Todos os projetos" : pinfo(project).name;
    const tcount = (k) => tasks.filter((t) => !k || (t.type || "task") === k).length;
    $("#type-filter").innerHTML = TYPES.map(([k, label]) => `<button type="button" class="fchip ${k === type ? "active" : ""}" data-type="${k}">${esc(label)} <span>${tcount(k)}</span></button>`).join("");
    const scoped = tasks.filter((t) => !type || (t.type || "task") === type);
    const counts = {};
    for (const t of scoped) if (t.agent) counts[t.agent] = (counts[t.agent] || 0) + 1;
    const chips = [["", "Todo o time", scoped.length]].concat(Object.values(agents).filter((a) => counts[a.slug]).map((a) => [a.slug, `${a.icon} ${a.name}`, counts[a.slug]]));
    if (agent && !counts[agent]) agent = "";
    $("#agent-filter").innerHTML = chips.map(([slug, label, n]) => `<button type="button" class="fchip ${slug === agent ? "active" : ""}" data-agent="${esc(slug)}">${esc(label)} <span>${n}</span></button>`).join("");
    const list = scoped.filter((t) => !agent || t.agent === agent);
    $("#board").innerHTML = COLUMNS.map((c) => {
      const items = list.filter((t) => c.statuses.includes(t.status));
      const hint = c.key === "done" || c.key === "closed" ? `últimos ${days} dias` : c.hint;
      return `<section class="kcol k-${c.key}" data-col="${c.key}">
        <header class="kcol-head"><span class="kdot"></span><h2>${esc(c.title)}</h2><b class="kcount">${items.length}</b></header>
        <p class="kcol-hint muted">${esc(hint)}</p>
        <div class="kcol-body">${items.map(card).join("") || '<div class="kempty">Nada aqui</div>'}</div>
      </section>`;
    }).join("");
    clearTimeout(poll);
    if (tasks.some((t) => ["approved", "running"].includes(t.status))) poll = setTimeout(load, 5000);
  }

  async function load() {
    try { tasks = (await api(`/api/board?project=${encodeURIComponent(project)}&days=${days}`)).tasks; render(); }
    catch (e) { $("#board").innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  }

  async function act(fn) {
    try { await fn(); } catch (e) { alert(e.message); }
    await load(); sw?.reload();
  }
  const decide = (id, approve) => act(() => api("/api/tasks/decide", { method: "POST", body: JSON.stringify({ ids: [id], approve }) }));
  const retry = (id) => act(() => api(`/api/tasks/${id}/retry`, { method: "POST" }));

  function openCard(id) {
    const t = byKey(id); if (!t) return;
    const isTask = (t.type || "task") === "task";
    $("#cd-title").textContent = t.title;
    $("#cd-body").innerHTML = `<div class="task-tags"><span class="badge t-${esc(t.status)}">${esc(STATUS[t.status] || t.status)}</span><span class="tag">${esc(aname(t.agent))}</span><span class="tag">${esc(pinfo(t.project).icon)} ${esc(pinfo(t.project).name)}</span>${origin(t)}</div>
      ${t.automation ? `<h4>Rotina</h4><p><a href="/automations#rotinas">${esc(t.automation)}</a></p>` : ""}
      ${t.rationale ? `<h4>Por quê</h4><p>${esc(t.rationale)}</p>` : ""}
      ${t.steps ? `<h4>Passos</h4><p>${esc(t.steps)}</p>` : ""}
      ${t.result ? `<h4>Resultado</h4><p>${esc(t.result.slice(0, 3000))}</p>` : ""}
      <div class="row">${t.status === "proposed" && !isTask ? `<a class="btn primary" href="${decideUrl(t)}">Decidir em Aprovações ${window.GOS.icon("arrow")}</a>` : ""}${t.type === "content" ? `<a class="btn" href="/content">Abrir na central de conteúdo</a>` : ""}${t.conversation_id ? `<a class="btn primary" href="/?c=${t.conversation_id}">Abrir a thread ${window.GOS.icon("arrow")}</a>` : ""}</div>`;
    $("#card-dialog").showModal();
  }

  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("[data-approve]"); if (a) return decide(Number(a.dataset.approve), true);
    const r = ev.target.closest("[data-reject]"); if (r) return decide(Number(r.dataset.reject), false);
    const y = ev.target.closest("[data-retry]"); if (y) return retry(Number(y.dataset.retry));
    const f = ev.target.closest("[data-agent]"); if (f) { agent = f.dataset.agent; return render(); }
    const ty = ev.target.closest("[data-type]"); if (ty) { type = ty.dataset.type; agent = ""; return render(); }
    const d = ev.target.closest("[data-days]");
    if (d) { days = Number(d.dataset.days); document.querySelectorAll("#period button").forEach((b) => b.classList.toggle("on", b === d)); return load(); }
    if (ev.target.closest("a, button")) return;
    const c = ev.target.closest(".kcard"); if (c) openCard(c.dataset.id);
  });
  document.addEventListener("keydown", (ev) => {
    const c = ev.target.closest?.(".kcard"); if (c && ev.key === "Enter") openCard(c.dataset.id);
  });
  $("#cd-close").onclick = () => $("#card-dialog").close();

  // Arrastar: proposta → "Na fila" aprova, → "Falhou ou rejeitada" rejeita; falha → "Na fila" tenta de novo.
  const allowed = (t, col) => t && ((t.status === "proposed" && (col === "approved" || col === "closed")) || (t.status === "failed" && col === "approved"));
  const board = $("#board");
  board.addEventListener("dragstart", (ev) => {
    const c = ev.target.closest(".kcard"); if (!c) return;
    dragId = c.dataset.id; c.classList.add("dragging");
    ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setData("text/plain", String(dragId));
    const t = byKey(dragId);
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
    const t = byKey(dragId); if (!t) return;
    if (t.status === "failed") retry(t.id);
    else decide(t.id, col.dataset.col === "approved");
  });

  (async () => {
    for (const a of await api("/api/agents")) agents[a.slug] = a;
    sw = await initProjects((slug, _info, list) => { project = slug || "all"; projects = list; agent = ""; load(); }, { allowAll: true });
  })();
})();
