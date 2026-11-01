(() => {
  const { $, esc, api } = window.GOS;
  let providers = []; let metrics = []; let agents = [];
  const STATUS = { proposed: "aguardando", approved: "na fila", rejected: "rejeitada", running: "executando", waiting: "esperando 2FA", done: "concluída", failed: "falhou" };

  function card(a) {
    const kpis = metrics.filter((m) => m.area === a.slug).map((m) => `<span class="tag">${esc(m.name)}</span>`).join("") || '<span class="muted">sem KPIs próprios</span>';
    const st = a.stats || {};
    const opts = [`<option value="">Padrão do sistema</option>`].concat(providers.map((p) => `<option value="${esc(p.id)}" ${a.provider === p.id ? "selected" : ""} ${p.available ? "" : "disabled"}>${esc(p.label)}</option>`)).join("");
    return `<article class="agent card ${a.enabled ? "" : "off"}" data-slug="${esc(a.slug)}">
      <header>
        <span class="agent-icon">${esc(a.icon)}</span>
        <div class="grow"><h2>${esc(a.name)}</h2><span class="muted">${esc(a.area)}</span></div>
        <label class="switch" title="Ativo no plano diário"><input type="checkbox" data-toggle ${a.enabled ? "checked" : ""}><span></span></label>
      </header>
      <div class="agent-stats"><span><b>${st.done || 0}</b> concluídas</span><span><b>${(st.approved || 0) + (st.running || 0) + (st.waiting || 0)}</b> em andamento</span><span><b>${st.proposed || 0}</b> aguardando</span></div>
      <div class="task-tags">${kpis}</div>
      <label>Missão<textarea rows="5" data-mission maxlength="4000">${esc(a.mission)}</textarea></label>
      <div class="agent-foot">
        <label class="inline">Provider <select data-provider>${opts}</select></label>
        <button class="btn small" type="button" data-save>Salvar</button>
      </div>
    </article>`;
  }

  async function load() {
    [providers, metrics, agents] = await Promise.all([api("/api/providers"), api("/api/metrics?days=7"), api("/api/agents")]);
    $("#agents").innerHTML = agents.map(card).join("");
    const tasks = await api("/api/tasks?limit=30");
    const names = Object.fromEntries(agents.map((a) => [a.slug, `${a.icon} ${a.name}`]));
    $("#tasks").innerHTML = tasks.length ? tasks.map((t) => `<div class="task-row">
        <span class="badge t-${t.status}">${esc(STATUS[t.status] || t.status)}</span>
        <span class="muted">${esc(names[t.agent] || t.agent)}</span>
        <span class="grow">${esc(t.title)}</span>
        ${t.conversation_id ? `<a href="/?c=${t.conversation_id}">conversa →</a>` : ""}
      </div>`).join("") : '<div class="muted pad">Nenhuma tarefa ainda. O primeiro plano sai amanhã cedo, ou gere agora em Plano do dia.</div>';
  }

  async function patch(slug, body, btn) {
    if (btn) btn.disabled = true;
    try { await api(`/api/agents/${slug}`, { method: "PATCH", body: JSON.stringify(body) }); if (btn) { btn.textContent = "Salvo ✓"; setTimeout(() => (btn.textContent = "Salvar"), 1500); } }
    catch (e) { alert(e.message); } finally { if (btn) btn.disabled = false; }
  }

  $("#agents").addEventListener("change", (ev) => {
    const cardEl = ev.target.closest("[data-slug]");
    if (ev.target.matches("[data-toggle]")) { cardEl.classList.toggle("off", !ev.target.checked); patch(cardEl.dataset.slug, { enabled: ev.target.checked }); }
  });
  $("#agents").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-save]"); if (!btn) return;
    const c = btn.closest("[data-slug]");
    patch(c.dataset.slug, { mission: c.querySelector("[data-mission]").value, provider: c.querySelector("[data-provider]").value }, btn);
  });
  load().catch((e) => { $("#agents").innerHTML = `<div class="alert">${esc(e.message)}</div>`; });
})();
