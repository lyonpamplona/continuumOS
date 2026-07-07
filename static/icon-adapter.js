/* icon-adapter.js (R0) - converte Material Symbols em icones do sprite SVG em runtime. */
(function(){
"use strict";
var MAP = {"add": "ic-plus", "add_circle": "ic-plus-circle", "add_reaction": "ic-smile", "admin_panel_settings": "ic-shield", "analytics": "ic-analytics", "arrow_back": "ic-back", "attach_file": "ic-clip", "build": "ic-build", "calendar_month": "ic-cal", "campaign": "ic-campaign", "chevron_left": "ic-back", "chevron_right": "ic-chev", "cleaning_services": "ic-broom", "close": "ic-x", "cloud_sync": "ic-cloud-sync", "construction": "ic-build", "contacts": "ic-contacts", "dashboard": "ic-dashboard", "delete": "ic-trash", "delete_sweep": "ic-broom", "description": "ic-doc", "dns": "ic-server", "done_all": "ic-done-all", "download": "ic-download", "edit": "ic-edit", "expand_more": "ic-chev-down", "filter_alt_off": "ic-filter-off", "format_list_bulleted": "ic-list", "forum": "ic-chat", "forward": "ic-forward", "groups": "ic-team", "history": "ic-history", "info": "ic-info", "inventory_2": "ic-box", "lock": "ic-lock", "manage_accounts": "ic-user-cog", "manage_search": "ic-search-cfg", "menu": "ic-menu", "mic": "ic-mic", "monitoring": "ic-monitor", "person": "ic-user", "person_add": "ic-user-plus", "play_arrow": "ic-play", "playlist_add_check": "ic-list-check", "playlist_remove": "ic-list-x", "public": "ic-globe", "push_pin": "ic-pin", "query_stats": "ic-monitor", "reply": "ic-reply", "rule": "ic-rule", "search": "ic-search", "select_all": "ic-select-all", "send": "ic-send", "settings_backup_restore": "ic-restore", "smart_toy": "ic-bot", "smartphone": "ic-phone", "sort_by_alpha": "ic-sort-alpha", "stop_circle": "ic-stop", "sync_alt": "ic-swap", "visibility": "ic-eye", "schedule": "ic-clock", "image": "ic-image", "article": "ic-article", "settings": "ic-build", "event_upcoming": "ic-clock", "save": "ic-check", "open_in_new": "ic-forward", "logout": "ic-logout", "notifications": "ic-bell", "notification_important": "ic-bell", "person_alert": "ic-user", "light_mode": "ic-theme", "palette": "ic-theme", "mood": "ic-smile", "local_fire_department": "ic-bolt", "local_florist": "ic-bolt", "call": "ic-phone", "dialpad": "ic-phone", "checklist": "ic-list-check", "refresh": "ic-refresh", "more_vert": "ic-more", "badge": "ic-user", "error": "ic-info", "done": "ic-check", "filter_alt": "ic-filter", "sync": "ic-refresh", "cancel": "ic-x", "check": "ic-check", "check_circle": "ic-check", "group": "ic-team", "groups_2": "ic-team", "key": "ic-lock", "tune": "ic-build", "list": "ic-list", "star": "ic-bolt", "warning": "ic-info", "schedule_send": "ic-send", "event": "ic-cal", "today": "ic-cal"};
var NS="http://www.w3.org/2000/svg";
function toSvg(el){
  if(!el || (el.getAttribute && el.getAttribute("data-ic")==="1")) return;
  var name=(el.textContent||"").trim(); if(!name) return;
  var id=MAP[name]||"ic-bolt";
  var svg=document.createElementNS(NS,"svg"); svg.setAttribute("class","ic"); svg.setAttribute("data-ic","1");
  var use=document.createElementNS(NS,"use"); use.setAttribute("href","#"+id); svg.appendChild(use);
  var fs=el.style && el.style.fontSize; if(fs){ svg.style.width=fs; svg.style.height=fs; }
  if(el.style && el.style.color){ svg.style.color=el.style.color; }
  var cls=(el.className||"").split(/\s+/);
  for(var i=0;i<cls.length;i++){ var c=cls[i]; if(c && c.indexOf("material-symbols")<0 && c.indexOf("material-icons")<0){ try{svg.classList.add(c);}catch(e){} } }
  if(el.parentNode){ el.parentNode.replaceChild(svg,el); }
}
function scan(root){ try{ var l=(root||document).querySelectorAll(".material-symbols-outlined,.material-symbols-rounded,.material-icons"); for(var i=0;i<l.length;i++){ toSvg(l[i]); } }catch(e){} }
function boot(){ scan(document);
  try{ var mo=new MutationObserver(function(ms){ for(var i=0;i<ms.length;i++){ var m=ms[i]; for(var j=0;j<m.addedNodes.length;j++){ var n=m.addedNodes[j]; if(n.nodeType===1){ if(n.classList && (n.classList.contains("material-symbols-outlined")||n.classList.contains("material-symbols-rounded")||n.classList.contains("material-icons"))){ toSvg(n); } else { scan(n); } } } } });
  mo.observe(document.body,{childList:true,subtree:true}); }catch(e){}
}
if(document.readyState==="loading"){ document.addEventListener("DOMContentLoaded",boot); } else { boot(); }
window.ConsoleIcons={ scan:scan, map:MAP, toSvg:toSvg };
})();
