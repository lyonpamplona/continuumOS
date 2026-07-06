/*
 * continnumOS - Modais in-app (substituem confirm()/prompt()/alert() nativos).
 * Expoe window.uiConfirm(msg,opts) -> Promise<bool>,
 *        window.uiPrompt(msg,def,opts) -> Promise<string|null>,
 *        window.uiAlert(msg,opts) -> Promise<void>.
 * Tema Console (mesma identidade visual dos paineis).
 */
(function () {
  "use strict";
  if (window.uiConfirm && window.uiPrompt) return;

  function ensureCss() {
    if (document.getElementById("uimodal-css")) return;
    var css = ""
      + "#uimodal-ov{position:fixed;inset:0;z-index:100050;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.6);-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);animation:uimodal-fade .15s ease}"
      + ".uimodal{width:min(420px,92vw);background:var(--surface,#141417);border:1px solid var(--border,#2a2a30);border-radius:16px;box-shadow:0 24px 70px rgba(0,0,0,.6);padding:20px;font-family:'Space Grotesk',system-ui,sans-serif;color:var(--text,#f2ede6);animation:uimodal-pop .16s ease}"
      + ".uimodal h4{margin:0 0 6px;font-size:15px;font-weight:700}"
      + ".uimodal p{margin:0 0 14px;font-size:13px;line-height:1.5;color:var(--muted,#c9c4bc);white-space:pre-wrap}"
      + ".uimodal input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:10px;border:1px solid var(--border,#2a2a30);background:#0d0d0f;color:var(--text,#f2ede6);font:inherit;font-size:13px;margin-bottom:14px}"
      + ".uimodal input:focus{outline:none;border-color:var(--primary,#ff6a00)}"
      + ".uimodal-actions{display:flex;gap:8px;justify-content:flex-end}"
      + ".uimodal button{font:inherit;font-size:12.5px;font-weight:600;padding:9px 16px;border-radius:10px;cursor:pointer;border:1px solid var(--border,#2a2a30);background:transparent;color:var(--text,#f2ede6);transition:all .15s ease}"
      + ".uimodal button:hover{border-color:var(--primary,#ff6a00)}"
      + ".uimodal button.primary{background:var(--primary,#ff6a00);border-color:var(--primary,#ff6a00);color:#0a0a0a}"
      + ".uimodal button.primary:hover{filter:brightness(1.06)}"
      + ".uimodal button.danger{background:#ff4d4d;border-color:#ff4d4d;color:#0a0a0a}"
      + "@keyframes uimodal-fade{from{opacity:0}to{opacity:1}}"
      + "@keyframes uimodal-pop{from{opacity:0;transform:translateY(8px) scale(.98)}to{opacity:1;transform:none}}";
    var s = document.createElement("style");
    s.id = "uimodal-css";
    s.textContent = css;
    document.head.appendChild(s);
  }

  function dialog(opts) {
    ensureCss();
    return new Promise(function (resolve) {
      var ov = document.createElement("div");
      ov.id = "uimodal-ov";
      var box = document.createElement("div");
      box.className = "uimodal";
      var h = document.createElement("h4");
      h.textContent = opts.title || "continnumOS";
      var p = document.createElement("p");
      p.textContent = opts.message || "";
      box.appendChild(h);
      box.appendChild(p);
      var input = null;
      if (opts.prompt) {
        input = document.createElement("input");
        input.type = "text";
        input.value = opts.def || "";
        box.appendChild(input);
      }
      var acts = document.createElement("div");
      acts.className = "uimodal-actions";
      var done = false;
      function close(val) {
        if (done) return;
        done = true;
        ov.style.opacity = "0";
        setTimeout(function () { ov.remove(); }, 150);
        document.removeEventListener("keydown", onKey);
        resolve(val);
      }
      var cancel = document.createElement("button");
      cancel.textContent = opts.cancelText || "Cancelar";
      cancel.onclick = function () { close(opts.prompt ? null : false); };
      var ok = document.createElement("button");
      ok.className = "primary" + (opts.danger ? " danger" : "");
      ok.textContent = opts.okText || "Confirmar";
      ok.onclick = function () { close(opts.prompt ? (input ? input.value : "") : true); };
      acts.appendChild(cancel);
      acts.appendChild(ok);
      box.appendChild(acts);
      ov.appendChild(box);
      ov.onclick = function (e) { if (e.target === ov) close(opts.prompt ? null : false); };
      function onKey(e) {
        if (e.key === "Escape") close(opts.prompt ? null : false);
        else if (e.key === "Enter") { e.preventDefault(); ok.onclick(); }
      }
      document.addEventListener("keydown", onKey);
      document.body.appendChild(ov);
      if (input) { input.focus(); input.select(); }
    });
  }

  window.uiConfirm = function (message, opts) {
    opts = opts || {};
    return dialog({ message: message, title: opts.title || "Confirmar acao", okText: opts.okText || "Confirmar", cancelText: opts.cancelText || "Cancelar", danger: opts.danger });
  };
  window.uiPrompt = function (message, def, opts) {
    opts = opts || {};
    return dialog({ message: message, prompt: true, def: def || "", title: opts.title || "Informe um valor", okText: opts.okText || "OK", cancelText: opts.cancelText || "Cancelar" });
  };
  window.uiAlert = function (message, opts) {
    opts = opts || {};
    return dialog({ message: message, title: opts.title || "Aviso", okText: opts.okText || "OK", cancelText: "Fechar" });
  };
})();
