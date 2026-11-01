// Aprovações: a fila única do owner (plano do dia, pedidos entre agentes, missões, conteúdo e ações externas).
(() => {
  const { $, esc, api, initProjects } = window.GOS;
  let agents = {}; let projects = []; let project = "all"; let kind = "all"; let sw = null;
  let items = []; let counts = {}; let plan = null; let schedule = null; let poll = null;

  const KINDS = [["all", "Tudo"], ["task", "Tarefas"], ["mission", "Missões"], ["content", "Conteúdo"], ["action", "Ações externas"]];
  const KIND_LABEL = { task: "Tarefa", mission: "Missão", content: "Conteúdo", action: "Ação externa" };
  const RISK = { baixo: "risco baixo", medio: "risco médio", alto: "risco alto" };
  const ORIGIN = { plano: "☀ plano do dia", pedido: "🤝 pedido de agente", rotina: "🔁 rotina", owner: "✋ pedido seu", chat: "💬 do chat", missao: "🎯 missão", conteudo: "✎ conteúdo", "acao-externa": "↗ fora do OS" };
  const pinfo = (slug) => projects.find((p) => p.slug === slug) || { name: slug, icon: "◆" };
  const aname = (slug) => agents[slug] ? `${agents[slug].icon} ${agents[slug].name}` : slug;
  const who = (r) => r.startsWith("agente:") ? aname(r.slice(7)) : r;

  function card(it) {
    const id = it.id;
    const chan = it.channel ? `<span class="tag">${esc(it.channel)}</span>` : "";
    const by = it.requested_by ? `<span class="tag handoff" title="quem pediu">${esc(ORIGIN[it.origin] || "")} · ${esc(who(it.requested_by))}</span>` : "";
    const twofa = it.needs_2fa
      ? `<label class="inline totp"><span class="muted">2FA</span><input data-totp inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="código" aria-label="Código 2FA"></label>` : "";
    const link = it.kind === "content" ? `<a class="btn small ghost" href="/content">Editar</a>` : "";
    return `<div class="task proposed approval" id="${esc(id)}" data-kind="${esc(it.kind)}" data-ref="${it.ref_id}">
      <div class="task-main">
        <div class="task-title"><span class="tag kind-${esc(it.kind)}">${esc(KIND_LABEL[it.kind])}</span> ${project === "all" ? `<span class="tag">${esc(pinfo(it.project).icon)} ${esc(pinfo(it.project).name)}</span> ` : ""}${esc(it.title)}</div>
        ${it.why ? `<div class="task-why">${esc(it.why)}</div>` : ""}
        <div class="task-tags">${by}${it.agent ? `<span class="tag">${esc(aname(it.agent))}</span>` : ""}${chan}<span class="tag r-${esc(it.risk)}">${esc(RISK[it.risk] || it.risk)}</span></div>
        ${it.preview ? `<details ${it.kind === "action" ? "open" : ""}><summary>${it.kind === "action" ? "O que vai acontecer" : "Detalhes"}</summary><p class="pre">${esc(it.preview.slice(0, 2500))}</p></details>` : ""}
      </div>
      <div class="task-actions">${twofa}${link}<button class="btn small danger" data-reject type="button">Rejeitar</button><button class="btn small primary" data-approve type="button">Aprovar</button></div>
    </div>`;
  }

  function renderPlan() {
    const state = $("#plan-state");
    if (!plan) return (state.innerHTML = "");
    const s = schedule;
    $("#generate .lbl").textContent = plan.plan && plan.plan.status === "ready" ? "Refazer plano" : "Gerar plano";
    $("#generate").disabled = plan.plan?.status === "generating";
    $("#plan-day").textContent = new Date(plan.day + "T12:00:00").toLocaleDateString("pt-BR", { weekday: "long", day: "numeric", month: "long" });
    $("#sched-note").textContent = `O plano sai todo dia às ${s.hour}h (${s.timezone})${s.enabled ? "" : " — agendador desligado"}. ${s.telegram ? "Aviso no Telegram ligado." : "Aviso no Telegram desligado."} Nada roda nem sai sem o seu ok.`;
    const p = plan.plan;
    if (!p) state.innerHTML = `<div class="empty-card card"><div class="logo">☀</div><h2>O plano de hoje ainda não foi gerado</h2><p class="muted">Ele sai sozinho às ${s.hour}h. Quer gerar agora?</p></div>`;
    else if (p.status === "generating") state.innerHTML = `<div class="empty-card card"><span class="typing"><i></i><i></i><i></i></span><h2>Os agentes estão montando o plano…</h2><p class="muted">Cada um lê os KPIs e propõe até 3 tarefas. Leva alguns minutos.</p></div>`;
    else if (p.status === "failed") state.innerHTML = `<div class="alert">Não foi possível gerar o plano.<br>${esc(p.error || "")}</div>`;
    else state.innerHTML = p.error ? `<div class="alert">Alguns agentes falharam:<br>${esc(p.error)}</div>` : "";
  }

  function render() {
    $("#page-crumb").textContent = project === "all" ? "Todos os projetos" : pinfo(project).name;
    $("#kind-filter").innerHTML = KINDS.map(([k, label]) => {
      const n = k === "all" ? counts.total : counts[k];
      return `<button type="button" class="fchip ${k === kind ? "active" : ""}" data-kind-filter="${k}">${esc(label)} <span>${n || 0}</span></button>`;
    }).join("");
    $("#plan-bar").hidden = !items.length;
    $("#pending-count").textContent = items.length;
    renderPlan();
    const byProject = {};
    for (const it of items) (byProject[it.project] ||= []).push(it);
    $("#queue").innerHTML = Object.entries(byProject).map(([slug, list]) => {
      const p = pinfo(slug);
      return `<section class="project-block"><h2 class="project-head"><span>${esc(p.icon)}</span>${esc(p.name)}<b>${list.length} para decidir</b></h2>
        <div class="agent-plan card">${list.map(card).join("")}</div></section>`;
    }).join("") || `<div class="empty-card card"><div class="logo">${window.GOS.icon("check")}</div><h2>Nada esperando você</h2><p class="muted">Quando um agente propuser algo, pedir uma ação fora do OS ou escrever conteúdo, aparece aqui.</p></div>`;
    focusHash();
    clearTimeout(poll);
    if (plan?.plan?.status === "generating") poll = setTimeout(load, 4000);
  }

  function focusHash() {
    const id = location.hash.slice(1); if (!id) return;
    const el = document.getElementById(id); if (!el) return;
    el.scrollIntoView({ block: "center" }); el.classList.add("flash");
  }

  async function load() {
    try {
      const q = `project=${encodeURIComponent(project)}`;
      const [a, p] = await Promise.all([api(`/api/approvals?${q}&kind=${kind}`), api("/api/plans/today")]);
      items = a.items; counts = a.counts; plan = p; schedule = p.schedule;
      render();
    } catch (e) { $("#plan-state").innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  }

  async function loadHistory() {
    const rows = await api(`/api/approvals/history?project=${encodeURIComponent(project)}&limit=40`);
    const VIA = { "2fa": "com 2FA", clique: "", herdada: "herdada da missão", rotina: "rotina" };
    $("#history").innerHTML = rows.map((r) => `<div class="task-row"><span class="badge t-${r.status === "approved" ? "done" : "rejected"}">${r.status === "approved" ? "aprovada" : "rejeitada"}</span>
      <span class="muted">${esc(KIND_LABEL[r.kind] || r.kind)}${VIA[r.via] ? ` · ${VIA[r.via]}` : ""}</span><span class="grow">${esc(r.title)}</span>
      <span class="muted">${new Date(r.at + "Z").toLocaleString("pt-BR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</span></div>`).join("")
      || '<div class="muted pad">Ainda sem decisões.</div>';
  }

  async function decide(list, approve, totp = "") {
    if (!list.length) return;
    try {
      const r = await api("/api/approvals/decide", { method: "POST", body: JSON.stringify({ items: list, approve, totp }) });
      if (r.errors?.length) alert(r.errors.map((e) => e.error).join("\n"));
    } catch (e) { alert(e.message); }
    await load(); sw?.reload(); if ($("#history-box").open) loadHistory();
  }

  document.addEventListener("click", (ev) => {
    const row = ev.target.closest(".approval");
    if (row && (ev.target.closest("[data-approve]") || ev.target.closest("[data-reject]"))) {
      const approve = !!ev.target.closest("[data-approve]");
      const totp = row.querySelector("[data-totp]")?.value.trim() || "";
      if (approve && row.querySelector("[data-totp]") && !totp) { row.querySelector("[data-totp]").focus(); return alert("Digite o código 2FA para liberar esta ação."); }
      return decide([{ kind: row.dataset.kind, ref_id: Number(row.dataset.ref) }], approve, totp);
    }
    const f = ev.target.closest("[data-kind-filter]"); if (f) { kind = f.dataset.kindFilter; load(); }
  });
  // "Aprovar todos" deixa de fora o que exige 2FA: esse você decide item a item.
  const batch = (needsNo2fa) => items.filter((i) => !needsNo2fa || !i.needs_2fa).map((i) => ({ kind: i.kind, ref_id: i.ref_id }));
  $("#approve-all").onclick = () => decide(batch(true), true);
  $("#reject-all").onclick = () => {
    if (confirm(`Rejeitar tudo o que está na lista${project === "all" ? " (todos os projetos)" : ` de ${pinfo(project).name}`}?`)) decide(batch(false), false);
  };
  $("#history-box").addEventListener("toggle", (ev) => { if (ev.target.open) loadHistory(); });
  window.addEventListener("hashchange", focusHash);

  $("#generate").onclick = async () => {
    if (plan?.plan?.status === "ready" && !confirm("Refazer o plano? As tarefas ainda não decididas serão descartadas.")) return;
    $("#generate").disabled = true;
    try { await api("/api/plans/generate", { method: "POST" }); } catch (e) { alert(e.message); }
    setTimeout(load, 800);
  };
  $("#new-task").onclick = () => {
    $("#task-project").innerHTML = projects.map((p) => `<option value="${esc(p.slug)}">${esc(p.icon)} ${esc(p.name)}</option>`).join("");
    if (project !== "all") $("#task-project").value = project;
    $("#task-agent").innerHTML = Object.values(agents).map((a) => `<option value="${esc(a.slug)}">${esc(a.icon)} ${esc(a.name)}</option>`).join("");
    $("#task-title").value = ""; $("#task-steps").value = "";
    $("#task-dialog").showModal();
  };
  $("#task-cancel").onclick = () => $("#task-dialog").close();
  $("#task-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/tasks", { method: "POST", body: JSON.stringify({ project: $("#task-project").value, agent: $("#task-agent").value, title: $("#task-title").value, steps: $("#task-steps").value }) });
      $("#task-dialog").close(); load();
    } catch (e) { alert(e.message); }
  };

  (async () => {
    for (const a of await api("/api/agents")) agents[a.slug] = a;
    sw = await initProjects((slug, _info, list) => { project = slug || "all"; projects = list; load(); }, { allowAll: true });
  })();
})();
