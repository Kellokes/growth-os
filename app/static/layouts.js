// Estúdio de layouts de UM projeto: formato (e papel, no carrossel) em abas; templates prontos, adicionar (subir ou gerar) e artes.
(() => {
  const { $, esc, api, initProjects } = window.GOS;
  const STATUS = { proposed: "proposta", approved: "aprovado", rejected: "rejeitado" };
  const HINT = {
    story: "Story 9:16: um template por post. Tenha quantos quiser e escolha em cada peça.",
    reel: "Reel 9:16: o vídeo ou a foto do post entra no layout, com o texto por cima.",
    carrossel: "O carrossel sempre roda em três layouts: a capa, as páginas do meio e o CTA final. Sem template de páginas ou de CTA, o da capa é usado.",
  };
  let project = null; let state = null; let busy = false;
  let fmt = "story"; let role = "capa";
  try { fmt = localStorage.getItem("lay-fmt") || "story"; role = localStorage.getItem("lay-role") || "capa"; } catch { /* sem storage */ }

  const fileToB64 = (f) => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result));
    r.onerror = () => rej(new Error("não consegui ler o arquivo"));
    r.readAsDataURL(f);
  });
  const cur = () => state.formats.find((f) => f.key === fmt) || state.formats[0];
  const curRole = () => (fmt === "carrossel" ? role : "");
  const ratioCls = () => `ratio-${cur().ratio.replace(":", "x")}`;
  const mine = (rows) => rows.filter((a) => a.format === fmt && (fmt !== "carrossel" || (a.role || "capa") === role));
  const partName = () => (fmt === "carrossel" ? `${cur().name} · ${cur().roles.find((r) => r.key === role)?.name || ""}` : cur().name);

  function draw() {
    if (!state) return;
    if (!state.formats.some((f) => f.key === fmt)) fmt = state.formats[0].key;
    $("#lay-brief").value = state.brief;
    $("#lay-model").textContent = state.image_model || "nenhum";
    $("#lay-style-sum").textContent = `${state.brief ? "briefing salvo" : "sem briefing"} · ${state.references.length} ativo(s) de imagem`;
    const warn = $("#lay-problem");
    warn.hidden = !state.problem; warn.textContent = state.problem || "";
    $("#lay-generate").disabled = busy || !!state.problem;
    $("#lay-fmt").innerHTML = state.formats.map((f) => `<button type="button" role="tab" data-fmt="${esc(f.key)}" class="${f.key === fmt ? "on" : ""}" aria-selected="${f.key === fmt}">${esc(f.name)} <small>${esc(f.ratio)}</small></button>`).join("");
    const roles = cur().roles.filter((r) => r.key);
    $("#lay-role").hidden = !roles.length;
    $("#lay-role").innerHTML = roles.map((r) => `<button type="button" role="tab" data-role="${esc(r.key)}" class="${r.key === role ? "on" : ""}" aria-selected="${r.key === role}">${esc(r.name)}</button>`).join("");
    $("#lay-hint").textContent = HINT[fmt] || "";
    $("#lay-tpl-title").textContent = `Templates · ${partName()}`;
    $("#lay-add-title").textContent = `Adicionar template · ${partName()}`;
    const style = state.references.filter((a) => a.always || ["logo", "estilo"].includes(a.category));
    $("#lay-ref-n").textContent = style.length;
    $("#lay-refs").innerHTML = style.slice(0, 6).map((a) => `<figure class="lay-thumb"><img src="${esc(a.url)}" alt="${esc(a.name || a.note || "referência")}" loading="lazy"><figcaption>${esc(a.name || a.note)}</figcaption></figure>`).join("");
    drawTemplates(); drawArts();
  }

  function drawTemplates() {
    const rows = mine(state.templates || []).sort((a, b) => (b.is_default - a.is_default) || (a.status === "approved" ? -1 : 1));
    $("#lay-templates").innerHTML = rows.length ? rows.map((t) => `
      <figure class="lay-thumb ${ratioCls()} ${t.is_default ? "official" : ""}">
        <img src="${esc(t.url)}" alt="template ${esc(t.name)}" loading="lazy">
        <figcaption><span class="lay-name">${esc(t.name || `Template #${t.id}`)}</span>${t.is_default ? '<span class="lay-badge def">padrão</span>' : `<span class="lay-badge">${STATUS[t.status] || t.status}</span>`}
          <br>${t.model === "texto-padrao" ? "texto padrão" : "texto pela IA"}${t.media_slot ? " · com espaço para foto/vídeo" : ""}</figcaption>
        <div class="row">
          ${t.status !== "approved" ? `<button class="btn small primary" type="button" data-ok="${t.id}">Aprovar</button>` : ""}
          ${t.status === "approved" && !t.is_default ? `<button class="btn small" type="button" data-def="${t.id}">Tornar padrão</button>` : ""}
          ${t.status === "proposed" ? `<button class="btn small" type="button" data-no="${t.id}">Rejeitar</button>` : ""}
          <button class="btn small ghost" type="button" data-ren="${t.id}">Renomear</button>
          ${fmt === "carrossel" ? `<button class="btn small ghost" type="button" data-move="${t.id}">Mover</button>` : ""}
          <button class="btn small danger" type="button" data-del="${t.id}">Apagar</button></div>
      </figure>`).join("") : `<div class="muted">Nenhum template de ${esc(partName())} ainda. Suba uma arte ou gere com a IA logo abaixo.</div>`;
  }

  function drawArts() {
    const rows = mine(state.layouts || []);
    const pos = { embaixo: "embaixo", meio: "no meio", topo: "em cima" };
    $("#lay-arts").innerHTML = rows.length ? rows.map((l) => `
      <figure class="lay-thumb ${ratioCls()}">
        <img src="${esc(l.url)}" alt="arte ${esc(l.name)}" loading="lazy">
        <figcaption><span class="lay-name">${esc(l.name || `Arte #${l.id}`)}</span><span class="lay-badge">${l.uploaded ? "sua" : "IA"} · ${STATUS[l.status] || l.status}</span>${l.note ? `<br>${esc(l.note)}` : ""}</figcaption>
        <div class="row">${l.status === "approved"
          ? `<button class="btn small primary" type="button" data-html="${l.id}">Texto com IA</button>
             <select class="small" data-simple-pos="${l.id}" aria-label="Onde vai o texto">${Object.entries(pos).map(([k, v]) => `<option value="${k}">texto ${v}</option>`).join("")}</select>
             <button class="btn small" type="button" data-simple="${l.id}">Texto padrão</button>`
          : `<button class="btn small primary" type="button" data-ok="${l.id}">Aprovar</button>`}
          ${l.status === "proposed" ? `<button class="btn small" type="button" data-no="${l.id}">Rejeitar</button>` : ""}
          <button class="btn small danger" type="button" data-del="${l.id}">Apagar</button></div>
      </figure>`).join("") : `<div class="muted">Nenhuma arte de ${esc(partName())} ainda.</div>`;
  }

  async function load() {
    if (!project) return;
    try { state = await api(`/api/layouts?project=${encodeURIComponent(project)}`); draw(); } catch (e) { alert(e.message); }
  }
  const act = async (fn) => { try { await fn(); await load(); } catch (e) { alert(e.message); } };
  const remember = () => { try { localStorage.setItem("lay-fmt", fmt); localStorage.setItem("lay-role", role); } catch { /* sem storage */ } };

  document.addEventListener("click", (ev) => {
    const tab = ev.target.closest("[data-fmt],[data-role]");
    if (tab) { if (tab.dataset.fmt) fmt = tab.dataset.fmt; else role = tab.dataset.role; remember(); draw(); return; }
    const b = ev.target.closest("button[data-ok],button[data-no],button[data-del],button[data-html],button[data-simple],button[data-def],button[data-ren],button[data-move]");
    if (!b) return;
    const d = b.dataset;
    if (d.html) {
      const feedback = prompt("Ajustes para o texto do template (opcional):", "") ?? null;
      if (feedback === null) return;
      b.disabled = true; b.textContent = "Montando… (até 1 min)";
      act(() => api(`/api/layouts/${d.html}/template`, { method: "POST", body: JSON.stringify({ feedback }) }));
    } else if (d.simple) {
      const position = document.querySelector(`[data-simple-pos="${d.simple}"]`)?.value || "embaixo";
      b.disabled = true; b.textContent = "Montando…";
      act(() => api(`/api/layouts/${d.simple}/template`, { method: "POST", body: JSON.stringify({ simple: true, position }) }));
    } else if (d.def) act(() => api(`/api/layouts/${d.def}/default`, { method: "POST" }));
    else if (d.ren) {
      const t = (state.templates || []).find((x) => String(x.id) === d.ren);
      const name = prompt("Nome do template:", t?.name || ""); if (name === null) return;
      act(() => api(`/api/layouts/${d.ren}`, { method: "PATCH", body: JSON.stringify({ name }) }));
    } else if (d.move) {
      const to = prompt("Mover para qual parte do carrossel? capa, pagina ou cta", role); if (!to) return;
      act(() => api(`/api/layouts/${d.move}`, { method: "PATCH", body: JSON.stringify({ role: to }) }));
    } else if (d.ok) act(() => api(`/api/layouts/${d.ok}/decide`, { method: "POST", body: JSON.stringify({ approve: true }) }));
    else if (d.no) act(() => api(`/api/layouts/${d.no}/decide`, { method: "POST", body: JSON.stringify({ approve: false }) }));
    else if (confirm("Apagar esta imagem?")) act(() => api(`/api/layouts/assets/${d.del}`, { method: "DELETE" }));
  });

  $("#lay-brief-save").onclick = () => act(async () => {
    await api("/api/layouts/brief", { method: "PUT", body: JSON.stringify({ project, brief: $("#lay-brief").value }) });
    $("#lay-brief-msg").textContent = "Salvo.";
  });

  $("#up-file").onchange = async (ev) => {
    const f = ev.target.files[0]; ev.target.value = ""; if (!f) return;
    const msg = $("#up-msg");
    const text = $("#up-text").value;
    msg.textContent = text === "ia" ? "Enviando e montando o texto com a IA… (até 1 min)" : "Enviando…";
    try {
      const r = await api("/api/layouts/upload", { method: "POST", body: JSON.stringify({
        project, format: fmt, role: curRole(), name: $("#up-name").value || f.name.replace(/\.[^.]+$/, ""), data: await fileToB64(f), text }) });
      msg.textContent = r.error ? `A arte entrou, mas o template falhou: ${r.error}` : text === "ia" ? "Pronto: aprove o template abaixo." : "Pronto: o template já pode ser usado.";
      $("#up-name").value = "";
    } catch (e) { msg.textContent = e.message; }
    await load();
  };

  $("#lay-generate").onclick = async () => {
    if (busy) return;
    busy = true; $("#lay-generate").disabled = true;
    const msg = $("#lay-gen-msg"); msg.textContent = "Gerando… pode levar de 20 a 60 segundos por imagem.";
    try {
      const r = await api("/api/layouts/generate", { method: "POST", body: JSON.stringify({
        project, format: fmt, role: curRole(), name: $("#lay-name").value, count: Number($("#lay-count").value), feedback: $("#lay-feedback").value }) });
      msg.textContent = r.error ? `Saiu ${r.layouts.length}; depois falhou: ${r.error}` : `${r.layouts.length} arte(s) nova(s) em Artes, abaixo.`;
    } catch (e) { msg.textContent = e.message; }
    busy = false; await load();
  };

  initProjects((slug) => { project = slug; $("#page-crumb").textContent = "Layouts"; load(); });
})();
