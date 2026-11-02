// Configurações: resetar dados. A página só mostra a prévia; apagar exige RESETAR + código 2FA.
(() => {
  const { $, esc, api } = window.GOS;
  let word = "RESETAR";
  let busy = false;

  const row = (r) => `<li><span>${esc(r.label)}</span><b>${r.count}</b></li>`;

  function ready() {
    const ok = !busy && $("#reset-confirm").value.trim() === word && /^[0-9]{6,8}$/.test($("#reset-totp").value.trim());
    $("#reset-go").disabled = !ok;
  }

  async function load() {
    const p = await api("/api/reset/preview");
    word = p.word;
    $("#reset-word").textContent = word;
    $("#reset-clear").innerHTML = p.clear.map(row).join("");
    $("#reset-keep").innerHTML = p.keep.map(row).join("");
    busy = !!p.busy;
    const b = $("#reset-busy");
    b.hidden = !busy;
    if (busy) {
      const items = (p.busy.items || []).map((i) => {
        const link = i.conversation_id ? ` · <a href="/?c=${i.conversation_id}">abrir thread</a>` : "";
        return `<li>${esc(i.kind)} #${i.id}: <code>${esc(i.label)}</code> (desde ${esc(new Date(i.since + "Z").toLocaleString("pt-BR"))})${link}</li>`;
      }).join("");
      b.innerHTML = `Tem trabalho rodando agora. Espere terminar para resetar.<ul class="reset-busy">${items}</ul>
        Se nada está rodando de verdade, é sobra de um restart do app: reiniciar o Growth OS marca esses comandos como interrompidos.`;
    }
    ready();
  }

  $("#reset-confirm").addEventListener("input", ready);
  $("#reset-totp").addEventListener("input", ready);
  $("#reset-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("#reset-go");
    btn.disabled = true;
    try {
      const r = await api("/api/reset", { method: "POST", body: JSON.stringify({ confirm: $("#reset-confirm").value, totp: $("#reset-totp").value.trim() }) });
      const total = Object.values(r.deleted).reduce((a, b) => a + b, 0);
      const done = $("#reset-done");
      done.hidden = false;
      done.textContent = `Pronto: ${total} registro(s) apagado(s). A configuração ficou como estava.`;
      $("#reset-form").reset();
      await load();
      window.GOS.threads?.reload?.();
    } catch (e) {
      $("#alerts").innerHTML = `<div class="alert">${esc(e.message)}</div>`;
      ready();
    }
  });

  load().catch((e) => { $("#alerts").innerHTML = `<div class="alert">${esc(e.message)}</div>`; });
})();
