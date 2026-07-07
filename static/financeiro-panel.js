// r10: aba Financeiro no painel de atendimento. Consome /api/billing/me (protegido por billing:manage).
(function(){
  function money(cents){ return 'R$ ' + ((Number(cents)||0)/100).toFixed(2).replace('.', ','); }
  function fmtDate(s){ if(!s) return '-'; try { return new Date(s).toLocaleDateString('pt-BR'); } catch(e){ return String(s); } }
  function statusLabel(st){ return {paid:'Paga', open:'Em aberto', past_due:'Vencida', overdue:'Vencida', void:'Cancelada', pending:'Pendente', draft:'Rascunho'}[st] || st || '-'; }
  function statusColor(st){ return {paid:'#2ecc71', past_due:'#ff5252', overdue:'#ff5252', pending:'#f5a623', open:'#f5a623'}[st] || 'var(--text-muted)'; }

  window.loadFinanceiro = async function(){
    var planBox = document.getElementById('fin-plan');
    var usageBox = document.getElementById('fin-usage');
    var invBox = document.getElementById('fin-invoices');
    if(!planBox) return;
    try{
      var r = await fetch('/api/billing/me', {headers:{'Accept':'application/json'}});
      if(r.status === 403){ planBox.innerHTML = '<span style="color:var(--text-muted);">Você não tem permissão de faturamento.</span>'; if(usageBox) usageBox.innerHTML=''; if(invBox) invBox.innerHTML=''; return; }
      if(!r.ok) throw new Error('HTTP '+r.status);
      var d = await r.json();
      var t = d.tenant || {};
      var planName = t.plan_code || t.plan || '-';
      planBox.innerHTML = '<div style="font-size:1.15rem; font-weight:700; color:var(--primary);">'+planName+'</div>'
        + '<div style="font-size:0.82rem; color:var(--text-muted); margin-top:4px;">Status: '+(t.status||'-')+'</div>'
        + (t.trial_ends_at ? '<div style="font-size:0.82rem; color:var(--text-muted);">Trial até '+fmtDate(t.trial_ends_at)+'</div>' : '');
      var u = d.usage || {};
      var keys = Object.keys(u);
      usageBox.innerHTML = keys.length
        ? keys.map(function(k){ return '<div style="display:flex; justify-content:space-between; font-size:0.85rem; padding:4px 0; border-bottom:1px solid var(--border);"><span style="color:var(--text-muted);">'+k+'</span><strong>'+u[k]+'</strong></div>'; }).join('')
        : '<span style="color:var(--text-muted);">Sem dados de uso.</span>';
      var inv = d.invoices || [];
      if(!inv.length){ invBox.innerHTML = '<span style="color:var(--text-muted);">Nenhuma fatura emitida.</span>'; return; }
      var html = '<table style="width:100%; border-collapse:collapse; font-size:0.85rem;">'
        + '<tr style="text-align:left; color:var(--text-muted); font-size:0.75rem; text-transform:uppercase;"><th style="padding:6px 4px;">Vencimento</th><th style="padding:6px 4px;">Valor</th><th style="padding:6px 4px;">Status</th></tr>';
      inv.forEach(function(f){
        html += '<tr style="border-top:1px solid var(--border);">'
          + '<td style="padding:8px 4px;">'+fmtDate(f.due_date || f.created_at)+'</td>'
          + '<td style="padding:8px 4px;">'+money(f.amount_cents)+'</td>'
          + '<td style="padding:8px 4px; color:'+statusColor(f.status)+'; font-weight:600;">'+statusLabel(f.status)+'</td>'
          + '</tr>';
      });
      html += '</table>';
      invBox.innerHTML = html;
    }catch(e){
      planBox.innerHTML = '<span style="color:#ff5252;">Falha ao carregar dados financeiros.</span>';
      if(usageBox) usageBox.innerHTML=''; if(invBox) invBox.innerHTML='';
    }
  };

  document.addEventListener('DOMContentLoaded', function(){
    var perms = window.CONTINNUM_PERMS || [];
    if(Array.isArray(perms) && perms.indexOf('billing:manage') >= 0){
      var nav = document.getElementById('nav-financeiro');
      if(nav) nav.style.display = '';
    }
  });
})();
