(() => {
  const { $, esc, api, initProjects } = window.GOS;
  const M = window.GOSModels;
  const pickers = {};
  let providers = []; let metrics = []; let agents = []; let project = null; let info = null; let switcher = null;
  const STATUS = { proposed: "aguardando", approved: "na fila", rejected: "rejeitada", running: "executando", waiting: "esperando 2FA", done: "concluída", failed: "falhou" };

  function card(a) {
    const kpis = metrics.filter((m) => m.area === a.slug).map((m) => `<span class="tag">${esc(m.name)}</span>`).join("") || '<span class="muted">sem KPIs próprios</span>';
    const st = a.stats || {};
    return `<article class="agent card ${a.enabled ? "" : "off"}" data-slug="${esc(a.slug)}">
      <header>
        <span class="agent-icon">${esc(a.icon)}</span>
        <div class="grow"><h2>${esc(a.name)}</h2><span class="muted">${esc(a.area)} · ${esc(info.name)}</span></div>
        <label class="switch" title="Ativo no plano diário deste projeto"><input type="checkbox" data-toggle ${a.enabled ? "checked" : ""}><span></span></label>
      </header>
      <div class="agent-stats"><span><b>${st.done || 0}</b> concluídas</span><span><b>${(st.approved || 0) + (st.running || 0) + (st.waiting || 0)}</b> em andamento</span><span><b>${st.proposed || 0}</b> aguardando</span></div>
      <div class="task-tags">${kpis}</div>
      <label>Missão neste projeto ${a.custom ? '<span class="tag">personalizada</span>' : '<span class="muted">(padrão da área)</span>'}<textarea rows="5" data-mission maxlength="4000">${esc(a.mission)}</textarea></label>
      <div class="agent-model" data-mp></div>
      <div class="agent-foot">
        <span class="muted small-print">Plano do dia e execução usam este modelo. Em “Padrão” vale a tela <a href="/settings">Modelos</a>.</span>
        <button class="btn small" type="button" data-save>Salvar</button>
      </div>
    </article>`;
  }

  function fillProject() {
    $("#project-summary").innerHTML = `${esc(info.icon)} <b>${esc(info.name)}</b>${info.repo ? ` · <span class="muted">${esc(info.repo)}</span>` : ' · <span class="muted">sem repositório</span>'}${info.description ? "" : ' · <span class="warn-text">falta a descrição</span>'}`;
    $("#pf-name").value = info.name; $("#pf-icon").value = info.icon; $("#pf-desc").value = info.description;
    $("#pf-repo").value = info.repo; $("#pf-domains").value = info.domains; $("#pf-enabled").checked = info.enabled;
    if (!info.description) $("#project-card").open = true;
  }

  async function load() {
    $("#page-crumb").textContent = info.name;
    fillProject();
    [providers, metrics, agents] = await Promise.all([M.load(api, true), api(`/api/gprojects/${project}/metrics?days=7`), api(`/api/gprojects/${project}/agents`)]);
    $("#agents").innerHTML = agents.map(card).join("");
    const eff = providers.effective.agents_run;
    const inherit = eff?.provider ? `Padrão (${M.describe(eff)})` : "Padrão do sistema";
    for (const a of agents) {
      const root = document.querySelector(`[data-slug="${CSS.escape(a.slug)}"] [data-mp]`);
      pickers[a.slug] = M.mount(root, api, { kind: "text", inherit, value: { provider: a.provider, model: a.model, reasoning: a.reasoning } });
    }
    const notes = await api(`/api/gprojects/${project}/notes?limit=30`);
    const who = (s) => { const a = agents.find((x) => x.slug === s); return a ? `${a.icon} ${a.name}` : s; };
    const KIND = { pedido: "pediu a", resposta: "respondeu a", nota: "deixou recado para" };
    $("#notes").innerHTML = notes.length ? notes.map((n) => `<div class="note ${esc(n.kind)}">
        <div class="note-head"><b>${esc(who(n.from))}</b> <span class="muted">${esc(KIND[n.kind] || "→")}</span> <b>${n.to ? esc(who(n.to)) : "o time"}</b>
        <span class="muted note-time">${new Date(n.at + "Z").toLocaleString("pt-BR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</span></div>
        <div class="note-text">${esc(n.text)}</div></div>`).join("")
      : '<div class="muted pad">Ainda sem conversa. Quando um agente pedir algo a outro ou deixar um recado, aparece aqui.</div>';
    const tasks = await api(`/api/tasks?project=${project}&limit=30`);
    const names = Object.fromEntries(agents.map((a) => [a.slug, `${a.icon} ${a.name}`]));
    $("#tasks").innerHTML = tasks.length ? tasks.map((t) => `<div class="task-row">
        <span class="badge t-${t.status}">${esc(STATUS[t.status] || t.status)}</span>
        <span class="muted">${esc(names[t.agent] || t.agent)}</span>
        <span class="grow">${t.requested_by ? `<span class="tag handoff">🤝 de ${esc(names[t.requested_by] || t.requested_by)}</span> ` : ""}${esc(t.title)}</span>
        ${t.conversation_id ? `<a href="/?c=${t.conversation_id}">thread →</a>` : ""}
      </div>`).join("") : '<div class="muted pad">Nenhuma tarefa ainda neste projeto. O primeiro plano sai amanhã cedo, ou gere agora em Plano do dia.</div>';
  }

  async function patch(slug, body, btn) {
    if (btn) btn.disabled = true;
    try { await api(`/api/gprojects/${project}/agents/${slug}`, { method: "PATCH", body: JSON.stringify(body) }); if (btn) { btn.textContent = "Salvo ✓"; setTimeout(() => (btn.textContent = "Salvar"), 1500); } }
    catch (e) { alert(e.message); } finally { if (btn) btn.disabled = false; }
  }

  $("#agents").addEventListener("change", (ev) => {
    const cardEl = ev.target.closest("[data-slug]");
    if (ev.target.matches("[data-toggle]")) { cardEl.classList.toggle("off", !ev.target.checked); patch(cardEl.dataset.slug, { enabled: ev.target.checked }); }
  });
  $("#agents").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-save]"); if (!btn) return;
    const c = btn.closest("[data-slug]");
    patch(c.dataset.slug, { mission: c.querySelector("[data-mission]").value, ...pickers[c.dataset.slug].get() }, btn);
  });
  $("#project-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await api(`/api/gprojects/${project}`, { method: "PATCH", body: JSON.stringify({ name: $("#pf-name").value, icon: $("#pf-icon").value || "◆", description: $("#pf-desc").value, repo: $("#pf-repo").value, domains: $("#pf-domains").value, enabled: $("#pf-enabled").checked }) });
      $("#project-card").open = false; await switcher.reload();
    } catch (e) { alert(e.message); }
  };
  const openNew = () => { $("#np-name").value = ""; $("#np-desc").value = ""; $("#np-dialog").showModal(); };
  $("#new-project").onclick = openNew;
  $("#np-cancel").onclick = () => $("#np-dialog").close();
  $("#np-import").onclick = async () => {
    try { const r = await api("/api/gprojects/import", { method: "POST" }); alert(r.created.length ? `Importados: ${r.created.join(", ")}` : "Nenhum projeto novo no VPS."); $("#np-dialog").close(); await switcher.reload(); }
    catch (e) { alert(e.message); }
  };
  $("#np-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      const p = await api("/api/gprojects", { method: "POST", body: JSON.stringify({ name: $("#np-name").value, description: $("#np-desc").value }) });
      try { localStorage.setItem("gos.project", p.slug); } catch { /* ok */ }
      $("#np-dialog").close(); await switcher.reload();
    } catch (e) { alert(e.message); }
  };

  (async () => {
    switcher = await initProjects((slug, p) => {
      project = slug; info = p;
      document.querySelector(".page").hidden = !slug;
      if (slug) load().catch((e) => { $("#agents").innerHTML = `<div class="alert">${esc(e.message)}</div>`; });
    });
    if (location.hash === "#novo") openNew();
  })();
})();
