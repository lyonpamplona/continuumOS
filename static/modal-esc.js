/*
 * continnumOS - Fechamento universal de modais/paineis.
 * - Tecla ESC fecha o overlay mais recente (modais nativos, paineis laterais,
 *   dropdown de perfil e o dock de Ferramentas).
 * - Clique no backdrop (a propria area .modal) fecha o modal.
 * Aditivo: nao altera a logica de abertura existente.
 */
(function () {
  "use strict";
  function visibleModals() {
    return Array.prototype.filter.call(document.querySelectorAll('.modal'), function (m) {
      var d = window.getComputedStyle(m).display;
      return d && d !== 'none';
    });
  }
  function closeTop() {
    // 1. Modais custom (ui-modals) tratam o proprio ESC.
    if (document.getElementById('uimodal-ov')) return false;
    // 2. Modais nativos (.modal via display)
    var mods = visibleModals();
    if (mods.length) { mods[mods.length - 1].style.display = 'none'; return true; }
    // 3. Paineis laterais (copiloto / quick wins)
    var panel = document.querySelector('.ai-panel.open, .qw-panel.open');
    if (panel) { panel.classList.remove('open'); return true; }
    // 4. Dropdown de perfil
    var dd = document.querySelector('.dropdown.active');
    if (dd) { dd.classList.remove('active'); return true; }
    // 5. Dock de ferramentas
    var dock = document.getElementById('cdock');
    if (dock && dock.classList.contains('open')) {
      if (typeof window.closeToolsDock === 'function') window.closeToolsDock();
      else dock.classList.remove('open');
      return true;
    }
    return false;
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' || e.key === 'Esc' || e.keyCode === 27) closeTop();
  });
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('modal')) {
      t.style.display = 'none';
    }
  });
})();
