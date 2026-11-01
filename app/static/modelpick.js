// Seletor de provider + modelo + raciocínio, usado nas threads, nos agentes e na tela de modelos.
window.GOSModels = (() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let data = null;
  const orCache = {};
  let seq = 0;

  async function load(api, force = false) {
    if (!data || force) data = await api("/api/models");
    return data;
  }
  const prov = (id) => data?.providers.find((p) => p.id === id);
  const short = (label) => String(label || "").replace(/\s*\(.*\)$/, "");

  async function suggestions(api, pid, kind) {
    const p = prov(pid);
    if (!p) return [];
    const local = (p.models || []).filter((m) => !m.kinds || m.kinds.includes(kind));
    if (pid !== "openrouter") return local;
    if (!orCache[kind]) orCache[kind] = api(`/api/models/openrouter?kind=${encodeURIComponent(kind)}`).catch(() => local);
    return orCache[kind];
  }

  // "Claude · opus · Alto" (curto, para botões e resumos)
  function describe(c) {
    if (!c || !c.provider) return "";
    const p = prov(c.provider);
    const r = p?.reasoning?.find((x) => x.id === c.reasoning);
    return [short(p?.label || c.provider), c.model || "modelo padrão", r ? `raciocínio ${r.label.toLowerCase()}` : ""].filter(Boolean).join(" · ");
  }

  // Monta os três campos em `root`. opts: { kind, value, inherit (texto da opção "seguir o padrão", ou null), onChange }
  function mount(root, api, opts = {}) {
    const kind = opts.kind || "text";
    const id = `mp-dl-${++seq}`;
    root.classList.add("mpick");
    root.innerHTML = `
      <label>Provider <select data-mp-provider></select></label>
      <label>Modelo <input data-mp-model list="${id}" autocomplete="off" spellcheck="false" maxlength="120"></label>
      <datalist id="${id}"></datalist>
      <label>Raciocínio <select data-mp-reasoning></select></label>`;
    const selP = root.querySelector("[data-mp-provider]");
    const inM = root.querySelector("[data-mp-model]");
    const selR = root.querySelector("[data-mp-reasoning]");
    const dl = root.querySelector("datalist");

    const usable = data.providers.filter((p) => (p.kinds || ["text"]).includes(kind));
    const fillProviders = (current) => {
      const head = opts.inherit != null ? `<option value="">${esc(opts.inherit)}</option>` : "";
      selP.innerHTML = head + usable.map((p) => {
        const off = !p.available && p.id !== current;
        return `<option value="${esc(p.id)}" ${off ? "disabled" : ""}>${esc(short(p.label))}${p.available ? "" : ` (${esc(p.reason || "indisponível")})`}</option>`;
      }).join("");
      if (!usable.length) selP.innerHTML = head || '<option value="">Nenhum provider faz isso ainda</option>';
    };
    const fillDependent = async (keep = {}) => {
      const p = prov(selP.value);
      const on = !!p;
      inM.disabled = selR.disabled = !on;
      inM.placeholder = !on ? "—" : p.id === "openrouter" ? `Padrão: ${data.openrouter_default}` : "Padrão da sua conta";
      selR.innerHTML = '<option value="">Padrão do modelo</option>' + (p?.reasoning || []).map((r) => `<option value="${esc(r.id)}">${esc(r.label)}</option>`).join("");
      selR.value = on && (p.reasoning || []).some((r) => r.id === keep.reasoning) ? keep.reasoning : "";
      inM.value = on ? (keep.model || "") : "";
      dl.innerHTML = "";
      if (on) {
        const list = await suggestions(api, p.id, kind);
        if (selP.value === p.id) dl.innerHTML = list.map((m) => `<option value="${esc(m.id)}">${esc(m.label || m.id)}</option>`).join("");
      }
    };
    const get = () => ({ provider: selP.value, model: selP.value ? inM.value.trim() : "", reasoning: selP.value ? selR.value : "" });
    const set = async (v = {}) => {
      fillProviders(v.provider || "");
      const want = v.provider || "";
      const exists = [...selP.options].some((o) => o.value === want);
      selP.value = exists ? want : (opts.inherit != null ? "" : (usable.find((p) => p.available)?.id || ""));
      await fillDependent(selP.value === want ? v : {});
    };
    const changed = () => opts.onChange && opts.onChange(get());
    selP.addEventListener("change", async () => { await fillDependent({}); changed(); });
    inM.addEventListener("change", changed);
    selR.addEventListener("change", changed);
    const ready = set(opts.value || {});
    return { get, set, ready, el: root };
  }

  return { load, mount, describe, prov, data: () => data };
})();
