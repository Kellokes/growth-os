(function () {
  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      window.gosSetTheme(cur === "dark" ? "light" : "dark");
    });
  });
})();
