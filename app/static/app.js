(() => {
  const $ = (s) => document.querySelector(s);
  const app = $("#app");
  const CSRF = app.dataset.csrf;
  const state = { conv: null, commands: {}, boxes: {}, pending: null, busy: false };

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
      state.boxes[code] = box;
      const past = state.commands[code];
      if (past) showRun(box, past);
      else {
        const btn = el("button", "btn small", "▶ Executar");
        btn.type = "button";
        btn.onclick = () => runCommand(code, box, btn);
        head.appendChild(btn);
      }
    }
    return box;
  }

  function showRun(box, run) {
    box.querySelector(".run-out")?.remove();
    box.querySelector(".code-head button")?.remove(); // já rodou: o botão manual não faz mais sentido
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
      state.commands[run.command] = done; if (box) showRun(box, done);
      $("#approve-dialog").close();
      continueAgent();
    } catch (e) { alert(e.message); }
  };
  $("#approve-cancel").onclick = () => $("#approve-dialog").close();
  $("#approve-reject").onclick = async () => {
    const { run, box } = state.pending;
    try {
      const r = await api(`/api/commands/${run.id}/reject`, { method: "POST" });
      state.commands[run.command] = r; if (box) showRun(box, r);
      $("#approve-dialog").close();
      continueAgent();
    } catch (e) { alert(e.message); $("#approve-dialog").close(); }
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
    const e = el("div", "empty", '<div class="logo">◆</div><h2>O que vamos operar hoje?</h2><p>A IA executa sozinha o que é comum e pede o seu 2FA só para o que é perigoso.</p>');
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
    state.conv = c; state.commands = {}; state.boxes = {};
    for (const r of c.commands) state.commands[r.command] = r;
    $("#conv-title").textContent = c.title;
    $("#messages").innerHTML = "";
    if (!c.messages.length) $("#messages").appendChild(emptyState());
    c.messages.forEach(addMessage);
    const pending = c.commands.find((r) => r.status === "pending");
    if (pending) openApproval(pending, state.boxes[pending.command]);
    loadConversations();
  }

  async function newConversation() {
    const c = await api("/api/conversations", { method: "POST" });
    await openConversation(c.id);
    $("#input").focus();
  }

  // Lê o NDJSON do laço do agente e desenha cada evento assim que chega.
  async function consume(path, body, provider) {
    state.busy = true; $("#send").disabled = true;
    let typing = addMessage({ role: "assistant", provider, typing: true });
    const setTyping = (label) => { typing?.querySelector(".content").setAttribute("data-label", label); };
    const dropTyping = () => { typing?.remove(); typing = null; };
    try {
      const res = await fetch(path, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
        body: JSON.stringify(body),
      });
      if (res.status === 401) { location.href = "/login"; return; }
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `erro ${res.status}`); }
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === "step") { setTyping(`passo ${ev.n}`); }
          else if (ev.type === "message") { dropTyping(); addMessage({ role: "assistant", provider: ev.provider, content: ev.content }); typing = addMessage({ role: "assistant", provider, typing: true }); }
          else if (ev.type === "command") {
            state.commands[ev.run.command] = ev.run;
            const box = state.boxes[ev.run.command]; if (box) showRun(box, ev.run);
          } else if (ev.type === "approval") {
            dropTyping();
            state.commands[ev.run.command] = ev.run;
            const box = state.boxes[ev.run.command]; if (box) showRun(box, ev.run);
            openApproval(ev.run, box);
          } else if (ev.type === "notice") { dropTyping(); addMessage({ role: "assistant", provider, content: ev.message }); }
          else if (ev.type === "error") { dropTyping(); addMessage({ role: "assistant", provider, content: `Erro: ${ev.message}`, error: true }); }
        }
      }
    } catch (e) {
      dropTyping(); addMessage({ role: "assistant", provider, content: `Erro: ${e.message}`, error: true });
    } finally {
      dropTyping(); state.busy = false; $("#send").disabled = false; $("#input").focus();
      loadConversations();
    }
  }

  function continueAgent() {
    const provider = $("#provider").value;
    if (state.conv && provider) consume(`/api/conversations/${state.conv.id}/continue`, { provider }, provider);
  }

  $("#composer").onsubmit = async (ev) => {
    ev.preventDefault();
    if (state.busy) return;
    const input = $("#input"); const text = input.value.trim(); if (!text) return;
    const provider = $("#provider").value;
    if (!provider) { alert("Nenhum provider disponível. Configure Claude, Codex ou OpenRouter no servidor."); return; }
    if (!state.conv) await newConversation();
    input.value = ""; input.style.height = "auto";
    addMessage({ role: "user", content: text });
    if (state.conv.title === "Nova conversa") { state.conv.title = text.split("\n")[0].slice(0, 80); $("#conv-title").textContent = state.conv.title; }
    consume(`/api/conversations/${state.conv.id}/messages`, { provider, content: text }, provider);
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
  (async () => {
    await loadProviders();
    const convs = await loadConversations();
    if (convs.length) await openConversation(convs[0].id);
  })().catch((e) => console.error(e));
})();
