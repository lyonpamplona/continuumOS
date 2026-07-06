/*
 * continnumOS - Dock unificado de ferramentas.
 * Consolida os FABs flutuantes (Copiloto, Caixa unificada, Campanhas, Vendas,
 * Equipe, Ferramentas e IA & Automacao) em uma unica barra lateral organizada,
 * com "um painel por vez" e camadas de z-index consistentes.
 * Aditivo: nao altera a logica interna dos partials, apenas os reposiciona.
 */
(function () {
  "use strict";

  var PRES_SELECTOR = ".tm-pres";
  var FAB_SELECTORS = [".aia-fab", ".om-fab", ".cmp-fab", ".sl-fab", ".ai-fab", ".qw-fab", ".tm-fab"];
  var SIDE_PANELS = [".ai-panel", ".qw-panel"];

  function injectCss() {
    if (document.getElementById("cdock-style")) return;
    var rules = [
      "#cdock{position:fixed;right:12px;top:50%;transform:translateY(-50%);z-index:9990;display:flex;flex-direction:column;gap:8px;align-items:stretch;padding:8px;width:196px;background:rgba(20,20,23,.74);border:1px solid #2a2a30;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.5);max-height:calc(100vh - 130px);overflow-y:auto;-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);transition:opacity .18s ease,transform .18s ease}",
      "#cdock.hidden{opacity:0;pointer-events:none;transform:translateY(-50%) translateX(24px)}",
      "#cdock>button,#cdock>.tm-pres{position:static!important;top:auto!important;right:auto!important;left:auto!important;bottom:auto!important;inset:auto!important;margin:0!important;width:100%!important;height:40px!important;padding:0 10px!important;font-size:11px!important;box-shadow:none!important;justify-content:center;overflow:hidden}",
      "#cdock>button:hover{border-color:#ff6a00!important}",
      "#cdock-title{font:700 10px 'JetBrains Mono',monospace;color:#6b6b72;text-transform:uppercase;letter-spacing:.08em;text-align:center;padding:2px 0 4px}",
      "#cdock-tab{position:fixed;right:12px;bottom:18px;z-index:9991;width:48px;height:48px;border-radius:14px;border:1px solid #2a2a30;background:#ff6a00;color:#0a0a0a;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 10px 30px rgba(0,0,0,.55)}",
      "#cdock-tab:hover{filter:brightness(1.05)}",
      ".ai-panel{left:auto!important;right:216px!important;bottom:24px!important}",
      ".qw-panel{right:216px!important;bottom:24px!important}",
      ".qw-head{flex-wrap:nowrap!important;overflow-x:auto!important}",
      ".ai-head{flex-wrap:nowrap!important;overflow-x:auto!important}",
      "#cdock::-webkit-scrollbar,.qw-head::-webkit-scrollbar,.ai-head::-webkit-scrollbar{width:6px;height:6px}",
      "#cdock::-webkit-scrollbar-thumb{background:#2a2a30;border-radius:4px}",
      ".ai-badge.cdock-empty{opacity:.4}",
      "#internal-chat-history:empty::after{content:'Nenhuma mensagem por aqui ainda. Selecione um contato na lista ou escreva a primeira mensagem para a equipe.';display:flex;height:100%;min-height:220px;align-items:center;justify-content:center;text-align:center;color:var(--text-muted,#9a9aa0);font-size:.86rem;line-height:1.55;padding:32px;opacity:.72}",
      "@media(max-width:820px){#cdock{display:none}.ai-panel,.qw-panel{right:16px!important;left:16px!important;width:auto!important}}"
    ];
    var s = document.createElement("style");
    s.id = "cdock-style";
    s.textContent = rules.join("\n");
    document.head.appendChild(s);
  }

  function collect(dock) {
    var moved = 0;
    var order = [PRES_SELECTOR].concat(FAB_SELECTORS);
    for (var s = 0; s < order.length; s++) {
      var els = document.querySelectorAll(order[s]);
      for (var i = 0; i < els.length; i++) {
        if (els[i].getAttribute("data-cdock") === "1") continue;
        els[i].setAttribute("data-cdock", "1");
        dock.appendChild(els[i]);
        moved++;
      }
    }
    return moved;
  }

  function enforceSinglePanel() {
    var panels = [];
    for (var i = 0; i < SIDE_PANELS.length; i++) {
      var p = document.querySelector(SIDE_PANELS[i]);
      if (p) panels.push(p);
    }
    if (panels.length < 2) return;
    panels.forEach(function (panel) {
      var obs = new MutationObserver(function () {
        if (panel.classList.contains("open")) {
          panels.forEach(function (other) {
            if (other !== panel) other.classList.remove("open");
          });
        }
      });
      obs.observe(panel, { attributes: true, attributeFilter: ["class"] });
    });
  }

  function dimBadges() {
    var host = document.querySelector(".ai-panel");
    if (!host) return;
    var obs = new MutationObserver(function () {
      ["ai-sent", "ai-lead"].forEach(function (id) {
        var el = document.getElementById(id);
        if (!el) return;
        var empty = /:\s*-\s*$/.test((el.textContent || "").trim());
        el.classList.toggle("cdock-empty", empty);
      });
    });
    obs.observe(host, { subtree: true, childList: true, characterData: true });
  }

  function build() {
    if (document.getElementById("cdock")) {
      collect(document.getElementById("cdock"));
      return true;
    }
    var dock = document.createElement("div");
    dock.id = "cdock";
    var title = document.createElement("div");
    title.id = "cdock-title";
    title.textContent = "Ferramentas";
    dock.appendChild(title);
    document.body.appendChild(dock);

    if (collect(dock) === 0) {
      dock.remove();
      return false;
    }

    var tab = document.createElement("button");
    tab.id = "cdock-tab";
    tab.title = "Mostrar/ocultar ferramentas";
    tab.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>';
    tab.onclick = function () { dock.classList.toggle("hidden"); };
    document.body.appendChild(tab);

    enforceSinglePanel();
    dimBadges();

    var tries = 0;
    var iv = setInterval(function () {
      collect(dock);
      if (++tries >= 8) clearInterval(iv);
    }, 500);
    return true;
  }

  function init() {
    injectCss();
    if (build()) return;
    var tries = 0;
    var iv = setInterval(function () {
      if (build() || ++tries >= 16) clearInterval(iv);
    }, 250);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
