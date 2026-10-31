// Partes comuns das páginas logadas: sair e auditoria.
(() => {
  const $ = (s) => document.querySelector(s);
  const CSRF = $("#app").dataset.csrf;
  const call = async (path, method = "GET") => {
    const res = await fetch(path, { method, credentials: "same-origin", headers: { "X-CSRF-Token": CSRF } });
    if (res.status === 401) { location.href = "/login"; throw new Error("sessão expirada"); }
    return res.json().catch(() => ({}));
  };
  $("#logout").onclick = async () => { await call("/logout", "POST").catch(() => {}); location.href = "/login"; };
  $("#show-audit").onclick = async () => {
    const rows = await call("/api/audit");
    const box = $("#audit-list"); box.innerHTML = "";
    for (const r of rows) {
      const d = document.createElement("div");
      for (const v of [new Date(r.at + "Z").toLocaleString("pt-BR"), r.action, r.detail]) {
        const s = document.createElement("span"); s.textContent = v; s.title = v; d.appendChild(s);
      }
      box.appendChild(d);
    }
    $("#audit-dialog").showModal();
  };
})();
