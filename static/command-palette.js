/* command-palette.js (R1) - Paleta de comandos Ctrl+K.
   Descobre as abas do painel a partir dos .main-tab-btn existentes (preserva hooks)
   e oferece troca de tema. Nao depende de framework. */
(function(){
  "use strict";
  var overlay, input, list, items = [], sel = 0, filtered = [];

  function buildCommands(){
    var cmds = [];
    // Abas do painel: le os botoes reais e reaproveita o onclick original
    document.querySelectorAll(".main-tab-btn").forEach(function(btn){
      var lbl = (btn.querySelector(".nav-lbl") || btn).textContent.trim();
      if(!lbl) return;
      cmds.push({ label: lbl, hint: "Ir para", run: function(){ btn.click(); } });
    });
    // Temas
    if(window.ConsoleTheme){
      window.ConsoleTheme.themes.forEach(function(t){
        cmds.push({ label: "Tema: " + t.charAt(0).toUpperCase() + t.slice(1), hint: "Tema", run: function(){ window.ConsoleTheme.set(t); } });
      });
      cmds.push({ label: "Alternar tema", hint: "Tema", run: function(){ window.ConsoleTheme.cycle(); } });
    }
    // Acoes comuns, apenas se existirem no escopo
    var maybe = [
      ["Novo status", function(){ var m=document.getElementById("statusModal"); if(m) m.style.display="flex"; }],
      ["Listas de contatos", function(){ var m=document.getElementById("manageListsModal"); if(m) m.style.display="flex"; }],
      ["Sair", function(){ window.location.href="/logout"; }]
    ];
    maybe.forEach(function(p){ cmds.push({ label: p[0], hint: "Acao", run: p[1] }); });
    return cmds;
  }

  function render(){
    list.innerHTML = "";
    if(!filtered.length){ list.innerHTML = '<div class="cmdk-empty">Nada encontrado</div>'; return; }
    filtered.forEach(function(c, i){
      var row = document.createElement("div");
      row.className = "cmdk-item" + (i===sel ? " sel" : "");
      row.innerHTML = '<svg class="ic sm"><use href="#ic-bolt"></use></svg><span class="k"></span><span class="h"></span>';
      row.querySelector(".k").textContent = c.label;
      row.querySelector(".h").textContent = c.hint;
      row.addEventListener("click", function(){ exec(c); });
      list.appendChild(row);
    });
  }

  function filter(q){
    q = (q||"").toLowerCase().trim();
    filtered = !q ? items.slice(0) : items.filter(function(c){ return c.label.toLowerCase().indexOf(q) >= 0; });
    sel = 0; render();
  }

  function open(){
    items = buildCommands();
    overlay.classList.add("on");
    input.value = ""; filter("");
    setTimeout(function(){ input.focus(); }, 10);
  }
  function close(){ overlay.classList.remove("on"); }
  function exec(c){ close(); try{ c.run(); }catch(e){ console.error(e); } }

  function ensureDom(){
    if(document.querySelector(".cmdk-overlay")) return;
    overlay = document.createElement("div");
    overlay.className = "cmdk-overlay";
    overlay.innerHTML = '<div class="cmdk"><input class="cmdk-input" type="text" placeholder="Buscar acao, aba ou tema..."><div class="cmdk-list"></div></div>';
    document.body.appendChild(overlay);
    input = overlay.querySelector(".cmdk-input");
    list = overlay.querySelector(".cmdk-list");
    overlay.addEventListener("click", function(e){ if(e.target===overlay) close(); });
    input.addEventListener("input", function(){ filter(input.value); });
    input.addEventListener("keydown", function(e){
      if(e.key==="ArrowDown"){ e.preventDefault(); sel=Math.min(sel+1, filtered.length-1); render(); }
      else if(e.key==="ArrowUp"){ e.preventDefault(); sel=Math.max(sel-1, 0); render(); }
      else if(e.key==="Enter"){ e.preventDefault(); if(filtered[sel]) exec(filtered[sel]); }
      else if(e.key==="Escape"){ close(); }
    });
  }

  function boot(){
    ensureDom();
    document.addEventListener("keydown", function(e){
      if((e.ctrlKey||e.metaKey) && (e.key==="k"||e.key==="K")){ e.preventDefault(); overlay.classList.contains("on") ? close() : open(); }
    });
  }
  if(document.readyState==="loading"){ document.addEventListener("DOMContentLoaded", boot); } else { boot(); }
  window.ConsolePalette = { open: open, close: close };
})();
