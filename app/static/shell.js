// Partes comuns das páginas logadas: sair, auditoria e helpers (window.GOS) para as páginas.
(() => {
  const $ = (s) => document.querySelector(s);
  const CSRF = $("#app").dataset.csrf;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF } });
    if (res.status === 401) { location.href = "/login"; throw new Error("sessão expirada"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `erro ${res.status}`);
    return data;
  }
  const nf = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });
  const cf = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 });
  const fmt = (v, unit) => v == null ? "—" : unit === "currency" ? cf.format(v) : unit === "percent" ? `${nf.format(v)}%` : nf.format(v);
  // Seletor de projeto: agentes, KPIs e tarefas são sempre de UM projeto (ou "todos" onde faz sentido).
  async function initProjects(onChange, { allowAll = false } = {}) {
    const box = $("#project-switch");
    let list = [];
    let current = null;
    try { current = localStorage.getItem("gos.project"); } catch { /* sem storage */ }
    const draw = () => {
      if (!list.length) {
        box.innerHTML = `<div class="pswitch-empty"><span>Nenhum projeto cadastrado ainda.</span>
          <button class="btn small" type="button" data-import>Importar do VPS</button>
          <a class="btn small ghost" href="/agents#novo">Criar projeto</a></div>`;
        box.querySelector("[data-import]").onclick = async () => {
          try { await api("/api/gprojects/import", { method: "POST" }); await reload(); } catch (e) { alert(e.message); }
        };
        return;
      }
      const pills = (allowAll ? [{ slug: "all", name: "Todos os projetos", icon: "▦", pending: list.reduce((n, p) => n + p.pending, 0) }] : []).concat(list);
      box.innerHTML = `<span class="pswitch-label">Projeto</span><div class="pswitch-pills">${pills.map((p) =>
        `<button type="button" class="pill ${p.slug === current ? "on" : ""} ${p.enabled === false ? "off" : ""}" data-p="${esc(p.slug)}">
          <span>${esc(p.icon)}</span>${esc(p.name)}${p.pending ? `<b title="tarefas esperando decisão">${p.pending}</b>` : ""}</button>`).join("")}</div>`;
    };
    const pick = (slug) => {
      current = slug; try { localStorage.setItem("gos.project", slug); } catch { /* ok */ }
      draw();
      onChange(slug, list.find((p) => p.slug === slug) || null, list);
    };
    async function reload() {
      list = await api("/api/gprojects");
      const valid = (allowAll && current === "all") || list.some((p) => p.slug === current);
      if (!valid) current = allowAll ? "all" : list[0]?.slug || null;
      draw();
      if (current) onChange(current, list.find((p) => p.slug === current) || null, list);
      else onChange(null, null, list);
    }
    box.addEventListener("click", (ev) => { const b = ev.target.closest("[data-p]"); if (b) pick(b.dataset.p); });
    await reload();
    return { reload, current: () => current, list: () => list };
  }

  const icon = (name) => `<svg class="ic" aria-hidden="true" focusable="false"><use href="/static/icons.svg#i-${name}"/></svg>`;
  window.GOS = { $, esc, api, fmt, icon, initProjects };
  const call = async (path, method = "GET") => {
    const res = await fetch(path, { method, credentials: "same-origin", headers: { "X-CSRF-Token": CSRF } });
    if (res.status === 401) { location.href = "/login"; throw new Error("sessão expirada"); }
    return res.json().catch(() => ({}));
  };
  $("#logout").onclick = async () => { await call("/logout", "POST").catch(() => {}); location.href = "/login"; };
  $("#show-audit").onclick = async () => {
    const rows = await call("/api/audit");
    const box = $("#audit-list"); box.innerHTML = "";
    for (const r of rows) {
      const d = document.createElement("div");
      for (const v of [new Date(r.at + "Z").toLocaleString("pt-BR"), r.action, r.detail]) {
        const s = document.createElement("span"); s.textContent = v; s.title = v; d.appendChild(s);
      }
      box.appendChild(d);
    }
    $("#audit-dialog").showModal();
  };
})();
