// Tela do 2FA: só dígitos e envio automático ao completar 6 (inclusive quando o celular preenche sozinho).
(function () {
  var form = document.querySelector("[data-otp-form]");
  if (!form) return;
  var input = form.querySelector("input[name=totp]");
  var sent = false;
  input.addEventListener("input", function () {
    var digits = input.value.replace(/\D/g, "").slice(0, 8);
    if (input.value !== digits) input.value = digits;
    if (digits.length === 6 && !sent) {
      sent = true;
      form.classList.add("sending");
      form.submit();
    }
  });
  form.addEventListener("submit", function () { sent = true; form.classList.add("sending"); });
})();
