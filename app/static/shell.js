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
          <a class="btn small ghost" href="/automations#novo">Criar projeto</a></div>`;
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
  const TRASH = "__trash__", NONE = "";
  const isChat = app.classList.contains("chat-shell");
  let activeId = null, searchTimer = null;
  // Grupos fechados ficam lembrados neste navegador. A lixeira começa fechada.
  let collapsed = new Set([TRASH]);
  try { const saved = JSON.parse(localStorage.getItem("gos.convCollapsed") || "null"); if (Array.isArray(saved)) collapsed = new Set(saved); } catch { /* sem storage */ }
  const saveCollapsed = () => { try { localStorage.setItem("gos.convCollapsed", JSON.stringify([...collapsed])); } catch { /* ok */ } };
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const ICON = (n) => `<svg class="ic" aria-hidden="true" focusable="false"><use href="/static/icons.svg#i-${n}"/></svg>`;

  async function loadCategories() {
    const data = await api("/api/conversations/categories");
    const dl = $("#conv-cats");
    if (dl) { dl.innerHTML = ""; for (const c of data.categories) if (c.name) { const o = el("option"); o.value = c.name; dl.appendChild(o); } }
    return data;
  }

  function convItem(c) {
    const li = el("li");
    const b = el("button", "conv-item" + (activeId === c.id ? " active" : ""));
    b.type = "button"; b.dataset.id = c.id;
    if (activeId === c.id) b.setAttribute("aria-current", "true");
    b.innerHTML = `<span class="conv-t">${esc(c.title)}</span>`;
    b.onclick = () => pick(c.id);
    li.appendChild(b);
    return li;
  }
  function pick(id) {
    closePanel();
    if (threads.onPick) threads.onPick(id); else location.href = `/?c=${id}`;
  }

  // Um grupo: cabeçalho que abre/fecha + botão "+" para nova thread já na categoria.
  function convGroup(key, label, items, { forceOpen = false, canAdd = true } = {}) {
    const open = forceOpen || !collapsed.has(key);
    const g = el("li", "conv-group" + (open ? " open" : "") + (key === TRASH ? " trash" : ""));
    const head = el("div", "conv-group-head");
    const toggle = el("button", "conv-group-toggle");
    toggle.type = "button"; toggle.setAttribute("aria-expanded", open);
    toggle.innerHTML = `${ICON("chevron")}<span class="conv-group-name">${esc(label)}</span><span class="conv-group-count">${items.length}</span>`;
    toggle.onclick = () => {
      const nowOpen = !g.classList.contains("open");
      g.classList.toggle("open", nowOpen); toggle.setAttribute("aria-expanded", nowOpen);
      if (nowOpen) collapsed.delete(key); else collapsed.add(key);
      saveCollapsed(); renderGroupBody(g, items, nowOpen);
      if (nowOpen) setTimeout(() => g.scrollIntoView({ block: "nearest", behavior: "smooth" }), 320);
    };
    head.appendChild(toggle);
    if (canAdd) {
      const add = el("button", "conv-group-add", null);
      add.innerHTML = ICON("plus");
      add.type = "button"; add.title = `Nova thread em ${label}`; add.setAttribute("aria-label", add.title);
      add.onclick = () => { closePanel(); if (threads.onNew) threads.onNew(key); else location.href = `/?new=1&cat=${encodeURIComponent(key)}`; };
      head.appendChild(add);
    }
    g.appendChild(head);
    const body = el("div", "conv-group-body"); body.appendChild(el("ul", "conv-items"));
    g.appendChild(body);
    renderGroupBody(g, items, open);
    return g;
  }

  // Grupo fechado ainda mostra a thread aberta, como o Slack faz com o canal atual.
  function renderGroupBody(g, items, open) {
    const ul = g.querySelector(".conv-items"); ul.innerHTML = "";
    const shown = open ? items : items.filter((c) => c.id === activeId);
    g.classList.toggle("peek", !open && shown.length > 0);
    for (const c of shown) ul.appendChild(convItem(c));
  }

  async function reloadThreads() {
    const q = $("#conv-search").value.trim();
    const qs = new URLSearchParams(); if (q) qs.set("q", q);
    const tq = new URLSearchParams({ trash: "1" }); if (q) tq.set("q", q);
    const [cats, list, trash] = await Promise.all([
      loadCategories().catch(() => ({ trash: 0 })),
      api("/api/conversations?" + qs),
      api("/api/conversations?" + tq).catch(() => []),
    ]);
    const groups = new Map();
    for (const c of list) { const k = c.category || NONE; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(c); }
    const keys = [...groups.keys()].sort((x, y) => (x === NONE) - (y === NONE) || x.localeCompare(y, "pt-BR", { sensitivity: "base" }));
    const ul = $("#conv-list"); ul.innerHTML = "";
    if (!list.length && !trash.length) ul.appendChild(el("li", "conv-empty", q ? "Nada encontrado." : "Nenhuma thread ainda."));
    // Sem nenhuma categoria e sem lixeira, um cabeçalho "Sem categoria" sozinho só atrapalha.
    const flat = keys.length === 1 && keys[0] === NONE && !trash.length && !cats.trash;
    if (flat) for (const c of groups.get(NONE)) ul.appendChild(convItem(c));
    else for (const k of keys) ul.appendChild(convGroup(k, k || "Sem categoria", groups.get(k), { forceOpen: !!q }));
    if (trash.length) ul.appendChild(convGroup(TRASH, "Lixeira", trash, { forceOpen: !!q, canAdd: false }));
    return list;
  }
  $("#conv-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(reloadThreads, 250); });
  document.querySelectorAll("[data-new-thread]").forEach((b) => b.addEventListener("click", () => {
    closePanel();
    if (threads.onNew) threads.onNew(NONE); else location.href = "/?new=1";
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
    TRASH, NONE, reload: reloadThreads, closePanel,
    setActive(id) {
      activeId = id;
      document.querySelectorAll("#conv-list .conv-item").forEach((b) => {
        const on = Number(b.dataset.id) === id;
        b.classList.toggle("active", on);
        if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
      });
    },
    expand(key) { collapsed.delete(key); saveCollapsed(); },
    onPick: null, onNew: null,
  };
  if (!isChat) reloadThreads().catch(() => {});

  // ---------- feedback de ações demoradas: botão/cartão "trabalhando", aviso (toast) e piscada de sucesso/erro ----------
  function toast(message, kind = "ok", { action = null, ms = 6000 } = {}) {
    let box = $("#toasts");
    if (!box) { box = document.createElement("div"); box.id = "toasts"; box.setAttribute("role", "status"); box.setAttribute("aria-live", "polite"); document.body.append(box); }
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    const glyph = kind === "ok" ? icon("check") : kind === "err" ? icon("close") : icon("spark");
    el.innerHTML = `<span class="toast-ic">${glyph}</span><span class="toast-msg">${esc(message)}</span>${action ? `<button class="toast-act" type="button">${esc(action.label)}</button>` : ""}<button class="toast-x" type="button" aria-label="Fechar">${icon("close")}</button>`;
    const bye = () => { el.classList.add("out"); setTimeout(() => el.remove(), 260); };
    el.querySelector(".toast-x").onclick = bye;
    if (action) el.querySelector(".toast-act").onclick = () => { bye(); action.run(); };
    box.append(el);
    while (box.children.length > 4) box.firstChild.remove();
    if (ms) setTimeout(bye, kind === "err" ? ms * 2 : ms);
    return el;
  }

  // Roda `fn` com a tela já respondendo: o botão clicado vira "Aprovando…" com spinner e brilho, o resto do cartão (ou do
  // painel) fica travado com uma barra de progresso, e `phases` troca o texto conforme o tempo passa ([[ms, texto], ...]).
  // Devolve {ok, value} ou {ok:false, error}; quem chama decide o aviso final.
  async function working({ btn = null, scope = null, phases = [[0, "Processando…"]] } = {}, fn) {
    const box = scope || btn?.closest(".cpiece, dialog, .approval, .kcard") || null;
    const locked = box ? [...box.querySelectorAll("button, a.btn")] : (btn ? [btn] : []);
    if (btn && !locked.includes(btn)) locked.push(btn);
    const before = locked.map((b) => [b, b.disabled, b.getAttribute("aria-disabled")]);
    const original = btn?.innerHTML;
    const label = (t) => { if (btn) btn.innerHTML = `<span class="spin" aria-hidden="true"></span><span class="lbl">${esc(t)}</span>`; box?.querySelector?.(":scope > .busy-bar .busy-label")?.replaceChildren(t); };
    locked.forEach((b) => { b.disabled = true; b.setAttribute("aria-disabled", "true"); });
    btn?.classList.add("is-busy");
    if (box) {
      box.classList.add("is-busy"); box.setAttribute("aria-busy", "true");
      if (!box.querySelector(":scope > .busy-bar")) box.insertAdjacentHTML("beforeend", '<div class="busy-bar"><i aria-hidden="true"></i><em class="busy-label" aria-live="polite"></em></div>');
    }
    const timers = phases.map(([ms, t]) => setTimeout(() => label(t), ms));
    label(phases[0][1]);
    try { return { ok: true, value: await fn() }; }
    catch (e) { return { ok: false, error: e }; }
    finally {
      timers.forEach(clearTimeout);
      box?.classList.remove("is-busy"); box?.removeAttribute("aria-busy"); box?.querySelector(":scope > .busy-bar")?.remove();
      btn?.classList.remove("is-busy");
      before.forEach(([b, d, a]) => { if (!b.isConnected) return; b.disabled = d; a == null ? b.removeAttribute("aria-disabled") : b.setAttribute("aria-disabled", a); });
      if (btn?.isConnected && original != null) btn.innerHTML = original;
    }
  }

  // Pisca um elemento (o cartão já redesenhado) em verde ou vermelho para mostrar o desfecho.
  function flash(el, kind = "ok") {
    if (!el) return;
    const cls = kind === "ok" ? "flash-ok" : "flash-err";
    el.classList.remove("flash-ok", "flash-err"); void el.offsetWidth; el.classList.add(cls);
    setTimeout(() => el.classList.remove(cls), 1800);
  }

  window.GOS = { $, esc, api, fmt, icon, initProjects, threads, toast, working, flash };
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
