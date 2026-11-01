(() => {
  const $ = (s) => document.querySelector(s);
  const CSRF = $("#app").dataset.csrf;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const state = { logs: null, restart: null, domains: {} };

  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF } });
    if (res.status === 401) { location.href = "/login"; throw new Error("sessão expirada"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `erro ${res.status}`);
    return data;
  }

  const LABEL = { ok: "no ar", warn: "atenção", down: "fora do ar" };
  const dot = (c) => {
    const st = c.state !== "running" ? "down" : c.health === "unhealthy" ? "down" : c.health === "starting" ? "warn" : "ok";
    return `<i class="dot ${st}" title="${esc(c.status)}"></i>`;
  };

  function domainChip(host) {
    const d = state.domains[host];
    const cls = !d ? "pending" : d.ok ? "ok" : "down";
    const label = !d ? "…" : d.code ? d.code : (d.error || "erro");
    return `<a class="domain ${cls}" href="https://${esc(host)}" target="_blank" rel="noopener noreferrer"><i class="dot ${cls}"></i>${esc(host)}<b>${esc(label)}</b></a>`;
  }

  function render(data) {
    const all = data.projects.flatMap((p) => p.containers);
    const running = all.filter((c) => c.state === "running").length;
    const sick = all.filter((c) => c.health === "unhealthy" || c.state !== "running").length;
    const hosts = data.projects.flatMap((p) => p.sites.flatMap((s) => s.hosts));
    const realProjects = data.projects.filter((p) => !["avulsos", "_caddy"].includes(p.name)).length;
    $("#summary").innerHTML = [
      ["Projetos", realProjects], ["Containers rodando", `${running}/${all.length}`],
      ["Com problema", sick], ["Domínios", hosts.length],
    ].map(([k, v]) => `<div class="stat card"><span>${esc(k)}</span><strong class="${k === "Com problema" && v ? "bad" : ""}">${esc(v)}</strong></div>`).join("");
    $("#alerts").innerHTML = data.errors.map((e) => `<div class="alert">${esc(e)}</div>`).join("");

    const box = $("#projects"); box.innerHTML = "";
    if (!data.projects.length) { box.innerHTML = '<div class="muted">Nada encontrado ainda.</div>'; return; }
    for (const p of data.projects) {
      const card = document.createElement("article");
      card.className = "project card";
      const title = p.name === "_caddy" ? "Domínios sem container conhecido" : p.name === "avulsos" ? "Containers avulsos" : p.name;
      const sites = p.sites.map((s) => `<div class="site">${s.hosts.map(domainChip).join("")}<span class="muted">${s.redirect ? `↪ ${esc(s.redirect)}` : s.upstream ? `→ ${esc(s.upstream)}` : ""}</span></div>`).join("");
      const rows = p.containers.map((c) => `
        <div class="ctr">
          <div class="ctr-main">${dot(c)}<div><strong>${esc(c.service || c.name)}</strong><span class="muted">${esc(c.name)} · ${esc(c.image)}</span></div></div>
          <div class="ctr-status muted">${esc(c.status)}</div>
          <div class="ctr-actions">
            <button class="btn small" type="button" data-logs="${esc(c.name)}">Logs</button>
            <button class="btn small danger" type="button" data-restart="${esc(c.name)}">Reiniciar</button>
          </div>
        </div>`).join("");
      card.innerHTML = `
        <header><h2>${esc(title)}</h2>${p.containers.length ? `<span class="badge ${p.status}">${LABEL[p.status]}</span>` : ""}</header>
        ${sites ? `<div class="sites">${sites}</div>` : ""}
        ${rows ? `<div class="ctrs">${rows}</div>` : ""}`;
      box.appendChild(card);
    }
  }

  let last = null;
  async function load() {
    $("#refresh").disabled = true;
    try {
      last = await api("/api/projects"); render(last);
      state.domains = await api("/api/projects/domains"); render(last);
    } catch (e) {
      $("#alerts").innerHTML = `<div class="alert">${esc(e.message)}</div>`;
    } finally { $("#refresh").disabled = false; }
  }

  async function showLogs(name) {
    state.logs = name;
    $("#logs-title").textContent = `Logs · ${name}`;
    $("#logs-body").textContent = "Carregando…";
    if (!$("#logs-dialog").open) $("#logs-dialog").showModal();
    try {
      const r = await api(`/api/containers/${encodeURIComponent(name)}/logs?tail=${$("#logs-tail").value}`);
      $("#logs-body").textContent = r.logs || "(sem logs)";
      $("#logs-body").scrollTop = $("#logs-body").scrollHeight;
    } catch (e) { $("#logs-body").textContent = `Erro: ${e.message}`; }
  }

  $("#projects").addEventListener("click", (ev) => {
    const l = ev.target.closest("[data-logs]"); if (l) return showLogs(l.dataset.logs);
    const r = ev.target.closest("[data-restart]");
    if (r) {
      state.restart = r.dataset.restart;
      $("#restart-name").textContent = state.restart; $("#restart-totp").value = "";
      $("#restart-dialog").showModal(); $("#restart-totp").focus();
    }
  });
  $("#logs-tail").onchange = () => state.logs && showLogs(state.logs);
  $("#restart-cancel").onclick = () => $("#restart-dialog").close();
  $("#restart-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = ev.target.querySelector("button[type=submit]"); btn.disabled = true;
    try {
      await api(`/api/containers/${encodeURIComponent(state.restart)}/restart`, { method: "POST", body: JSON.stringify({ totp: $("#restart-totp").value }) });
      $("#restart-dialog").close(); setTimeout(load, 1500);
    } catch (e) { alert(e.message); } finally { btn.disabled = false; }
  };
  $("#refresh").onclick = load;
  load();
})();
