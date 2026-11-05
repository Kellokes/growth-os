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

  // Gráfico de barras por dia em SVG (preenche dias sem chamadas com zero).
  function dayChart(rows, days) {
    const map = Object.fromEntries(rows.map((r) => [r.day, r.cost_usd]));
    const pts = [];
    const now = new Date();
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
      const k = d.toISOString().slice(0, 10);
      pts.push({ k, v: map[k] || 0 });
    }
    const max = Math.max(...pts.map((p) => p.v), 0);
    if (!max) return `<p class="muted">Sem custo registrado no período.</p>`;
    const W = 600, H = 170, L = 8, B = 22, T = 8, bw = (W - L * 2) / pts.length;
    const bars = pts.map((p, i) => {
      const h = Math.max(p.v ? 2 : 0, (p.v / max) * (H - B - T));
      return `<rect class="bar" x="${(L + i * bw + bw * 0.14).toFixed(1)}" y="${(H - B - h).toFixed(1)}" width="${(bw * 0.72).toFixed(1)}" height="${h.toFixed(1)}" rx="2"><title>${p.k}: ${usd(p.v)}</title></rect>`;
    }).join("");
    const step = Math.ceil(pts.length / 6);
    const ticks = pts.map((p, i) => (i % step === 0 || i === pts.length - 1) && (pts.length - 1 - i === 0 || pts.length - 1 - i >= step / 2)
      ? `<text class="tick" x="${(L + i * bw + bw / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle">${p.k.slice(8)}/${p.k.slice(5, 7)}</text>` : "").join("");
    return `<p class="muted small-print">Máximo em um dia: <b>${usd(max)}</b></p>
      <svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Custo por dia">
      <line class="axis" x1="${L}" x2="${W - L}" y1="${H - B}" y2="${H - B}"/>${bars}${ticks}</svg>`;
  }

  // Barras horizontais por atividade (HTML, quebra bem no celular).
  function actChart(rows) {
    const rs = rows.filter((r) => r.cost_usd > 0).sort((a, b) => b.cost_usd - a.cost_usd);
    if (!rs.length) return `<p class="muted">Sem custo registrado no período.</p>`;
    const max = rs[0].cost_usd;
    return `<div class="hbars">` + rs.map((r) => `<div class="hbar"><div class="hbar-top"><span>${esc(r.label)}</span><b>${usd(r.cost_usd)}</b></div>
      <div class="hbar-track"><div class="hbar-fill" style="width:${Math.max(2, (r.cost_usd / max) * 100).toFixed(1)}%"></div></div></div>`).join("") + `</div>`;
  }

  async function load() {
    const d = await api("/api/costs?days=" + $("#costs-days").value);
    const t = d.total;
    const src = d.sources || {};
    const sub = Object.entries(d.subscription_calls || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
    $("#costs-total").innerHTML = `<h2>${usd(t.cost_usd)}</h2>
      <p class="muted">${t.calls} chamada(s) ao OpenRouter nos últimos ${d.days} dias · ${num(t.input_tokens)} tokens de entrada · ${num(t.output_tokens)} de saída.</p>
      <p class="muted small-print">Custo informado pelo OpenRouter: ${src.api || 0} chamada(s) · estimado pelos preços publicados: ${src["estimado"] || 0} · sem uso registrado (anteriores a esta tela, ficam fora do total): ${src["sem uso"] || 0}.${d.prices_live ? "" : " Os preços do OpenRouter não puderam ser lidos agora; as estimativas ficam de fora."}${sub ? ` Sem custo por chamada (CLI): ${esc(sub)}.` : ""}</p>`;
    $("#costs-chart-day").innerHTML = dayChart(d.by_day, d.days);
    $("#costs-chart-act").innerHTML = actChart(d.by_activity);
    $("#costs-activity").innerHTML = table(d.by_activity, (r) => r.label, "Atividade");
    $("#costs-model").innerHTML = table(d.by_model, (r) => r.model, "Modelo");
    $("#costs-day").innerHTML = table(d.by_day.slice().reverse(), (r) => r.day, "Dia (UTC)");
  }

  $("#costs-days").addEventListener("change", () => load().catch(() => {}));
  load().catch((e) => { $("#costs-total").innerHTML = `<p class="alert">${esc(e.message || "Não consegui carregar os custos.")}</p>`; });
})();
