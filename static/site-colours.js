// Show / hide button colour overrides based on the "follow navbar" checkbox.
(function () {
  function init() {
    var cb = document.getElementById("btn-follow");
    var box = document.getElementById("btn-overrides");
    if (!cb || !box) return;
    function sync() { box.style.display = cb.checked ? "none" : ""; }
    cb.addEventListener("change", sync);
    sync();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
