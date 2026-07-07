// Fase 8: painel modular por perfil. Esconde ferramentas de modulos que o
// usuario nao tem permissao de acessar (window.CONTINNUM_PERMS).
(function(){
  var MAP = {
    'module:omnichannel': ['.om-fab'],
    'module:campaigns': ['.cmp-fab'],
    'module:sales': ['.sl-fab'],
    'module:team': ['.tm-pres', '.tm-fab'],
    'module:ai': ['.ai-fab', '.aia-fab', '.qw-fab']
  };
  function apply(){
    var perms = window.CONTINNUM_PERMS;
    if(!Array.isArray(perms)) return;
    Object.keys(MAP).forEach(function(perm){
      if(perms.indexOf(perm) === -1){
        MAP[perm].forEach(function(sel){
          document.querySelectorAll(sel).forEach(function(el){ el.style.display='none'; });
        });
      }
    });
  }
  document.addEventListener('DOMContentLoaded', function(){
    apply();
    setTimeout(apply, 400);
    setTimeout(apply, 1200);
  });
  window.addEventListener('load', apply);
})();
