(() => {
  const $ = (s) => document.querySelector(s);
  const app = $("#app");
  const CSRF = app.dataset.csrf;
  const state = { conv: null, commands: {}, pending: null };

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF, ...(opts.headers || {}) },
      credentials: "same-origin",
    });
    if (res.status === 401) { location.href = "/login"; throw new Error("sessão expirada"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `erro ${res.status}`);
    return data;
  }

  const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

  // Markdown mínimo e seguro: tudo é escapado antes; só blocos de código e `inline` viram HTML.
  function render(text, container) {
    const parts = text.split(/```(\w*)\n?([\s\S]*?)```/g);
    for (let i = 0; i < parts.length; i += 3) {
      const prose = parts[i];
      if (prose && prose.trim()) {
        for (const para of prose.trim().split(/\n{2,}/)) {
          const html = esc(para).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
          container.appendChild(el("p", null, html));
        }
      }
      if (i + 2 < parts.length) container.appendChild(codeBlock(parts[i + 1], parts[i + 2].replace(/\n$/, "")));
    }
  }

  function codeBlock(lang, code) {
    const box = el("div", "code");
    const head = el("div", "code-head", `<span>${esc(lang || "código")}</span>`);
    const pre = el("pre"); pre.textContent = code;
    box.append(head, pre);
    if (["bash", "sh", "shell", "zsh", "console"].includes(lang)) {
      const btn = el("button", "btn small", "▶ Executar");
      btn.type = "button";
      btn.onclick = () => runCommand(code, box, btn);
      head.appendChild(btn);
      const past = state.commands[code];
      if (past) showRun(box, past);
    }
    return box;
  }

  function showRun(box, run) {
    box.querySelector(".run-out")?.remove();
    const out = el("div", "run-out");
    const label = { done: "concluído", failed: `falhou (código ${run.exit_code})`, pending: "aguardando aprovação 2FA", blocked: "bloqueado", rejected: "rejeitado", running: "executando" }[run.status] || run.status;
    out.appendChild(el("div", "code-head", `<span>saída</span><span class="badge ${run.status}">${esc(label)}</span>`));
    if (run.output) { const pre = el("pre"); pre.textContent = run.output; out.appendChild(pre); }
    box.appendChild(out);
  }

  async function runCommand(code, box, btn) {
    btn.disabled = true;
    try {
      const run = await api("/api/commands", { method: "POST", body: JSON.stringify({ command: code, conversation_id: state.conv?.id }) });
      state.commands[code] = run;
      showRun(box, run);
      if (run.status === "pending") openApproval(run, box);
    } catch (e) { alert(e.message); } finally { btn.disabled = false; }
  }

  function openApproval(run, box) {
    state.pending = { run, box };
    $("#approve-cmd").textContent = run.command;
    $("#approve-totp").value = "";
    $("#approve-dialog").showModal();
    $("#approve-totp").focus();
  }

  $("#approve-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const { run, box } = state.pending;
    try {
      const done = await api(`/api/commands/${run.id}/approve`, { method: "POST", body: JSON.stringify({ totp: $("#approve-totp").value }) });
      state.commands[run.command] = done; showRun(box, done);
      $("#approve-dialog").close();
    } catch (e) { alert(e.message); }
  };
  $("#approve-cancel").onclick = () => $("#approve-dialog").close();
  $("#approve-reject").onclick = async () => {
    const { run, box } = state.pending;
    try { const r = await api(`/api/commands/${run.id}/reject`, { method: "POST" }); state.commands[run.command] = r; showRun(box, r); } catch (e) { alert(e.message); }
    $("#approve-dialog").close();
  };

  function addMessage(m) {
    $("#empty")?.remove();
    const row = el("div", `msg ${m.role}${m.error ? " error" : ""}`);
    row.appendChild(el("div", "avatar", m.role === "user" ? "Eu" : "◆"));
    const body = el("div", "body");
    body.appendChild(el("div", "meta", m.role === "user" ? "Você" : esc(`Growth OS · ${m.provider || ""}`)));
    const content = el("div", "content");
    if (m.typing) content.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    else render(m.content, content);
    body.appendChild(content);
    row.appendChild(body);
    $("#messages").appendChild(row);
    $("#messages").scrollTop = $("#messages").scrollHeight;
    return row;
  }

  const PROMPTS = [
    ["Estado do servidor", "Mostre o estado do servidor: disco, memória e containers rodando."],
    ["Listar projetos", "Liste os projetos que existem no VPS e o que cada um faz."],
    ["Novo projeto Python", "Crie a estrutura inicial de um novo projeto Python com FastAPI."],
  ];

  function emptyState() {
    const e = el("div", "empty", '<div class="logo">◆</div><h2>O que vamos operar hoje?</h2><p>A IA propõe os comandos e você decide o que roda.</p>');
    e.id = "empty";
    const chips = el("div", "chips");
    for (const [label, prompt] of PROMPTS) {
      const b = el("button", "chip"); b.type = "button"; b.textContent = label; b.dataset.prompt = prompt;
      chips.appendChild(b);
    }
    e.appendChild(chips);
    return e;
  }

  async function loadProviders() {
    const list = await api("/api/providers");
    const sel = $("#provider");
    sel.innerHTML = "";
    for (const p of list) {
      const o = el("option", null, esc(p.label + (p.available ? "" : ` (${p.reason})`)));
      o.value = p.id; o.disabled = !p.available;
      sel.appendChild(o);
    }
    const saved = localStorage.getItem("gos.provider");
    const first = list.find((p) => p.available);
    sel.value = (saved && list.find((p) => p.id === saved && p.available)) ? saved : (first?.id || "");
    sel.onchange = () => localStorage.setItem("gos.provider", sel.value);
  }

  async function loadConversations() {
    const list = await api("/api/conversations");
    const ul = $("#conv-list"); ul.innerHTML = "";
    for (const c of list) {
      const li = el("li"); const b = el("button", state.conv?.id === c.id ? "active" : "");
      b.type = "button"; b.textContent = c.title; b.onclick = () => openConversation(c.id);
      li.appendChild(b); ul.appendChild(li);
    }
    return list;
  }

  async function openConversation(id) {
    const c = await api(`/api/conversations/${id}`);
    state.conv = c; state.commands = {};
    for (const r of c.commands) state.commands[r.command] = r;
    $("#conv-title").textContent = c.title;
    $("#messages").innerHTML = "";
    if (!c.messages.length) $("#messages").appendChild(emptyState());
    c.messages.forEach(addMessage);
    loadConversations();
  }

  async function newConversation() {
    const c = await api("/api/conversations", { method: "POST" });
    await openConversation(c.id);
    $("#input").focus();
  }

  $("#composer").onsubmit = async (ev) => {
    ev.preventDefault();
    const input = $("#input"); const text = input.value.trim(); if (!text) return;
    const provider = $("#provider").value;
    if (!provider) { alert("Nenhum provider disponível. Configure Claude, Codex ou OpenRouter no servidor."); return; }
    if (!state.conv) await newConversation();
    input.value = ""; input.style.height = "auto";
    addMessage({ role: "user", content: text });
    const typing = addMessage({ role: "assistant", provider, typing: true });
    $("#send").disabled = true;
    try {
      const m = await api(`/api/conversations/${state.conv.id}/messages`, { method: "POST", body: JSON.stringify({ provider, content: text }) });
      typing.remove(); addMessage(m);
      if (m.title && m.title !== state.conv.title) { state.conv.title = m.title; $("#conv-title").textContent = m.title; loadConversations(); }
    } catch (e) {
      typing.remove(); addMessage({ role: "assistant", provider, content: `Erro: ${e.message}`, error: true });
    } finally { $("#send").disabled = false; input.focus(); }
  };

  $("#input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#input").addEventListener("input", (ev) => { ev.target.style.height = "auto"; ev.target.style.height = ev.target.scrollHeight + "px"; });
  $("#messages").addEventListener("click", (ev) => {
    const chip = ev.target.closest(".chip");
    if (!chip) return;
    $("#input").value = chip.dataset.prompt;
    $("#composer").requestSubmit();
  });
  $("#new-conv").onclick = newConversation;
  $("#logout").onclick = async () => { await api("/logout", { method: "POST" }).catch(() => {}); location.href = "/login"; };
  $("#show-audit").onclick = async () => {
    const rows = await api("/api/audit");
    const box = $("#audit-list"); box.innerHTML = "";
    for (const r of rows) {
      const d = el("div");
      for (const v of [new Date(r.at + "Z").toLocaleString("pt-BR"), r.action, r.detail]) { const s = el("span"); s.textContent = v; s.title = v; d.appendChild(s); }
      box.appendChild(d);
    }
    $("#audit-dialog").showModal();
  };

  (async () => {
    await loadProviders();
    const convs = await loadConversations();
    if (convs.length) await openConversation(convs[0].id);
  })().catch((e) => console.error(e));
})();
