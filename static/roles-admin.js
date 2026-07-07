// Fase 8: gerenciador de papeis e permissoes (lado admin).
(function(){
  let CATALOG = [];
  let ROLES = [];

  async function apiGet(url){
    const r = await fetch(url, {headers:{'Accept':'application/json'}});
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }

  async function loadRoles(){
    try{
      const data = await apiGet('/api/roles');
      CATALOG = data.catalog || [];
      ROLES = data.roles || [];
      renderRolesList();
      populateRoleDropdown();
    }catch(e){
      const box = document.getElementById('roles-list');
      if(box) box.innerHTML = '<p style="color:var(--neg,#ff4d5e);font-size:0.85rem;">Nao foi possivel carregar os papeis.</p>';
    }
  }

  function renderRolesList(){
    const box = document.getElementById('roles-list');
    if(!box) return;
    if(!ROLES.length){ box.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem;">Nenhum papel.</p>'; return; }
    let html = '<table class="data-table"><tr><th>Papel</th><th>Permissoes</th><th style="text-align:right;">Acoes</th></tr>';
    ROLES.forEach(function(r){
      const isSys = r.system;
      const count = (r.permissions||[]).length;
      const tag = isSys
        ? '<span style="font-size:0.7rem;color:var(--text-muted);">padrao</span>'
        : '<span style="font-size:0.7rem;color:var(--primary);">customizado</span>';
      const perms = (isSys && r.role==='admin') ? 'Todas' : (count + ' permissao(oes)');
      let actions;
      if(isSys){
        actions = '<button class="btn" style="padding:5px 10px;font-size:0.78rem;" onclick="viewRole(\''+r.role+'\')">Ver</button>';
      } else {
        actions = '<button class="btn" style="padding:5px 10px;font-size:0.78rem;" onclick="openRoleModal(\''+r.role+'\')">Editar</button> '
                + '<button class="btn btn-danger" style="padding:5px 10px;font-size:0.78rem;" onclick="deleteRole(\''+r.role+'\')">Excluir</button>';
      }
      html += '<tr><td><strong>'+(r.label||r.role)+'</strong><br><span style="font-size:0.72rem;color:var(--text-muted);">'+r.role+'</span> '+tag+'</td>'
            + '<td style="font-size:0.8rem;">'+perms+'</td>'
            + '<td style="text-align:right;">'+actions+'</td></tr>';
    });
    html += '</table>';
    box.innerHTML = html;
  }

  function populateRoleDropdown(){
    const sel = document.getElementById('u-role');
    if(!sel) return;
    const current = sel.value;
    Array.from(sel.querySelectorAll('option[data-custom="1"]')).forEach(function(o){ o.remove(); });
    ROLES.filter(function(r){ return !r.system; }).forEach(function(r){
      if(sel.querySelector('option[value="'+r.role+'"]')) return;
      const o = document.createElement('option');
      o.value = r.role; o.textContent = r.label || r.role; o.setAttribute('data-custom','1');
      sel.appendChild(o);
    });
    if(current) sel.value = current;
  }

  function renderPermChecks(selected){
    const box = document.getElementById('role-perms');
    if(!box) return;
    const sel = {};
    (selected||[]).forEach(function(k){ sel[k] = true; });
    const groups = {};
    const order = [];
    CATALOG.forEach(function(c){
      if(!groups[c.group]){ groups[c.group] = []; order.push(c.group); }
      groups[c.group].push(c);
    });
    let html = '';
    order.forEach(function(g){
      html += '<div><div style="font-size:0.72rem;text-transform:uppercase;letter-spacing:.5px;color:var(--primary);margin-bottom:6px;">'+g+'</div>';
      groups[g].forEach(function(c){
        const ck = sel[c.key] ? 'checked' : '';
        html += '<label style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:0.85rem;cursor:pointer;">'
              + '<input type="checkbox" class="role-perm-ck" value="'+c.key+'" '+ck+'> '+c.label+'</label>';
      });
      html += '</div>';
    });
    box.innerHTML = html;
  }

  window.openRoleModal = function(key){
    const save = document.querySelector('#roleModal button.btn-primary');
    if(save) save.style.display = '';
    document.getElementById('role-title').textContent = key ? 'Editar Papel' : 'Novo Papel';
    const keyInput = document.getElementById('role-key');
    const labelInput = document.getElementById('role-label');
    const activeSel = document.getElementById('role-active');
    let perms = [];
    if(key){
      const r = ROLES.find(function(x){ return x.role===key; }) || {};
      keyInput.value = r.role || ''; keyInput.readOnly = true;
      labelInput.value = r.label || '';
      activeSel.value = String(r.is_active===0 ? 0 : 1);
      perms = r.permissions || [];
    } else {
      keyInput.value=''; keyInput.readOnly=false; labelInput.value=''; activeSel.value='1';
    }
    renderPermChecks(perms);
    document.getElementById('roleModal').style.display='flex';
  };

  window.viewRole = function(key){
    window.openRoleModal(key);
    const save = document.querySelector('#roleModal button.btn-primary');
    if(save) save.style.display='none';
    document.querySelectorAll('.role-perm-ck').forEach(function(c){ c.disabled = true; });
  };

  window.saveRole = async function(){
    const role_key = (document.getElementById('role-key').value||'').trim().toLowerCase();
    const label = (document.getElementById('role-label').value||'').trim();
    const is_active = parseInt(document.getElementById('role-active').value||'1',10);
    const permissions = Array.from(document.querySelectorAll('.role-perm-ck:checked')).map(function(c){ return c.value; });
    if(!role_key){ alert('Informe o identificador do papel.'); return; }
    try{
      const r = await fetch('/api/roles', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({role_key:role_key, label:label, permissions:permissions, is_active:is_active})});
      const j = await r.json().catch(function(){ return {}; });
      if(!r.ok){ alert(j.detail || 'Falha ao salvar papel.'); return; }
      document.getElementById('roleModal').style.display='none';
      await loadRoles();
      if(window.__roleFromUser){ const usel=document.getElementById('u-role'); if(usel){ usel.value=role_key; } window.__roleFromUser=false; }
    }catch(e){ alert('Erro de rede ao salvar papel.'); }
  };

  window.deleteRole = async function(key){
    if(!confirm('Excluir o papel "'+key+'"?')) return;
    try{
      const r = await fetch('/api/roles/'+encodeURIComponent(key), {method:'DELETE'});
      const j = await r.json().catch(function(){ return {}; });
      if(!r.ok){ alert(j.detail || 'Falha ao excluir.'); return; }
      loadRoles();
    }catch(e){ alert('Erro de rede ao excluir.'); }
  };

  window.openRoleModalFromUser = function(){
    window.__roleFromUser = true;
    if(!CATALOG.length){ loadRoles().then(function(){ window.openRoleModal(); }); }
    else { window.openRoleModal(); }
  };

  document.addEventListener('DOMContentLoaded', function(){
    if(document.getElementById('roles-list')) loadRoles();
    else populateRoleDropdown();
  });
})();
