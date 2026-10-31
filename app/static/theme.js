// Carregado no <head>, antes da pintura, para não piscar o tema errado.
(function () {
  var t = null;
  try { t = localStorage.getItem("gos.theme"); } catch (e) {}
  if (t !== "light" && t !== "dark") {
    t = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme", t);
  window.gosSetTheme = function (next) {
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("gos.theme", next); } catch (e) {}
  };
})();
