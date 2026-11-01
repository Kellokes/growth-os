(() => {
  const { $, esc, api, fmt, initProjects } = window.GOS;
  let project = null; let projectInfo = null;
  const SVG = "http://www.w3.org/2000/svg";
  let metrics = []; let agents = {}; let days = 90; let selected = null;
  try { days = Number(localStorage.getItem("gos.days")) || 90; } catch { /* sem storage */ }

  // Variação entre o último e o penúltimo ponto; a cor diz se foi bom ou ruim para este KPI.
  function delta(m) {
    if (m.last == null || m.prev == null || m.prev === 0) return null;
    const pct = ((m.last - m.prev) / Math.abs(m.prev)) * 100;
    const good = m.direction === "up" ? pct >= 0 : pct <= 0;
    return { pct, good };
  }
  const deltaHtml = (m) => {
    const d = delta(m);
    if (!d) return "";
    return `<span class="delta ${d.good ? "good" : "bad"}">${d.pct >= 0 ? "▲" : "▼"} ${Math.abs(d.pct).toFixed(1)}%</span>`;
  };

  // "90.744" vira 90<span>.744</span>: a parte final fica mais clara, como nos números grandes do layout.
  const big = (txt) => {
    const m = /^(.*\d)([.,]\d+)(\D*)$/.exec(txt);
    return m ? `${esc(m[1])}<span class="dim">${esc(m[2])}</span>${esc(m[3])}` : esc(txt);
  };

  function spark(points) {
    if (points.length < 2) return '<div class="spark empty"></div>';
    const vals = points.map((p) => p.value); const min = Math.min(...vals); const max = Math.max(...vals);
    const w = 120, h = 34, r = max - min || 1;
    const d = vals.map((v, i) => `${i ? "L" : "M"}${(i / (vals.length - 1)) * w},${h - 3 - ((v - min) / r) * (h - 6)}`).join("");
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${d}" pathLength="1"/></svg>`;
  }

  function node(tag, attrs = {}, parent) {
    const n = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  }

  function niceTicks(min, max, count = 4) {
    const span = max - min || Math.abs(max) || 1;
    const step0 = span / count; const mag = 10 ** Math.floor(Math.log10(step0));
    const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((s) => s >= step0);
    const lo = Math.floor(min / step) * step; const hi = Math.ceil(max / step) * step;
    const out = []; for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function drawChart(m) {
    const box = $("#chart"); box.innerHTML = "";
    if (!m || m.points.length === 0) {
      box.innerHTML = `<div class="chart-empty"><p>Ainda não há dados de <b>${esc(m?.name || "KPI")}</b>.</p><button class="btn small" type="button" id="empty-add">+ Lançar o primeiro número</button></div>`;
      $("#empty-add").onclick = () => openPoint(m?.key);
      return;
    }
    const W = box.clientWidth || 800, H = 280, pad = { l: 56, r: 16, t: 14, b: 28 };
    const svg = node("svg", { viewBox: `0 0 ${W} ${H}`, class: "line-chart", role: "img", "aria-label": `Gráfico de ${m.name}` }, box);
    const pts = m.points.map((p) => ({ t: new Date(p.day + "T12:00:00").getTime(), v: p.value, day: p.day }));
    const vals = pts.map((p) => p.v).concat(m.target != null ? [m.target] : []);
    const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const t0 = pts[0].t, t1 = pts.length > 1 ? pts[pts.length - 1].t : t0 + 864e5;
    const X = (t) => pad.l + ((t - t0) / (t1 - t0 || 1)) * (W - pad.l - pad.r);
    const Y = (v) => pad.t + (1 - (v - y0) / (y1 - y0 || 1)) * (H - pad.t - pad.b);

    for (const tv of ticks) {
      node("line", { x1: pad.l, x2: W - pad.r, y1: Y(tv), y2: Y(tv), class: "grid" }, svg);
      node("text", { x: pad.l - 8, y: Y(tv) + 4, class: "axis", "text-anchor": "end" }, svg).textContent = fmt(tv, m.unit);
    }
    const nX = Math.min(W < 520 ? 3 : 6, pts.length);
    for (let i = 0; i < nX; i++) {
      const p = pts[Math.round((i / Math.max(1, nX - 1)) * (pts.length - 1))];
      node("text", { x: X(p.t), y: H - 8, class: "axis", "text-anchor": i === 0 ? "start" : i === nX - 1 ? "end" : "middle" }, svg)
        .textContent = new Date(p.t).toLocaleDateString("pt-BR", { day: "2-digit", month: "short" });
    }
    if (m.target != null) {
      node("line", { x1: pad.l, x2: W - pad.r, y1: Y(m.target), y2: Y(m.target), class: "target" }, svg);
      node("text", { x: W - pad.r, y: Y(m.target) - 6, class: "axis target-label", "text-anchor": "end" }, svg).textContent = `meta ${fmt(m.target, m.unit)}`;
    }
    const line = pts.map((p, i) => `${i ? "L" : "M"}${X(p.t)},${Y(p.v)}`).join("");
    const defs = node("defs", {}, svg);
    const grad = node("linearGradient", { id: "area-grad", x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
    node("stop", { offset: "0%", class: "grad-top" }, grad); node("stop", { offset: "100%", class: "grad-bottom" }, grad);
    // Área listrada (hachura) por cima do degradê, como no layout de referência.
    const hatch = node("pattern", { id: "area-hatch", width: 8, height: 8, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
    node("line", { x1: 0, y1: 0, x2: 0, y2: 8, class: "hatch-line" }, hatch);
    if (pts.length > 1) {
      const area = `${line}L${X(pts[pts.length - 1].t)},${Y(y0)}L${X(pts[0].t)},${Y(y0)}Z`;
      node("path", { d: area, class: "area", fill: "url(#area-grad)" }, svg);
      node("path", { d: area, class: "area hatch", fill: "url(#area-hatch)" }, svg);
    }
    node("path", { d: line, class: "line", pathLength: 1 }, svg);
    const last = pts[pts.length - 1];
    node("circle", { cx: X(last.t), cy: Y(last.v), r: 4.5, class: "last-dot" }, svg);

    // Hover: linha vertical + ponto + tooltip no ponto mais próximo.
    const cross = node("line", { y1: pad.t, y2: H - pad.b, class: "cross", visibility: "hidden" }, svg);
    const dot = node("circle", { r: 5, class: "hover-dot", visibility: "hidden" }, svg);
    const tip = document.createElement("div"); tip.className = "tip"; tip.hidden = true; box.appendChild(tip);
    const hit = node("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.t - pad.b, fill: "transparent" }, svg);
    const move = (ev) => {
      const rect = svg.getBoundingClientRect();
      const x = ((ev.clientX - rect.left) / rect.width) * W;
      let best = pts[0]; for (const p of pts) if (Math.abs(X(p.t) - x) < Math.abs(X(best.t) - x)) best = p;
      const cx = X(best.t), cy = Y(best.v);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.innerHTML = `<span>${new Date(best.t).toLocaleDateString("pt-BR", { weekday: "short", day: "2-digit", month: "short" })}</span><b>${esc(fmt(best.v, m.unit))}</b>`;
      const left = (cx / W) * rect.width; tip.style.left = `${Math.min(Math.max(left, 60), rect.width - 60)}px`;
      tip.style.top = `${(cy / H) * rect.height - 52}px`;
    };
    hit.addEventListener("pointermove", move); hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; });
  }

  function selectMetric(key) {
    selected = key; try { localStorage.setItem(`gos.kpi.${project}`, key); } catch { /* ok */ }
    const m = metrics.find((x) => x.key === key) || metrics[0];
    const a = agents[m.area];
    $("#hero-area").textContent = a ? `${a.icon} ${a.name}` : m.area;
    $("#hero-name").textContent = m.name;
    $("#hero-value").innerHTML = big(fmt(m.last, m.unit));
    $("#hero-delta").innerHTML = deltaHtml(m) + (m.target != null && m.last != null ? `<span class="muted"> · ${Math.round((m.last / m.target) * 100)}% da meta</span>` : "");
    drawChart(m);
    document.querySelectorAll(".kpi").forEach((el) => el.classList.toggle("on", el.dataset.key === m.key));
  }

  function render() {
    const groups = {};
    for (const m of metrics) (groups[m.area] ||= []).push(m);
    const order = Object.keys(agents);
    const areas = Object.keys(groups).sort((a, b) => (order.indexOf(a) + 99) % 99 - (order.indexOf(b) + 99) % 99);
    $("#areas").innerHTML = areas.map((area) => {
      const a = agents[area];
      return `<h3 class="section-title">${a ? `${esc(a.icon)} ${esc(a.name)}` : esc(area)}</h3><div class="kpis">${groups[area].map((m) => `
        <button class="kpi card ${m.points.length ? "" : "nodata"}" type="button" data-key="${esc(m.key)}">
          <span class="kpi-name">${esc(m.name)}</span>
          <span class="kpi-row"><strong>${big(fmt(m.last, m.unit))}</strong>${deltaHtml(m)}</span>
          ${m.points.length ? spark(m.points) : '<span class="muted kpi-empty">sem dados · lançar</span>'}
        </button>`).join("")}</div>`;
    }).join("");
    $("#table").innerHTML = `<table><thead><tr><th>KPI</th><th>Área</th><th>Último</th><th>Anterior</th><th>Meta</th><th>Pontos</th></tr></thead><tbody>${metrics.map((m) =>
      `<tr><td>${esc(m.name)}</td><td>${esc(m.area)}</td><td>${esc(fmt(m.last, m.unit))}</td><td>${esc(fmt(m.prev, m.unit))}</td><td>${esc(fmt(m.target, m.unit))}</td><td>${m.points.length}</td></tr>`).join("")}</tbody></table>`;
    const withData = metrics.find((m) => m.points.length);
    selectMetric(metrics.some((m) => m.key === selected) ? selected : (withData || metrics[0]).key);
  }

  async function renderOps() {
    const [plan, proj] = await Promise.allSettled([api("/api/plans/today"), api("/api/projects")]);
    const items = [];
    if (plan.status === "fulfilled") {
      const t = plan.value.tasks.filter((x) => x.project === project);
      const c = (s) => t.filter((x) => x.status === s).length;
      const st = plan.value.plan?.status;
      items.push(["Plano de hoje", st === "ready" ? `${c("proposed")} para decidir` : st === "generating" ? "gerando…" : "não gerado", "/plan", c("proposed") ? "warn" : "ok"]);
      items.push(["Tarefas concluídas hoje", String(c("done")), "/plan", "ok"]);
      const waiting = t.concat(plan.value.carryover.filter((x) => x.project === project)).filter((x) => x.status === "waiting").length;
      items.push(["Esperando seu 2FA", String(waiting), "/plan", waiting ? "warn" : "ok"]);
    }
    if (proj.status === "fulfilled") {
      const mine = proj.value.projects.filter((p) => p.name === projectInfo?.compose_project);
      const all = mine.flatMap((p) => p.containers);
      const bad = all.filter((c) => c.state !== "running" || c.health === "unhealthy").length;
      if (!projectInfo?.compose_project) items.push(["Containers", "não ligado ao VPS", "/agents", "warn"]);
      else if (proj.value.errors.length && !all.length) items.push(["Containers", "gateway indisponível", "/projects", "down"]);
      else items.push(["Containers", bad ? `${bad} com problema` : `${all.length} no ar`, "/projects", bad ? "down" : "ok"]);
    }
    $("#ops").innerHTML = items.map(([k, v, href, st]) => `<a class="op card" href="${href}"><i class="dot ${st}"></i><span>${esc(k)}</span><b>${esc(v)}</b></a>`).join("");
  }

  async function load() {
    metrics = await api(`/api/gprojects/${project}/metrics?days=${days}`);
    render();
  }

  function openPoint(key) {
    $("#point-key").innerHTML = metrics.map((m) => `<option value="${esc(m.key)}">${esc(m.name)}</option>`).join("");
    $("#point-key").value = key || selected || metrics[0]?.key;
    $("#point-day").value = new Date(Date.now() - new Date().getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
    $("#point-value").value = ""; $("#point-note").value = "";
    const m = metrics.find((x) => x.key === $("#point-key").value); $("#point-target").value = m?.target ?? "";
    $("#point-dialog").showModal(); $("#point-value").focus();
  }

  $("#point-key").onchange = () => { const m = metrics.find((x) => x.key === $("#point-key").value); $("#point-target").value = m?.target ?? ""; };
  $("#add-point").onclick = () => openPoint();
  $("#point-cancel").onclick = () => $("#point-dialog").close();
  $("#point-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const key = $("#point-key").value; const m = metrics.find((x) => x.key === key);
    try {
      await api(`/api/gprojects/${project}/metrics/${key}/points`, { method: "POST", body: JSON.stringify({ value: Number($("#point-value").value), day: $("#point-day").value, note: $("#point-note").value }) });
      const target = $("#point-target").value === "" ? null : Number($("#point-target").value);
      if (target !== m.target) await api(`/api/gprojects/${project}/metrics`, { method: "POST", body: JSON.stringify({ key, name: m.name, area: m.area, unit: m.unit, direction: m.direction, target }) });
      $("#point-dialog").close(); selected = key; load();
    } catch (e) { alert(e.message); }
  };
  $("#new-kpi").onclick = () => {
    $("#kpi-area").innerHTML = Object.values(agents).map((a) => `<option value="${esc(a.slug)}">${esc(a.icon)} ${esc(a.name)}</option>`).join("");
    $("#point-dialog").close(); $("#kpi-dialog").showModal();
  };
  $("#kpi-name").oninput = () => {
    $("#kpi-key").value = $("#kpi-name").value.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^(\d)/, "k_$1").slice(0, 48);
  };
  $("#kpi-cancel").onclick = () => $("#kpi-dialog").close();
  $("#kpi-form").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await api(`/api/gprojects/${project}/metrics`, { method: "POST", body: JSON.stringify({ key: $("#kpi-key").value, name: $("#kpi-name").value, area: $("#kpi-area").value, unit: $("#kpi-unit").value, direction: $("#kpi-dir").value }) });
      selected = $("#kpi-key").value; $("#kpi-dialog").close(); await load(); openPoint(selected);
    } catch (e) { alert(e.message); }
  };
  $("#areas").addEventListener("click", (ev) => {
    const k = ev.target.closest(".kpi"); if (!k) return;
    const m = metrics.find((x) => x.key === k.dataset.key);
    if (!m.points.length) return openPoint(m.key);
    selectMetric(m.key); $("#hero").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  document.querySelectorAll("#range button").forEach((b) => {
    b.classList.toggle("on", Number(b.dataset.days) === days);
    b.onclick = () => {
      days = Number(b.dataset.days); try { localStorage.setItem("gos.days", days); } catch { /* ok */ }
      document.querySelectorAll("#range button").forEach((x) => x.classList.toggle("on", x === b)); load();
    };
  });
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => selectMetric(selected), 150); });

  async function onProject(slug, info) {
    project = slug; projectInfo = info;
    $("#page-crumb").textContent = info ? info.name : "Dashboard";
    document.querySelector(".page").hidden = !slug;
    if (!slug) return;
    try { selected = localStorage.getItem(`gos.kpi.${slug}`); } catch { selected = null; }
    agents = {};
    for (const a of await api(`/api/gprojects/${slug}/agents`)) agents[a.slug] = a;
    await load();
    renderOps().catch(() => {});
  }

  initProjects(onProject).catch((e) => { $("#areas").innerHTML = `<div class="alert">${esc(e.message)}</div>`; });
})();
