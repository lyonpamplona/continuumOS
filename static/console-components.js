/* console-components.js (R2) - Componentes base do prototipo disponibilizados no app:
   containers (#overlay/#modal/#pop/#ctx), modal(), custom-select (bindSelects) e
   helpers esc()/ic(). Nao substitui os modais nativos (userModal etc.), apenas
   oferece o padrao do prototipo para telas novas e reconcilia utilitarios. */
(function(){
  "use strict";
  if(typeof window.esc!=="function"){
    window.esc=function(s){ return String(s==null?"":s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];}); };
  }
  if(typeof window.ic!=="function"){
    window.ic=function(id,cls){ return '<svg class="'+(cls||"ic")+'"><use href="#'+id+'"></use></svg>'; };
  }
  var esc=window.esc, ic=window.ic;

  function ensureContainers(){
    var b=document.body; if(!b) return;
    if(!document.getElementById("overlay")){
      var ov=document.createElement("div"); ov.className="overlay"; ov.id="overlay";
      ov.innerHTML='<div class="panel" id="modal"></div>';
      b.appendChild(ov);
      ov.addEventListener("click",function(e){ if(e.target.id==="overlay") ov.classList.remove("on"); });
    }
    if(!document.getElementById("pop")){ var p=document.createElement("div"); p.className="pop"; p.id="pop"; b.appendChild(p); }
    if(!document.getElementById("ctx")){ var c=document.createElement("div"); c.className="pop"; c.id="ctx"; b.appendChild(c); }
  }

  // modal() compativel com o prototipo
  window.modal=function(o){
    o=o||{}; ensureContainers();
    var m=document.getElementById("modal"); var ov=document.getElementById("overlay");
    var hideConfirm=!!o.hideConfirm, danger=!!o.danger;
    m.innerHTML='<span class="tick tr"></span><span class="tick bl"></span>'+
      '<div class="m-head"><div class="t">'+esc(o.title||"")+'</div><button class="icbtn" id="mClose">'+ic("ic-x")+'</button></div>'+
      '<div class="m-body">'+(o.body||"")+'</div>'+
      '<div class="m-foot"><button class="btn" id="mCancel">'+esc(o.cancelText||"Cancelar")+'</button>'+
      (hideConfirm?"":'<button class="btn '+(danger?"bad":"pri")+'" id="mOk">'+esc(o.confirmText||"Confirmar")+'</button>')+'</div>';
    ov.classList.add("on");
    function close(){ ov.classList.remove("on"); }
    document.getElementById("mClose").onclick=close;
    document.getElementById("mCancel").onclick=close;
    var ok=document.getElementById("mOk");
    if(ok) ok.onclick=function(){ if(o.onConfirm && o.onConfirm(m)===false) return; close(); };
    bindSelects(m); return m;
  };

  // Custom select (para elementos [data-select], seguindo o prototipo)
  window.SELECTS=window.SELECTS||{};
  function selLabel(id,val){ var o=(window.SELECTS[id]||[]).find(function(x){return x[0]===val;}); return o?o[1]:val; }
  function openPop(pop,anchor,items){
    pop.innerHTML=items.map(function(it,i){ return it.sep?'<div class="sepp"></div>':'<button data-i="'+i+'" class="'+(it.danger?"danger":"")+'">'+(it.icon?ic(it.icon):"")+'<span>'+esc(it.label)+'</span></button>'; }).join("");
    var r=anchor.getBoundingClientRect(); pop.style.left=Math.min(r.left, window.innerWidth-210)+"px"; pop.style.top=(r.bottom+6)+"px"; pop.classList.add("on");
    items.forEach(function(it,i){ var bb=pop.querySelector('[data-i="'+i+'"]'); if(bb&&it.on) bb.onclick=function(){ pop.classList.remove("on"); it.on(); }; });
  }
  function renderSelect(node){
    var id=node.id, val=node.dataset.value;
    node.innerHTML='<div class="sel">'+esc(selLabel(id,val))+' '+ic("ic-chev")+'</div>';
    node.querySelector(".sel").onclick=function(e){ e.stopPropagation(); openPop(document.getElementById("pop"), node, (window.SELECTS[id]||[]).map(function(o){ return {label:o[1], on:function(){ node.dataset.value=o[0]; renderSelect(node); if(node.dataset.onchange) try{ window[node.dataset.onchange](o[0]); }catch(err){} }}; })); };
  }
  function bindSelects(root){ (root||document).querySelectorAll("[data-select]").forEach(renderSelect); }
  window.bindSelects=bindSelects; window.renderSelect=renderSelect; window.openPop=openPop;

  function boot(){
    ensureContainers();
    bindSelects(document);
    document.addEventListener("click",function(){ var p=document.getElementById("pop"), c=document.getElementById("ctx"); if(p)p.classList.remove("on"); if(c)c.classList.remove("on"); });
  }
  if(document.readyState==="loading"){ document.addEventListener("DOMContentLoaded", boot); } else { boot(); }
})();
