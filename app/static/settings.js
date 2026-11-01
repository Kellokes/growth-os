(() => {
  const { $, esc, api } = window.GOS;
  const M = window.GOSModels;

  function usingNow(data, key) {
    const eff = data.effective[key];
    if (!eff?.provider) {
      if (key === "default") return "Nada salvo: usa o provider dos agentes do servidor ou o primeiro disponível.";
      return ["image", "video", "audio"].includes(key) ? "Não configurado." : "Segue o Padrão (que ainda não foi configurado).";
    }
    const from = eff.source === key ? "" : " (vindo do Padrão)";
    return `Usando agora: ${M.describe(eff)}${from}`;
  }

  function card(a) {
    const box = document.createElement("article");
    box.className = "card model-rule";
    box.dataset.key = a.key;
    box.innerHTML = `<header><div class="grow"><h2>${esc(a.label)}</h2><p class="muted small-print">${esc(a.help)}</p></div>
        ${a.key === "default" ? '<span class="tag lime-tag">base</span>' : ""}</header>
      <div data-fields></div>
      <div class="agent-foot"><span class="muted small-print" data-now></span><button class="btn small" type="button" data-save>Salvar</button></div>`;
    return box;
  }

  async function render(data) {
    const text = $("#text-rules"); const media = $("#media-rules");
    text.innerHTML = ""; media.innerHTML = "";
    for (const a of data.activities) {
      const box = card(a);
      (a.kind === "text" ? text : media).appendChild(box);
      const inherit = a.key === "default" ? "Automático" : a.kind === "text" ? "Seguir o padrão" : "Não usar";
      const pick = M.mount(box.querySelector("[data-fields]"), api, { kind: a.kind, inherit, value: data.settings[a.key] || {} });
      box.querySelector("[data-now]").textContent = usingNow(data, a.key);
      box.querySelector("[data-save]").onclick = async (ev) => {
        const btn = ev.currentTarget; btn.disabled = true;
        try {
          await api(`/api/models/${a.key}`, { method: "PUT", body: JSON.stringify(pick.get()) });
          btn.textContent = "Salvo ✓"; setTimeout(() => (btn.textContent = "Salvar"), 1500);
          const fresh = await M.load(api, true);
          document.querySelectorAll(".model-rule").forEach((el) => { el.querySelector("[data-now]").textContent = usingNow(fresh, el.dataset.key); });
        } catch (e) { alert(e.message); } finally { btn.disabled = false; }
      };
    }
    $("#providers").innerHTML = data.providers.map((p) => `<div class="task-row">
        <span class="badge ${p.available ? "done" : "failed"}">${p.available ? "conectado" : "indisponível"}</span>
        <span class="grow"><b>${esc(p.label)}</b> <span class="muted">· ${esc((p.kinds || []).map((k) => ({ text: "texto", image: "imagem", video: "vídeo", audio: "áudio" }[k] || k)).join(", "))}</span></span>
        ${p.available ? "" : `<span class="muted small-print">${esc(p.reason)}</span>`}</div>`).join("");
  }

  M.load(api).then(render).catch((e) => { $("#alerts").innerHTML = `<div class="alert">${esc(e.message)}</div>`; });
})();
