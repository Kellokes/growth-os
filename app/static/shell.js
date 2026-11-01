// Partes comuns das páginas logadas: menu lateral com as threads, sair, auditoria e helpers (window.GOS).
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

  // ---------- menu lateral: seções em cima, threads embaixo (em todas as páginas) ----------
  // Na página de Threads o app.js define onPick/onNew; nas outras, clicar leva para "/?c=<id>".
  const app = $("#app");
  const TRASH = "__trash__", ALL = "__all__";
  const isChat = app.classList.contains("chat-shell");
  let filter = ALL, activeId = null, searchTimer = null;
  try { filter = localStorage.getItem("gos.convFilter") || ALL; } catch { /* sem storage */ }
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  async function loadFilters() {
    const data = await api("/api/conversations/categories");
    const box = $("#conv-filters"); box.innerHTML = "";
    const total = data.categories.reduce((n, c) => n + c.count, 0);
    const opts = [[ALL, "Todas", total], ...data.categories.map((c) => [c.name, c.name || "Sem categoria", c.count])];
    if (data.trash) opts.push([TRASH, "🗑 Lixeira", data.trash]);
    if (!opts.some(([v]) => v === filter)) filter = ALL;
    for (const [value, label, count] of opts) {
      const b = el("button", "fchip" + (value === filter ? " active" : ""));
      b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", value === filter);
      b.innerHTML = `${esc(label)} <span>${count}</span>`;
      b.onclick = () => { setFilter(value); reloadThreads(); };
      box.appendChild(b);
    }
    const dl = $("#conv-cats");
    if (dl) { dl.innerHTML = ""; for (const c of data.categories) if (c.name) { const o = el("option"); o.value = c.name; dl.appendChild(o); } }
  }
  function setFilter(value) { filter = value; try { localStorage.setItem("gos.convFilter", value); } catch { /* ok */ } }

  async function reloadThreads() {
    await loadFilters().catch(() => {});
    const qs = new URLSearchParams();
    if (filter === TRASH) qs.set("trash", "1"); else if (filter !== ALL) qs.set("category", filter);
    const q = $("#conv-search").value.trim(); if (q) qs.set("q", q);
    const list = await api("/api/conversations?" + qs);
    const ul = $("#conv-list"); ul.innerHTML = "";
    if (!list.length) ul.appendChild(el("li", "conv-empty", filter === TRASH ? "Lixeira vazia." : q ? "Nada encontrado." : "Nenhuma thread ainda."));
    for (const c of list) {
      const li = el("li");
      const a = el("a", activeId === c.id ? "active" : "");
      a.href = `/?c=${c.id}`; a.dataset.id = c.id;
      if (activeId === c.id) a.setAttribute("aria-current", "true");
      a.innerHTML = `<span class="conv-t">${esc(c.title)}</span>` + (c.category && filter === ALL ? `<span class="conv-cat">${esc(c.category)}</span>` : "");
      li.appendChild(a); ul.appendChild(li);
    }
    return list;
  }
  $("#conv-list").addEventListener("click", (ev) => {
    const a = ev.target.closest("a[data-id]");
    if (!a || !threads.onPick || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button) return;
    ev.preventDefault(); closePanel(); threads.onPick(Number(a.dataset.id));
  });
  $("#conv-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(reloadThreads, 250); });
  document.querySelectorAll("[data-new-thread]").forEach((b) => b.addEventListener("click", () => {
    closePanel();
    if (threads.onNew) threads.onNew(); else location.href = "/?new=1";
  }));

  // gaveta no celular
  const openPanel = () => { app.classList.add("panel-open"); $("#conv-backdrop").hidden = false; };
  const closePanel = () => { app.classList.remove("panel-open"); $("#conv-backdrop").hidden = true; };
  document.querySelectorAll("[data-side-open]").forEach((b) => b.addEventListener("click", () => (app.classList.contains("panel-open") ? closePanel() : openPanel())));
  $("#side-close").onclick = closePanel;
  $("#conv-backdrop").onclick = closePanel;
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closePanel(); });
  // recolher no computador: fica só a coluna de ícones (lembra a escolha)
  $("#side-mini").onclick = () => {
    const mini = document.documentElement.getAttribute("data-side") !== "mini";
    if (mini) document.documentElement.setAttribute("data-side", "mini"); else document.documentElement.removeAttribute("data-side");
    try { localStorage.setItem("gos.side", mini ? "mini" : "full"); } catch { /* ok */ }
  };

  const threads = {
    ALL, TRASH, reload: reloadThreads, closePanel,
    filter: () => filter, setFilter,
    setActive(id) {
      activeId = id;
      document.querySelectorAll("#conv-list a[data-id]").forEach((a) => {
        const on = Number(a.dataset.id) === id;
        a.classList.toggle("active", on);
        if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
      });
    },
    onPick: null, onNew: null,
  };
  if (!isChat) reloadThreads().catch(() => {});

  window.GOS = { $, esc, api, fmt, icon, initProjects, threads };
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
