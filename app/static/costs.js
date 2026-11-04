// Custos: totais das chamadas ao OpenRouter por atividade, modelo e dia (GET /api/costs).
(() => {
  const { $, esc, api } = window.GOS;
  const usd = (v) => "US$ " + (v >= 1 ? v.toFixed(2) : v.toFixed(v >= 0.01 ? 3 : 5));
  const num = (v) => (v || 0).toLocaleString("pt-BR");

  function table(rows, key, first) {
    if (!rows.length) return `<p class="muted">Nenhuma chamada ao OpenRouter no período.</p>`;
    const body = rows.map((r) => `<tr><td>${esc(key(r))}</td><td>${r.calls}</td><td>${r.errors}</td>
      <td>${num(r.input_tokens)}</td><td>${num(r.output_tokens)}</td><td><b>${usd(r.cost_usd)}</b></td>
      <td>${r.unpriced ? `${r.unpriced} sem uso registrado` : "—"}</td></tr>`).join("");
    return `<table><thead><tr><th>${first}</th><th>Chamadas</th><th>Erros</th><th>Tokens de entrada</th><th>Tokens de saída</th><th>Custo</th><th>Observação</th></tr></thead><tbody>${body}</tbody></table>`;
  }

  async function load() {
    const d = await api("/api/costs?days=" + $("#costs-days").value);
    const t = d.total;
    const src = d.sources || {};
    const sub = Object.entries(d.subscription_calls || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
    $("#costs-total").innerHTML = `<h2>${usd(t.cost_usd)}</h2>
      <p class="muted">${t.calls} chamada(s) ao OpenRouter nos últimos ${d.days} dias · ${num(t.input_tokens)} tokens de entrada · ${num(t.output_tokens)} de saída.</p>
      <p class="muted small-print">Custo informado pelo OpenRouter: ${src.api || 0} chamada(s) · estimado pelos preços publicados: ${src["estimado"] || 0} · sem uso registrado (anteriores a esta tela, ficam fora do total): ${src["sem uso"] || 0}.${d.prices_live ? "" : " Os preços do OpenRouter não puderam ser lidos agora; as estimativas ficam de fora."}${sub ? ` Sem custo por chamada (CLI): ${esc(sub)}.` : ""}</p>`;
    $("#costs-activity").innerHTML = table(d.by_activity, (r) => r.label, "Atividade");
    $("#costs-model").innerHTML = table(d.by_model, (r) => r.model, "Modelo");
    $("#costs-day").innerHTML = table(d.by_day.slice().reverse(), (r) => r.day, "Dia (UTC)");
  }

  $("#costs-days").addEventListener("change", () => load().catch(() => {}));
  load().catch((e) => { $("#costs-total").innerHTML = `<p class="alert">${esc(e.message || "Não consegui carregar os custos.")}</p>`; });
})();
