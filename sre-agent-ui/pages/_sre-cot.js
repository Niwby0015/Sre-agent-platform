// 工作台「思维链」面板：把 agent 的推理、工具调用与回报渲染成可读时间线。
//
// 数据有两个来源，产出的是同一种步骤对象：
//   实时 —— SSE 的 `cot` 事件（后端 _stream_agent 边流边发）
//   回放 —— GET /api/sessions/{id}/cot（后端从 langgraph checkpoint 重建）
// 回合结束时页面会用回放结果整体替换实时视图，所以两者必须能互相替换；
// 步骤键（step.key）相同即视为同一步，就地更新而不是追加。
//
// 与其它 console 页一致：纯 DOM 操作、无构建步骤。这里的内容是模型输出和工具
// 返回，全部走 textContent —— 不解析 markdown、不拼 HTML 字符串。
(function () {
  const LABEL = { reasoning: '推理', tool_call: '调用', tool_result: '回报', hitl: '审批', error: '错误' };
  const EMPTY = '开始对话后，这里显示 agent 的推理与动作';

  function dotClass(step) {
    // 状态不只用颜色表达——每行都带一个词（沿用 console 页面的既有规则）
    if (step.kind === 'error') return 'sre-status-fail';
    if (step.kind === 'hitl') return 'sre-status-warn';
    if (step.kind === 'reasoning') return 'sre-status-idle';
    if (step.status === 'running') return 'sre-status-run';
    return 'sre-status-pass';
  }

  function stepEl(step) {
    const wrap = document.createElement('div');
    wrap.className = 'sre-cot-step';
    wrap.dataset.key = step.key || '';

    const head = document.createElement('div');
    head.className = 'sre-cot-step-head';
    const dot = document.createElement('span');
    dot.className = 'sre-status-dot ' + dotClass(step);
    const label = document.createElement('span');
    label.className = 'sre-cot-label';
    label.textContent = LABEL[step.kind] || step.kind || '';
    const title = document.createElement('span');
    title.className = 'sre-cot-step-title';
    title.textContent = step.title || '';
    head.append(dot, label, title);
    wrap.appendChild(head);

    const meta = [];
    if (step.detail) meta.push(step.detail);
    if (step.node) meta.push('归属 ' + step.node);
    if (step.status === 'running') meta.push('进行中');
    if (meta.length) {
      const metaEl = document.createElement('div');
      metaEl.className = 'sre-cot-step-meta';
      metaEl.textContent = meta.join(' · ');
      wrap.appendChild(metaEl);
    }

    if (step.raw) {
      const det = document.createElement('details');
      const sum = document.createElement('summary');
      sum.textContent = '原文';
      const pre = document.createElement('pre');
      pre.className = 'sre-cot-raw';
      pre.textContent = step.raw;
      det.append(sum, pre);
      wrap.appendChild(det);
    }
    return wrap;
  }

  const box = () => document.getElementById('sre-cot-steps');
  const badge = () => document.getElementById('sre-cot-source');

  function setSource(text) {
    const el = badge();
    if (!el) return;
    if (text) { el.textContent = text; el.hidden = false; }
    else { el.hidden = true; }
  }

  function emptyState(message) {
    const host = box();
    if (!host) return;
    host.innerHTML = '';
    const el = document.createElement('div');
    el.className = 'sre-cot-empty';
    el.textContent = message;
    host.appendChild(el);
  }

  function append(step) {
    const host = box();
    if (!host || !step) return;
    const empty = host.querySelector('.sre-cot-empty');
    if (empty) empty.remove();
    let existing = null;
    for (const el of host.children) {
      if (el.dataset && el.dataset.key === step.key) { existing = el; break; }
    }
    const el = stepEl(step);
    if (existing) existing.replaceWith(el);
    else host.appendChild(el);
    host.scrollTop = host.scrollHeight;
  }

  function render(steps) {
    const host = box();
    if (!host) return;
    if (!steps || !steps.length) { emptyState(EMPTY); return; }
    host.innerHTML = '';
    steps.forEach(s => host.appendChild(stepEl(s)));
    host.scrollTop = host.scrollHeight;
  }

  function reset() {
    emptyState(EMPTY);
    setSource('');
  }

  function init() {
    const card = document.getElementById('sre-cot-card');
    const toggle = document.getElementById('sre-cot-toggle');
    if (toggle && card) {
      toggle.addEventListener('click', () => {
        const active = toggle.getAttribute('data-active') !== 'true';
        toggle.setAttribute('data-active', String(active));
        card.classList.toggle('sre-cot-card-collapsed', !active);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.SRECot = { append: append, render: render, reset: reset, setSource: setSource };
})();
