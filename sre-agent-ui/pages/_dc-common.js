/* Shared helpers and chart renderers for the /ui/datacenter*.html pages.
 *
 * Extracted so the five data-centre pages share one implementation of the
 * things that must not drift: the validated palette, the number formatters
 * (a MiB is formatted the same on every page), and the SVG mark specs — 2px
 * lines, 4px rounded data-ends, a crosshair tooltip on line charts.
 *
 * Exposes a single global, DCC. Plain ES5 on purpose: these pages are served
 * straight from the API with no build step, matching the rest of the console.
 */
(function (global) {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';

  // Validated against these pages' #ffffff card surface: lightness band, chroma
  // floor, CVD separation (ΔE 24.7), normal-vision floor (ΔE 33.6) and >=3:1
  // contrast all pass. Do not substitute by eye - re-run the palette validator.
  var CPU_COLOR = '#2a78d6';
  var MEM_COLOR = '#eb6834';

  function $(id) { return document.getElementById(id); }

  function el(tag, attrs) {
    var n = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) {
      if (attrs[k] !== null && attrs[k] !== undefined) { n.setAttribute(k, attrs[k]); }
    }
    return n;
  }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pct(v) { return (v === null || v === undefined) ? '—' : v.toFixed(1) + '%'; }

  function fmtBytes(n) {
    if (n === null || n === undefined) { return '—'; }
    var g = n / Math.pow(1024, 3);
    if (g >= 1) { return g.toFixed(1) + ' GB'; }
    return (n / Math.pow(1024, 2)).toFixed(0) + ' MB';
  }

  function fmtMilli(v) {
    if (v === null || v === undefined) { return '—'; }
    if (v < 1) { return v.toFixed(2) + 'm'; }
    return v.toFixed(v < 10 ? 1 : 0) + 'm';
  }

  function fmtMemMiB(v) {
    if (v === null || v === undefined) { return '—'; }
    return v >= 1024 ? (v / 1024).toFixed(2) + 'Gi' : v.toFixed(0) + 'Mi';
  }

  function fmtQuota(v, res) {
    if (v === null || v === undefined) { return '—'; }
    return res === 'memory'
      ? (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + ' MiB'
      : (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + 'm';
  }

  function fmtCores(v) { return (v === null || v === undefined) ? '—' : v.toFixed(0) + ' 核'; }

  function clock(ts) {
    var d = new Date(ts * 1000);
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  function clockSec(ts) { return clock(ts) + ':' + ('0' + new Date(ts * 1000).getSeconds()).slice(-2); }

  // Round a maximum up to a readable axis top (1/2/5 x 10^n) so gridline labels
  // are round numbers rather than the data's own extremes.
  function niceMax(v) {
    if (!(v > 0)) { return 1; }
    var exp = Math.floor(Math.log(v) / Math.LN10);
    var base = Math.pow(10, exp);
    var f = v / base;
    var step = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return step * base;
  }

  // ------------------------------------------------------------- line chart
  function lineChart(host, points, color, unit) {
    host.innerHTML = '';
    if (!points || points.length < 2) {
      host.innerHTML = '<div class="sre-empty" style="padding:32px">该时间范围内没有采样点</div>';
      return;
    }

    var W = host.clientWidth || 480;
    var H = 176;
    var pad = { t: 14, r: 58, b: 26, l: 44 };
    var pw = Math.max(10, W - pad.l - pad.r);
    var ph = H - pad.t - pad.b;

    var xs = points.map(function (p) { return p[0]; });
    var ys = points.map(function (p) { return p[1]; });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    var yMax = niceMax(Math.max.apply(null, ys) * 1.15);

    var X = function (t) { return pad.l + (x1 === x0 ? 0 : (t - x0) / (x1 - x0)) * pw; };
    var Y = function (v) { return pad.t + ph - (v / yMax) * ph; };

    var svg = el('svg', { width: '100%', height: H, viewBox: '0 0 ' + W + ' ' + H, role: 'img' });

    // Recessive grid; axis text in text tokens, never the series colour.
    [0, 0.5, 1].forEach(function (f) {
      var v = yMax * f, y = Y(v);
      svg.appendChild(el('line', {
        x1: pad.l, x2: pad.l + pw, y1: y, y2: y,
        stroke: 'var(--sre-border)', 'stroke-width': 1
      }));
      var lab = el('text', {
        x: pad.l - 8, y: y + 4, 'text-anchor': 'end',
        'font-size': 11, fill: 'var(--sre-muted-foreground)'
      });
      lab.textContent = (yMax <= 10 ? v.toFixed(1) : Math.round(v)) + (unit || '');
      svg.appendChild(lab);
    });

    var line = points.map(function (p, i) {
      return (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1);
    }).join(' ');
    var area = line + ' L' + X(x1).toFixed(1) + ',' + Y(0).toFixed(1) +
      ' L' + X(x0).toFixed(1) + ',' + Y(0).toFixed(1) + ' Z';

    svg.appendChild(el('path', { d: area, fill: color, opacity: 0.1 }));
    svg.appendChild(el('path', {
      d: line, fill: 'none', stroke: color, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round'
    }));

    [[x0, 'start', pad.l], [x1, 'end', pad.l + pw]].forEach(function (t) {
      var tx = el('text', {
        x: t[2], y: H - 8, 'text-anchor': t[1],
        'font-size': 11, fill: 'var(--sre-muted-foreground)'
      });
      tx.textContent = clock(t[0]);
      svg.appendChild(tx);
    });

    // Direct label on the final point; the series colour stays on the mark.
    var last = points[points.length - 1];
    var lx = X(last[0]), ly = Y(last[1]);
    svg.appendChild(el('circle', {
      cx: lx, cy: ly, r: 4, fill: color, stroke: 'var(--sre-card)', 'stroke-width': 2
    }));
    var lv = el('text', {
      x: Math.min(lx + 10, W - 4), y: ly + 4, 'font-size': 12, 'font-weight': 600,
      fill: 'var(--sre-foreground)', 'text-anchor': lx + 46 > W ? 'end' : 'start'
    });
    lv.textContent = last[1].toFixed(1) + (unit || '');
    svg.appendChild(lv);

    var cross = el('line', {
      x1: 0, x2: 0, y1: pad.t, y2: pad.t + ph, stroke: 'var(--sre-muted-foreground)',
      'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0
    });
    var dot = el('circle', {
      cx: 0, cy: 0, r: 4.5, fill: color, stroke: 'var(--sre-card)', 'stroke-width': 2, opacity: 0
    });
    svg.appendChild(cross);
    svg.appendChild(dot);

    var hit = el('rect', {
      x: pad.l, y: pad.t, width: pw, height: ph, fill: 'transparent',
      style: 'cursor:crosshair'
    });
    svg.appendChild(hit);
    host.appendChild(svg);

    var tip = document.createElement('div');
    tip.className = 'dc-tip';
    host.appendChild(tip);

    function nearest(px) {
      var best = 0, bd = Infinity;
      for (var i = 0; i < points.length; i++) {
        var d = Math.abs(X(points[i][0]) - px);
        if (d < bd) { bd = d; best = i; }
      }
      return best;
    }

    hit.addEventListener('mousemove', function (ev) {
      var box = svg.getBoundingClientRect();
      var px = (ev.clientX - box.left) * (W / box.width);
      var p = points[nearest(px)];
      var cx = X(p[0]), cy = Y(p[1]);
      cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('opacity', 1);
      dot.setAttribute('cx', cx); dot.setAttribute('cy', cy); dot.setAttribute('opacity', 1);
      tip.innerHTML = '<div class="dc-tip-v">' + esc(p[1].toFixed(1)) + (unit || '') +
        '</div><div class="dc-tip-t">' + esc(clockSec(p[0])) + '</div>';
      tip.setAttribute('data-show', 'true');
      var tw = tip.offsetWidth || 80;
      var left = (cx / W) * box.width + 14;
      if (left + tw > box.width) { left = (cx / W) * box.width - tw - 14; }
      tip.style.left = Math.max(0, left) + 'px';
      tip.style.top = Math.max(0, (cy / H) * box.height - 46) + 'px';
    });
    hit.addEventListener('mouseleave', function () {
      cross.setAttribute('opacity', 0);
      dot.setAttribute('opacity', 0);
      tip.setAttribute('data-show', 'false');
    });
  }

  // ------------------------------------------------------------ bar rows
  // 4px rounded data-end, flat at the zero baseline; the row gap keeps fills
  // from touching.
  function bars(host, rows, color, fmt, labelKey, valueKey) {
    var lk = labelKey || 'pod', vk = valueKey || 'value';
    if (!rows || !rows.length) {
      host.innerHTML = '<div class="sre-empty" style="padding:24px">没有数据</div>';
      return;
    }
    var max = Math.max.apply(null, rows.map(function (r) { return r[vk]; })) || 1;
    host.innerHTML = rows.map(function (r) {
      var w = Math.max(2, (r[vk] / max) * 100);
      return '<div class="dc-bar-row">' +
        '<div class="dc-bar-name" title="' + esc(r[lk]) + '">' + esc(r[lk]) + '</div>' +
        '<div class="dc-bar-track"><div class="dc-bar-fill" style="width:' + w.toFixed(1) +
        '%;background:' + color + '"></div></div>' +
        '<div class="dc-bar-value">' + esc(fmt(r[vk])) + '</div>' +
        '</div>';
    }).join('');
  }

  // ------------------------------------------------------------ meter rows
  // One ratio against its own limit. Thresholds match what the
  // performance-analyzer looks for: 80% is where throttling (CPU) or OOM
  // (memory) becomes plausible.
  function meterRows(host, rows, res, opts) {
    var o = opts || {};
    var nameKey = o.nameKey || 'pod';
    var emptyText = o.emptyText || ('没有容器设置了' + (res === 'memory' ? '内存' : 'CPU') + '限值');
    if (!rows || !rows.length) {
      host.innerHTML = '<div class="dc-quota-empty">' + esc(emptyText) + '</div>';
      return;
    }
    host.innerHTML = rows.map(function (r) {
      var ratio = r.ratio || 0;
      var width = Math.min(100, ratio * 100);
      var level = ratio >= 1 ? 'crit' : ratio >= 0.8 ? 'warn' : 'ok';
      var fill = level === 'crit' ? 'var(--sre-state-error)'
        : level === 'warn' ? 'var(--sre-state-warning)'
          : (res === 'memory' ? MEM_COLOR : CPU_COLOR);
      var label = r[nameKey] + (r.container ? ' [' + r.container + ']' : '');
      return '<div class="dc-quota-row">' +
        '<div class="dc-quota-name" title="' + esc(label) + '">' + esc(label) + '</div>' +
        '<div class="dc-meter" style="margin:0;height:8px"><div class="dc-meter-fill" style="width:' +
        width.toFixed(1) + '%;background:' + fill + '"></div></div>' +
        '<div class="dc-quota-val">' + esc(fmtQuota(r.usage, res)) + ' / ' +
        esc(fmtQuota(r.limit, res)) + '</div>' +
        '<div class="dc-quota-pct" data-level="' + level + '">' + width.toFixed(0) + '%</div>' +
        '</div>';
    }).join('');
  }

  // ------------------------------------------------------------ table
  function table(host, columns, rows, opts) {
    var o = opts || {};
    if (!rows || !rows.length) {
      host.innerHTML = '<div class="sre-empty" style="padding:24px">' + esc(o.emptyText || '没有数据') + '</div>';
      return;
    }
    var head = columns.map(function (c) { return '<th>' + esc(c.title) + '</th>'; }).join('');
    var body = rows.map(function (r) {
      var cells = columns.map(function (c) {
        var v = c.render ? c.render(r) : esc(r[c.key]);
        var style = c.cellStyle ? ' style="' + c.cellStyle + '"' : '';
        return '<td' + style + '>' + v + '</td>';
      }).join('');
      return '<tr>' + cells + '</tr>';
    }).join('');
    host.innerHTML = '<table class="sre-table" style="margin-top:4px"><thead><tr>' + head +
      '</tr></thead><tbody>' + body + '</tbody></table>';
  }

  // A status never rides on colour alone - every badge here carries a word.
  function badge(kind, text) {
    return '<span class="sre-badge sre-badge-' + kind + '">' + esc(text) + '</span>';
  }

  // ------------------------------------------------------------ page scaffold
  // Wires the range selector, refresh button, resize redraw and the poll timer
  // so each page only supplies its URL and its render function.
  function boot(opts) {
    var range = opts.defaultRange || '1h';
    var loading = false;
    var lastData = null;

    function load() {
      if (loading) { return; }
      loading = true;
      fetch(opts.url(range))
        .then(function (r) { return r.json(); })
        // render receives the banner helper as context rather than reaching for
        // a global: the first render happens before boot() has returned, so a
        // page cannot have assigned it to anything yet.
        .then(function (d) {
          lastData = d;
          opts.render(d, { banner: banner, range: range });
        })
        .catch(function (e) { showError(e); })
        .then(function () { loading = false; });
    }

    function showError(e) {
      var b = $('dc-banner');
      if (!b) { return; }
      b.style.display = 'block';
      b.innerHTML = '<i data-lucide="alert-triangle" style="width:16px;height:16px"></i>' +
        '<span style="margin-left:8px;font-weight:600">请求失败</span>' +
        '<span style="margin-left:8px;opacity:.85">' + esc(e.message) + '</span>';
      if (global.lucide) { global.lucide.createIcons(); }
    }

    // Standard backend banner: an unreachable metrics backend must never look
    // like a healthy-but-quiet cluster.
    function banner(d, okText) {
      var b = $('dc-banner');
      if (!b) { return; }
      b.style.display = 'block';
      if (!d.connected) {
        b.innerHTML = '<i data-lucide="cloud-off" style="width:16px;height:16px"></i>' +
          '<span style="margin-left:8px;font-weight:600">无法连接 Prometheus</span>' +
          '<span style="margin-left:8px;opacity:.85">' + esc(d.error || '') + '</span>';
      } else {
        b.innerHTML = '<i data-lucide="check-circle-2" style="width:16px;height:16px"></i>' +
          '<span style="margin-left:8px;font-weight:600">已连接 Prometheus</span>' +
          '<span style="margin-left:8px;opacity:.85">' + okText + '</span>';
      }
      if (global.lucide) { global.lucide.createIcons(); }
      var at = $('dc-checked-at');
      if (at && d.checked_at) {
        at.textContent = '更新于 ' + new Date(d.checked_at).toLocaleTimeString('zh-CN');
      }
      var pu = $('dc-prom-url');
      if (pu) { pu.textContent = d.prometheus_url || ''; }
    }

    var rangeBox = $('dc-range');
    if (rangeBox) {
      Array.prototype.forEach.call(rangeBox.querySelectorAll('button'), function (b) {
        b.addEventListener('click', function () {
          range = b.getAttribute('data-range');
          Array.prototype.forEach.call(rangeBox.querySelectorAll('button'), function (o) {
            o.removeAttribute('data-active');
          });
          b.setAttribute('data-active', 'true');
          load();
        });
      });
    }
    var refresh = $('dc-refresh');
    if (refresh) { refresh.addEventListener('click', load); }

    // The SVG is sized from the container's pixel width, so a reflow would
    // otherwise leave the plot stretched or clipped.
    var rt = null;
    global.addEventListener('resize', function () {
      clearTimeout(rt);
      rt = setTimeout(load, 220);
    });

    var poll = opts.pollMs === undefined ? 30000 : opts.pollMs;
    if (poll > 0) { setInterval(load, poll); }

    load();
    return { load: load, banner: banner, get data() { return lastData; } };
  }

  global.DCC = {
    CPU_COLOR: CPU_COLOR,
    MEM_COLOR: MEM_COLOR,
    $: $, el: el, esc: esc, pct: pct,
    fmtBytes: fmtBytes, fmtMilli: fmtMilli, fmtMemMiB: fmtMemMiB,
    fmtQuota: fmtQuota, fmtCores: fmtCores,
    clock: clock, clockSec: clockSec, niceMax: niceMax,
    lineChart: lineChart, bars: bars, meterRows: meterRows,
    table: table, badge: badge, boot: boot
  };
})(window);
