(() => {
  const { $, esc, api, initProjects } = window.GOS;
  let agents = {}; let data = null; let poll = null; let project = "all"; let projects = []; let sw = null;
  const pinfo = (slug) => projects.find((p) => p.slug === slug) || { name: slug, icon: "◆" };
  const visible = (list) => list.filter((t) => project === "all" || t.project === project);

  const STATUS = { proposed: "aguardando você", approved: "na fila", rejected: "rejeitada", running: "executando", waiting: "esperando 2FA", done: "concluída", failed: "falhou" };
  const RISK = { baixo: "risco baixo", medio: "risco médio", alto: "risco alto" };

  function taskHtml(t, showProject = false) {
    const conv = t.conversation_id ? `<a class="btn small ghost" href="/?c=${t.conversation_id}">Ver conversa</a>` : "";
    const actions = t.status === "proposed"
      ? `<button class="btn small danger" data-reject="${t.id}" type="button">Rejeitar</button><button class="btn small primary" data-approve="${t.id}" type="button">Aprovar</button>`
      : `<span class="badge t-${t.status}">${esc(STATUS[t.status] || t.status)}</span>${conv}`;
    return `<div class="task ${t.status}">
      <div class="task-main">
        <div class="task-title">${showProject ? `<span class="tag">${esc(pinfo(t.project).name)} · ${esc(agents[t.agent]?.name || t.agent)}</span> ` : ""}${esc(t.title)}</div>
        ${t.rationale ? `<div class="task-why">${esc(t.rationale)}</div>` : ""}
        <div class="task-tags">${t.requested_by ? `<span class="tag handoff">🤝 pedido por ${esc(agents[t.requested_by]?.name || t.requested_by)}</span>` : ""}<span class="tag r-${esc(t.risk)}">${esc(RISK[t.risk] || t.risk)}</span>${t.kpi ? `<span class="tag">KPI ${esc(t.kpi)}</span>` : ""}</div>
        ${t.steps ? `<details><summary>Passos</summary><p>${esc(t.steps)}</p></details>` : ""}
        ${t.result ? `<details ${t.status === "failed" ? "open" : ""}><summary>Resultado</summary><p>${esc(t.result.slice(0, 1500))}</p></details>` : ""}
      </div>
      <div class="task-actions">${actions}</div>
    </div>`;
  }

  function render() {
    $("#plan-day").textContent = new Date(data.day + "T12:00:00").toLocaleDateString("pt-BR", { weekday: "long", day: "numeric", month: "long" });
    const s = data.schedule;
    $("#sched-note").textContent = `Gerado todo dia às ${s.hour}h (${s.timezone})${s.enabled ? "" : " — agendador desligado"}. ${s.telegram ? "Aviso no Telegram ligado." : "Aviso no Telegram desligado."} Nada roda sem a sua aprovação.`;
    const plan = data.plan;
    const state = $("#plan-state");
    $("#generate").textContent = plan && plan.status === "ready" ? "Refazer plano" : "Gerar plano";
    $("#generate").disabled = plan?.status === "generating";
    if (!plan) state.innerHTML = `<div class="empty-card card"><div class="logo">☀</div><h2>O plano de hoje ainda não foi gerado</h2><p class="muted">Ele sai sozinho às ${s.hour}h. Quer gerar agora?</p></div>`;
    else if (plan.status === "generating") state.innerHTML = `<div class="empty-card card"><span class="typing"><i></i><i></i><i></i></span><h2>Os agentes estão montando o plano…</h2><p class="muted">Cada um lê os KPIs e propõe até 3 tarefas. Leva alguns minutos.</p></div>`;
    else if (plan.status === "failed") state.innerHTML = `<div class="alert">Não foi possível gerar o plano.<br>${esc(plan.error || "")}</div>`;
    else state.innerHTML = plan.error ? `<div class="alert">Alguns agentes falharam:<br>${esc(plan.error)}</div>` : "";

    const tasks = visible(data.tasks); const carry = visible(data.carryover);
    $("#page-title").textContent = project === "all" ? "Plano do dia · todos os projetos" : `Plano do dia · ${pinfo(project).name}`;
    const pending = tasks.filter((t) => t.status === "proposed");
    $("#plan-bar").hidden = !pending.length;
    $("#pending-count").textContent = pending.length;

    // Agrupa por projeto e, dentro dele, por agente: fica claro de qual negócio é cada tarefa.
    const byProject = {};
    for (const t of tasks) ((byProject[t.project] ||= {})[t.agent] ||= []).push(t);
    $("#plan-tasks").innerHTML = Object.entries(byProject).map(([pslug, byAgent]) => {
      const p = pinfo(pslug);
      const n = Object.values(byAgent).flat().filter((t) => t.status === "proposed").length;
      return `<section class="project-block"><h2 class="project-head"><span>${esc(p.icon)}</span>${esc(p.name)}${n ? `<b>${n} para decidir</b>` : ""}</h2>
        <div class="agent-plans">${Object.entries(byAgent).map(([slug, list]) => {
          const a = agents[slug] || { name: slug, icon: "◆", area: "" };
          return `<article class="agent-plan card"><header><span class="agent-icon">${esc(a.icon)}</span><div><h2>${esc(a.name)}</h2><span class="muted">${esc(a.area)} · ${esc(p.name)}</span></div></header>${list.map(taskHtml).join("")}</article>`;
        }).join("")}</div></section>`;
    }).join("") || (plan && plan.status === "ready" ? `<div class="muted pad">Nenhuma tarefa ${project === "all" ? "" : "deste projeto "}no plano de hoje.</div>` : "");
    $("#carry").innerHTML = carry.length
      ? `<h3 class="section-title">Pedidos entre agentes, fila e pendências</h3><div class="agent-plan card">${carry.map((t) => taskHtml(t, true)).join("")}</div>` : "";

    clearTimeout(poll);
    const busy = plan?.status === "generating" || tasks.concat(carry).some((t) => ["approved", "running"].includes(t.status));
    if (busy) poll = setTimeout(load, 4000);
  }

  async function load() {
    try { data = await api("/api/plans/today"); render(); }
    catch (e) { $("#plan-state").innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  }

  async function decide(ids, approve) {
    if (!ids.length) return;
    try { await api("/api/tasks/decide", { method: "POST", body: JSON.stringify({ ids, approve }) }); } catch (e) { alert(e.message); }
    await load(); sw?.reload();
  }

  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("[data-approve]"); if (a) return decide([Number(a.dataset.approve)], true);
    const r = ev.target.closest("[data-reject]"); if (r) return decide([Number(r.dataset.reject)], false);
  });
  const pendingIds = () => visible(data.tasks).filter((t) => t.status === "proposed").map((t) => t.id);
  $("#approve-all").onclick = () => decide(pendingIds(), true);
  $("#reject-all").onclick = () => { if (confirm(`Rejeitar as tarefas que faltam decidir${project === "all" ? " em todos os projetos" : ` em ${pinfo(project).name}`}?`)) decide(pendingIds(), false); };
  $("#generate").onclick = async () => {
    if (data?.plan?.status === "ready" && !confirm("Refazer o plano? As tarefas ainda não decididas serão descartadas.")) return;
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
    sw = await initProjects((slug, _info, list) => { project = slug || "all"; projects = list; if (data) render(); else load(); }, { allowAll: true });
  })();
})();
