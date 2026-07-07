/*
 * Dock de ferramentas ancorado a aba "Ajustes".
 * Recolhe os lancadores flutuantes (Copiloto, Caixa unificada, Campanhas,
 * Vendas, Equipe, IA & Automacao, Ferramentas de atendimento) e os posiciona
 * dentro do container #tools-launchers, na aba Ajustes do menu.
 * - Sem painel flutuante, sem backdrop, sem item extra no menu lateral.
 * - Aditivo: nao altera a logica dos partials, apenas reposiciona os botoes.
 */
(function () {
  "use strict";

  var PRES = ".tm-pres";
  var FABS = [".aia-fab", ".om-fab", ".cmp-fab", ".sl-fab", ".ai-fab", ".qw-fab", ".tm-fab"];

  function injectCss() {
    if (document.getElementById("cdock-style")) return;
    var rules = [
      ".aia-fab,.om-fab,.cmp-fab,.sl-fab,.ai-fab,.qw-fab,.tm-fab,.tm-pres{display:none!important}",
      ".tools-launchers-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}",
      ".tools-launchers-grid:empty::after{content:'Nenhuma ferramenta disponivel para o seu perfil.';color:var(--text-muted,#9a9aa0);font-size:.82rem}",
      ".tools-launchers-grid>*{display:flex!important;position:static!important;top:auto!important;right:auto!important;left:auto!important;bottom:auto!important;inset:auto!important;margin:0!important;width:100%!important;min-height:46px!important;height:auto!important;padding:12px 14px!important;font-family:'Space Grotesk',system-ui,sans-serif!important;font-weight:600!important;font-size:12px!important;letter-spacing:.02em!important;text-transform:uppercase!important;border:1px solid var(--border,#2a2a30)!important;border-radius:10px!important;color:var(--text,#f2ede6)!important;background:var(--surface,rgba(255,255,255,.03))!important;box-shadow:none!important;display:flex!important;align-items:center!important;justify-content:center!important;gap:8px!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;cursor:pointer;transition:border-color .15s ease,background .15s ease}",
      ".tools-launchers-grid>*:hover{border-color:var(--primary,#ff6a00)!important;background:rgba(255,106,0,.08)!important}",
      ".tools-launchers-grid>.tm-pres select{background:transparent!important;color:var(--text,#f2ede6)!important;border:0!important;font:inherit!important;cursor:pointer}",
      "#internal-chat-history:empty::after{content:'Nenhuma mensagem por aqui ainda. Selecione um contato na lista ou escreva a primeira mensagem para a equipe.';display:flex;height:100%;min-height:220px;align-items:center;justify-content:center;text-align:center;color:var(--text-muted,#9a9aa0);font-size:.86rem;line-height:1.55;padding:32px;opacity:.72}"
    ];
    var s = document.createElement("style");
    s.id = "cdock-style";
    s.textContent = rules.join("\n");
    document.head.appendChild(s);
  }

  function collect(box) {
    var moved = 0;
    var order = [PRES].concat(FABS);
    for (var s = 0; s < order.length; s++) {
      var els = document.querySelectorAll(order[s]);
      for (var i = 0; i < els.length; i++) {
        if (els[i].getAttribute("data-cdock") === "1") continue;
        els[i].setAttribute("data-cdock", "1");
        box.appendChild(els[i]);
        moved++;
      }
    }
    return moved;
  }

  function init() {
    injectCss();
    var box = document.getElementById("tools-launchers");
    if (!box) return;
    collect(box);
    var tries = 0;
    var iv = setInterval(function () {
      collect(box);
      if (++tries >= 12) clearInterval(iv);
    }, 400);
    try {
      var mo = new MutationObserver(function () { collect(box); });
      mo.observe(document.body, { childList: true });
    } catch (e) {}
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
