(() => {
  const $ = (s) => document.querySelector(s);
  const app = $("#app");
  const CSRF = app.dataset.csrf;
  const state = { conv: null, commands: {}, boxes: {}, pending: null, busy: false, choice: { provider: "", model: "", reasoning: "" } };

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
    row.appendChild(el("div", m.role === "user" ? "avatar" : "avatar logo", m.role === "user" ? "Eu" : "◆"));
    const body = el("div", "body");
    body.appendChild(el("div", "meta", m.role === "user" ? "Você" : esc(["Growth OS", m.provider, m.model].filter(Boolean).join(" · "))));
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
    const e = el("div", "empty", '<div class="orb" aria-hidden="true"><i></i></div><h2>O que vamos operar hoje?</h2><p>A IA executa sozinha o que é comum e pede o seu 2FA só para o que é perigoso.</p>');
    e.id = "empty";
    const chips = el("div", "chips");
    for (const [label, prompt] of PROMPTS) {
      const b = el("button", "chip"); b.type = "button"; b.textContent = label; b.dataset.prompt = prompt;
      chips.appendChild(b);
    }
    e.appendChild(chips);
    return e;
  }

  // ---------- provider, modelo e raciocínio da thread ----------
  const M = window.GOSModels;
  let picker = null;
  function defaultChoice() {
    const eff = M.data()?.effective?.threads;
    if (eff?.provider && M.prov(eff.provider)?.available) return { provider: eff.provider, model: eff.model, reasoning: eff.reasoning };
    const first = M.data()?.providers.find((p) => p.available);
    return { provider: first?.id || "", model: "", reasoning: "" };
  }
  function choiceFor(conv) {
    if (conv?.provider && M.prov(conv.provider)?.available) return { provider: conv.provider, model: conv.model || "", reasoning: conv.reasoning || "" };
    return defaultChoice();
  }
  function showChoice() {
    $("#model-label").textContent = M.describe(state.choice) || "Nenhum provider disponível";
    $("#model-btn").title = `Provider, modelo e raciocínio desta thread: ${M.describe(state.choice)}`;
  }
  async function loadProviders() {
    await M.load(api);
    picker = M.mount($("#model-fields"), api, { kind: "text", inherit: null });
    state.choice = defaultChoice();
    showChoice();
  }
  $("#model-btn").onclick = async () => { if (!picker) return; await picker.set(state.choice); $("#model-dialog").showModal(); };
  $("#model-cancel").onclick = () => $("#model-dialog").close();
  $("#model-reset").onclick = () => picker.set(defaultChoice());
  $("#model-form").onsubmit = (ev) => {
    ev.preventDefault();
    state.choice = picker.get(); showChoice();
    $("#model-dialog").close();
  };

  // ---------- lista de threads: fica no menu lateral (shell.js), aqui só reage ----------
  const T = window.GOS.threads;
  const { TRASH, NONE } = T;
  const NEW_TITLES = ["Nova thread", "Nova conversa"];
  const loadConversations = () => { T.setActive(state.conv?.id ?? null); return T.reload(); };
  const closePanel = T.closePanel;
  T.onPick = (id) => openConversation(id);
  T.onNew = (category) => newConversation(category);

  // ---------- organizar: título, categoria, excluir (só do sistema), restaurar ----------
  function showConvMeta(c) {
    $("#conv-title").textContent = c.title;
    $("#conv-edit").hidden = !c;
    $("#trash-bar").hidden = !c.deleted;
    $("#composer").classList.toggle("disabled", !!c.deleted);
    $("#input").disabled = !!c.deleted;
  }

  $("#conv-edit").onclick = () => {
    const c = state.conv; if (!c) return;
    $("#conv-f-title").value = c.title;
    $("#conv-f-cat").value = c.category || "";
    $("#conv-f-delete").hidden = !!c.deleted;
    $("#conv-f-restore").hidden = !c.deleted;
    $("#conv-dialog").showModal();
  };
  $("#conv-f-cancel").onclick = () => $("#conv-dialog").close();
  $("#conv-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      const c = await api(`/api/conversations/${state.conv.id}`, { method: "PATCH", body: JSON.stringify({ title: $("#conv-f-title").value, category: $("#conv-f-cat").value }) });
      Object.assign(state.conv, c); showConvMeta(state.conv);
      $("#conv-dialog").close(); loadConversations();
    } catch (e) { alert(e.message); }
  };
  $("#conv-f-delete").onclick = async () => {
    if (!confirm("Excluir esta thread do sistema? Ela vai para a lixeira e continua salva no banco.")) return;
    try {
      await api(`/api/conversations/${state.conv.id}`, { method: "DELETE" });
      $("#conv-dialog").close();
      state.conv = null;
      const list = await loadConversations();
      if (list.length) await openConversation(list[0].id); else resetChat();
    } catch (e) { alert(e.message); }
  };
  async function restore() {
    try {
      const c = await api(`/api/conversations/${state.conv.id}/restore`, { method: "POST" });
      Object.assign(state.conv, c); showConvMeta(state.conv);
      if ($("#conv-dialog").open) $("#conv-dialog").close();
      loadConversations();
    } catch (e) { alert(e.message); }
  }
  $("#conv-f-restore").onclick = restore;
  $("#trash-restore").onclick = restore;

  function resetChat() {
    state.conv = null; state.commands = {}; state.boxes = {};
    $("#conv-title").textContent = "Nova thread"; $("#conv-edit").hidden = true;
    $("#trash-bar").hidden = true; $("#input").disabled = false; $("#composer").classList.remove("disabled");
    $("#messages").innerHTML = ""; $("#messages").appendChild(emptyState());
    state.choice = defaultChoice(); showChoice();
  }

  async function openConversation(id) {
    const c = await api(`/api/conversations/${id}`);
    state.conv = c; state.commands = {}; state.boxes = {};
    state.choice = choiceFor(c); showChoice();
    for (const r of c.commands) state.commands[r.command] = r;
    showConvMeta(c);
    $("#messages").innerHTML = "";
    if (!c.messages.length) $("#messages").appendChild(emptyState());
    c.messages.forEach(addMessage);
    const pending = c.commands.find((r) => r.status === "pending");
    if (pending && !c.deleted) openApproval(pending, state.boxes[pending.command]);
    loadConversations();
  }

  // category: nome da categoria quando vem do "+" de um grupo; vazio = sem categoria.
  // category: nome da categoria quando vem do "+" de um grupo; vazio = sem categoria.
  async function newConversation(category = NONE) {
    closePanel();
    const want = typeof category === "string" ? category : NONE;
    // Reaproveita a conversa vazia aberta em vez de empilhar várias "Nova thread".
    const reuse = state.conv && !state.conv.deleted && NEW_TITLES.includes(state.conv.title) && !$("#messages .msg");
    if (reuse && (state.conv.category || NONE) === want) { $("#input").focus(); return; }
    const c = reuse ? state.conv : await api("/api/conversations", { method: "POST" });
    if (want || reuse) {
      Object.assign(c, await api(`/api/conversations/${c.id}`, { method: "PATCH", body: JSON.stringify({ category: want }) }));
    }
    if (want) T.expand(want);
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
          else if (ev.type === "message") { dropTyping(); addMessage({ role: "assistant", provider: ev.provider, model: ev.model, content: ev.content }); typing = addMessage({ role: "assistant", provider, typing: true }); }
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
    const { provider } = state.choice;
    if (state.conv && provider) consume(`/api/conversations/${state.conv.id}/continue`, { ...state.choice }, provider);
  }

  $("#composer").onsubmit = async (ev) => {
    ev.preventDefault();
    if (state.busy) return;
    const input = $("#input"); const text = input.value.trim(); if (!text) return;
    const { provider } = state.choice;
    if (!provider) { alert("Nenhum provider disponível. Configure Claude, Codex ou OpenRouter no servidor."); return; }
    if (!state.conv) { const keep = state.choice; await newConversation(); state.choice = keep; showChoice(); }
    if (state.conv.deleted) return;
    input.value = ""; input.style.height = "auto";
    addMessage({ role: "user", content: text });
    if (NEW_TITLES.includes(state.conv.title)) { state.conv.title = text.split("\n")[0].slice(0, 80); $("#conv-title").textContent = state.conv.title; }
    Object.assign(state.conv, state.choice);
    consume(`/api/conversations/${state.conv.id}/messages`, { ...state.choice, content: text }, provider);
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
  (async () => {
    await loadProviders();
    const convs = await loadConversations();
    const params = new URLSearchParams(location.search);
    const wanted = Number(params.get("c"));
    if (params.has("c") || params.has("new")) history.replaceState(null, "", "/");
    if (params.has("new")) await newConversation(params.get("cat") || NONE);
    else if (wanted) await openConversation(wanted);
    else if (convs.length) await openConversation(convs[0].id);
  })().catch((e) => console.error(e));
})();
