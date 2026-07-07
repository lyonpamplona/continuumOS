/* console-toast.js (R2) - Toast do console (padrao do prototipo) + shim compativel
   com a API do Toastify usada no app legado. Renderiza em #toasts.
   Carregar DEPOIS do CDN Toastify para sobrescreve-lo (e permitir remove-lo no R7). */
(function(){
  "use strict";
  function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];}); }
  function ensureHost(){
    var h=document.getElementById("toasts");
    if(!h){ h=document.createElement("div"); h.id="toasts"; (document.body||document.documentElement).appendChild(h); }
    return h;
  }
  function icon(type){ return ({ok:"ic-check",bad:"ic-x",warn:"ic-bell"}[type])||"ic-bolt"; }
  function show(title,sub,type,duration){
    var host=ensureHost();
    var el=document.createElement("div"); el.className="toast "+(type||"");
    el.innerHTML='<svg class="ic sm"><use href="#'+icon(type)+'"></use></svg>'+
      '<div><div class="tt">'+esc(title)+'</div>'+(sub?'<div class="ts">'+esc(sub)+'</div>':'')+'</div>';
    host.appendChild(el);
    var ms=duration||3200;
    setTimeout(function(){ el.style.transition=".3s"; el.style.opacity="0"; el.style.transform="translateX(30px)"; setTimeout(function(){el.remove();},300); }, ms);
    return el;
  }
  // Helper do prototipo
  window.toast=function(title,sub,type){ return show(title,sub,type); };

  // Shim compativel com Toastify({...}).showToast()
  function looksError(o){
    try{
      var st=o.style||{}; var bg=(st.background||"").toLowerCase(); var col=(st.color||"").toLowerCase();
      if(o.className && /error|danger|bad/.test(o.className)) return true;
      if(/f15c6d|ff4d|ff5|d13b|2a1114|4a1c22|#b00|red/.test(bg+col)) return true;
      if((o.duration||0)>=5000) return true;
    }catch(e){}
    return false;
  }
  function Toastify(opts){
    opts=opts||{};
    return { showToast:function(){
        var type = looksError(opts) ? "bad" : (opts.type||"");
        return show(opts.text||"", opts.subtext||"", type, opts.duration);
      }, hideToast:function(){} };
  }
  window.Toastify=Toastify;
})();
