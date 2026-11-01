// Central de conteúdo: peças (posts, e-mails...), agenda e saídas dos agentes, por projeto (ou todos).
(() => {
  const { $, esc, api, icon, initProjects } = window.GOS;
  const TABS = ["pecas", "agenda", "saidas"];
  const STAGES = [
    ["pending", "Aguardando você", "aprovar ou rejeitar"],
    ["scheduled", "Agendadas", "aprovadas, com data"],
    ["ready", "Prontas para sair", "aprovadas, é só publicar"],
    ["sent", "Enviadas", "publicadas ou enviadas"],
    ["closed", "Rejeitadas ou falharam", ""],
  ];
  const STAGE_LABEL = { pending: "aguardando aprovação", scheduled: "agendada", ready: "pronta para sair", sent: "enviada", rejected: "rejeitada", failed: "falhou" };
  const STAGE_BADGE = { pending: "t-proposed", scheduled: "t-approved", ready: "c-ready", sent: "t-done", rejected: "t-rejected", failed: "t-failed" };
  const TASK_STATUS = { done: "concluída", failed: "falhou" };

  let agents = {}; let projects = []; let channels = []; let project = "all";
  let items = []; let stageF = "pending"; let groupF = "";
  let aDays = 7; let oDays = 14; let outAgent = ""; let outputs = [];
  let current = null; let sw = null;
  const loaded = { agenda: false, saidas: false };

  const pinfo = (slug) => projects.find((p) => p.slug === slug) || { name: slug, icon: "◆" };
  const aname = (slug) => slug ? (agents[slug] ? `${agents[slug].icon} ${agents[slug].name}` : slug) : "✋ você";
  const chan = (key) => channels.find((c) => c.key === key) || { key, name: key, icon: "◆", group: "outro" };
  const inStage = (it, s) => s === "closed" ? ["rejected", "failed"].includes(it.stage) : it.stage === s;
  const parseLocal = (iso) => new Date(iso.length <= 16 ? iso + ":00" : iso); // horário local do owner
  const parseUtc = (iso) => new Date(iso + "Z");

  function when(iso, utc = false) {
    if (!iso) return "";
    const d = utc ? parseUtc(iso) : parseLocal(iso);
    return d.toLocaleString("pt-BR", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  }
  function ago(iso) {
    if (!iso) return "";
    const s = (Date.now() - parseUtc(iso).getTime()) / 1000;
    if (s < 60) return "agora";
    if (s < 3600) return `há ${Math.round(s / 60)} min`;
    if (s < 86400) return `há ${Math.round(s / 3600)} h`;
    return parseUtc(iso).toLocaleDateString("pt-BR", { day: "2-digit", month: "short" });
  }
  const strip = (t) => String(t || "").replace(/```[\s\S]*?```/g, "").trim();

  // ---------- abas ----------
  function showTab(name) {
    if (!TABS.includes(name)) name = TABS[0];
    document.querySelectorAll("#tabs [data-tab]").forEach((b) => { b.classList.toggle("on", b.dataset.tab === name); b.setAttribute("aria-selected", b.dataset.tab === name); });
    document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
    if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
    if (name === "agenda" && !loaded.agenda) loadAgenda();
    if (name === "saidas" && !loaded.saidas) loadOutputs();
  }
  const activeTab = () => { const h = location.hash.slice(1); return TABS.includes(h) ? h : TABS[0]; };
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  $("#tabs").addEventListener("click", (ev) => { const b = ev.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });

  // ---------- peças ----------
  function piece(it) {
    const c = chan(it.channel);
    const email = it.group === "email";
    const head = email
      ? `<div class="cmail"><span>${icon("mail")}</span><div><b>${esc(it.title || "(sem assunto)")}</b><small class="muted">para ${esc(it.recipient || "—")}</small></div></div>`
      : (it.title ? `<div class="ctitle">${esc(it.title)}</div>` : "");
    const date = it.stage === "sent" ? `enviada ${ago(it.sent_at)}` : it.scheduled_at ? when(it.scheduled_at) : ago(it.created_at);
    let actions = "";
    if (it.status === "pending") actions = `<button class="btn small danger" type="button" data-reject="${it.id}">Rejeitar</button><button class="btn small primary" type="button" data-approve="${it.id}">Aprovar</button>`;
    else if (it.status === "approved" || it.status === "failed") {
      const auto = it.group === "email" || it.group === "social";
      actions = `${auto ? `<button class="btn small primary" type="button" data-publish="${it.id}">${icon("send")} Publicar</button>` : ""}<button class="btn small" type="button" data-sent="${it.id}">Marcar enviada</button>`;
    }
    else if (it.status === "sent" && it.link) actions = `<a class="btn small ghost" href="${esc(it.link)}" target="_blank" rel="noopener noreferrer">Ver ${icon("arrow-ur")}</a>`;
    return `<article class="cpiece card st-${esc(it.stage)} g-${esc(it.group)}" data-id="${it.id}" tabindex="0">
      <header class="cpiece-top"><span class="cchan">${esc(c.icon)} ${esc(c.name)}</span><span class="badge ${STAGE_BADGE[it.stage] || ""}">${esc(STAGE_LABEL[it.stage] || it.stage)}</span></header>
      ${head}
      <p class="cbody">${esc(it.body)}</p>
      ${it.media ? `<div class="cmedia">🖼 ${esc(it.media)}</div>` : ""}
      <div class="task-tags"><span class="tag">${esc(aname(it.agent))}</span>${project === "all" ? `<span class="tag">${esc(pinfo(it.project).icon)} ${esc(pinfo(it.project).name)}</span>` : ""}${it.purpose ? `<span class="tag">${esc(it.purpose)}</span>` : ""}</div>
      <footer class="cpiece-foot"><span class="muted">${it.scheduled_at && it.stage !== "sent" ? icon("calendar") : ""}${esc(date)}</span>${actions ? `<div class="kcard-actions">${actions}</div>` : ""}</footer>
    </article>`;
  }

  function renderPieces() {
    $("#page-crumb").textContent = project === "all" ? "Todos os projetos" : pinfo(project).name;
    $("#cstats").innerHTML = STAGES.map(([key, label, hint]) => {
      const n = items.filter((it) => inStage(it, key)).length;
      return `<button type="button" class="cstat s-${key} ${stageF === key ? "on" : ""}" data-stage="${key}" aria-pressed="${stageF === key}">
        <b>${n}</b><span>${esc(label)}</span>${hint ? `<small>${esc(hint)}</small>` : ""}</button>`;
    }).join("");
    const inS = items.filter((it) => inStage(it, stageF));
    const groups = {};
    for (const it of inS) groups[it.group] = (groups[it.group] || 0) + 1;
    const names = Object.fromEntries(channels.map((c) => [c.group, c.group_name]));
    if (groupF && !groups[groupF]) groupF = "";
    const chips = [["", "Todos os canais", inS.length]].concat(Object.keys(groups).map((g) => [g, names[g] || g, groups[g]]));
    $("#group-filter").innerHTML = chips.length > 2 || groupF ? chips.map(([g, l, n]) => `<button type="button" class="fchip ${g === groupF ? "active" : ""}" data-group="${esc(g)}">${esc(l)} <span>${n}</span></button>`).join("") : "";
    const list = inS.filter((it) => !groupF || it.group === groupF);
    if (stageF === "scheduled") list.sort((a, b) => (a.scheduled_at || "").localeCompare(b.scheduled_at || ""));
    $("#pieces").innerHTML = list.map(piece).join("") || emptyPieces();
  }
  function emptyPieces() {
    const msg = {
      pending: ["Nada esperando você", "Quando um agente escrever um post ou e-mail, ele aparece aqui para você aprovar."],
      scheduled: ["Nenhuma peça agendada", "Abra uma peça aprovada e escolha quando ela sai."],
      ready: ["Nada pronto para sair", "As peças aprovadas sem data (ou cuja hora chegou) ficam aqui."],
      sent: ["Nada enviado ainda", "Depois de publicar, marque a peça como enviada para guardar o histórico."],
      closed: ["Nenhuma rejeitada", ""],
    }[stageF];
    return `<div class="empty-card card tile-empty"><h2>${esc(msg[0])}</h2>${msg[1] ? `<p class="muted">${esc(msg[1])}</p>` : ""}</div>`;
  }

  async function loadPieces() {
    try { items = (await api(`/api/content?project=${encodeURIComponent(project)}&days=60`)).items; renderPieces(); }
    catch (e) { $("#pieces").innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  }

  async function act(fn) {
    try { await fn(); } catch (e) { alert(e.message); return false; }
    await loadPieces(); loaded.agenda = false; loaded.saidas = false;
    if (activeTab() === "agenda") loadAgenda();
    return true;
  }
  const decide = (id, approve, note = "") => act(() => api(`/api/content/${id}/decide`, { method: "POST", body: JSON.stringify({ approve, note }) }));
  const markSent = (id, link = "") => act(() => api(`/api/content/${id}/sent`, { method: "POST", body: JSON.stringify({ link }) }));

  // ---------- diálogo: ver, editar, aprovar, agendar, marcar enviada ----------
  function fillChannels(sel) {
    const groups = {};
    for (const c of channels) (groups[c.group_name] ||= []).push(c);
    $("#cf-channel").innerHTML = Object.entries(groups).map(([g, cs]) => `<optgroup label="${esc(g)}">${cs.map((c) => `<option value="${esc(c.key)}" ${c.key === sel ? "selected" : ""}>${esc(c.icon)} ${esc(c.name)}</option>`).join("")}</optgroup>`).join("");
  }
  function labelsFor(channel) {
    const email = chan(channel).group === "email";
    $("#cf-title-label").textContent = email ? "Assunto" : "Título";
    $("#cf-to-label").textContent = email ? "Para (e-mail ou nome)" : "Público";
  }
  $("#cf-channel").addEventListener("change", () => labelsFor($("#cf-channel").value));

  function openPiece(it) {
    current = it;
    const isNew = !it;
    const ro = it && ["sent", "rejected"].includes(it.status);
    $("#cf-title").textContent = isNew ? "Nova peça" : `${chan(it.channel).icon} ${it.title || chan(it.channel).name}`;
    $("#cf-project-wrap").hidden = !isNew;
    if (isNew) {
      $("#cf-project").innerHTML = projects.map((p) => `<option value="${esc(p.slug)}" ${p.slug === project ? "selected" : ""}>${esc(p.icon)} ${esc(p.name)}</option>`).join("");
    }
    fillChannels(it?.channel || "instagram");
    labelsFor(it?.channel || "instagram");
    $("#cf-head").value = it?.title || ""; $("#cf-to").value = it?.recipient || ""; $("#cf-purpose").value = it?.purpose || "";
    $("#cf-body").value = it?.body || ""; $("#cf-media").value = it?.media || ""; $("#cf-when").value = it?.scheduled_at || "";
    $("#content-form").querySelectorAll("input, textarea, select").forEach((el) => { if (el.id !== "cf-link") el.disabled = !!ro; });
    $("#cf-tags").innerHTML = isNew ? "" : `<span class="badge ${STAGE_BADGE[it.stage] || ""}">${esc(STAGE_LABEL[it.stage] || it.stage)}</span>
      <span class="tag">${esc(aname(it.agent))}</span><span class="tag">${esc(pinfo(it.project).icon)} ${esc(pinfo(it.project).name)}</span>
      ${it.sent_at ? `<span class="tag">enviada ${esc(when(it.sent_at, true))}</span>` : ""}
      ${it.link ? `<a class="tag" href="${esc(it.link)}" target="_blank" rel="noopener noreferrer">ver onde saiu ${icon("arrow-ur")}</a>` : ""}
      ${it.task_id ? `<a class="tag" href="/board">tarefa #${it.task_id}</a>` : ""}`;
    const note = $("#cf-note");
    note.hidden = !(it && (it.note || it.stage === "ready"));
    note.textContent = it?.note ? `Motivo: ${it.note}` : it?.stage === "ready" ? "Aprovada. O envio automático chega com as integrações; por enquanto copie o texto, publique e marque como enviada." : "";
    $("#cf-sent-box").hidden = true; $("#cf-link").value = "";
    renderActions();
    $("#content-dialog").showModal();
  }

  function renderActions() {
    const it = current; const b = [];
    if (!it) b.push('<button class="btn ghost" type="button" data-cf="cancel">Cancelar</button>', '<button class="btn primary" type="submit">Salvar (já aprovada)</button>');
    else {
      b.push(`<button class="btn ghost" type="button" data-cf="copy">${icon("copy")} Copiar texto</button>`);
      if (it.status !== "sent") b.push(`<button class="btn ghost" type="button" data-cf="delete">${icon("trash")} Excluir</button>`);
      if (["rejected", "failed", "approved"].includes(it.status)) b.push('<button class="btn ghost" type="button" data-cf="reopen">Voltar para aprovação</button>');
      if (it.status === "pending") b.push('<button class="btn danger" type="button" data-cf="reject">Rejeitar</button>', '<button class="btn" type="submit">Salvar</button>', `<button class="btn primary" type="button" data-cf="approve">${icon("check")} Salvar e aprovar</button>`);
      if (["approved", "failed"].includes(it.status)) b.push('<button class="btn" type="submit">Salvar</button>', `<button class="btn primary" type="button" data-cf="sent">${icon("send")} Marcar enviada</button>`);
    }
    $("#cf-actions").innerHTML = b.join("");
  }

  const formBody = () => ({ channel: $("#cf-channel").value, title: $("#cf-head").value, recipient: $("#cf-to").value,
    purpose: $("#cf-purpose").value, body: $("#cf-body").value, media: $("#cf-media").value, scheduled_at: $("#cf-when").value });
  // Os campos do formulário têm o mesmo nome que os do JSON da peça.
  const changed = () => { const f = formBody(); return Object.keys(f).some((k) => (f[k] || "") !== (current[k] || "")); };

  async function save() {
    if (!current) {
      if (!$("#cf-body").value.trim()) return alert("Escreva o texto.");
      return act(() => api("/api/content", { method: "POST", body: JSON.stringify({ ...formBody(), project: $("#cf-project").value }) }));
    }
    if (!changed()) return true;
    return act(() => api(`/api/content/${current.id}`, { method: "PATCH", body: JSON.stringify(formBody()) }));
  }

  $("#content-form").onsubmit = async (ev) => { ev.preventDefault(); if (await save()) $("#content-dialog").close(); };
  $("#cf-close").onclick = () => $("#content-dialog").close();
  $("#cf-actions").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-cf]"); if (!btn) return;
    const it = current; const what = btn.dataset.cf;
    if (what === "cancel") return $("#content-dialog").close();
    if (what === "copy") {
      const text = [it.group === "email" && it.title ? `Assunto: ${it.title}` : "", $("#cf-body").value].filter(Boolean).join("\n\n");
      try { await navigator.clipboard.writeText(text); btn.innerHTML = `${icon("check")} Copiado`; } catch { alert("Não deu para copiar; selecione o texto e copie."); }
      return;
    }
    if (what === "delete") { if (!confirm("Excluir esta peça?")) return; if (await act(() => api(`/api/content/${it.id}`, { method: "DELETE" }))) $("#content-dialog").close(); return; }
    if (what === "reopen") { if (await act(() => api(`/api/content/${it.id}/reopen`, { method: "POST" }))) $("#content-dialog").close(); return; }
    if (what === "reject") { const note = prompt("Motivo (opcional):", ""); if (note === null) return; if (await decide(it.id, false, note)) $("#content-dialog").close(); return; }
    if (what === "approve") { if (!(await save())) return; if (await decide(it.id, true)) $("#content-dialog").close(); return; }
    if (what === "sent") { $("#cf-sent-box").hidden = false; $("#cf-actions").hidden = true; $("#cf-link").focus(); }
  });
  $("#cf-sent-cancel").onclick = () => { $("#cf-sent-box").hidden = true; $("#cf-actions").hidden = false; };
  $("#cf-sent-ok").onclick = async () => {
    const link = $("#cf-link").value.trim();
    if (link && !/^https?:\/\/\S+$/.test(link)) return alert("O link precisa começar com http:// ou https://");
    if (!(await save())) return;
    if (await markSent(current.id, link)) $("#content-dialog").close();
  };
  $("#content-dialog").addEventListener("close", () => { $("#cf-actions").hidden = false; });
  $("#new-content").onclick = () => {
    if (!projects.length) return alert("Cadastre um projeto primeiro.");
    showTab("pecas"); openPiece(null);
  };

  // ---------- agenda ----------
  function dayLabel(d) {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const diff = Math.round((d - today) / 86400000);
    const base = d.toLocaleDateString("pt-BR", { weekday: "long", day: "2-digit", month: "short" });
    return diff === 0 ? `Hoje · ${base}` : diff === 1 ? `Amanhã · ${base}` : base.charAt(0).toUpperCase() + base.slice(1);
  }
  function renderAgenda(data) {
    const byDay = {};
    for (const e of data.events) (byDay[e.at.slice(0, 10)] ||= []).push(e);
    const days = Object.keys(byDay).sort();
    const extra = data.unscheduled ? `<div class="secret-note">${data.unscheduled} peça(s) aprovada(s) sem data estão em “Prontas para sair”.</div>` : "";
    if (!days.length) {
      $("#agenda").innerHTML = extra + `<div class="empty-card card"><h2>Nada programado nos próximos ${data.days} dias</h2><p class="muted">Dê uma data às peças aprovadas ou crie rotinas em Automações.</p></div>`;
      return;
    }
    $("#agenda").innerHTML = extra + days.map((day) => {
      const d = parseLocal(day + "T00:00");
      return `<section class="aday card"><h2>${esc(dayLabel(d))}</h2><ol>${byDay[day].map((e) => {
        const hour = e.at.slice(11, 16);
        if (e.kind === "content") {
          const it = e.item; const c = chan(it.channel);
          return `<li class="aev st-${esc(it.stage)}" data-id="${it.id}" tabindex="0"><time>${esc(hour)}</time><span class="aev-ic">${esc(c.icon)}</span>
            <div class="grow"><b>${esc(it.title || strip(it.body).slice(0, 90))}</b><small class="muted">${esc(c.name)} · ${esc(aname(it.agent))}${project === "all" ? ` · ${esc(pinfo(it.project).name)}` : ""}</small></div>
            <span class="badge ${STAGE_BADGE[it.stage] || ""}">${esc(STAGE_LABEL[it.stage] || it.stage)}</span></li>`;
        }
        const r = e.routine;
        return `<li class="aev routine"><time>${esc(hour)}</time><span class="aev-ic">${icon("repeat")}</span>
          <div class="grow"><b>${esc(r.title)}</b><small class="muted">rotina · ${esc(aname(r.agent))}${project === "all" ? ` · ${esc(pinfo(r.project).name)}` : ""} · ${esc(r.schedule.toLowerCase())}</small></div>
          <span class="badge">${r.auto_approve ? "roda sozinha" : "pede aprovação"}</span></li>`;
      }).join("")}</ol></section>`;
    }).join("");
  }
  async function loadAgenda() {
    loaded.agenda = true;
    try { renderAgenda(await api(`/api/content/agenda?project=${encodeURIComponent(project)}&days=${aDays}`)); }
    catch (e) { $("#agenda").innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  }

  // ---------- saídas dos agentes ----------
  function renderOutputs() {
    const counts = {};
    for (const t of outputs) counts[t.agent] = (counts[t.agent] || 0) + 1;
    if (outAgent && !counts[outAgent]) outAgent = "";
    const chips = [["", "Todo o time", outputs.length]].concat(Object.values(agents).filter((a) => counts[a.slug]).map((a) => [a.slug, `${a.icon} ${a.name}`, counts[a.slug]]));
    $("#out-agents").innerHTML = outputs.length ? chips.map(([s, l, n]) => `<button type="button" class="fchip ${s === outAgent ? "active" : ""}" data-oagent="${esc(s)}">${esc(l)} <span>${n}</span></button>`).join("") : "";
    const list = outputs.filter((t) => !outAgent || t.agent === outAgent);
    $("#outputs").innerHTML = list.map((t) => {
      const res = strip(t.result);
      return `<article class="out card s-${esc(t.status)}">
        <header><span class="kcard-agent">${esc(aname(t.agent))}</span>${project === "all" ? `<span class="muted">${esc(pinfo(t.project).icon)} ${esc(pinfo(t.project).name)}</span>` : ""}<span class="grow"></span><span class="badge t-${esc(t.status)}">${esc(TASK_STATUS[t.status] || t.status)}</span><span class="muted">${esc(ago(t.finished_at || t.created_at))}</span></header>
        <h2>${esc(t.title)}</h2>
        ${!res ? '<p class="muted">Sem resumo.</p>' : res.length <= 280 ? `<p class="pre">${esc(res)}</p>`
          : `<details><summary>${esc(res.slice(0, 220))}…</summary><p class="pre">${esc(res.slice(0, 6000))}</p></details>`}
        <div class="out-foot">${t.content.map((c) => `<button type="button" class="tag link" data-piece="${c.id}">${esc(chan(c.channel).icon)} ${esc(c.title || chan(c.channel).name)} · ${esc(STAGE_LABEL[c.stage] || c.stage)}</button>`).join("")}
          ${t.conversation_id ? `<a class="btn small ghost" href="/?c=${t.conversation_id}">Ver thread ${icon("arrow")}</a>` : ""}</div>
      </article>`;
    }).join("") || `<div class="empty-card card"><h2>Nenhuma entrega nos últimos ${oDays} dias</h2><p class="muted">Quando os agentes concluírem tarefas aprovadas, o resultado aparece aqui.</p></div>`;
  }
  async function loadOutputs() {
    loaded.saidas = true;
    try { outputs = (await api(`/api/content/outputs?project=${encodeURIComponent(project)}&days=${oDays}`)).outputs; renderOutputs(); }
    catch (e) { $("#outputs").innerHTML = `<div class="alert">${esc(e.message)}</div>`; }
  }

  // ---------- eventos ----------
  // Publicar pelo OS: e-mail (SMTP, pede 2FA) ou rede social (Postiz, escolhe o canal).
  async function publish(id) {
    const it = items.find((x) => x.id === id); if (!it) return;
    const body = {};
    try {
      if (it.group === "email") {
        const t = prompt(`Enviar este e-mail para ${it.recipient || "?"} agora?\nDigite o código 2FA para confirmar:`, "");
        if (!t) return; body.totp = t;
      } else {
        const list = await api(`/api/gprojects/${it.project}/postiz/canais`);
        if (!list.length) return alert("Nenhum canal conectado no Postiz deste projeto.");
        let pick = list[0];
        if (list.length > 1) {
          const n = prompt("Em qual canal publicar?\n" + list.map((c, i) => `${i + 1}) ${c.nome} (${c.rede})`).join("\n"), "1");
          pick = list[Number(n) - 1]; if (!pick) return;
        } else if (!confirm(`Publicar agora em ${pick.nome} (${pick.rede})?`)) return;
        body.canal_id = pick.id;
      }
      await api(`/api/content/${id}/publish`, { method: "POST", body: JSON.stringify(body) });
    } catch (e) { alert(e.message); }
    await loadPieces(); sw?.reload?.();
  }
  const findPiece = async (id) => {
    let it = items.find((x) => x.id === id);
    if (!it) { await loadPieces(); it = items.find((x) => x.id === id); }
    return it;
  };
  document.addEventListener("click", async (ev) => {
    const a = ev.target.closest("[data-approve]"); if (a) return decide(Number(a.dataset.approve), true);
    const r = ev.target.closest("[data-reject]");
    if (r) { const note = prompt("Motivo (opcional):", ""); if (note !== null) decide(Number(r.dataset.reject), false, note); return; }
    const pb = ev.target.closest("[data-publish]"); if (pb) return publish(Number(pb.dataset.publish));
    const s = ev.target.closest("[data-sent]"); if (s) { const it = items.find((x) => x.id === Number(s.dataset.sent)); if (it) { openPiece(it); $("#cf-actions [data-cf=sent]")?.click(); } return; }
    const st = ev.target.closest("[data-stage]"); if (st) { stageF = st.dataset.stage; return renderPieces(); }
    const g = ev.target.closest("[data-group]"); if (g) { groupF = g.dataset.group; return renderPieces(); }
    const o = ev.target.closest("[data-oagent]"); if (o) { outAgent = o.dataset.oagent; return renderOutputs(); }
    const ad = ev.target.closest("[data-adays]");
    if (ad) { aDays = Number(ad.dataset.adays); document.querySelectorAll("#agenda-days button").forEach((b) => b.classList.toggle("on", b === ad)); return loadAgenda(); }
    const od = ev.target.closest("[data-odays]");
    if (od) { oDays = Number(od.dataset.odays); document.querySelectorAll("#out-days button").forEach((b) => b.classList.toggle("on", b === od)); return loadOutputs(); }
    const pc = ev.target.closest("[data-piece]"); if (pc) { const it = await findPiece(Number(pc.dataset.piece)); if (it) openPiece(it); return; }
    if (ev.target.closest("a, button, summary, dialog")) return;
    const card = ev.target.closest(".cpiece, .aev[data-id]"); if (card) { const it = await findPiece(Number(card.dataset.id)); if (it) openPiece(it); }
  });
  document.addEventListener("keydown", async (ev) => {
    if (ev.key !== "Enter") return;
    const card = ev.target.closest?.(".cpiece, .aev[data-id]"); if (card) { const it = await findPiece(Number(card.dataset.id)); if (it) openPiece(it); }
  });

  (async () => {
    showTab(activeTab());
    const [cat, chs] = await Promise.all([api("/api/agents"), api("/api/content/channels")]);
    for (const a of cat) agents[a.slug] = a;
    channels = chs;
    sw = await initProjects((slug, _info, list) => {
      project = slug || "all"; projects = list; groupF = ""; outAgent = "";
      loaded.agenda = false; loaded.saidas = false;
      loadPieces();
      if (activeTab() === "agenda") loadAgenda();
      if (activeTab() === "saidas") loadOutputs();
    }, { allowAll: true });
  })();
})();
