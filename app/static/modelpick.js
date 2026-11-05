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

  // Lista do OpenRouter com preços: { live, updated_at, models: [{ id, label, kinds, inputs, pricing, free }] }
  function openrouter(api, kind) {
    if (!orCache[kind]) {
      const local = (prov("openrouter")?.models || []).filter((m) => !m.kinds || m.kinds.includes(kind));
      orCache[kind] = api(`/api/models/openrouter?kind=${encodeURIComponent(kind)}`)
        .catch(() => ({ live: false, models: local.map((m) => ({ ...m, pricing: {} })) }));
    }
    return orCache[kind];
  }
  async function suggestions(api, pid, kind) {
    const p = prov(pid);
    if (!p) return [];
    if (pid === "openrouter") return (await openrouter(api, kind)).models;
    return (p.models || []).filter((m) => !m.kinds || m.kinds.includes(kind));
  }

  // ---------- preços (dólar, como o OpenRouter informa) ----------
  const PER_UNIT = { image: "imagem", request: "requisição", web_search: "busca" };
  const NAMES = {
    prompt: "Entrada", completion: "Saída", image: "Imagem", request: "Requisição", web_search: "Busca na web",
    internal_reasoning: "Raciocínio", input_cache_read: "Cache (leitura)", input_cache_write: "Cache (escrita)",
    audio: "Áudio", input_audio: "Áudio de entrada", output_audio: "Áudio de saída",
  };
  const usd = (v) => {
    if (v === 0) return "$0";
    const opts = v >= 1 ? { minimumFractionDigits: 2, maximumFractionDigits: 2 } : { maximumSignificantDigits: 3 };
    return "$" + new Intl.NumberFormat("en-US", opts).format(v);
  };
  function prices(m) {
    const pr = m.pricing || {};
    const order = (k) => { const i = Object.keys(NAMES).indexOf(k); return i < 0 ? 99 : i; };
    const keys = Object.keys(pr).sort((a, b) => order(a) - order(b));
    return keys.map((k) => {
      const name = NAMES[k] || k.replace(/_/g, " ");
      if (pr[k] == null) return { k, name, text: "variável" };
      return PER_UNIT[k] ? { k, name, text: `${usd(pr[k])} / ${PER_UNIT[k]}` } : { k, name, text: `${usd(pr[k] * 1e6)} / 1M tokens` };
    });
  }
  function priceShort(m) {
    if (!m || !m.pricing) return "";
    if (m.free) return "grátis";
    const pr = m.pricing; const bits = [];
    if (pr.prompt != null) bits.push(`entrada ${usd(pr.prompt * 1e6)}`);
    if (pr.completion != null) bits.push(`saída ${usd(pr.completion * 1e6)}`);
    let txt = bits.length ? `${bits.join(" · ")} /1M` : "";
    if (pr.image != null) txt += `${txt ? " · " : ""}imagem ${usd(pr.image)}`;
    if (pr.request != null) txt += `${txt ? " · " : ""}${usd(pr.request)}/req`;
    return txt || "preço variável";
  }
  const cost = (m) => {
    const pr = m.pricing || {};
    if (Object.values(pr).some((v) => v == null)) return Infinity;
    return (pr.completion || 0) * 1e6 + (pr.prompt || 0) * 1e6 + (pr.image || 0) * 100 + (pr.request || 0) * 100;
  };

  // ---------- janela "todos os modelos e preços" ----------
  let browser = null;
  function ensureBrowser() {
    if (browser) return browser;
    const d = document.createElement("dialog");
    d.className = "card wide mpb";
    d.innerHTML = `<div class="dialog-head"><h3>Modelos do OpenRouter</h3>
        <button class="icon-btn" type="button" data-close aria-label="Fechar"><svg class="ic" aria-hidden="true"><use href="/static/icons.svg#i-close"/></svg></button></div>
      <p class="muted small-print" data-note></p>
      <div class="mpb-tools">
        <input type="search" data-q placeholder="Buscar por nome ou id…" autocomplete="off" aria-label="Buscar modelo">
        <select data-sort aria-label="Ordenar"><option value="cost">Mais barato primeiro</option><option value="name">Nome (A–Z)</option></select>
        <label class="inline"><input type="checkbox" data-free> Só grátis</label>
      </div>
      <div class="mpb-count muted small-print" data-count></div>
      <div class="mpb-list" data-list role="listbox" aria-label="Modelos"></div>`;
    document.body.appendChild(d);
    d.querySelector("[data-close]").onclick = () => d.close();
    browser = { d, models: [], onPick: null, current: "" };
    const draw = () => {
      const q = d.querySelector("[data-q]").value.trim().toLowerCase();
      const free = d.querySelector("[data-free]").checked;
      let list = browser.models.filter((m) => (!q || m.id.toLowerCase().includes(q) || (m.label || "").toLowerCase().includes(q)) && (!free || m.free));
      list = d.querySelector("[data-sort]").value === "name"
        ? list.sort((a, b) => (a.label || a.id).localeCompare(b.label || b.id))
        : list.sort((a, b) => cost(a) - cost(b) || (a.label || a.id).localeCompare(b.label || b.id));
      d.querySelector("[data-count]").textContent = `${list.length} de ${browser.models.length} modelos`;
      d.querySelector("[data-list]").innerHTML = list.length ? list.map((m) => {
        const tags = [
          m.kinds?.includes("image") ? "gera imagem" : "", m.kinds?.includes("video") ? "gera vídeo" : "",
          m.kinds?.includes("audio") ? "gera áudio" : "", !m.kinds?.includes("audio") && m.inputs?.includes("audio") ? "entende áudio" : "",
        ].filter(Boolean);
        const pr = m.free ? '<span class="mpb-price free">grátis</span>'
          : prices(m).map((p) => `<span class="mpb-price"><i>${esc(p.name)}</i>${esc(p.text)}</span>`).join("") || '<span class="mpb-price">sem preço informado</span>';
        return `<button type="button" class="mpb-row${m.id === browser.current ? " on" : ""}" data-id="${esc(m.id)}" role="option" aria-selected="${m.id === browser.current}">
          <span class="mpb-name"><b>${esc(m.label || m.id)}</b><code>${esc(m.id)}</code>
            ${tags.length ? `<span class="mpb-tags">${tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</span>` : ""}</span>
          <span class="mpb-prices">${pr}</span></button>`;
      }).join("") : '<div class="muted pad">Nenhum modelo encontrado.</div>';
    };
    browser.draw = draw;
    d.querySelector("[data-q]").addEventListener("input", draw);
    d.querySelector("[data-sort]").addEventListener("change", draw);
    d.querySelector("[data-free]").addEventListener("change", draw);
    d.querySelector("[data-list]").addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-id]"); if (!b) return;
      if (browser.onPick) browser.onPick(b.dataset.id);
      d.close();
    });
    return browser;
  }
  const KIND_NAME = { text: "texto", image: "imagem", video: "vídeo", audio: "áudio" };
  async function browse(api, kind, current, onPick) {
    const b = ensureBrowser();
    b.onPick = onPick; b.current = current || "";
    b.d.querySelector("[data-q]").value = "";
    b.d.querySelector("[data-list]").innerHTML = '<div class="muted pad">Carregando modelos e preços…</div>';
    b.d.querySelector("[data-note]").textContent = "";
    if (!b.d.open) b.d.showModal();
    const data = await openrouter(api, kind);
    b.models = data.models || [];
    const when = data.updated_at ? new Date(data.updated_at * 1000).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) : "";
    b.d.querySelector("[data-note]").textContent = data.live
      ? `Modelos que servem para ${KIND_NAME[kind] || kind}, com os preços que o OpenRouter informa (em dólar)${when ? `, lidos às ${when}` : ""}. Modelos de imagem e áudio costumam cobrar a mídia gerada como tokens de saída.`
      : "Não consegui falar com o OpenRouter agora: esta é a lista de reserva, sem preços. Tente de novo mais tarde.";
    if (!b.models.length) {
      b.d.querySelector("[data-count]").textContent = "";
      b.d.querySelector("[data-list]").innerHTML = `<div class="muted pad">O OpenRouter não lista nenhum modelo de ${esc(KIND_NAME[kind] || kind)} agora.</div>`;
      return;
    }
    b.draw();
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
      <label>Raciocínio <select data-mp-reasoning></select></label>
      <div class="mp-browse" data-mp-browse hidden>
        <button class="btn small ghost" type="button" data-mp-open>Ver todos os modelos e preços</button>
        <span class="muted small-print" data-mp-price></span>
      </div>`;
    const selP = root.querySelector("[data-mp-provider]");
    const inM = root.querySelector("[data-mp-model]");
    const selR = root.querySelector("[data-mp-reasoning]");
    const dl = root.querySelector("datalist");
    const browseRow = root.querySelector("[data-mp-browse]");
    const priceEl = root.querySelector("[data-mp-price]");
    let known = [];
    const showPrice = () => {
      const m = known.find((x) => x.id === inM.value.trim());
      priceEl.textContent = m ? priceShort(m) : "";
    };

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
      dl.innerHTML = ""; known = [];
      browseRow.hidden = !(on && p.id === "openrouter");
      showPrice();
      if (on) {
        const list = await suggestions(api, p.id, kind);
        if (selP.value === p.id) {
          known = list;
          dl.innerHTML = list.map((m) => {
            const price = priceShort(m);
            return `<option value="${esc(m.id)}">${esc((m.label || m.id) + (price ? ` · ${price}` : ""))}</option>`;
          }).join("");
          showPrice();
        }
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
    inM.addEventListener("input", showPrice);
    root.querySelector("[data-mp-open]").addEventListener("click", () => {
      browse(api, kind, inM.value.trim(), (id) => { inM.value = id; showPrice(); changed(); });
    });
    selR.addEventListener("change", changed);
    const ready = set(opts.value || {});
    return { get, set, ready, el: root };
  }

  return { load, mount, describe, prov, browse, priceShort, data: () => data };
})();
