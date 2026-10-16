"""Generate the four /ui/datacenter-*.html sub-pages.

The five data-centre pages share their entire head, their sre-* component
styles and their nav markup. Rather than hand-copy that boilerplate four times
(where it would immediately start to drift), this extracts it from the existing
overview page and emits each sub-page around its own panels.

Run once; the generated HTML is what ships. Re-run after editing a page's
content block below, or after the overview page's head/nav changes.
"""
import pathlib

PAGES = pathlib.Path("/home/node1/myproject/sre-agent-ui/pages")
src = (PAGES / "datacenter.html").read_text(encoding="utf-8")
lines = src.split("\n")

# ---- boilerplate extracted from the overview page -------------------------
# Head + topbar: everything through </header>.
head_end = next(i for i, l in enumerate(lines) if l.strip() == "</header>")
HEAD = "\n".join(lines[: head_end + 1])

# The shared sre-* component styles (the <style> block after the lucide
# bootstrap, which is the one that defines .sre-card and friends).
style_starts = [i for i, l in enumerate(lines) if l.strip() == "<style>"]
sre_start = next(i for i in style_starts if ".sre-shell" in lines[i + 1] or
                 any(".sre-shell" in lines[j] for j in range(i, min(i + 5, len(lines)))))
sre_end = next(i for i in range(sre_start + 1, len(lines)) if lines[i].strip() == "</style>")
SRE_STYLES = "\n".join(lines[sre_start : sre_end + 1])
assert ".sre-card" in SRE_STYLES

NAV_ITEMS = [
    ("datacenter.html", "datacenter", "总览"),
    ("datacenter-pods.html", "dc-pods", "Pod"),
    ("datacenter-workloads.html", "dc-workloads", "工作负载"),
    ("datacenter-nodes.html", "dc-nodes", "节点"),
    ("datacenter-storage.html", "dc-storage", "存储"),
]


def nav(active: str) -> str:
    sub = "\n".join(
        '        <a href="%s" class="sre-nav-item sre-nav-sub-item" data-nav-key="%s" '
        'data-dom-id="nav-%s"%s>%s</a>'
        % (href, key, key, ' data-active="true"' if key == active else "", label)
        for href, key, label in NAV_ITEMS
    )
    return (
        '  <nav class="sre-sidebar">\n'
        '    <a href="dashboard.html" class="sre-nav-item" data-nav-key="dashboard" data-dom-id="nav-dashboard">'
        '<i data-lucide="layout-dashboard"></i> 总览</a>\n'
        '    <div class="sre-nav-group">\n'
        '      <div class="sre-nav-item sre-nav-group-label"><i data-lucide="message-square"></i> 智能助手</div>\n'
        '      <div class="sre-nav-sub">\n'
        '        <a href="workbench.html" class="sre-nav-item sre-nav-sub-item" data-nav-key="workbench" data-dom-id="nav-workbench">工作台</a>\n'
        '        <a href="skillhub.html" class="sre-nav-item sre-nav-sub-item" data-nav-key="skillhub" data-dom-id="nav-skillhub">技能中心</a>\n'
        '      </div>\n'
        '    </div>\n'
        '    <a href="runbooks.html" class="sre-nav-item" data-nav-key="runbooks" data-dom-id="nav-runbooks">'
        '<i data-lucide="book-open"></i> Runbook 资料库</a>\n'
        '    <a href="health.html" class="sre-nav-item" data-nav-key="health" data-dom-id="nav-health">'
        '<i data-lucide="activity"></i> 健康检查</a>\n'
        '    <div class="sre-nav-group">\n'
        '      <div class="sre-nav-item sre-nav-group-label"><i data-lucide="bar-chart-3"></i> 数据中心</div>\n'
        '      <div class="sre-nav-sub">\n' + sub + "\n"
        '      </div>\n'
        '    </div>\n'
        '    <a href="alerts.html" class="sre-nav-item" data-nav-key="alerts" data-dom-id="nav-alerts">'
        '<i data-lucide="bell-ring"></i> 告警中心</a>\n'
        '    <a href="approvals.html" class="sre-nav-item" data-nav-key="approvals" data-dom-id="nav-approvals">'
        '<i data-lucide="shield-check"></i> 审批中心</a>\n'
        '    <a href="audit.html" class="sre-nav-item" data-nav-key="audit" data-dom-id="nav-audit">'
        '<i data-lucide="clipboard-list"></i> 审计日志</a>\n'
        "  </nav>"
    )


def page(title: str, active: str, heading: str, subtitle: str, content: str, script: str) -> str:
    # The stylesheet belongs in <head>, not beside the body markup: a link in
    # the body is tolerated by browsers but renders a flash of unstyled content
    # first, and HEAD here spans the whole head plus the topbar.
    head = HEAD.replace("<title>数据中心</title>", "<title>%s</title>" % title)
    assert "</head>" in head, "no </head> to inject into"
    head = head.replace(
        "</head>", '    <link rel="stylesheet" href="/ui/_dc-common.css">\n</head>', 1
    )
    return "\n".join([
        head,
        "",
        nav(active),
        "",
        '  <main class="sre-main">',
        '    <div class="dc-header">',
        "      <div>",
        '        <h1 class="sre-section-title" style="margin-bottom:4px">%s</h1>' % heading,
        '        <div class="sre-text-tertiary" id="dc-subtitle">%s</div>' % subtitle,
        "      </div>",
        '      <div class="dc-controls">',
        '        <button type="button" id="dc-refresh" class="sre-btn sre-btn-secondary sre-btn-sm">'
        '<i data-lucide="refresh-cw" style="width:14px;height:14px"></i> 刷新</button>',
        "      </div>",
        "    </div>",
        "",
        '    <div id="dc-banner" class="sre-card" style="margin-bottom:16px;padding:14px 20px;display:none"></div>',
        "",
        content,
        "",
        '    <div class="dc-foot">',
        '      <span class="sre-text-tertiary" id="dc-checked-at"></span>',
        '      <span class="sre-text-tertiary" id="dc-prom-url"></span>',
        "    </div>",
        "  </main>",
        "</div>",
        "",
        "    <script>lucide.createIcons();</script>",
        "",
        SRE_STYLES,
        "",
        '<script src="/ui/_dc-common.js"></script>',
        "<script>",
        script,
        "</script>",
        '  <script src="/ui/_sre-alerts.js"></script>',
        "</body>",
        "</html>",
    ])


# ============================================================ POD
POD_CONTENT = """    <div class="sre-kpi-grid" style="margin-bottom:24px">
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-total">—</div>
        <div class="sre-kpi-label">Pod 总数</div>
        <div class="sre-kpi-delta sre-text-muted" id="kpi-total-delta">—</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-running">—</div>
        <div class="sre-kpi-label">Running</div>
        <div class="sre-kpi-delta sre-text-muted" id="kpi-running-delta">—</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-notready">—</div>
        <div class="sre-kpi-label">未就绪容器</div>
        <div class="sre-kpi-delta sre-text-muted">readiness probe 未通过</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-restarts">—</div>
        <div class="sre-kpi-label">累计重启次数</div>
        <div class="sre-kpi-delta sre-text-muted">整个集群，非本次运行</div>
      </div>
    </div>

    <div class="dc-grid-2">
      <div class="sre-card dc-panel">
        <div class="dc-panel-title">运行阶段分布</div>
        <div class="dc-panel-sub">Pod 当前所处阶段（kube_pod_status_phase）</div>
        <div id="phase-bars" class="dc-bars"></div>
      </div>
      <div class="sre-card dc-panel">
        <div class="dc-panel-title">节点 Pod 密度</div>
        <div class="dc-panel-sub">每个节点上调度的 Pod 数</div>
        <div id="node-bars" class="dc-bars"></div>
      </div>
    </div>

    <div class="sre-card dc-panel" style="margin-bottom:24px">
      <div class="dc-panel-head">
        <div>
          <div class="dc-panel-title">重启次数排行</div>
          <div class="dc-panel-sub" id="restart-sub">—</div>
        </div>
      </div>
      <div id="restart-table"></div>
    </div>"""

POD_SCRIPT = """(function () {
  'use strict';
  var D = window.DCC, $ = D.$;

  function render(d, ctx) {

    if (!d.connected) {
      ctx.banner(d, '');
      ['kpi-total','kpi-running','kpi-notready','kpi-restarts'].forEach(function (id) { $(id).textContent = '—'; });
      ['phase-bars','node-bars'].forEach(function (id) { $(id).innerHTML = ''; });
      $('restart-table').innerHTML = '';
      return;
    }

    var phases = d.phases || {};
    var total = 0;
    for (var k in phases) { total += phases[k]; }

    $('kpi-total').textContent = total;
    $('kpi-total-delta').textContent = Object.keys(phases).length + ' 种阶段';
    $('kpi-running').textContent = phases.Running || 0;
    $('kpi-running-delta').textContent = total ? Math.round((phases.Running || 0) / total * 100) + '% 占比' : '—';
    $('kpi-notready').textContent = d.not_ready_containers;
    $('kpi-restarts').textContent = d.restart_total;
    ctx.banner(d, total + ' 个 Pod，' + (phases.Running || 0) + ' 个 Running');

    // Phase counts are magnitudes, so bars on one hue - identity is the row
    // label, not the colour.
    var phaseRows = Object.keys(phases).map(function (k) {
      return { label: k, value: phases[k] };
    }).sort(function (a, b) { return b.value - a.value; });
    D.bars($('phase-bars'), phaseRows, D.CPU_COLOR, function (v) { return v + ' 个'; }, 'label');

    var nodeRows = Object.keys(d.by_node || {}).map(function (k) {
      return { label: k, value: d.by_node[k] };
    }).sort(function (a, b) { return b.value - a.value; });
    D.bars($('node-bars'), nodeRows, D.CPU_COLOR, function (v) { return v + ' 个'; }, 'label');

    var rs = d.restarts || [];
    var extra = d.restart_total - rs.reduce(function (s, r) { return s + r.count; }, 0);
    $('restart-sub').textContent = rs.length
      ? 'Top ' + rs.length + (extra > 0 ? '（另有 ' + extra + ' 次未列出）' : '')
      : '没有容器发生过重启';

    D.table($('restart-table'), [
      { title: 'Pod', render: function (r) { return D.esc(r.pod); }, cellStyle: 'font-family:var(--sre-font-mono);font-size:13px' },
      { title: '容器', render: function (r) { return D.esc(r.container); } },
      { title: '命名空间', render: function (r) { return D.esc(r.namespace); } },
      { title: '重启次数', render: function (r) {
          var kind = r.count >= 10 ? 'error' : r.count >= 5 ? 'warning' : 'info';
          return D.badge(kind, r.count + ' 次');
        }, cellStyle: 'text-align:right' }
    ], rs, { emptyText: '没有容器发生过重启' });
  }

  D.boot({ url: function () { return '/api/ui/datacenter/pods'; }, render: render });
})();"""


# ======================================================== WORKLOADS
WL_CONTENT = """    <div class="sre-kpi-grid" style="margin-bottom:24px">
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-wl-total">—</div>
        <div class="sre-kpi-label">控制器总数</div>
        <div class="sre-kpi-delta sre-text-muted">Deployment / StatefulSet / DaemonSet</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-wl-bad">—</div>
        <div class="sre-kpi-label">副本未对齐</div>
        <div class="sre-kpi-delta sre-text-muted">就绪副本 &lt; 期望副本</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-wl-deploy">—</div>
        <div class="sre-kpi-label">Deployment</div>
        <div class="sre-kpi-delta sre-text-muted">无状态工作负载</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-wl-sts">—</div>
        <div class="sre-kpi-label">StatefulSet + DaemonSet</div>
        <div class="sre-kpi-delta sre-text-muted">有状态与节点级</div>
      </div>
    </div>

    <div class="sre-card dc-panel" style="margin-bottom:24px">
      <div class="dc-panel-title">Deployment</div>
      <div class="dc-panel-sub">期望 / 就绪 / 可用 / 已更新副本</div>
      <div id="deploy-table"></div>
    </div>

    <div class="dc-grid-2">
      <div class="sre-card dc-panel">
        <div class="dc-panel-title">StatefulSet</div>
        <div class="dc-panel-sub">期望 / 就绪副本</div>
        <div id="sts-table"></div>
      </div>
      <div class="sre-card dc-panel">
        <div class="dc-panel-title">DaemonSet</div>
        <div class="dc-panel-sub">期望调度 / 就绪</div>
        <div id="ds-table"></div>
      </div>
    </div>"""

WL_SCRIPT = """(function () {
  'use strict';
  var D = window.DCC, $ = D.$;

  function replicaCell(r) {
    var ok = r.ready >= r.desired;
    return D.badge(ok ? 'success' : 'error', r.ready + ' / ' + r.desired);
  }

  function tableOpts(extra) {
    var cols = [
      // nowrap: the shared table style sets word-break:break-all, which in the
      // half-width StatefulSet/DaemonSet cards splits "monitoring" across two
      // lines. A namespace is short and should never be broken mid-word.
      { title: '命名空间', render: function (r) { return D.esc(r.namespace); },
        cellStyle: 'white-space:nowrap' },
      { title: '名称', render: function (r) { return D.esc(r.name); },
        cellStyle: 'font-family:var(--sre-font-mono);font-size:13px' },
      { title: '就绪 / 期望', render: replicaCell, cellStyle: 'text-align:right;white-space:nowrap' }
    ];
    if (extra) { cols = cols.concat(extra); }
    return cols;
  }

  function render(d, ctx) {
    if (!d.connected) {
      ctx.banner(d, '');
      ['kpi-wl-total','kpi-wl-bad','kpi-wl-deploy','kpi-wl-sts'].forEach(function (id) { $(id).textContent = '—'; });
      ['deploy-table','sts-table','ds-table'].forEach(function (id) { $(id).innerHTML = ''; });
      return;
    }
    var dep = d.deployments || [], sts = d.statefulsets || [], ds = d.daemonsets || [];
    var s = d.summary || {};

    $('kpi-wl-total').textContent = s.total;
    $('kpi-wl-bad').textContent = s.degraded;
    $('kpi-wl-bad').style.color = s.degraded > 0 ? 'var(--sre-state-error)' : '';
    $('kpi-wl-deploy').textContent = dep.length;
    $('kpi-wl-sts').textContent = sts.length + ds.length;
    ctx.banner(d, s.total + ' 个控制器，' +
      (s.degraded ? s.degraded + ' 个副本未对齐' : '全部副本已对齐'));

    // Deployment carries two extra numbers worth seeing: available and
    // up-to-date. A rolling update mid-flight shows up as up_to_date < desired.
    D.table($('deploy-table'), tableOpts([
      { title: '可用', render: function (r) {
          return D.esc(r.available === undefined ? '—' : r.available);
        }, cellStyle: 'text-align:right' },
      { title: '已更新', render: function (r) {
          var stale = r.up_to_date !== undefined && r.up_to_date < r.desired;
          return stale
            ? '<span style="color:var(--sre-state-warning);font-weight:600">' + D.esc(r.up_to_date) + '</span>'
            : D.esc(r.up_to_date === undefined ? '—' : r.up_to_date);
        }, cellStyle: 'text-align:right' }
    ]), dep, { emptyText: '没有 Deployment' });

    D.table($('sts-table'), tableOpts(), sts, { emptyText: '没有 StatefulSet' });
    D.table($('ds-table'), tableOpts(), ds, { emptyText: '没有 DaemonSet' });
  }

  D.boot({ url: function () { return '/api/ui/datacenter/workloads'; }, render: render });
})();"""


# ============================================================ NODES
NODES_CONTENT = """    <div class="sre-kpi-grid" style="margin-bottom:24px">
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-node-count">—</div>
        <div class="sre-kpi-label">节点总数</div>
        <div class="sre-kpi-delta sre-text-muted" id="kpi-node-ready">—</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-node-cpu">—</div>
        <div class="sre-kpi-label">可分配 CPU</div>
        <div class="sre-kpi-delta sre-text-muted" id="kpi-node-cpu-cap">—</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-node-mem">—</div>
        <div class="sre-kpi-label">可分配内存</div>
        <div class="sre-kpi-delta sre-text-muted" id="kpi-node-mem-cap">—</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-node-pods">—</div>
        <div class="sre-kpi-label">已调度 Pod</div>
        <div class="sre-kpi-delta sre-text-muted">跨所有节点</div>
      </div>
    </div>

    <div id="node-cards"></div>"""

NODES_SCRIPT = """(function () {
  'use strict';
  var D = window.DCC, $ = D.$;

  function row(label, value, extra) {
    return '<div class="sre-list-row"><span class="sre-text-muted">' + D.esc(label) +
      '</span><span style="font-variant-numeric:tabular-nums">' + value + '</span></div>' +
      (extra || '');
  }

  function render(d, ctx) {
    if (!d.connected) {
      ctx.banner(d, '');
      ['kpi-node-count','kpi-node-cpu','kpi-node-mem','kpi-node-pods'].forEach(function (id) { $(id).textContent = '—'; });
      ['kpi-node-ready','kpi-node-cpu-cap','kpi-node-mem-cap'].forEach(function (id) { $(id).textContent = '—'; });
      $('node-cards').innerHTML = '';
      return;
    }
    var ns = d.nodes || [];
    var ready = ns.filter(function (n) { return n.ready; }).length;
    var totalPods = ns.reduce(function (s, n) { return s + n.pods; }, 0);
    var cpu = ns.reduce(function (s, n) { return s + (n.allocatable_cpu || 0); }, 0);
    var mem = ns.reduce(function (s, n) { return s + (n.allocatable_memory || 0); }, 0);

    $('kpi-node-count').textContent = ns.length;
    $('kpi-node-ready').textContent = ready + ' 个 Ready';
    $('kpi-node-cpu').textContent = cpu ? D.fmtCores(cpu) : '—';
    $('kpi-node-cpu-cap').textContent = 'capacity ' + (ns[0] && ns[0].capacity_cpu ? D.fmtCores(ns[0].capacity_cpu) : '—');
    $('kpi-node-mem').textContent = mem ? D.fmtBytes(mem) : '—';
    $('kpi-node-mem-cap').textContent = 'capacity ' + (ns[0] && ns[0].capacity_memory ? D.fmtBytes(ns[0].capacity_memory) : '—');
    $('kpi-node-pods').textContent = totalPods;
    ctx.banner(d, ns.length + ' 个节点，' + ready + ' 个 Ready');

    $('node-cards').innerHTML = ns.map(function (n) {
      // A node with pressure is the actionable state; a node without it should
      // not get a green badge that implies it was checked and praised.
      var pressure = (n.pressure && n.pressure.length)
        ? n.pressure.map(function (p) { return D.badge('error', p); }).join(' ')
        : '<span class="sre-text-tertiary">无压力条件</span>';
      var availPct = (n.capacity_memory && n.allocatable_memory)
        ? Math.round(n.allocatable_memory / n.capacity_memory * 100) : null;
      return '<div class="sre-card dc-panel" style="margin-bottom:16px">' +
        '<div class="dc-panel-head">' +
          '<div>' +
            '<div class="dc-panel-title" style="font-family:var(--sre-font-mono)">' + D.esc(n.name) + '</div>' +
            '<div class="dc-panel-sub">kubelet ' + D.esc(n.kubelet_version || '—') + '</div>' +
          '</div>' +
          D.badge(n.ready ? 'success' : 'error', n.ready ? 'Ready' : 'NotReady') +
        '</div>' +
        '<div class="dc-grid-2" style="gap:24px;margin-bottom:0">' +
          '<div class="sre-list">' +
            row('CPU 可分配', n.allocatable_cpu ? D.fmtCores(n.allocatable_cpu) : '—') +
            row('CPU 容量', n.capacity_cpu ? D.fmtCores(n.capacity_cpu) : '—') +
          '</div>' +
          '<div class="sre-list">' +
            row('内存可分配', n.allocatable_memory ? D.fmtBytes(n.allocatable_memory) : '—') +
            row('内存容量', n.capacity_memory ? D.fmtBytes(n.capacity_memory) : '—') +
          '</div>' +
        '</div>' +
        '<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--sre-border)">' +
          '<div class="sre-text-tertiary" style="margin-bottom:8px">已调度 ' + n.pods + ' 个 Pod' +
          (availPct !== null ? ' · 可分配内存占容量 ' + availPct + '%' : '') + '</div>' +
          '<div class="sre-text-tertiary" style="margin-bottom:6px">压力条件</div>' +
          '<div>' + pressure + '</div>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  D.boot({ url: function () { return '/api/ui/datacenter/nodes'; }, render: render });
})();"""


# ========================================================== STORAGE
ST_CONTENT = """    <div class="sre-kpi-grid" style="margin-bottom:24px">
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-pvc-total">—</div>
        <div class="sre-kpi-label">PVC 总数</div>
        <div class="sre-kpi-delta sre-text-muted">持久卷声明</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-pvc-bound">—</div>
        <div class="sre-kpi-label">已绑定</div>
        <div class="sre-kpi-delta sre-text-muted">Bound</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-pvc-bad">—</div>
        <div class="sre-kpi-label">未绑定</div>
        <div class="sre-kpi-delta sre-text-muted">Pending / Lost 等</div>
      </div>
      <div class="sre-card sre-kpi">
        <div class="sre-kpi-value" id="kpi-pvc-cap">—</div>
        <div class="sre-kpi-label">申请总容量</div>
        <div class="sre-kpi-delta sre-text-muted">所有 PVC 之和</div>
      </div>
    </div>

    <div class="sre-card dc-panel" style="margin-bottom:24px">
      <div class="dc-panel-title">持久卷声明</div>
      <div class="dc-panel-sub">阶段与申请容量</div>
      <div id="pvc-table"></div>
    </div>

    <div class="sre-card dc-panel">
      <div class="dc-panel-title">StorageClass</div>
      <div class="dc-panel-sub">供应者与回收策略</div>
      <div id="sc-table"></div>
    </div>"""

ST_SCRIPT = """(function () {
  'use strict';
  var D = window.DCC, $ = D.$;

  function render(d, ctx) {
    if (!d.connected) {
      ctx.banner(d, '');
      ['kpi-pvc-total','kpi-pvc-bound','kpi-pvc-bad','kpi-pvc-cap'].forEach(function (id) { $(id).textContent = '—'; });
      ['pvc-table','sc-table'].forEach(function (id) { $(id).innerHTML = ''; });
      return;
    }
    var pvcs = d.pvcs || [], scs = d.storage_classes || [], s = d.summary || {};
    var cap = pvcs.reduce(function (a, r) { return a + (r.requested_bytes || 0); }, 0);

    $('kpi-pvc-total').textContent = s.total;
    $('kpi-pvc-bound').textContent = s.bound;
    $('kpi-pvc-bad').textContent = s.not_bound;
    $('kpi-pvc-bad').style.color = s.not_bound > 0 ? 'var(--sre-state-error)' : '';
    $('kpi-pvc-cap').textContent = cap ? D.fmtBytes(cap) : '—';
    ctx.banner(d, s.total + ' 个 PVC，' + s.bound + ' 个已绑定');

    D.table($('pvc-table'), [
      { title: '状态', render: function (r) {
          // Bound is the only phase that is not a problem; everything else
          // (Pending, Lost) means a volume the workload cannot use.
          return D.badge(r.phase === 'Bound' ? 'success' : 'error', r.phase);
        } },
      { title: '命名空间', render: function (r) { return D.esc(r.namespace); } },
      { title: '名称', render: function (r) { return D.esc(r.name); },
        cellStyle: 'font-family:var(--sre-font-mono);font-size:13px' },
      { title: 'StorageClass', render: function (r) { return D.esc(r.storage_class || '—'); } },
      { title: '申请容量', render: function (r) {
          return D.esc(r.requested_bytes === null || r.requested_bytes === undefined
            ? '—' : D.fmtBytes(r.requested_bytes));
        }, cellStyle: 'text-align:right' }
    ], pvcs, { emptyText: '没有 PersistentVolumeClaim' });

    D.table($('sc-table'), [
      { title: '名称', render: function (r) { return D.esc(r.name); },
        cellStyle: 'font-family:var(--sre-font-mono);font-size:13px' },
      { title: 'Provisioner', render: function (r) { return D.esc(r.provisioner); } },
      { title: '回收策略', render: function (r) { return D.esc(r.reclaim_policy); } },
      { title: '绑定模式', render: function (r) { return D.esc(r.binding_mode); } }
    ], scs, { emptyText: '没有 StorageClass' });
  }

  D.boot({ url: function () { return '/api/ui/datacenter/storage'; }, render: render });
})();"""


TARGETS = [
    ("datacenter-pods.html", "Pod", "dc-pods", "Pod", "集群内所有 Pod 的阶段、就绪与重启情况", POD_CONTENT, POD_SCRIPT),
    ("datacenter-workloads.html", "工作负载", "dc-workloads", "工作负载", "控制器副本对齐情况", WL_CONTENT, WL_SCRIPT),
    ("datacenter-nodes.html", "节点", "dc-nodes", "节点", "节点状态、容量与压力条件", NODES_CONTENT, NODES_SCRIPT),
    ("datacenter-storage.html", "存储", "dc-storage", "存储", "持久卷声明与 StorageClass", ST_CONTENT, ST_SCRIPT),
]

for filename, title, active, heading, subtitle, content, script in TARGETS:
    (PAGES / filename).write_text(
        page(title, active, heading, subtitle, content, script), encoding="utf-8"
    )
    print("wrote %s" % filename)
