// 站内「当前上下文」：拉取 /api/ui/context，把真实集群 / 命名空间 / 告警 / 待审批填进页面。
//
// 页面只需声明挂点，不用写 JS：
//   data-sre-context="cluster"      填入真实集群名
//   data-sre-context="namespaces"   填入受监控命名空间数量（title 里是完整列表）
//   data-sre-context="alerts"       告警徽标，按严重程度着色
//   data-sre-context="approvals"    待审批徽标
//   容器内带 data-sre-ctx-state     的连接状态圆点，按连通性着色
//
// 接口失败时保留占位符并标记为不可用 —— 绝不回填假值。这个面板的用途是告诉
// 操作者下一条命令会落到哪里，编一个集群名比空着更糟。
(function () {
  // 页面里若没有 lucide 之类依赖，也不影响本脚本；纯 DOM 操作。
  function badge(el, n, level) {
    el.textContent = n > 99 ? '99+' : String(n);
    el.classList.remove('sre-badge-error', 'sre-badge-warning', 'sre-badge-success', 'sre-badge-info');
    el.classList.add('sre-badge-' + level);
  }

  function unavailable(el, why) {
    el.textContent = '—';
    el.title = why || '上下文接口不可用';
  }

  function apply(ctx) {
    // 集群名：来自 kubeconfig context / in-cluster 标识，不依赖 API Server，
    // 因此即使集群连不上也照常填。
    document.querySelectorAll('[data-sre-context="cluster"]').forEach(function (el) {
      el.textContent = (ctx.cluster && ctx.cluster.name) || '—';
      el.title = ctx.cluster
        ? (ctx.cluster.in_cluster ? '集群内 ServiceAccount 身份' : 'kubeconfig context: ' + ctx.cluster.name)
        : '';
    });

    const ns = ctx.namespaces || {};
    document.querySelectorAll('[data-sre-context="namespaces"]').forEach(function (el) {
      if (!ctx.connected) {
        unavailable(el, '无法连接集群：' + (ctx.error || ''));
        return;
      }
      const watched = ns.watched || [];
      el.textContent = watched.length + ' 个' + (ns.explicit ? '（已指定）' : '');
      el.title = watched.join('、') +
        '（集群共 ' + (ns.total || 0) + ' 个命名空间' +
        (ns.explicit ? '，受 DEFAULT_NAMESPACES 指定' : '，已排除系统命名空间') + '）';
    });

    // 告警与待审批来自进程内状态（Postgres findings / 会话），集群挂了也准确。
    const a = ctx.alerts || { open: 0, critical: 0 };
    document.querySelectorAll('[data-sre-context="alerts"]').forEach(function (el) {
      badge(el, a.open || 0, (a.critical || 0) > 0 ? 'error' : (a.open || 0) > 0 ? 'warning' : 'success');
      el.title = '未处理告警 ' + (a.open || 0) + ' 条（critical ' + (a.critical || 0) +
        ' / warning ' + (a.warning || 0) + '）';
    });

    const p = (ctx.approvals && ctx.approvals.pending) || 0;
    document.querySelectorAll('[data-sre-context="approvals"]').forEach(function (el) {
      badge(el, p, p > 0 ? 'warning' : 'success');
      el.title = p > 0 ? p + ' 项变更操作等待人工确认' : '当前没有待审批的变更';
    });

    document.querySelectorAll('[data-sre-ctx-state]').forEach(function (dot) {
      dot.setAttribute('data-state', ctx.connected ? 'live' : 'down');
      dot.title = ctx.connected ? '已连接集群' : ('集群不可达：' + (ctx.error || ''));
    });
  }

  function markAllUnavailable() {
    document.querySelectorAll('[data-sre-context]').forEach(function (el) {
      unavailable(el, '上下文接口不可用');
    });
    document.querySelectorAll('[data-sre-ctx-state]').forEach(function (dot) {
      dot.setAttribute('data-state', 'down');
    });
  }

  function load() {
    fetch('/api/ui/context')
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(apply)
      .catch(markAllUnavailable);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
})();
