// 站内告警未读角标：拉取 /api/ui/alerts，把未处理告警数打到侧栏「告警中心」和顶栏「通知」铃铛上。
// 纯增强，接口失败时静默退出，不影响页面。
(function () {
  function inject() {
    fetch('/api/ui/alerts')
      .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(data => {
        const n = (data.summary && data.summary.open) || 0;
        const crit = (data.summary && data.summary.critical) || 0;

        // 1) 侧栏「告警中心」计数徽标（告警中心页自身不显示，避免重复）
        const nav = document.querySelector('[data-nav-key="alerts"]');
        if (nav && nav.getAttribute('data-active') !== 'true') {
          let pill = nav.querySelector('.sre-nav-badge');
          if (n > 0) {
            if (!pill) {
              pill = document.createElement('span');
              pill.className = 'sre-nav-badge';
              nav.appendChild(pill);
            }
            pill.textContent = n > 99 ? '99+' : String(n);
            pill.style.cssText =
              'margin-left:auto;min-width:18px;height:18px;padding:0 5px;border-radius:9px;' +
              'background:var(--sre-state-error,#dc2626);color:#fff;font-size:11px;line-height:18px;' +
              'text-align:center;font-weight:600;display:inline-flex;align-items:center;justify-content:center';
          } else if (pill) {
            pill.remove();
          }
        }

        // 2) 顶栏铃铛红点
        const bell = document.querySelector('button[aria-label="通知"], [aria-label="通知"]');
        if (bell) {
          let dot = bell.querySelector('.sre-bell-dot');
          if (n > 0) {
            if (!dot) {
              dot = document.createElement('span');
              dot.className = 'sre-bell-dot';
              bell.appendChild(dot);
            }
            dot.style.cssText =
              'position:absolute;top:6px;right:6px;width:8px;height:8px;border-radius:50%;' +
              'background:var(--sre-state-error,#dc2626);box-shadow:0 0 0 2px var(--sre-surface,#0f172a)';
            if (getComputedStyle(bell).position === 'static') bell.style.position = 'relative';
          } else if (dot) {
            dot.remove();
          }
        }
      })
      .catch(() => {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
