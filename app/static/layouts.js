// Estúdio de layouts: briefing, referências e layouts gerados, sempre de UM projeto.
(() => {
  const { $, esc, api, initProjects } = window.GOS;
  const STATUS = { proposed: "proposta", approved: "oficial", rejected: "rejeitado" };
  let project = null; let state = null; let busy = false;

  const fileToB64 = (f) => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result));
    r.onerror = () => rej(new Error("não consegui ler o arquivo"));
    r.readAsDataURL(f);
  });

  function draw() {
    if (!state) return;
    $("#lay-brief").value = state.brief;
    $("#lay-ref-max").textContent = state.limits.references;
    $("#lay-model").textContent = state.image_model || "nenhum";
    const warn = $("#lay-problem");
    warn.hidden = !state.problem; warn.textContent = state.problem || "";
    $("#lay-generate").disabled = busy || !!state.problem;
    $("#lay-format").innerHTML = state.formats.map((f) => `<option value="${esc(f.key)}">${esc(f.name)} · ${esc(f.ratio)}</option>`).join("");
    $("#lay-refs").innerHTML = state.references.length ? state.references.map((a) => `
      <figure class="lay-thumb"><img src="${esc(a.url)}" alt="${esc(a.note || "referência")}" loading="lazy">
        <figcaption>${esc(a.note) || "<span class='muted'>sem legenda</span>"}</figcaption>
        <button class="btn small danger" type="button" data-del="${a.id}">Apagar</button></figure>`).join("")
      : `<div class="muted">Nenhuma referência ainda.</div>`;
    $("#lay-formats").innerHTML = state.formats.map((f) => {
      const rows = state.layouts.filter((l) => l.format === f.key);
      return `<div class="card lay-card"><h3>${esc(f.name)} <span class="muted">${esc(f.ratio)}</span></h3>
        <div class="lay-grid">${rows.length ? rows.map((l) => `
          <figure class="lay-thumb ratio-${f.ratio.replace(":", "x")} ${l.status === "approved" ? "official" : ""}">
            <img src="${esc(l.url)}" alt="layout ${esc(f.name)}" loading="lazy">
            <figcaption><b>${STATUS[l.status] || l.status}</b>${l.note ? ` · ${esc(l.note)}` : ""}</figcaption>
            <div class="row">${l.status === "approved" ? `<button class="btn small primary" type="button" data-html="${l.id}">Converter em HTML</button>` : `<button class="btn small primary" type="button" data-ok="${l.id}">Aprovar</button>`}
              ${l.status === "proposed" ? `<button class="btn small" type="button" data-no="${l.id}">Rejeitar</button>` : ""}
              ${l.status === "approved" ? "" : `<button class="btn small danger" type="button" data-del="${l.id}">Apagar</button>`}</div>
          </figure>`).join("") : `<div class="muted">Nenhum layout ainda. Gere propostas acima.</div>`}</div>
        ${templatesBlock(f)}</div>`;
    }).join("");
  }

  function templatesBlock(f) {
    const rows = (state.templates || []).filter((t) => t.format === f.key);
    const hm = state.html_model || {};
    return `<h4 class="lay-sub">Template HTML ${hm.problem && !rows.length ? `<small class="muted">· ${esc(hm.problem)}</small>` : ""}</h4>
      <p class="muted">É o que vale nos posts: o desenho fica fixo e só o texto e o fundo mudam. Aprove um por formato.</p>
      <div class="lay-grid">${rows.length ? rows.map((t) => `
        <figure class="lay-thumb ratio-${f.ratio.replace(":", "x")} ${t.status === "approved" ? "official" : ""}">
          <img src="${esc(t.url)}" alt="template ${esc(f.name)}" loading="lazy">
          <figcaption><b>${STATUS[t.status] || t.status}</b>${t.note ? ` · ${esc(t.note)}` : ""}</figcaption>
          <div class="row">${t.status === "approved" ? "" : `<button class="btn small primary" type="button" data-ok="${t.id}">Aprovar</button>`}
            ${t.status === "proposed" ? `<button class="btn small" type="button" data-no="${t.id}">Rejeitar</button>` : ""}
            ${t.status === "approved" ? "" : `<button class="btn small danger" type="button" data-del="${t.id}">Apagar</button>`}</div>
        </figure>`).join("") : `<div class="muted">Aprove um layout e use "Converter em HTML".</div>`}</div>`;
  }

  async function load() {
    if (!project) return;
    try { state = await api(`/api/layouts?project=${encodeURIComponent(project)}`); draw(); } catch (e) { alert(e.message); }
  }
  const act = async (fn) => { try { await fn(); await load(); } catch (e) { alert(e.message); } };

  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-ok],button[data-no],button[data-del],button[data-html]");
    if (!b) return;
    if (b.dataset.html) {
      const feedback = prompt("Ajustes para o template (opcional):", "") ?? null;
      if (feedback === null) return;
      b.disabled = true; b.textContent = "Convertendo… (até 1 min)";
      act(() => api(`/api/layouts/${b.dataset.html}/template`, { method: "POST", body: JSON.stringify({ feedback }) }));
    } else if (b.dataset.ok) act(() => api(`/api/layouts/${b.dataset.ok}/decide`, { method: "POST", body: JSON.stringify({ approve: true }) }));
    else if (b.dataset.no) act(() => api(`/api/layouts/${b.dataset.no}/decide`, { method: "POST", body: JSON.stringify({ approve: false }) }));
    else if (confirm("Apagar esta imagem?")) act(() => api(`/api/layouts/assets/${b.dataset.del}`, { method: "DELETE" }));
  });

  $("#lay-brief-save").onclick = () => act(async () => {
    await api("/api/layouts/brief", { method: "PUT", body: JSON.stringify({ project, brief: $("#lay-brief").value }) });
    $("#lay-brief-msg").textContent = "Salvo.";
  });

  $("#lay-file").onchange = async (ev) => {
    const msg = $("#lay-ref-msg");
    for (const f of ev.target.files) {
      msg.textContent = `Enviando ${f.name}…`;
      try {
        await api("/api/layouts/references", { method: "POST", body: JSON.stringify({ project, data: await fileToB64(f), note: f.name.replace(/\.[^.]+$/, "") }) });
      } catch (e) { alert(`${f.name}: ${e.message}`); }
    }
    ev.target.value = ""; msg.textContent = ""; await load();
  };

  $("#lay-generate").onclick = async () => {
    if (busy) return;
    busy = true; $("#lay-generate").disabled = true;
    const msg = $("#lay-gen-msg"); msg.textContent = "Gerando… pode levar de 20 a 60 segundos por imagem.";
    try {
      const r = await api("/api/layouts/generate", { method: "POST", body: JSON.stringify({
        project, format: $("#lay-format").value, count: Number($("#lay-count").value), feedback: $("#lay-feedback").value }) });
      msg.textContent = r.error ? `Saiu ${r.layouts.length}; depois falhou: ${r.error}` : `${r.layouts.length} layout(s) novo(s).`;
    } catch (e) { msg.textContent = e.message; }
    busy = false; await load();
  };

  initProjects((slug) => { project = slug; $("#page-crumb").textContent = "Layouts"; load(); });
})();
