/*
 * continnumOS - Icones de reacao (SVG inline).
 * Trocam os emojis do seletor de reacoes por icones vetoriais, que renderizam
 * de forma consistente sem depender da fonte de emoji do sistema operacional.
 * IMPORTANTE: o VALOR da reacao continua sendo o emoji (e o dado enviado ao
 * WhatsApp / armazenado como reacao interna). Aqui trocamos apenas a
 * REPRESENTACAO VISUAL do botao.
 */
(function () {
  function svg(inner, opts) {
    opts = opts || {};
    var fill = opts.fill || 'none';
    var color = opts.color ? ' style="color:' + opts.color + '"' : '';
    var sw = opts.strokeWidth || 2;
    return '<svg width="22" height="22" viewBox="0 0 24 24" fill="' + fill + '" stroke="currentColor" stroke-width="' + sw + '" stroke-linecap="round" stroke-linejoin="round"' + color + '>' + inner + '</svg>';
  }
  window.REACTION_ICONS = {
    'THUMBSUP': svg('<path d="M7 10v11"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>', { color: '#ffb020' }),
    'HEART': svg('<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>', { fill: 'currentColor', color: '#ff4d4d' }),
    'LAUGH': svg('<circle cx="12" cy="12" r="10"/><path d="M18 13a6 6 0 0 1-12 0"/><line x1="9" x2="9.01" y1="9" y2="9"/><line x1="15" x2="15.01" y1="9" y2="9"/>', { color: '#ffb020' }),
    'WOW': svg('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="15" r="2.2"/><line x1="9" x2="9.01" y1="9" y2="9"/><line x1="15" x2="15.01" y1="9" y2="9"/>', { color: '#ffb020' }),
    'SAD': svg('<circle cx="12" cy="12" r="10"/><path d="M16 16s-1.5-2-4-2-4 2-4 2"/><line x1="9" x2="9.01" y1="9" y2="9"/><line x1="15" x2="15.01" y1="9" y2="9"/>', { color: '#2aabee' }),
    'PRAY': svg('<path d="m11 17 2 2a1 1 0 1 0 3-3"/><path d="m14 14 2.5 2.5a1 1 0 1 0 3-3l-3.88-3.88a3 3 0 0 0-4.24 0l-.88.88a1 1 0 1 1-3-3l2.81-2.81a5.79 5.79 0 0 1 7.06-.87l.47.28a2 2 0 0 0 1.42.25L21 4"/><path d="m21 3 1 11h-2"/><path d="M3 3 2 14l6.5 6.5a1 1 0 1 0 3-3"/><path d="M3 4h8"/>', { color: '#ff6a00' }),
    'CHECK': svg('<path d="M21.8 10A10 10 0 1 1 17 3.34"/><path d="m9 11 3 3L22 4"/>', { color: '#3ddc84' }),
    'EYES': svg('<path d="M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0"/><circle cx="12" cy="12" r="3"/>', { color: '#f2ede6' })
  };
  var ALIAS = {};
  ALIAS[String.fromCodePoint(0x1F44D)] = 'THUMBSUP';
  ALIAS[String.fromCodePoint(0x2764, 0xFE0F)] = 'HEART';
  ALIAS[String.fromCodePoint(0x2764)] = 'HEART';
  ALIAS[String.fromCodePoint(0x1F602)] = 'LAUGH';
  ALIAS[String.fromCodePoint(0x1F62E)] = 'WOW';
  ALIAS[String.fromCodePoint(0x1F622)] = 'SAD';
  ALIAS[String.fromCodePoint(0x1F64F)] = 'PRAY';
  ALIAS[String.fromCodePoint(0x2705)] = 'CHECK';
  ALIAS[String.fromCodePoint(0x1F440)] = 'EYES';
  window.reactionIcon = function (emoji) {
    var key = ALIAS[emoji];
    if (key && window.REACTION_ICONS[key]) return window.REACTION_ICONS[key];
    return emoji;
  };
  function render(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll('.reaction-emoji[data-emoji]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var e = el.getAttribute('data-emoji');
      var html = window.reactionIcon(e);
      if (html !== e) {
        el.innerHTML = html;
        el.style.display = 'inline-flex';
        el.style.alignItems = 'center';
        el.style.justifyContent = 'center';
      }
    }
  }
  window.renderReactionIcons = render;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { render(); });
  } else {
    render();
  }
})();
