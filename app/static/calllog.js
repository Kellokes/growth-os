// Rodapé com o log das chamadas em segundo plano (IA, imagem, renderização). Lê GET /api/calls.
(() => {
  const $ = (s) => document.querySelector(s);
  const root = $("#calllog");
  if (!root || !window.GOS) return;
  const { api } = window.GOS;
  let open = false, filter = "", timer = null, openId = null;

  const when = (iso) => new Date(iso + "Z").toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const dur = (ms) => ms == null ? "…" : ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`;
  const STATUS = { ok: "ok", error: "erro", running: "em andamento" };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function row(c) {
    const item = el("div", `calllog-row ${c.status}${c.id === openId ? " open" : ""}`);
    item.setAttribute("role", "listitem");
    const head = el("button", "calllog-head");
    head.type = "button";
    head.append(
      el("span", `calllog-st ${c.status}`, STATUS[c.status] || c.status),
      el("span", "calllog-act", c.activity_label || c.activity),
      el("span", "calllog-who", [c.provider, c.model].filter(Boolean).join(" · ") || "—"),
      el("span", "calllog-proj muted", c.project || ""),
      el("span", "calllog-time muted", when(c.at)),
      el("span", "calllog-dur muted", dur(c.duration_ms)),
    );
    head.onclick = () => { openId = openId === c.id ? null : c.id; draw(last); };
    item.append(head);
    if (c.error) item.append(el("div", "calllog-err", c.error));
    if (c.id === openId) {
      const d = el("dl", "calllog-det");
      for (const [k, v] of [["Pedido", c.request], ["Resposta", c.response], ["Quem", c.actor]]) {
        if (!v) continue;
        d.append(el("dt", null, k), el("dd", null, v));
      }
      item.append(d);
    }
    return item;
  }

  let last = null;
  function draw(data) {
    if (!data) return;
    last = data;
    const dot = $("#calllog-dot");
    dot.className = "calllog-dot" + (data.running ? " run" : data.failed_24h ? " err" : "");
    const bits = [];
    if (data.running) bits.push(`${data.running} em andamento`);
    if (data.failed_24h) bits.push(`${data.failed_24h} com erro nas últimas 24 h`);
    $("#calllog-sum").textContent = bits.join(" · ") || (data.calls.length ? "tudo certo" : "nenhuma chamada ainda");
    const top = data.calls[0];
    $("#calllog-last").textContent = top ? `${top.activity_label || top.activity} · ${STATUS[top.status] || top.status}` : "";
    if (!open) return;
    const box = $("#calllog-list");
    box.replaceChildren();
    if (!data.calls.length) box.append(el("p", "muted calllog-empty", "Nada por aqui ainda. Quando o sistema chamar uma IA ou renderizar uma peça, aparece aqui."));
    for (const c of data.calls) box.append(row(c));
  }

  async function load() {
    try { draw(await api("/api/calls?limit=100" + (filter ? `&status=${filter}` : ""))); } catch { /* sessão expirada já redireciona */ }
  }

  function schedule() {
    clearTimeout(timer);
    if (document.hidden) return;
    timer = setTimeout(async () => { await load(); schedule(); }, open ? 4000 : 20000);
  }

  $("#calllog-toggle").onclick = () => {
    open = !open;
    $("#calllog-toggle").setAttribute("aria-expanded", String(open));
    $("#calllog-panel").hidden = !open;
    root.classList.toggle("open", open);
    load(); schedule();
  };
  $("#calllog-clear").onclick = async () => {
    const n = last ? last.failed_total : 0;
    if (!n) { alert("Não há erros no log."); return; }
    if (!confirm(`Apagar ${n} chamada(s) com erro do log?`)) return;
    try { await api("/api/calls?status=error", { method: "DELETE" }); } catch (e) { alert(e.message); return; }
    openId = null; load();
  };
  $("#calllog-filters").onclick = (e) => {
    const b = e.target.closest("[data-f]");
    if (!b) return;
    filter = b.dataset.f;
    document.querySelectorAll("#calllog-filters .btn").forEach((x) => x.classList.toggle("on", x === b));
    load();
  };
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { load(); schedule(); } });
  load(); schedule();
})();
