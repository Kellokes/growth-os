// Habilidades, integrações e rotinas (automações) de UM projeto.
(() => {
  const { $, esc, api, icon, initProjects } = window.GOS;
  let project = null; let info = null; let team = []; let catalog = null;
  let skills = []; let integrations = []; let autos = [];
  let editingSkill = null; let editingAuto = null;
  const TABS = ["agentes", "habilidades", "integracoes", "rotinas"];
  let sw = null;
  const STATUS = { proposed: "aguardando você", approved: "na fila", rejected: "rejeitada", running: "executando", waiting: "esperando 2FA", done: "concluída", failed: "falhou" };
  const INT_STATUS = { off: ["", "não ligada"], needs_secret: ["warn", "falta a chave"], ready: ["ok", "pronta"], paused: ["", "pausada"] };
  const agentLabel = (slug) => { const a = team.find((x) => x.slug === slug); return a ? `${a.icon} ${a.name}` : slug; };

  // ---------- abas ----------
  function showTab(name) {
    if (!TABS.includes(name)) name = TABS[0];
    document.querySelectorAll("#tabs [data-tab]").forEach((b) => { b.classList.toggle("on", b.dataset.tab === name); b.setAttribute("aria-selected", b.dataset.tab === name); });
    document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
    try { localStorage.setItem("gos.routines.tab", name); } catch { /* ok */ }
    if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  }
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  $("#tabs").addEventListener("click", (ev) => { const b = ev.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });

  // ---------- habilidades ----------
  function skillCard(s) {
    const who = s.agents.length ? s.agents.map((a) => `<span class="tag">${esc(agentLabel(a))}</span>`).join("") : '<span class="tag">todo o time</span>';
    return `<article class="tile card ${s.enabled ? "" : "off"}" data-skill="${s.id}">
      <header><span class="tile-icon">${icon("spark")}</span><div class="grow"><h2>${esc(s.name)}</h2><span class="muted">${esc(s.description || "sem descrição")}</span></div>
        <label class="switch" title="Ligada"><input type="checkbox" data-skill-toggle ${s.enabled ? "checked" : ""}><span></span></label></header>
      <div class="task-tags">${who}</div>
      <details><summary>Passos</summary><p class="pre">${esc(s.instructions)}</p></details>
      <div class="tile-foot"><button class="btn small ghost" type="button" data-skill-del>${icon("trash")} Excluir</button><button class="btn small" type="button" data-skill-edit>${icon("edit")} Editar</button></div>
    </article>`;
  }
  function renderSkills() {
    $("#skills").innerHTML = skills.map(skillCard).join("") || `<div class="empty-card card tile-empty"><h2>Nenhuma habilidade ainda</h2><p class="muted">Crie uma do zero ou adicione da biblioteca abaixo.</p></div>`;
    const have = new Set(skills.map((s) => s.template).filter(Boolean));
    const lib = catalog.skills.filter((t) => !have.has(t.key));
    $("#skill-library").innerHTML = lib.map((t) => `<article class="tile card lib">
      <header><span class="tile-icon">${icon("spark")}</span><div class="grow"><h2>${esc(t.name)}</h2><span class="muted">${esc(t.description)}</span></div></header>
      <div class="task-tags">${t.agents.map((a) => `<span class="tag">${esc(agentLabel(a))}</span>`).join("")}</div>
      <div class="tile-foot"><button class="btn small" type="button" data-lib="${esc(t.key)}">${icon("plus")} Adicionar a ${esc(info.name)}</button></div>
    </article>`).join("") || '<div class="muted pad">Todas as habilidades da biblioteca já estão neste projeto.</div>';
  }
  function openSkill(s = null) {
    editingSkill = s;
    $("#skill-dialog-title").textContent = s ? "Editar habilidade" : `Nova habilidade · ${info.name}`;
    $("#sk-name").value = s?.name || ""; $("#sk-desc").value = s?.description || ""; $("#sk-instr").value = s?.instructions || "";
    const on = new Set(s?.agents || []);
    $("#sk-agents").innerHTML = team.map((a) => `<label class="pick-item"><input type="checkbox" value="${esc(a.slug)}" ${on.has(a.slug) ? "checked" : ""}><span>${esc(a.icon)} ${esc(a.name)}</span></label>`).join("");
    $("#skill-dialog").showModal();
  }
  $("#new-skill").onclick = () => openSkill();
  $("#sk-cancel").onclick = () => $("#skill-dialog").close();
  $("#skill-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const body = { name: $("#sk-name").value, description: $("#sk-desc").value, instructions: $("#sk-instr").value,
      agents: [...document.querySelectorAll("#sk-agents input:checked")].map((i) => i.value) };
    try {
      if (editingSkill) await api(`/api/gprojects/${project}/skills/${editingSkill.id}`, { method: "PATCH", body: JSON.stringify(body) });
      else await api(`/api/gprojects/${project}/skills`, { method: "POST", body: JSON.stringify(body) });
      $("#skill-dialog").close(); await loadSkills();
    } catch (e) { alert(e.message); }
  };
  async function loadSkills() { skills = await api(`/api/gprojects/${project}/skills`); renderSkills(); }

  // ---------- integrações ----------
  function intCard(i) {
    const [cls, label] = INT_STATUS[i.status] || ["", i.status];
    const fields = i.fields.map((f) => `<label>${esc(f.label)}<input data-field="${esc(f.key)}" value="${esc(i.config[f.key] || "")}" placeholder="${esc(f.placeholder)}" maxlength="300" autocomplete="off"></label>`).join("");
    const secret = i.secret_env
      ? `<div class="secret-note ${i.has_secret ? "ok" : ""}">${i.has_secret ? "✓ Chave encontrada no VPS" : "Chave: adicione no .env do VPS e reinicie o Growth OS"}<code>${esc(i.secret_env)}=…</code></div>` : "";
    return `<article class="tile card int st-${i.status}" data-int="${esc(i.kind)}">
      <header><span class="tile-icon emoji">${esc(i.icon)}</span><div class="grow"><h2>${esc(i.name)}</h2><span class="muted">${esc(i.category)} · ${esc(i.cost)}</span></div><span class="badge ${cls}">${esc(label)}</span></header>
      <p class="tile-about">${esc(i.about)}</p>
      ${i.kpis ? `<div class="task-tags"><span class="tag">alimenta: ${esc(i.kpis)}</span></div>` : ""}
      <details><summary>${i.connected ? "Configuração" : "Ligar neste projeto"}</summary>
        <form class="int-form" data-int-form>${fields}${secret}
          <div class="tile-foot">${i.connected ? `<label class="inline"><span class="switch"><input type="checkbox" data-int-toggle ${i.enabled ? "checked" : ""}><span></span></span> Ligada</label><button class="btn small ghost" type="button" data-int-del>Remover</button>` : "<span></span>"}<button class="btn small primary" type="submit">Salvar</button></div>
        </form>
      </details>
    </article>`;
  }
  function renderIntegrations() { $("#integrations").innerHTML = integrations.map(intCard).join(""); }
  async function loadIntegrations() { integrations = await api(`/api/gprojects/${project}/integrations`); renderIntegrations(); }

  // ---------- rotinas ----------
  const fmtNext = (iso) => iso ? new Date(iso).toLocaleString("pt-BR", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
  function autoCard(a) {
    const last = a.last_task ? `<span class="badge t-${esc(a.last_task.status)}">última: ${esc(STATUS[a.last_task.status] || a.last_task.status)}</span>` : '<span class="muted">ainda não rodou</span>';
    return `<article class="tile card ${a.enabled ? "" : "off"}" data-auto="${a.id}">
      <header><span class="tile-icon">${icon("repeat")}</span><div class="grow"><h2>${esc(a.title)}</h2><span class="muted">${esc(agentLabel(a.agent))}</span></div>
        <label class="switch" title="Ligada"><input type="checkbox" data-auto-toggle ${a.enabled ? "checked" : ""}><span></span></label></header>
      <div class="sched"><span class="sched-chip">${icon("calendar")} ${esc(a.schedule)}</span>${a.enabled ? `<span class="muted">${a.frequency === "event" ? "espera o evento" : "próxima: " + esc(fmtNext(a.next_run))}</span>` : '<span class="muted">pausada</span>'}</div>
      <div class="task-tags">${a.skill ? `<span class="tag">${icon("spark")} ${esc(a.skill)}</span>` : ""}<span class="tag ${a.auto_approve ? "r-medio" : ""}">${a.auto_approve ? "roda sem pedir aprovação" : "pede sua aprovação"}</span>${last}</div>
      <div class="tile-foot"><button class="btn small ghost" type="button" data-auto-del aria-label="Excluir" title="Excluir">${icon("trash")}</button><button class="btn small" type="button" data-auto-edit>${icon("edit")} Editar</button><button class="btn small primary" type="button" data-auto-run>${icon("play")} Rodar agora</button></div>
    </article>`;
  }
  function renderAutos() {
    $("#automations").innerHTML = autos.map(autoCard).join("") || `<div class="empty-card card tile-empty"><h2>Nenhuma rotina ainda</h2><p class="muted">Use uma sugestão abaixo ou crie a sua. Ela aparece no Kanban na hora marcada.</p></div>`;
    const have = new Set(autos.map((a) => a.title));
    $("#auto-suggest").innerHTML = catalog.automations.filter((t) => !have.has(t.title)).map((t, i) =>
      `<button class="chip" type="button" data-suggest="${catalog.automations.indexOf(t)}">${esc(agentLabel(t.agent))} · ${esc(t.title)}</button>`).join("") || '<span class="muted">Todas as sugestões já viraram rotinas.</span>';
  }
  async function loadAutos() { autos = await api(`/api/gprojects/${project}/automations`); renderAutos(); }

  function syncFreq() {
    const f = $("#au-freq").value;
    $("#au-wd-wrap").hidden = f !== "weekly"; $("#au-md-wrap").hidden = f !== "monthly";
    const ev = f === "event";
    $("#au-hour-wrap").hidden = ev; $("#au-event-wrap").hidden = !ev;
    $("#au-evmin-wrap").hidden = !ev || $("#au-event").value !== "kpi.updated";
  }
  function openAuto(a = null, tpl = null) {
    editingAuto = a;
    const src = a || tpl || {};
    $("#auto-dialog-title").textContent = a ? "Editar rotina" : `Nova rotina · ${info.name}`;
    $("#au-agent").innerHTML = team.map((x) => `<option value="${esc(x.slug)}">${esc(x.icon)} ${esc(x.name)}</option>`).join("");
    $("#au-agent").value = src.agent || team[0]?.slug || "";
    const libOpts = tpl?.skill_template && !skills.some((s) => s.template === tpl.skill_template)
      ? catalog.skills.filter((s) => s.key === tpl.skill_template).map((s) => `<option value="tpl:${esc(s.key)}">${esc(s.name)} (da biblioteca)</option>`).join("") : "";
    $("#au-skill").innerHTML = '<option value="">Nenhuma</option>' + skills.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("") + libOpts;
    const fromTpl = tpl?.skill_template ? (skills.find((s) => s.template === tpl.skill_template)?.id ?? `tpl:${tpl.skill_template}`) : "";
    $("#au-skill").value = String(a ? (a.skill_id ?? "") : fromTpl);
    $("#au-title").value = src.title || ""; $("#au-steps").value = src.steps || "";
    $("#au-freq").value = src.frequency || "daily";
    $("#au-weekday").innerHTML = catalog.weekdays.map((d, i) => `<option value="${i}">${esc(d)}</option>`).join("");
    $("#au-weekday").value = String(src.weekday ?? 0);
    $("#au-monthday").innerHTML = Array.from({ length: 28 }, (_, i) => `<option value="${i + 1}">dia ${i + 1}</option>`).join("");
    $("#au-monthday").value = String(src.monthday ?? 1);
    $("#au-hour").innerHTML = Array.from({ length: 24 }, (_, h) => `<option value="${h}">${String(h).padStart(2, "0")}:00</option>`).join("");
    $("#au-hour").value = String(src.hour ?? 9);
    $("#au-event").innerHTML = (catalog.events || []).map((e) => `<option value="${esc(e.key)}">${esc(e.label)}</option>`).join("");
    $("#au-event").value = src.event_type || "site.down";
    $("#au-evfilter").value = src.event_filter || ""; $("#au-evmin").value = String(src.event_min || 20);
    $("#au-auto").checked = !!src.auto_approve; syncAutoNote();
    syncFreq();
    $("#auto-dialog").showModal();
  }
  function syncAutoNote() {
    $("#au-auto-note").textContent = $("#au-auto").checked
      ? "Ligado: entra direto na fila do agente. Comandos perigosos continuam pedindo 2FA."
      : "Desligado: cada vez entra como proposta e espera você aprovar.";
  }
  $("#au-auto").onchange = syncAutoNote;
  $("#au-freq").onchange = syncFreq;
  $("#au-event").onchange = syncFreq;
  $("#new-auto").onclick = () => openAuto();
  $("#au-cancel").onclick = () => $("#auto-dialog").close();
  $("#auto-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const sk = $("#au-skill").value;
    const body = { agent: $("#au-agent").value, title: $("#au-title").value, steps: $("#au-steps").value,
      frequency: $("#au-freq").value, weekday: Number($("#au-weekday").value), monthday: Number($("#au-monthday").value),
      hour: Number($("#au-hour").value), auto_approve: $("#au-auto").checked };
    if (body.frequency === "event") Object.assign(body, { event_type: $("#au-event").value, event_filter: $("#au-evfilter").value.trim(), event_min: Number($("#au-evmin").value) || 20 });
    if (sk.startsWith("tpl:")) body.skill_template = sk.slice(4); else body.skill_id = sk ? Number(sk) : null;
    try {
      if (editingAuto) await api(`/api/gprojects/${project}/automations/${editingAuto.id}`, { method: "PATCH", body: JSON.stringify(body) });
      else await api(`/api/gprojects/${project}/automations`, { method: "POST", body: JSON.stringify(body) });
      $("#auto-dialog").close(); await Promise.all([loadAutos(), loadSkills()]);
    } catch (e) { alert(e.message); }
  };

  // ---------- eventos ----------
  const page = document.querySelector(".page");
  page.addEventListener("click", async (ev) => {
    const t = ev.target;
    const lib = t.closest("[data-lib]");
    if (lib) { lib.disabled = true; try { await api(`/api/gprojects/${project}/skills/template/${lib.dataset.lib}`, { method: "POST" }); await loadSkills(); } catch (e) { alert(e.message); lib.disabled = false; } return; }
    const sc = t.closest("[data-skill]");
    if (sc) {
      const s = skills.find((x) => x.id === Number(sc.dataset.skill));
      if (t.closest("[data-skill-edit]")) return openSkill(s);
      if (t.closest("[data-skill-del]")) {
        if (!confirm(`Excluir a habilidade “${s.name}”? Rotinas que usam ela continuam, só sem a habilidade.`)) return;
        try { await api(`/api/gprojects/${project}/skills/${s.id}`, { method: "DELETE" }); await Promise.all([loadSkills(), loadAutos()]); } catch (e) { alert(e.message); }
        return;
      }
    }
    const ic = t.closest("[data-int]");
    if (ic && t.closest("[data-int-del]")) {
      if (!confirm("Remover esta integração do projeto? A chave no .env do VPS não é apagada.")) return;
      try { await api(`/api/gprojects/${project}/integrations/${ic.dataset.int}`, { method: "DELETE" }); await loadIntegrations(); } catch (e) { alert(e.message); }
      return;
    }
    const sg = t.closest("[data-suggest]");
    if (sg) return openAuto(null, catalog.automations[Number(sg.dataset.suggest)]);
    const ac = t.closest("[data-auto]");
    if (ac) {
      const a = autos.find((x) => x.id === Number(ac.dataset.auto));
      if (t.closest("[data-auto-edit]")) return openAuto(a);
      if (t.closest("[data-auto-del]")) {
        if (!confirm(`Excluir a rotina “${a.title}”? As tarefas que ela já criou continuam no Kanban.`)) return;
        try { await api(`/api/gprojects/${project}/automations/${a.id}`, { method: "DELETE" }); await loadAutos(); } catch (e) { alert(e.message); }
        return;
      }
      const run = t.closest("[data-auto-run]");
      if (run) {
        run.disabled = true;
        try { await api(`/api/gprojects/${project}/automations/${a.id}/run`, { method: "POST" }); run.innerHTML = "Na fila ✓"; setTimeout(loadAutos, 1500); }
        catch (e) { alert(e.message); run.disabled = false; }
      }
    }
  });
  page.addEventListener("change", async (ev) => {
    const t = ev.target;
    try {
      if (t.matches("[data-skill-toggle]")) {
        const id = t.closest("[data-skill]").dataset.skill; t.closest(".tile").classList.toggle("off", !t.checked);
        await api(`/api/gprojects/${project}/skills/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: t.checked }) });
      } else if (t.matches("[data-auto-toggle]")) {
        const id = t.closest("[data-auto]").dataset.auto;
        await api(`/api/gprojects/${project}/automations/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: t.checked }) });
        await loadAutos();
      } else if (t.matches("[data-int-toggle]")) {
        const kind = t.closest("[data-int]").dataset.int;
        const cfg = Object.fromEntries([...t.closest("form").querySelectorAll("[data-field]")].map((i) => [i.dataset.field, i.value]));
        await api(`/api/gprojects/${project}/integrations/${kind}`, { method: "PUT", body: JSON.stringify({ config: cfg, enabled: t.checked }) });
        await loadIntegrations();
      }
    } catch (e) { alert(e.message); }
  });
  page.addEventListener("submit", async (ev) => {
    const f = ev.target.closest("[data-int-form]"); if (!f) return;
    ev.preventDefault();
    const kind = f.closest("[data-int]").dataset.int;
    const cfg = Object.fromEntries([...f.querySelectorAll("[data-field]")].map((i) => [i.dataset.field, i.value]));
    try { await api(`/api/gprojects/${project}/integrations/${kind}`, { method: "PUT", body: JSON.stringify({ config: cfg }) }); await loadIntegrations(); }
    catch (e) { alert(e.message); }
  });

  async function load() {
    $("#page-crumb").textContent = info.name;
    team = await api(`/api/gprojects/${project}/agents`);
    window.GOSAgents.show(project, info, () => sw);
    await Promise.all([loadSkills(), loadIntegrations(), loadAutos()]);
  }

  (async () => {
    const wantsNew = location.hash === "#novo";
    let tab = wantsNew ? "agentes" : location.hash.slice(1);
    if (!TABS.includes(tab)) { try { tab = localStorage.getItem("gos.routines.tab") || ""; } catch { tab = ""; } }
    showTab(tab);
    catalog = await api("/api/routines/catalog");
    sw = await initProjects((slug, p) => {
      project = slug; info = p; page.hidden = !slug;
      if (slug) load().catch((e) => { $("#skills").innerHTML = `<div class="alert">${esc(e.message)}</div>`; });
    });
    if (wantsNew) window.GOSAgents.openNew();
  })();
})();
