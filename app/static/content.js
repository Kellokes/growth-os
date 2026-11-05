// Central de conteúdo: peças (posts, e-mails...), agenda e saídas dos agentes, por projeto (ou todos).
(() => {
  const { $, esc, api, icon, initProjects } = window.GOS;
  const TABS = ["pecas", "agenda", "saidas", "ativos"];
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
  const loaded = { agenda: false, saidas: false, ativos: false };

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
    if (name === "ativos" && !loaded.ativos) loadAssets();
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
      ${it.image_url ? `<img class="cimg" src="${esc(it.image_url)}" alt="imagem da peça" loading="lazy">` : it.needs_image ? `<div class="cmedia cneeds">🖼 Sem imagem${it.image_error ? ` · ${esc(it.image_error)}` : " · aguardando layout ou geração"}</div>` : ""}
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
    $("#cf-body").value = it?.body || ""; $("#cf-media").value = it?.media || ""; $("#cf-format").value = it?.visual_format;
    $("#cf-mode").value = it?.image_mode || "template"; $("#cf-iprompt").value = it?.image_prompt || ""; initVisual(it);
    $("#cf-when").value = it?.scheduled_at || "";
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
    renderActions(); renderImage();
    $("#content-dialog").showModal();
  }

  // ---------- visual da peça: layout (templates do projeto), mídia que entra nele e texto ----------
  const layoutCache = {};
  async function projectLayouts(slug) {
    if (!slug) return { templates: [], references: [] };
    try { layoutCache[slug] ||= await api(`/api/layouts?project=${encodeURIComponent(slug)}`); } catch (e) { return { templates: [], references: [] }; }
    return layoutCache[slug];
  }
  const MODE_HINT = {
    template: "A foto ou o vídeo entram no layout escolhido e o texto vai por cima: todas as peças saem com o mesmo desenho.",
    reference: "Sem layout: o modelo cria a imagem inteira a partir da referência escolhida, sem texto escrito nela (1 imagem paga).",
    prompt: "Sem layout e sem referência: a imagem sai só do prompt abaixo (o agente propõe um; você pode editar). 1 imagem paga.",
  };
  const SOURCES = [
    ["gerar_imagem", "Gerar imagem (IA)", "A IA de imagem cria o cenário dentro da arte do layout. Cobra 1 imagem do OpenRouter."],
    ["gerar_video", "Gerar vídeo (IA)", "A IA de vídeo (Modelos › Vídeos) gera o clipe; leva alguns minutos e a imagem aparece sozinha quando ficar pronto. Cobra 1 vídeo do OpenRouter."],
    ["upload", "Subir arquivo", "Foto (PNG, JPEG, WebP, até 8 MB) ou vídeo (MP4/MOV, até 25 MB) que você já tem. Se o layout não tiver espaço para foto, a IA encaixa a foto na arte (1 imagem)."],
    ["link", "Link da internet", "Endereço direto do arquivo (termina em .jpg, .png, .mp4…). O sistema baixa uma vez e guarda. Página de post do Instagram não serve: copie o endereço da mídia."],
    ["nenhuma", "Só o layout", "Sem foto nem vídeo: a arte do layout com o texto. Sem custo de IA."],
  ];
  const ROLES = [["capa", "Capa"], ["pagina", "Páginas"], ["cta", "CTA final"]];
  let src = "gerar_imagem"; let pages = []; let tplSel = {};

  function initVisual(it) {
    src = it?.media_source || "gerar_imagem";
    const slides = it?.slides || [];
    pages = slides.filter((s) => s.role === "pagina").map((s) => ({ title: s.title, text: s.text }));
    const cta = slides.find((s) => s.role === "cta");
    $("#cf-cta-title").value = cta ? cta.title : ""; $("#cf-cta-text").value = cta ? cta.text : "";
    $("#cf-imgtitle").value = it?.img_title || ""; $("#cf-imgtext-in").value = it?.img_text || "";
    $("#cf-url").value = it?.media_url || "";
    assetSel = [...(it?.asset_ids || [])];
    tplSel = it?.visual_format === "carrossel" ? { ...(it?.template_ids || {}) } : { "": it?.template_id || 0 };
    delete $("#cf-ref").dataset.sel;
  }

  function drawPages() {
    $("#cf-pages").innerHTML = pages.map((p, i) => `<div class="cf-page" data-page="${i}">
      <label>Página ${i + 2} · título<input data-pf="title" maxlength="120" value="${esc(p.title)}"></label>
      <label>Texto<input data-pf="text" maxlength="300" value="${esc(p.text)}"></label>
      <button class="btn small ghost" type="button" data-pdel="${i}" aria-label="Tirar página">✕</button></div>`).join("")
      || `<small class="muted">Sem páginas definidas: cada parágrafo do texto vira uma página.</small>`;
    $("#cf-add-page").hidden = pages.length >= 8;
  }
  $("#cf-pages").addEventListener("input", (ev) => {
    const row = ev.target.closest("[data-page]"); if (!row || !ev.target.dataset.pf) return;
    pages[Number(row.dataset.page)][ev.target.dataset.pf] = ev.target.value;
  });
  $("#cf-pages").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-pdel]"); if (!b) return;
    pages.splice(Number(b.dataset.pdel), 1); drawPages();
  });
  $("#cf-add-page").onclick = () => { pages.push({ title: "", text: "" }); drawPages(); };

  async function renderMode() {
    const fmt = $("#cf-format").value; const mode = $("#cf-mode").value;
    $("#cf-visual").hidden = !fmt;
    if (!fmt) return;
    const tplMode = mode === "template";
    const carousel = fmt === "carrossel";
    const slug = current ? current.project : $("#cf-project").value;
    const lay = await projectLayouts(slug);
    const ok = (lay.templates || []).filter((t) => t.format === fmt && ["approved", "proposed"].includes(t.status));
    limitTpls = ok;
    const roles = carousel ? ROLES : [["", "Template"]];
    $("#cf-tpls").innerHTML = roles.map(([r, label]) => {
      const rows = ok.filter((t) => !carousel || (t.role || "capa") === r);
      const def = rows.find((t) => t.is_default) || rows.find((t) => t.status === "approved");
      const sel = String(tplSel[r] || "");
      const fallback = carousel && r !== "capa" && !rows.length ? "o da capa" : def ? (def.name || `#${def.id}`) : "nenhum ainda";
      return `<label>${esc(label)}<select data-trole="${esc(r)}" ${ro() ? "disabled" : ""}><option value="">Padrão: ${esc(fallback)}</option>${rows.map((t) => `<option value="${t.id}" ${String(t.id) === sel ? "selected" : ""}>${esc(t.name || `Template #${t.id}`)}${t.status === "approved" ? "" : " · proposta"}</option>`).join("")}</select></label>`;
    }).join("") + (ok.length ? "" : `<small class="muted">Este projeto ainda não tem template de ${esc(fmt)}: crie em <a href="/layouts">Layouts</a>.</small>`);
    $("#cf-tpls").hidden = !tplMode;
    const rsel = $("#cf-ref").dataset.sel ?? String(current?.ref_id || "");
    const refs = lay.references || [];
    $("#cf-ref").innerHTML = `<option value="">Todas as referências do projeto</option>` + refs.map((r) => `<option value="${r.id}">#${r.id}${r.note ? " · " + esc(r.note) : ""}</option>`).join("");
    $("#cf-ref").value = refs.some((r) => String(r.id) === rsel) ? rsel : "";
    $("#cf-ref-wrap").hidden = mode !== "reference";
    $("#cf-iprompt-wrap").hidden = tplMode;
    $("#cf-modehint").textContent = MODE_HINT[mode];
    $("#cf-src-step").hidden = !tplMode; $("#cf-imgtext").hidden = !tplMode;
    drawLimits();
    $("#cf-src").innerHTML = SOURCES.map(([k, label]) => `<button type="button" data-src="${k}" class="${k === src ? "on" : ""}" aria-pressed="${k === src}" ${ro() ? "disabled" : ""}>${esc(label)}</button>`).join("");
    $("#cf-srchint").textContent = (SOURCES.find((x) => x[0] === src) || SOURCES[0])[2];
    $("#cf-url-wrap").hidden = src !== "link";
    $("#cf-carousel").hidden = !carousel;
    $("#cf-imgtitle-label").textContent = carousel ? "Capa · título" : "Título na imagem";
    if (carousel) drawPages();
    renderPieceAssets();
  }
  const ro = () => !!current && ["sent", "rejected"].includes(current.status);
  $("#cf-tpls").addEventListener("change", (ev) => { const s = ev.target.closest("[data-trole]"); if (s) { tplSel[s.dataset.trole] = Number(s.value) || 0; drawLimits(); } });

  // Limite de caracteres das caixas do template da capa (feito no editor de Layouts): mostra quanto cabe e se vai cortar.
  let limitTpls = [];
  function drawLimits() {
    const fmt = $("#cf-format").value; const el = $("#cf-limits");
    const role = fmt === "carrossel" ? "capa" : "";
    const rows = limitTpls.filter((t) => t.format === fmt && (fmt !== "carrossel" || (t.role || "capa") === "capa"));
    const t = rows.find((x) => x.id === tplSel[role]) || rows.find((x) => x.is_default) || rows.find((x) => x.status === "approved");
    const b = t && t.boxes && (t.boxes.titulo || t.boxes.texto) ? t.boxes : null;
    el.hidden = !b; if (!b) return;
    const nt = $("#cf-imgtitle").value.trim().length; const ns = $("#cf-imgtext-in").value.trim().length;
    const over = (b.titulo && nt > b.titulo.max) || (b.texto && ns > b.texto.max);
    const parts = [b.titulo ? `título ${nt}/${b.titulo.max}` : "sem caixa de título", b.texto ? `frase ${ns}/${b.texto.max}` : "sem caixa de frase"];
    el.textContent = `Cabe na arte: ${parts.join(" · ")} caracteres${over ? ". Acima disso o texto é cortado no fim da palavra." : ""}`;
    el.classList.toggle("warn-text", over);
  }
  $("#cf-imgtitle").addEventListener("input", drawLimits);
  $("#cf-imgtext-in").addEventListener("input", drawLimits);
  $("#cf-src").addEventListener("click", (ev) => { const b = ev.target.closest("[data-src]"); if (!b) return; src = b.dataset.src; renderMode(); renderImage(false); });

  const slidesBody = () => [...pages.filter((p) => p.title.trim() || p.text.trim()).map((p) => ({ role: "pagina", title: p.title, text: p.text })),
    { role: "cta", title: $("#cf-cta-title").value, text: $("#cf-cta-text").value }];
  function modeBody() {
    const fmt = $("#cf-format").value;
    const b = { image_mode: $("#cf-mode").value, ref_id: Number($("#cf-ref").value) || 0, image_prompt: $("#cf-iprompt").value,
      media_source: src, media_url: $("#cf-url").value.trim(), asset_ids: assetSel };
    if (fmt === "carrossel") {
      b.template_ids = Object.fromEntries(ROLES.map(([r]) => [r, tplSel[r] || 0]).filter(([, v]) => v));
      if (!pages.length && !current?.slides?.some((s) => s.role === "pagina") && !$("#cf-cta-title").value && !$("#cf-cta-text").value) { /* nada a salvar */ } else b.slides = slidesBody();
    } else b.template_id = tplSel[""] || 0;
    return b;
  }
  const same = (a, b) => JSON.stringify(a ?? "") === JSON.stringify(b ?? "");
  async function saveMode() {  // grava as escolhas antes de gerar, senão o botão usaria as antigas
    if (!current || ro()) return;
    const body = { ...modeBody(), img_title: $("#cf-imgtitle").value, img_text: $("#cf-imgtext-in").value };
    const diff = Object.fromEntries(Object.entries(body).filter(([k, v]) => {
      if (k === "slides") return !same(v.map((x) => [x.role, x.title, x.text]), (current.slides || []).map((x) => [x.role, x.title, x.text]));
      if (k === "template_ids") return !same(v, current.template_ids || {});
      if (k === "asset_ids") return !same(v, current.asset_ids || []);
      return (v || "") !== (current[k] || "");
    }));
    if (Object.keys(diff).length) current = await api(`/api/content/${current.id}`, { method: "PATCH", body: JSON.stringify(diff) });
  }
  $("#cf-mode").addEventListener("change", () => { renderMode(); renderImage(false); });
  $("#cf-ref").addEventListener("change", () => { $("#cf-ref").dataset.sel = $("#cf-ref").value; });
  $("#cf-project").addEventListener("change", () => { tplSel = {}; $("#cf-ref").dataset.sel = ""; renderMode(); });

  const GEN_LABEL = { gerar_imagem: "Gerar imagem", gerar_video: "Gerar vídeo com IA", upload: "Escolher foto ou vídeo", link: "Baixar o link e montar", nenhuma: "Montar no layout" };
  function renderImage(withMode = true) {
    const it = current; const box = $("#cf-image");
    if (withMode) renderMode();
    const can = it && ["pending", "approved", "failed"].includes(it.status) && $("#cf-format").value && it.visual_format;
    const tplMode = $("#cf-mode").value === "template";
    const action = tplMode ? src : "gerar_imagem";
    const slides = (it?.slides || []).filter((s) => s.image_url);
    const again = it?.image_url && action !== "upload" ? "Refazer" : GEN_LABEL[action];
    box.innerHTML = `${it?.image_url ? `<img class="cimg big" src="${esc(it.image_url)}" alt="imagem da peça (capa)">` : ""}
      ${slides.length && it.visual_format === "carrossel" ? `<div class="cf-strip">${slides.map((s, i) => `<figure><img src="${esc(s.image_url)}" alt="${s.role === "cta" ? "CTA final" : `página ${i + 2}`}" loading="lazy"><figcaption>${s.role === "cta" ? "CTA final" : `Página ${i + 2}`}</figcaption></figure>`).join("")}</div>` : ""}
      ${it?.media_file_url && tplMode && ["upload", "link", "gerar_video"].includes(src) ? `<small class="muted">Mídia da peça: <a href="${esc(it.media_file_url)}" target="_blank" rel="noopener">abrir</a> · <button class="btn small ghost" type="button" data-img="rmmedia">tirar</button></small>` : ""}
      ${it?.media_generating ? `<div class="secret-note">Gerando o vídeo com a IA… a imagem aparece aqui sozinha quando ficar pronto (alguns minutos).</div>` : ""}
      ${it?.image_error ? `<div class="alert">${esc(it.image_error)}</div>` : ""}
      ${!it && $("#cf-format").value ? `<small class="muted">Salve a peça para gerar ou subir a mídia.</small>` : ""}
      ${can ? `<div class="row"><button class="btn small primary" type="button" data-img="gen" ${it.media_generating ? "disabled" : ""}>${esc(again)}</button>
        ${action === "upload" && it.media_file_url ? '<button class="btn small" type="button" data-img="compose">Montar de novo</button>' : ""}
        <button class="btn small ghost" type="button" data-img="up" title="Substitui tudo: a imagem final vai como está">Subir imagem final pronta</button></div>` : ""}
      ${it?.video_url ? `<video class="cimg big" src="${esc(it.video_url)}" controls playsinline preload="metadata"></video>` : ""}
      ${can && it.can_video ? `<div class="row"><button class="btn small" type="button" data-vid="gen">${it.video_url ? "Montar o vídeo de novo" : "Montar vídeo animado"}</button>
        ${it.video_url ? '<button class="btn small ghost" type="button" data-vid="del">Apagar vídeo</button>' : ""}</div>
        <small class="muted">Anima o layout com o texto e a mídia desta peça (com vídeo de fonte, ele roda dentro do layout). Sem custo de IA.</small>` : ""}`;
  }
  $("#cf-format").onchange = () => { tplSel = {}; renderImage(); };
  const refresh = async () => { await loadPieces(); renderImage(); renderActions(); };
  $("#cf-image").addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-img]"); if (!b || !current) return;
    const what = b.dataset.img;
    if (what === "up") return $("#cf-image-file").click();
    try {
      if (what === "rmmedia") { current = await api(`/api/content/${current.id}/media`, { method: "DELETE" }); return refresh(); }
      await saveMode();
      const tplMode = $("#cf-mode").value === "template";
      if (what === "gen" && tplMode && src === "upload") return $("#cf-media-file").click();
      b.disabled = true; b.textContent = src === "gerar_video" && tplMode ? "Pedindo o vídeo…" : "Montando… (até 1 min)";
      if (what === "gen" && tplMode && src === "link") {
        const url = $("#cf-url").value.trim(); if (!url) { alert("Cole o link do arquivo."); return refresh(); }
        current = await api(`/api/content/${current.id}/media/link`, { method: "POST", body: JSON.stringify({ url }) });
      } else current = await api(`/api/content/${current.id}/image`, { method: "POST", body: JSON.stringify({}) });
    } catch (e) { alert(e.message); }
    refresh();
  });
  $("#cf-image").addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-vid]"); if (!b || !current) return;
    if (b.dataset.vid === "del" && !confirm("Apagar o vídeo desta peça?")) return;
    b.disabled = true; if (b.dataset.vid === "gen") b.textContent = "Renderizando… (até 2 min)";
    try { current = await api(`/api/content/${current.id}/video`, { method: b.dataset.vid === "del" ? "DELETE" : "POST" }); }
    catch (e) { alert(e.message); }
    refresh();
  });
  const readFile = (f) => new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result)); r.readAsDataURL(f); });
  $("#cf-image-file").onchange = async (ev) => {
    const f = ev.target.files[0]; ev.target.value = ""; if (!f || !current) return;
    try { current = await api(`/api/content/${current.id}/image/upload`, { method: "POST", body: JSON.stringify({ data: await readFile(f) }) }); }
    catch (e) { alert(e.message); }
    refresh();
  };
  $("#cf-media-file").onchange = async (ev) => {
    const f = ev.target.files[0]; ev.target.value = ""; if (!f || !current) return;
    const box = $("#cf-image"); box.insertAdjacentHTML("afterbegin", `<small class="muted">Enviando ${esc(f.name)} e montando no layout…</small>`);
    try { current = await api(`/api/content/${current.id}/media/upload`, { method: "POST", body: JSON.stringify({ data: await readFile(f) }) }); }
    catch (e) { alert(e.message); }
    refresh();
  };

  // ---------- ativos do projeto (biblioteca usada nas artes) ----------
  let assets = []; let assetCats = []; let asKind = "img"; let asFilter = ""; let asEdit = 0;
  const assetCache = {};
  async function projectAssets(slug, fresh = false) {
    if (!slug || slug === "all") return [];
    if (fresh || !assetCache[slug]) {
      try { const r = await api(`/api/assets?project=${encodeURIComponent(slug)}`); assetCache[slug] = r.assets; assetCats = r.categories; }
      catch (e) { return []; }
    }
    return assetCache[slug];
  }
  async function loadAssets() {
    const single = project && project !== "all";
    $("#as-pick").hidden = single; $("#as-body").hidden = !single;
    if (!single) return;
    assets = await projectAssets(project, true);
    loaded.ativos = true;
    renderAssets();
  }
  function renderAssets() {
    $("#as-cat").innerHTML = assetCats.filter((c) => c.key !== "video" && c.key !== "texto").map((c) => `<option value="${esc(c.key)}">${esc(c.name)}</option>`).join("");
    document.querySelectorAll("#as-kind [data-ak]").forEach((b) => b.classList.toggle("on", b.dataset.ak === asKind));
    const link = asKind === "link"; const txt = asKind === "text"; const img = !link && !txt;
    $("#as-cat-wrap").hidden = !img; $("#as-link-wrap").hidden = !link; $("#as-always-wrap").hidden = !img; $("#as-text-wrap").hidden = !txt;
    $("#as-file-btn").hidden = !img; $("#as-add-link").hidden = img;
    const counts = {};
    for (const a of assets) counts[a.category] = (counts[a.category] || 0) + 1;
    if (asFilter && !counts[asFilter]) asFilter = "";
    const chips = [["", "Todos", assets.length], ...assetCats.filter((c) => counts[c.key]).map((c) => [c.key, c.name, counts[c.key]])];
    $("#as-filter").innerHTML = chips.length > 2 ? chips.map(([k, l, n]) => `<button type="button" class="fchip ${k === asFilter ? "active" : ""}" data-afilter="${esc(k)}">${esc(l)} <span>${n}</span></button>`).join("") : "";
    const list = assets.filter((a) => !asFilter || a.category === asFilter);
    $("#as-list").innerHTML = list.map((a) => a.id === asEdit ? `
      <div class="as-card" data-aid="${a.id}">
        <label>Nome<input data-ae="name" maxlength="80" value="${esc(a.name)}"></label>
        ${a.is_text ? `<label>Texto<textarea data-ae="text" rows="5" maxlength="4000">${esc(a.text)}</textarea></label>`
          : a.is_link ? `<label>Link<input data-ae="link" maxlength="1000" value="${esc(a.link)}"></label>`
          : `<label>Tipo<select data-ae="category">${assetCats.filter((c) => c.key !== "video" && c.key !== "texto").map((c) => `<option value="${esc(c.key)}" ${c.key === a.category ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>`}
        <label>O que é<textarea data-ae="note" rows="2" maxlength="500">${esc(a.note)}</textarea></label>
        <label>Quando usar<textarea data-ae="use_when" rows="2" maxlength="500">${esc(a.use_when)}</textarea></label>
        ${a.is_link || a.is_text ? "" : `<label class="check"><input type="checkbox" data-ae="always" ${a.always ? "checked" : ""}> Vai em toda arte</label>`}
        <div class="row"><button class="btn small primary" type="button" data-asave="${a.id}">Salvar</button><button class="btn small ghost" type="button" data-acancel="1">Cancelar</button></div>
      </div>` : `
      <figure class="as-card" data-aid="${a.id}">
        ${a.is_text ? `<p class="as-txt">${esc(a.text.slice(0, 160))}${a.text.length > 160 ? "…" : ""}</p>` : a.is_link ? `<a class="as-vid" href="${esc(a.link)}" target="_blank" rel="noopener noreferrer" title="abrir o vídeo">▶</a>` : `<img src="${esc(a.url)}" alt="${esc(a.name)}" loading="lazy">`}
        <b>${esc(a.name || "sem nome")}</b>
        <small>${esc(a.category_name)}${a.always ? " · vai em toda arte" : ""}</small>
        ${a.note ? `<small>${esc(a.note)}</small>` : ""}
        ${a.use_when ? `<small><b>Quando usar:</b> ${esc(a.use_when)}</small>` : ""}
        <div class="row"><button class="btn small ghost" type="button" data-aedit="${a.id}">Editar</button><button class="btn small danger" type="button" data-adel="${a.id}">Apagar</button></div>
      </figure>`).join("") || `<div class="empty-card card tile-empty"><h2>Nenhum ativo ainda</h2><p class="muted">Suba a logo, as cartas, os objetos e os links de vídeo do projeto.</p></div>`;
  }
  $("#as-kind").addEventListener("click", (ev) => { const b = ev.target.closest("[data-ak]"); if (b) { asKind = b.dataset.ak; renderAssets(); } });
  $("#as-filter").addEventListener("click", (ev) => { const b = ev.target.closest("[data-afilter]"); if (b) { asFilter = b.dataset.afilter; renderAssets(); } });
  const assetForm = () => ({ project, name: $("#as-name").value, category: $("#as-cat").value, note: $("#as-note").value,
    use_when: $("#as-when").value, always: $("#as-always").checked });
  const clearAssetForm = () => { ["#as-name", "#as-note", "#as-when", "#as-link", "#as-text"].forEach((s) => { $(s).value = ""; }); $("#as-always").checked = false; };
  $("#as-file").onchange = async (ev) => {
    const files = [...ev.target.files]; ev.target.value = ""; const msg = $("#as-msg");
    for (const f of files) {
      msg.textContent = `Enviando ${f.name}…`;
      const body = { ...assetForm(), data: await readFile(f) };
      if (!body.name || files.length > 1) body.name = f.name.replace(/\.[^.]+$/, "");
      try { await api("/api/assets", { method: "POST", body: JSON.stringify(body) }); } catch (e) { alert(`${f.name}: ${e.message}`); }
    }
    msg.textContent = files.length ? "Salvo." : ""; clearAssetForm(); loadAssets();
  };
  $("#as-add-link").onclick = async () => {
    const extra = asKind === "text" ? { category: "texto", text: $("#as-text").value } : { category: "video", link: $("#as-link").value };
    try { await api("/api/assets", { method: "POST", body: JSON.stringify({ ...assetForm(), always: false, ...extra }) }); $("#as-msg").textContent = "Salvo."; clearAssetForm(); }
    catch (e) { alert(e.message); }
    loadAssets();
  };
  $("#as-list").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    if (b.dataset.aedit) { asEdit = Number(b.dataset.aedit); return renderAssets(); }
    if (b.dataset.acancel) { asEdit = 0; return renderAssets(); }
    if (b.dataset.adel) {
      if (!confirm("Apagar este ativo? As peças que o usavam passam a usar o automático.")) return;
      try { await api(`/api/layouts/assets/${b.dataset.adel}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
      return loadAssets();
    }
    if (b.dataset.asave) {
      const card = b.closest("[data-aid]"); const body = {};
      card.querySelectorAll("[data-ae]").forEach((el) => { body[el.dataset.ae] = el.type === "checkbox" ? el.checked : el.value; });
      try { await api(`/api/assets/${b.dataset.asave}`, { method: "PATCH", body: JSON.stringify(body) }); asEdit = 0; } catch (e) { alert(e.message); }
      loadAssets();
    }
  });

  // ativos na arte de uma peça: escolhidos, ou automático ("toda arte" + os que combinam com o assunto)
  let assetSel = [];
  async function renderPieceAssets() {
    const slug = current ? current.project : $("#cf-project").value;
    const list = await projectAssets(slug);
    const imgs = list.filter((a) => !a.is_link && !a.is_text); const vids = list.filter((a) => a.is_link);
    $("#cf-assets-step").hidden = $("#cf-visual").hidden || (!imgs.length && !vids.length);
    $("#cf-assets").innerHTML = imgs.map((a) => {
      const on = assetSel.includes(a.id); const auto = !assetSel.length && a.always;
      return `<button type="button" data-asset="${a.id}" class="${on ? "on" : ""} ${auto ? "auto" : ""}" aria-pressed="${on}" ${ro() ? "disabled" : ""} title="${esc(a.note)}">${esc(a.name || `#${a.id}`)}</button>`;
    }).join("");
    $("#cf-assets-hint").textContent = !imgs.length ? "Este projeto ainda não tem ativos de imagem (aba Ativos)."
      : assetSel.length ? `${assetSel.length} escolhido(s): vão como referência junto do prompt (até 5).`
        : "Nenhum escolhido: vão os marcados \"toda arte\" (tracejados) e os que combinam com o assunto da peça. Toque para escolher.";
    $("#cf-vidlib").innerHTML = `<option value="">Colar um link abaixo</option>` + vids.map((v) => `<option value="${esc(v.link)}" ${v.link === $("#cf-url").value ? "selected" : ""}>${esc(v.name || v.link)}</option>`).join("");
    $("#cf-vid-wrap").hidden = src !== "link" || !vids.length;
  }
  $("#cf-assets").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-asset]"); if (!b) return;
    const id = Number(b.dataset.asset);
    assetSel = assetSel.includes(id) ? assetSel.filter((x) => x !== id) : assetSel.length >= 5 ? assetSel : [...assetSel, id];
    renderPieceAssets();
  });
  // @ no prompt da imagem: lista os ativos do projeto; o servidor manda a imagem como referência e troca @Nome por "Nome (image N)"
  const mention = { open: false, items: [], at: -1, sel: 0 };
  function closeMention() { mention.open = false; $("#cf-mention").hidden = true; }
  async function updateMention() {
    const ta = $("#cf-iprompt"); const pos = ta.selectionStart;
    const m = /(^|[\s(])@([^\s@]*)$/.exec(ta.value.slice(0, pos));
    if (!m || ro()) return closeMention();
    const q = m[2].toLowerCase();
    const slug = current ? current.project : $("#cf-project").value;
    const list = (await projectAssets(slug)).filter((a) => a.name && a.name.toLowerCase().includes(q));
    if (!list.length) return closeMention();
    Object.assign(mention, { open: true, items: list.slice(0, 8), at: pos - m[2].length - 1, sel: 0 });
    const box = $("#cf-mention");
    box.innerHTML = mention.items.map((a, i) => `<button type="button" data-m="${i}" class="${i === 0 ? "on" : ""}">${a.is_text ? `<i class="m-ico">T</i>` : a.is_link ? `<i class="m-ico">▶</i>` : `<img src="${esc(a.url)}" alt="" loading="lazy">`}<span>${esc(a.name)}</span><small>${esc(a.category_name)}</small></button>`).join("");
    box.hidden = false;
  }
  function pickMention(i) {
    const a = mention.items[i]; if (!a) return;
    const ta = $("#cf-iprompt"); const pos = ta.selectionStart;
    const ins = `@${a.name} `;
    ta.value = ta.value.slice(0, mention.at) + ins + ta.value.slice(pos);
    ta.selectionStart = ta.selectionEnd = mention.at + ins.length;
    closeMention(); ta.focus();
  }
  $("#cf-iprompt").addEventListener("input", updateMention);
  $("#cf-iprompt").addEventListener("blur", () => setTimeout(closeMention, 150));
  $("#cf-iprompt").addEventListener("keydown", (ev) => {
    if (!mention.open) return;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault(); mention.sel = (mention.sel + (ev.key === "ArrowDown" ? 1 : -1) + mention.items.length) % mention.items.length;
      $("#cf-mention").querySelectorAll("button").forEach((b, i) => b.classList.toggle("on", i === mention.sel));
    } else if (ev.key === "Enter" || ev.key === "Tab") { ev.preventDefault(); pickMention(mention.sel); }
    else if (ev.key === "Escape") { ev.preventDefault(); closeMention(); }
  });
  $("#cf-mention").addEventListener("mousedown", (ev) => { const b = ev.target.closest("[data-m]"); if (b) { ev.preventDefault(); pickMention(Number(b.dataset.m)); } });
  $("#cf-vidlib").addEventListener("change", () => { if ($("#cf-vidlib").value) $("#cf-url").value = $("#cf-vidlib").value; });

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
    purpose: $("#cf-purpose").value, body: $("#cf-body").value, media: $("#cf-media").value, visual_format: $("#cf-format").value, ...modeBody(), img_title: $("#cf-imgtitle").value, img_text: $("#cf-imgtext-in").value, scheduled_at: $("#cf-when").value });
  // Os campos do formulário têm o mesmo nome que os do JSON da peça.
  const norm = (k, v) => (k === "slides" ? (v || []).map((x) => [x.role, x.title, x.text]) : v || (k === "template_ids" ? {} : k === "asset_ids" ? [] : ""));
  const changed = () => { const f = formBody(); return Object.keys(f).some((k) => !same(norm(k, f[k]), norm(k, current[k]))); };

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
      loaded.ativos = false; if (activeTab() === "ativos") loadAssets();
    }, { allowAll: true });
  })();
})();
