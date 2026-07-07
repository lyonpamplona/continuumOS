/* theme-engine.js (R0) - 4 temas + personalizacao persistente. */
(function(){
"use strict";
var THEMES=["reator","circuito","cobre","claro"];
var KEY="cnn_theme", CKEY="cnn_theme_custom";
var VARS=["--bg-0","--bg-1","--bg-2","--bg-3","--line","--line-2","--txt","--txt-dim","--txt-mut","--acc","--acc-2","--acc-ink","--ok","--warn","--bad","--info","--grid","--glow"];
function root(){ return document.documentElement; }
function norm(k){ return k.charAt(0)==="-" ? k : "--"+k; }
function applyCustom(o){ if(!o) return; Object.keys(o).forEach(function(k){ root().style.setProperty(norm(k),o[k]); }); }
function clearCustom(){ VARS.forEach(function(n){ root().style.removeProperty(n); }); }
function set(name){ if(THEMES.indexOf(name)<0) name="reator"; root().setAttribute("data-theme",name); try{ localStorage.setItem(KEY,name); }catch(e){} try{ window.dispatchEvent(new CustomEvent("themechange",{detail:{theme:name}})); }catch(e){} }
function readCustom(){ try{ var r=localStorage.getItem(CKEY); return r?JSON.parse(r):{}; }catch(e){ return {}; } }
function saveCustom(o){ try{ localStorage.setItem(CKEY,JSON.stringify(o||{})); }catch(e){} }
function setVar(k,v){ root().style.setProperty(norm(k),v); var o=readCustom(); o[norm(k)]=v; saveCustom(o); }
function setCustom(o){ saveCustom(o||{}); clearCustom(); applyCustom(o); }
function reset(){ try{ localStorage.removeItem(CKEY); }catch(e){} clearCustom(); }
function current(){ try{ return localStorage.getItem(KEY)||"reator"; }catch(e){ return "reator"; } }
function cycle(){ var c=current(); var i=THEMES.indexOf(c); i=(i+1)%THEMES.length; set(THEMES[i]); }
function init(){ set(current()); var c=readCustom(); if(c && Object.keys(c).length) applyCustom(c); }
init();
window.ConsoleTheme={ set:set, setVar:setVar, setCustom:setCustom, reset:reset, cycle:cycle, current:current, themes:THEMES, vars:VARS };
})();
