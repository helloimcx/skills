'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const trunc = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s; };
function ago(sec){ if (sec == null) return '—';
  if (sec < 90) return Math.round(sec) + ' 秒';
  if (sec < 5400) return Math.round(sec/60) + ' 分钟';
  if (sec < 172800) return (sec/3600).toFixed(1) + ' 小时';
  return (sec/86400).toFixed(1) + ' 天'; }
const two = n => String(n).padStart(2, '0');
const fmtShort = e => { if (e == null) return '—';
  const d = new Date(e*1000), n = new Date();
  return (d.getFullYear() === n.getFullYear() ? '' : d.getFullYear() + '-') +
    two(d.getMonth()+1) + '-' + two(d.getDate()) + ' ' + two(d.getHours()) + ':' + two(d.getMinutes()); };
const fmt = fmtShort;  // full string kept via title tooltips where it matters
// control-plane epochs arrive as text like 1788854889.87; github sends ISO-8601
const epochOf = v => {
  if (v == null || v === '') return null;
  const s = String(v).trim();
  if (/^17\d{8}(\.\d+)?$/.test(s)) return parseFloat(s);
  if (/^\d+(\.\d+)?$/.test(s)) return parseFloat(s);
  const t = Date.parse(s);
  return isNaN(t) ? null : t / 1000; };
const agoAbs = e => e == null ? null : Math.max(0, Date.now()/1000 - e);
const relWhen = v => { const e = epochOf(v); return e == null ? '—'
  : `<span title="${esc(String(v))}">${ago(agoAbs(e))}前 · ${fmtShort(e)}</span>`; };
const fmtCell = v => { const e = epochOf(v);   // humanize any timestamp-shaped cell
  return e == null ? esc(String(v ?? '')) : fmtShort(e); };

// ---- global project filter (clickable project names, header select, #p=&view= hash)
const HASH = () => { const m = k =>
  (location.hash.match(new RegExp('[?&]' + k + '=([^&]+)')) ||
   location.hash.match(new RegExp('#' + k + '=([^&]+)')) || [])[1];
  return {p: m('p') ? decodeURIComponent(m('p')) : '', view: m('view') || '',
          n: m('n') ? decodeURIComponent(m('n')) : '',
          exec: m('exec') ? decodeURIComponent(m('exec')) : ''}; };
const FILTER = {project: HASH().p};
const PROJECTS = () => (DATA.ov?.projects || []).map(p => p.org_repo);
const repoOf = org => ((DATA.ov?.projects || []).find(p => p.org_repo === org) || {}).repo
  || String(org || '').replace(/__/, '/');
// 展示名：org__repo → org/repo（projects.repo 字段即 org/repo 全名）；逻辑与深链仍用原始 org_repo
const orgRepoFull = repoOf;
const orgRepoShort = orgRepoFull;
const projSpan = org => `<span>${esc(orgRepoFull(org))}</span>`;
// 项目名统一展示（issue #325）：平台项目经 org/repo 映射；本地路径取末段 + 「本地」标记，
// 不与平台项目混排。未注册的 org__repo 也映射为 org/repo。
const displayProject = p => {
  const s = String(p || '').trim();
  if (!s) return '—';
  const known = (DATA.ov?.projects || []).find(x => x.org_repo === s);
  if (known) return known.repo;
  if (/^[a-zA-Z]:[\\/]/.test(s)){   // 裸文件系统路径（例如 D:\projects\skills）
    const leaf = s.replace(/[\\/]+$/, '').split(/[\\/]/).pop();
    return leaf ? leaf + '（本地项目）' : s;
  }
  return s.replace(/__/, '/');   // 双下划线原始 id → org/repo
};
// issue #334: 机器卡「授权项目」逗号串逐项过 displayProject——拆串逻辑留在
// projectsOf，展示语义与全站项目名一致（issue #325 的三种写法统一）。
const projectsOf = raw => (raw || '').split(',').filter(Boolean)
  .map(displayProject).join('、') || '无';
// 术语中文映射：chip 显示中文、title 保留平台原文；未命中原样展示
const ZH_MAP = {
  'Approved for manual merge': '可合并✓', 'Changes requested': '需修改',
  'Blocked': '阻塞', 'Validation failed': '校验失败',
  'open': '开放', 'closed': '已关闭',
  'SUCCEEDED': '成功', 'RUNNING': '运行中', 'CANCELLED': '已取消',
  'FAILED': '失败', 'BLOCKED': '阻塞', 'PENDING': '待处理',
  'SENT': '已发送', 'SENDING': '发送中', 'UNKNOWN': '状态未知',
  'enabled': '已启用', 'disabled': '已停用',
  'lease_expired': '租约过期', 'outbox': 'outbox 未决', 'pr': '评审阻塞'};
// 告警 rule → 中文标签（issue #324）：内部枚举直出用户界面不可读
const ALERT_RULE_ZH = {
  'needs_human_aging': '待决策积压', 'collect_stale': '采集过期',
  'machine_disabled': '机器停用', 'outbox_limbo': 'outbox 未决',
  'lease_expired': '租约过期', 'pr_blocked': 'PR 阻塞',
  'collect_never_ok': '从未采集成功'};
const zhRule = r => ALERT_RULE_ZH[r] || r;
const zhChip = (cls, v) => { const s = v || '—'; const t = ZH_MAP[s];
  return `<span class="chip ${cls}"${t ? ` title="${esc(s)}"` : ''}>${esc(t || s)}</span>`; };
// execution 终态/在途态 → chip 配色（renderLoops 与机器卡片共用）
const EXEC_CLS = {SUCCEEDED: 'ok', RUNNING: 'acc', CANCELLED: 'mut',
                  FAILED: 'err', BLOCKED: 'err'};
// payload 提取值里的 UUID / 长十六进制对人不承载数据量
const HUMANISH = v => !/^[0-9a-f]{8}-[0-9a-f]{4}|^[0-9a-f]{20,}/i.test(String(v));

let CURRENT_VIEW = 'kanban';
let DETAIL_N = '';          // decision detail currently open (issue number as string)
let DETAIL_ORG = '';        // org_repo of the issue shown in the detail view
let DETAIL_SEQ = 0;         // guard: a slow detail fetch must not paint over a newer one
function syncHash(){
  const parts = [];
  if (FILTER.project) parts.push('p=' + encodeURIComponent(FILTER.project));
  if (CURRENT_VIEW && CURRENT_VIEW !== 'kanban') parts.push('view=' + CURRENT_VIEW);
  if (CURRENT_VIEW === 'decision' && DETAIL_N) parts.push('n=' + encodeURIComponent(DETAIL_N));
  if (CURRENT_VIEW === 'run' && RUN_EXEC) parts.push('exec=' + encodeURIComponent(RUN_EXEC));
  const target = '#' + parts.join('&');
  if (location.hash !== target) location.hash = target;   // keeps back/forward working
}

function setFilter(p){
  const prev = FILTER.project;
  FILTER.project = p || '';
  // 项目聚焦切走时，正开着的详情属于旧项目——回看板，别把旧详情锚进新 hash
  if (CURRENT_VIEW === 'decision' && FILTER.project !== prev
      && FILTER.project !== DETAIL_ORG) {
    CURRENT_VIEW = 'kanban';
    DETAIL_N = '';
    DETAIL_ORG = '';
    showView('kanban');
  }
  if (CURRENT_VIEW === 'run' && FILTER.project !== prev
      && FILTER.project !== RUN_ORG) {
    stopRunPoll();
    CURRENT_VIEW = 'kanban';
    RUN_EXEC = '';
    RUN_ORG = '';
    showView('kanban');
  }
  syncHash();
  renderFilterBar();
  // trends / events / 看板都随聚焦变化，直接整页重取（4 个只读接口并行）
  refresh();
  // 点击项目后立刻给出看得见的反应：从项目页跳到看板页
  if (FILTER.project && FILTER.project !== prev
      && document.querySelector('#tabs button.active')?.dataset.v === 'projects')
    showView('kanban');
}
window.setFilter = setFilter;
window.showView = v => document.querySelector(`#tabs button[data-v="${v}"]`).click();
const inScope = x => !FILTER.project || x.org_repo === FILTER.project;

function renderFilterBar(){
  const ps = PROJECTS();
  const sel = $('#proj-filter');
  sel.style.display = (ps.length > 1 || FILTER.project) ? 'inline-block' : 'none';
  sel.innerHTML = '<option value="">全部项目</option>' +
    ps.map(p => `<option value="${esc(p)}"${p === FILTER.project ? ' selected' : ''}>${esc(orgRepoFull(p))}</option>`).join('');
  sel.value = FILTER.project || '';
  document.body.classList.toggle('hide-proj', ps.length <= 1);
  const bar = $('#focus-bar');
  bar.style.display = FILTER.project ? 'flex' : 'none';
  $('#focus-label').textContent = '聚焦项目：' + orgRepoFull(FILTER.project);
  document.querySelectorAll('#project-cards .card').forEach(c =>
    c.classList.toggle('focused', c.dataset.proj === FILTER.project));
  fitKanban();   // 聚焦条出现/消失会整体推移看板
}
async function jget(p){ const r = await fetch(p); if (!r.ok) throw new Error(p+': '+r.status);
  return r.json(); }

const VERDICT_STYLE = {
  'Approved for manual merge': 'ok', 'Changes requested': 'warn',
  'Blocked': 'err', 'Validation failed': 'err'};

let DATA = {ov:null, q:null, it:null, tr:null, rt:null, kb:null, wk:null};
// ===== issue #314：采集后台单飞 + 载荷未变不重绘 + 切视图卸载 =====
function collectStart(project){   // 提交并等待后台单飞完成；POST 失败立即 resolve null
  const org = project ?? FILTER.project;
  return fetch('/api/collect' + (org ? '?project=' + encodeURIComponent(org) : ''), {method: 'POST', headers: {'X-Requested-With': 'dashboard-fetch'}}).then(r => {
    if (!r.ok) return null;   // 4xx/5xx 立即失败，不进轮询（对齐改前 resp.ok 语义）
    return new Promise(resolve => {   // 轮询 status 到 idle（8min 兜底防挂）
      const t0 = Date.now(), poll = setInterval(async () => {
        let st = null;
        try { st = await (await fetch('/api/collect/status')).json(); } catch (e) {}
        if ((st && !st.running) || Date.now() - t0 > 480000) { clearInterval(poll); resolve(st); }
      }, 3000);
    });
  }).catch(() => null);
}
const LAST_PAINT = {}, paintIfChanged = (key, text, paint) => { if (LAST_PAINT[key] !== text) { LAST_PAINT[key] = text; paint(); } };
// 切走即卸载（万节点常驻是刷新长任务的放大器）；切回由进入路径 load+render 重载。
const VIEW_SLOTS = {
  kanban: ['#kanban-board'], decision: ['#decision-detail'], run: ['#run-detail'],
  history: ['#hist-table'], operator: ['#op-table'], projects: ['#project-cards'],
  pipeline: ['#flow-strip', '#pr-table', '#issue-table'], loops: ['#machines', '#loop-cards', '#runtime', '#cycle-cards'],
  trends: ['#chart', '#chart-legend'], events: ['#ev-chart', '#ev-list']};
function unmountViews(except){
  for (const [v, sels] of Object.entries(VIEW_SLOTS)){
    for (const sel of sels){ const el = $(sel);
      if (v !== except && el && el.children.length) el.innerHTML = ''; }
    delete LAST_PAINT[v];   // 卸载后重进必重绘（paint 键 = 视图名）
  }
  for (const k of ['ov', 'q', 'it', 'tr']) delete LAST_PAINT[k];   // 隐藏容器由下一次 refresh 恢复
}
let RT_PROJECT = '';

const ALERT_VIEW = {loop_stall: 'loops', lease_expired: 'loops', outbox_limbo: 'loops',
                    collect_stale: 'projects', machine_disabled: 'projects',
                    worker_stall: 'loops'};
window.jumpAlert = (org, view) => {
  $('#alert-panel').hidden = true;
  if (org && org !== FILTER.project) setFilter(org);
  showView(view || 'kanban');
};
function alertBar(){
  const list = DATA.ov?.alerts || [];
  const count = sev => list.filter(a => a.severity === sev).length;
  [['#b-err','err'], ['#b-warn','warn'], ['#b-info','info']].forEach(([id, sev]) => {
    const el = $(id), n = count(sev);
    el.textContent = ({err:'错误', warn:'注意', info:'提示'})[sev] + ' ' + n;
    el.classList.toggle('zero', n === 0);
  });
  // 同类去重：同 rule+org_repo 的告警只显示一条（如 needs_human_aging 62 条同类），
  // 计数并入该条文案，避免跑马灯式刷屏
  const seen = new Set();
  const dedup = list.filter(a => {
    const k = a.rule + '|' + a.org_repo;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  const groupCount = k => list.filter(a => a.rule + '|' + a.org_repo === k).length;
  $('#alert-items').innerHTML = dedup.length
    ? dedup.slice(0, 2).map(a =>
        `<span class="alert-line sev-${a.severity}">[${esc(orgRepoShort(a.org_repo))}] ${esc(a.message)}`
        + (groupCount(a.rule + '|' + a.org_repo) > 1
          ? ` <span class="mut">×${groupCount(a.rule + '|' + a.org_repo)}</span>` : '')
        + `</span>`)
      .join(' &nbsp;|&nbsp; ')
      + (list.length > 2 ? ` &nbsp;<span class="mut">…共 ${list.length} 条，点击查看</span>` : '')
    : '无告警';
}
window.toggleAlertPanel = () => {
  const p = $('#alert-panel');
  $('#alertbar').setAttribute('aria-expanded', p.hidden ? 'true' : 'false');
  if (!p.hidden){ p.hidden = true; return; }
  const list = DATA.ov?.alerts || [];
  p.innerHTML = `<div class="card"><div class="t mb-1">全部告警（${list.length}）
      <span class="small mut" style="font-weight:400">点击条目跳到对应视图</span></div>` +
    (list.length ? list.map(a => {
      const view = ALERT_VIEW[a.rule] || 'kanban';
      return `<div class="alert-row" role="button" tabindex="0" data-jump-org="${esc(a.org_repo)}" data-jump-view="${view}">
        <span class="sev-dot sev-${a.severity}" style="flex:0 0 .25rem;height:1.6rem"></span>
        <span class="proj">${esc(orgRepoShort(a.org_repo))}</span>
        <span style="flex:1">${esc(a.message)}</span>
        <span class="mut small" title="${esc(a.rule)}">${esc(zhRule(a.rule))}</span></div>`;
    }).join('') : '<div class="empty">✓ 无告警</div>') + '</div>';
  p.hidden = false;
};
document.addEventListener('click', e => {
  const row = e.target.closest('.alert-row');
  if (row){ jumpAlert(row.dataset.jumpOrg, row.dataset.jumpView); return; }
  if (e.target.closest('#alertbar')) toggleAlertPanel();
  else if (!e.target.closest('#alert-panel')) $('#alert-panel').hidden = true;
});

// 复制命令带点击回执；命令注册表避免引号转义进 onclick 属性
const CMDS = {}; let CMD_SEQ = 0;
const regCmd = cmd => { CMDS[++CMD_SEQ] = cmd; return CMD_SEQ; };
window.copyCmd = (id, btn) => copyText(CMDS[id] || '', btn);
window.copyText = async (t, btn) => {
  const done = ok => {
    if (!btn) return;
    if (btn.dataset.label == null) btn.dataset.label = btn.textContent;
    const old = btn.dataset.label;
    btn.textContent = ok ? '已复制 ✓' : '复制失败，请手选复制';
    if (!ok) { btn.title = '手动复制：' + t; btn.style.maxWidth = '22rem'; }
    btn.classList.toggle('btn-success', ok);
    btn.classList.toggle('btn-outline-primary', !ok);
    setTimeout(() => { btn.textContent = old;
      btn.classList.add('btn-outline-primary'); btn.classList.remove('btn-success'); }, 1200);
  };
  try { await navigator.clipboard.writeText(t); done(true); }
  catch (e) {
    try { const ta = document.createElement('textarea'); ta.value = t;
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand('copy'); ta.remove(); done(ok); }
    catch (e2) { done(false); }
  }
};
// 「已合并？验证」：结果存模块级状态，renderKanban 重渲染时回读——
// 避免点击后 await refresh() 重建 DOM 把回显写在脱离文档的旧节点上
let VERIFY = null;
const verifyText = (org, number) => {
  if (VERIFY && VERIFY.org === org && VERIFY.number === String(number)) {
    if (VERIFY.busy || Date.now()/1000 - VERIFY.at <= 90) return VERIFY.outcome;
  }
  return '已合并？验证';
};
window.collectVerify = async el => {
  if (VERIFY?.busy) return;   // 防重入
  const org = el.dataset.org || FILTER.project;
  const number = String(el.dataset.number ?? '');
  VERIFY = {org, number, outcome: '验证中…', busy: 1, at: Date.now()/1000};
  el.textContent = VERIFY.outcome;
  let ok = true;
  try { const st = await collectStart(org); ok = !!st && !st.last_error; }
  catch (e) { ok = false; }
  await refresh();   // 采集后取新数据再判定；本次 render 会以「验证中…」回显
  let outcome;
  if (!ok) outcome = '采集失败';
  else {
    const still = (DATA.q?.merges || []).some(m => m.org_repo === org
      && String(m.number) === number);
    outcome = still ? '仍在待合并' : '已不在待合并 ✓';
  }
  VERIFY = {org, number, outcome, busy: 0, at: Date.now()/1000};
  const live = document.querySelector(
    `.verify-link[data-org="${CSS.escape(org)}"][data-number="${CSS.escape(number)}"]`);
  if (live) live.textContent = outcome;   // 立即可见；90s 内的 60s 自动刷新也会回读同一文案
}

document.addEventListener('click', async ev => {
  const btn = ev.target.closest('.reply-send');
  if (!btn) return;
  const card = btn.closest('.card');
  const input = card && card.querySelector('.reply-input');
  const body = (input && input.value || '').trim();
  if (!body) { if (input) input.focus(); return; }
  btn.disabled = true; btn.textContent = '提交中…';
  let ok = false;
  try {
    const resp = await fetch('/api/reply', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'dashboard-fetch'},
      body: JSON.stringify({project: btn.dataset.org, number: Number(btn.dataset.number), body})
    });
    const data = await resp.json().catch(() => ({}));
    ok = resp.ok;
    if (!ok && data.error) { btn.textContent = '失败: ' + data.error; }
  } catch (e) { btn.textContent = '提交失败'; }
  if (ok) {
    // 详情页里回复成功 = 该决策将出队：就地反馈，不再强制跳走（返回队列交给用户）
    if (document.querySelector('#view-decision')?.classList.contains('active')) {
      const row = btn.closest('.reply-row');
      if (row) {
        const notice = document.createElement('div');
        notice.className = 'reply-done';
        notice.innerHTML = '<span class="chip ok">已提交 ✓</span>' +
          '<span>回复已发布，该决策将从队列出队。</span>' +
          '<a href="#" onclick="showView(\'kanban\');return false">返回看板</a>';
        row.replaceWith(notice);
      }
    }
    refresh();
    return;
  }
  btn.disabled = false;
  if (btn.textContent === '提交中…') btn.textContent = '提交回复';
  setTimeout(() => { if (!btn.disabled) btn.textContent = '提交回复'; }, 2000);
});

function queueCard(title, meta, url, extra='', internal=false, badge='', cardClick=''){
  const link = url ? `<a href="${esc(url)}"${internal ? '' : ' target="_blank"'}>${esc(trunc(title,80))}</a>`
                   : esc(trunc(title,80));
  // cardClick：整卡点击面（键盘可达性仍由标题 <a> 承担）；首个 <a> 即目标 href
  return `<div class="card"${cardClick ? ` onclick="${cardClick}" style="cursor:pointer"` : ''}><div class="t">${link}${badge ? ' ' + badge : ''}</div>
    <div class="s">${meta}</div>${extra}</div>`;
}

// 卡片摘要：剥离机器人签名行与 markdown 记号，取正文有效内容（issue #326）
function cardSummary(body, cap){
  const lines = String(body || '').split('\n')
    .filter(l => !/^\s*>\s*🤖\s*本条由 code-loop/.test(l)   // 机器人签名行
              && !/^\s*<!-- code-loop/.test(l)              // 机器标记
              && !/^\s*$/.test(l));                          // 空行
  let text = lines.join(' ').replace(/[#>*`~]/g, ' ').replace(/\s+/g, ' ').trim();
  return text ? trunc(text, cap || 120) : '';
}
// ---- decision detail view (entered via #p=<org>&view=decision&n=<number>)
// The only innerHTML channel for platform markdown: marked + DOMPurify, always.
function renderMd(text){
  if (window.marked && window.DOMPurify)
    return DOMPurify.sanitize(marked.parse(String(text ?? '')));
  console.warn('markdown libs missing; falling back to escaped plain text');
  return '<p>' + esc(text) + '</p>';
}
if (window.DOMPurify) DOMPurify.addHook('afterSanitizeAttributes', node => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer');
  }
});

async function openDetail(h){
  const seq = ++DETAIL_SEQ;
  DETAIL_N = String(h.n);
  DETAIL_ORG = h.p || '';
  CURRENT_VIEW = 'decision';
  // 详情是看板 block 列的子页：保持看板 tab 高亮以提供归属感（面包屑配合）
  document.querySelectorAll('#tabs button').forEach(b =>
    b.classList.toggle('active', b.dataset.v === 'kanban'));
  document.querySelectorAll('.view').forEach(v =>
    v.classList.toggle('active', v.id === 'view-decision'));
  const box = $('#decision-detail');
  box.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const d = await jget('/api/issue?project=' + encodeURIComponent(h.p || '') +
                         '&number=' + encodeURIComponent(h.n || ''));
    if (seq !== DETAIL_SEQ) return;   // superseded by a newer openDetail — drop it
    const dec = (DATA.q?.decisions || []).find(x => x.org_repo === d.org_repo
                                                   && x.number === d.number);
    box.innerHTML = `
      <div class="s small" style="margin-bottom:.5rem">
        <a href="#" onclick="showView('kanban');return false">← 返回看板</a>
        <span class="mut">｜ 看板 / 阻塞 / #${esc(d.number)}</span></div>
      <div class="card"><div class="t">#${esc(d.number)} ${esc(d.title)}
        ${d.state !== 'open' ? '<span class="chip mut">已关闭</span>' : ''}</div>
      <div class="s">${projSpan(d.org_repo)}
        <span>${relWhen(d.created_at)}创建</span>
        ${dec ? `<span>积压 ${ago(dec.waiting_seconds)}（自创建）</span>` : ''}
        <a href="${esc(d.html_url)}" target="_blank" rel="noopener noreferrer">在 GitHub 打开 ↗</a></div>
      <div class="md">${renderMd(d.body)}</div>
      ${(d.comments || []).map(c => `
        <div class="cmt"><div class="s small mut">@${esc(c.poster)} · ${relWhen(c.created_at)}</div>
        <div class="md">${renderMd(c.body)}</div></div>`).join('')}
      <div class="s small mut" style="margin-top:.8rem">直接回复即完成决策（如「1A 2B，本期做」）</div>
      <div class="reply-row">
        <textarea class="reply-input" rows="3" placeholder="输入决策回复…" data-org="${esc(d.org_repo)}" data-number="${esc(d.number)}"></textarea>
        <button class="btn btn-sm btn-primary reply-send" data-org="${esc(d.org_repo)}" data-number="${esc(d.number)}">提交回复</button>
      </div></div>`;
  } catch (e) {
    if (seq !== DETAIL_SEQ) return;
    box.innerHTML = `<div class="empty">详情加载失败：${esc(e.message)}
      <div class="s small"><a href="#" onclick="showView('kanban');return false">← 返回看板</a></div></div>`;
  }
}

// ---- run detail view (entered via #p=<org>&view=run&exec=<execution-id>；看板「审核中」列卡片整卡点击)
// 执行过程 = claude 会话 transcript 的完整回放：思考/工具/回答按时间序全量平铺，
// 不贴类型标签、不提供过滤；思考默认折叠（一行预览，点击展开），命令失败标红。
// 右侧控制面时间线 + 证据是确定性事实兜底：会话绑不上时视图退化但不空窗。
const RUN_POLL_MS = 2500;
let RUN_ORG = '', RUN_EXEC = '', RUN_SEQ = 0, RUN_OFFSET = 0;
let RUN_TIMER = null, RUN_TICK = null, RUN_STICK = true, RUN_STATE = '';
const runClock = e => { if (e == null) return '—'; const d = new Date(e * 1000);
  return two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds()); };
function stopRunPoll(){
  if (RUN_TIMER) { clearTimeout(RUN_TIMER); RUN_TIMER = null; }
  if (RUN_TICK) { clearInterval(RUN_TICK); RUN_TICK = null; }
}
function runEventNode(e){
  const body = e.fold   // 超长内容（思考）：默认折叠为一行预览
    ? `<details class="run-fold"><summary>${esc(trunc(e.text, 88))}</summary>
       <div class="run-full">${esc(e.text)}</div></details>`
    : esc(e.text);
  const cls = e.status === 'err' ? 'err' : (e.fold ? 'think' : (e.mono ? 'tool' : ''));
  const t = document.createElement('template');
  t.innerHTML = `<div class="run-ev ${cls}">
    <span class="tm">${runClock(e.t)}</span>
    <div class="tx">${body}${e.out ? `<div class="run-out">${esc(e.out)}</div>` : ''}</div></div>`;
  return t.content.firstChild;
}
function runBindChip(bound){
  if (bound === 'exact')
    return '<span class="chip" id="run-bind" title="会话流按 prompt 内 execution id 精确绑定">会话流 · 精确绑定</span>';
  if (bound === 'spawn-log')
    return '<span class="chip" id="run-bind" title="会话流来自本机 spawn 的轮日志（<state>/executions/<执行id>.log），claude / opencode 两种方言逐行解析；轮日志持久保留，终态轮也可回看">会话流 · 轮日志</span>';
  if (bound === 'window')
    return '<span class="chip" id="run-bind" title="时间窗内唯一候选会话，非精确匹配">会话流 · 时间窗推断</span>';
  if (bound === 'reported')
    return '<span class="chip" id="run-bind" title="会话流来自执行机 agent 的进度上报（远程轮与本地轮同构）">会话流 · 执行机上报</span>';
  if (bound === 'agent-session')
    return '<span class="chip" id="run-bind" title="子代理会话直读（zcode 子会话 parts / claude 子代理 transcript），与循环轮次同一平铺回放">会话流 · 子会话直读</span>';
  return '<span class="chip" id="run-bind" title="未找到该执行的会话记录（如 cron 轮次），只展示控制面时间线">无会话绑定 · 仅时间线</span>';
}
const runIsAgent = d => d?.execution?.source === 'agent';
function runGitHubLink(d){
  const num = d.item?.number;
  const card = num == null ? null
    : (DATA.kb?.reviewing || []).concat(DATA.kb?.review || [])
        .find(c => c.number === num && c.html_url);
  const url = card ? card.html_url : '';
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">在 GitHub 打开 PR ↗</a>` : '';
}
function runShellHtml(d){
  const ex = d.execution || {};
  const num = d.item?.number;
  const isAgent = runIsAgent(d);
  return `
    <div class="s small" style="margin-bottom:.5rem">
      <a href="#" onclick="showView(${isAgent ? "'history'" : "'kanban'"});return false">← 返回${isAgent ? '执行历史' : '看板'}</a>
      <span class="mut">｜ ${isAgent ? '执行历史 / 子代理执行' : '看板 / 审核中'} / ${num != null ? '#' + esc(num) : esc(d.item?.key || '')} / 运行详情</span></div>
    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;gap:8px 18px;flex-wrap:wrap;align-items:baseline">
        <div style="flex:1 1 380px;min-width:300px">
          <div class="t">${num != null ? '<span class="mono" style="color:var(--acc)">' + esc(num) + '</span> ' : ''}${esc(d.item?.title || d.item?.key || (isAgent ? '子代理执行' : '评审中'))}</div>
          <div class="s">${isAgent
            ? `<span>${esc(ex.project || '')}</span>${ex.platform ? `<span class="chip acc" title="会话来源平台">${esc(ex.platform)}</span>` : ''}${ex.parent_ref ? `<span class="chip mono" title="父会话指针">${esc(trunc(ex.parent_ref, 26))}</span>` : ''}`
            : `${projSpan(RUN_ORG)}<span>@${esc(ex.worker || '—')}</span>`}
            ${ex.branch ? `<span class="chip acc mono" title="PR 分支">${esc(ex.branch)}</span>` : ''}
            ${ex.worktree ? `<span class="mono mut" title="worktree">${esc(trunc(ex.worktree, 46))}</span>` : ''}</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">
          <div style="display:flex;gap:8px;align-items:center">
            <span id="run-state" class="chip acc">—</span>
            ${isAgent ? '' : '<span id="run-lease" class="chip">—</span>'}
            ${runBindChip(d.session?.bound)}</div>
          <div class="s"><span class="small">已运行 <b id="run-elapsed">—</b></span>
            ${runGitHubLink(d)}
          </div></div>
      </div></div>
    <div style="display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:14px;align-items:start" class="run-cols">
      <div class="card">
        <h2 style="font-size:13px;margin:0 0 8px;color:#5c6b78;font-weight:600">执行过程 · 实时刷新（思考/工具/回答全量平铺，不打标签；思考默认折叠）</h2>
        <div style="display:flex;margin-bottom:10px"><span class="mut" style="font-size:12px;margin-left:auto" id="run-stat"></span></div>
        <div style="position:relative">
          <div id="run-feed"></div>
          <div id="run-bottom"><button class="btn btn-sm btn-primary" style="border-radius:999px" onclick="runToBottom()">↓ 回到最新</button></div>
        </div></div>
      <div>
        <div class="card" style="margin-bottom:14px">
          <h2 style="font-size:13px;margin:0 0 8px;color:#5c6b78;font-weight:600">${isAgent ? '档案时间线（派生确定性事实）' : '控制面时间线（确定性事实，永远可得）'}</h2>
          <ul class="run-tl" id="run-tl"></ul></div>
        <div class="card">
          <h2 style="font-size:13px;margin:0 0 8px;color:#5c6b78;font-weight:600">${isAgent ? '档案凭证' : '证据（evidence）'}</h2>
          <div id="run-ev"></div></div></div></div>`;
}
function runPaintSide(d){
  const tl = $('#run-tl');
  if (tl) tl.innerHTML = (d.timeline || []).map(t => `
    <li style="position:relative;padding:0 0 14px 20px;border-left:2px solid #dfe5ea;margin-left:6px" class="run-tl-li">
      <div style="font-weight:600;font-size:13px">${esc(t.kind)}</div>
      <div class="mono mut" style="font-size:12px;word-break:break-all">${esc(trunc(typeof t.payload === 'string' ? t.payload : JSON.stringify(t.payload), 90))}</div>
      <div class="mono" style="font-size:11px;color:#9aa7b1">${t.at == null ? '' : fmtShort(t.at)}</div></li>`).join('')
    || '<div class="empty">暂无控制面事件</div>';
  const ev = $('#run-ev');
  if (ev) ev.innerHTML = (d.evidence || []).map(v => `
    <div style="margin-bottom:8px"><span class="chip acc">${esc(v.kind)}</span>
      <span class="mut" style="font-size:12.5px">${esc(trunc(typeof v.payload === 'string' ? v.payload : JSON.stringify(v.payload), 120))}</span></div>`).join('')
    || '<div class="empty">暂无证据</div>';
}
let RUN_COUNT = 0;
function runPaint(d, first){
  const ex = d.execution || {};
  RUN_STATE = ex.state || '';
  // 头部 chips：状态 / 租约 / 已运行
  const stateEl = $('#run-state');
  if (stateEl){
    const zh = ZH_MAP[RUN_STATE] || RUN_STATE || '—';
    stateEl.textContent = (RUN_STATE === 'RUNNING' ? '● ' : '') + zh;
    stateEl.className = 'chip ' + (EXEC_CLS[RUN_STATE] || 'mut');
  }
  const sess = d.session || {};
  RUN_OFFSET = sess.offset ?? RUN_OFFSET;
  RUN_SIZE = sess.size ?? RUN_SIZE;
  const feed = $('#run-feed');
  if (feed && first) feed.innerHTML = '';
  for (const e of (sess.events || [])){
    if (!feed) break;
    feed.appendChild(runEventNode(e));
    RUN_COUNT++;
  }
  const stat = $('#run-stat');
  if (stat) stat.textContent = `已渲染 ${RUN_COUNT} 条 · ${(((sess.bytes ?? sess.size) || 0) / 1024).toFixed(1)} KB`
    + (RUN_STATE && RUN_STATE !== 'RUNNING' ? ' · 流已结束（保留现场，可回看）' : '');
  if (feed && sess.events?.length && RUN_STICK) feed.scrollTop = feed.scrollHeight;
  runPaintSide(d);
}
function runTick(){
  // 租约倒计时 + 已运行时长（1s 局部时钟，仅在本视图存活）
  // 「租约」= 该轮任务占用的执行名额，倒计时归零即被回收（内部词首现处 tooltip 解释）
  const lease = $('#run-lease'), elapsed = $('#run-elapsed');
  if (lease && RUN_LEASE_END != null){
    const left = Math.round(RUN_LEASE_END - Date.now() / 1000);
    if (left > 0) { lease.textContent = `租约 ${two(Math.floor(left / 60))}:${two(left % 60)}`;
      lease.title = '租约 = 该轮占用的执行名额，归零即回收'; }
    else { lease.textContent = '租约已过期 · 等待收回'; lease.className = 'chip'; }
  }
  if (elapsed && RUN_FROZEN_ELAPSED != null)
    elapsed.textContent = ago(RUN_FROZEN_ELAPSED);   // 终态档案：冻结为实际时长
  else if (elapsed && RUN_START != null)
    elapsed.textContent = ago(Math.max(0, Date.now() / 1000 - RUN_START));
}
let RUN_LEASE_END = null, RUN_START = null, RUN_SIZE = 0, RUN_FROZEN_ELAPSED = null;
function runWantsMore(){
  // 运行中持续轮询；已终态的执行把积压排干即停——单次最多 400 条事件，
  // 深链已完成执行的全量回放靠 since 续拉，不能因终态而截断
  return RUN_STATE === 'RUNNING' || RUN_OFFSET < RUN_SIZE;
}
async function runPollOnce(seq){
  try {
    const d = await jget('/api/run?org=' + encodeURIComponent(RUN_ORG)
      + '&exec=' + encodeURIComponent(RUN_EXEC) + '&since=' + RUN_OFFSET);
    if (seq !== RUN_SEQ || CURRENT_VIEW !== 'run') return;
    runPaint(d, false);
  } catch (e) {}   // 轮询失败静默：下一轮重试，不抖动已渲染内容
  if (seq === RUN_SEQ && CURRENT_VIEW === 'run' && runWantsMore())
    RUN_TIMER = setTimeout(() => runPollOnce(seq), RUN_POLL_MS);
}
function scheduleRunPoll(seq){
  stopRunPoll();
  if (runWantsMore()) RUN_TIMER = setTimeout(() => runPollOnce(seq), RUN_POLL_MS);
  RUN_TICK = setInterval(runTick, 1000);
  runTick();
}
function runToBottom(){ RUN_STICK = true; const feed = $('#run-feed');
  if (feed) feed.scrollTop = feed.scrollHeight;
  $('#run-bottom').style.display = 'none'; }
async function openRun(h){
  const seq = ++RUN_SEQ;
  RUN_ORG = h.p || ''; RUN_EXEC = String(h.exec || '');
  const isAgentRun = RUN_EXEC.includes(':');   // agent-run id：执行历史入口
  const isOperatorRun = RUN_EXEC.startsWith('operator:');   // operator 轮：干预台账入口
  RUN_OFFSET = 0; RUN_COUNT = 0; RUN_STICK = true; RUN_FROZEN_ELAPSED = null;
  stopRunPoll();
  CURRENT_VIEW = 'run';
  // 与 decision 详情同一套归属语义：入口 tab 保持高亮，面包屑提供回路
  document.querySelectorAll('#tabs button').forEach(b =>
    b.classList.toggle('active', b.dataset.v === (isOperatorRun ? 'operator'
      : (isAgentRun ? 'history' : 'kanban'))));
  document.querySelectorAll('.view').forEach(v =>
    v.classList.toggle('active', v.id === 'view-run'));
  const box = $('#run-detail');
  box.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const d = await jget('/api/run?org=' + encodeURIComponent(RUN_ORG)
                         + '&exec=' + encodeURIComponent(RUN_EXEC));
    if (seq !== RUN_SEQ) return;
    RUN_LEASE_END = epochOf(d.execution?.expires_at);
    RUN_START = epochOf(d.execution?.created_at);
    RUN_FROZEN_ELAPSED = (runIsAgent(d) && d.execution?.duration_s != null)
      ? d.execution.duration_s : null;
    box.innerHTML = runShellHtml(d);
    // 思考折叠展开后可能超高：展开不改自动跟随；向上滚动即暂停跟随
    const feed = $('#run-feed');
    if (feed) feed.addEventListener('scroll', () => {
      RUN_STICK = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
      $('#run-bottom').style.display = RUN_STICK ? 'none' : 'block';
    });
    runPaint(d, true);
    scheduleRunPoll(seq);
  } catch (e) {
    if (seq !== RUN_SEQ) return;
    box.innerHTML = `<div class="empty">运行详情加载失败：${esc(e.message)}
      <div class="s small"><a href="#" onclick="showView('kanban');return false">← 返回看板</a></div></div>`;
  }
}

// ---- 执行历史（view=history）：循环轮次 + 子代理档案统一列表（行点击 → view=run）
// agent 筛选选项由响应的 agents facet 数据驱动填充，不硬编码 agent 清单。
let HIST = {rows: [], agents: [], projects: [], total: 0};
const HIST_COLLAPSED = new Set();   // 折叠的任务组（父会话指针）
const histDur = s => s == null ? '—'
  : (s >= 3600 ? Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分'
     : s >= 60 ? Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒'
     : s + ' 秒');
function histFilters(){
  return {src: document.querySelector('#hist-src .btn.active')?.dataset.src || 'all',
          state: $('#hist-state').value, agent: $('#hist-agent').value,
          project: $('#hist-project').value, q: $('#hist-q').value.trim()};
}
async function loadHistory(){
  const f = histFilters(), qs = new URLSearchParams();
  if (f.src !== 'all') qs.set('source', f.src);
  if (f.state) qs.set('state', f.state);
  if (f.agent) qs.set('agent', f.agent);
  if (f.project) qs.set('project', f.project);
  if (f.q) qs.set('q', f.q);
  qs.set('limit', '200');
  try { HIST = await jget('/api/history?' + qs); }
  catch (e) { $('#hist-table').innerHTML =
    `<div class="empty">执行历史加载失败：${esc(e.message)}</div>`; return; }
  // 筛选下拉全部由响应 facet 数据驱动填充（新增 agent/项目零改动）
  const fill = (id, list, cur) => {
    const sel = $(id);
    sel.innerHTML = sel.options[0].outerHTML + list.map(v =>
      `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(v)}</option>`).join('');
  };
  fill('#hist-agent', HIST.agents || [], f.agent);
  fill('#hist-project', HIST.projects || [], f.project);
  renderHistory();
}
function histRowHtml(r){
  const src = r.kind === 'loop'
    ? `<span class="chip acc" title="循环轮次">循环·${esc(r.loop || '—')}</span>`
    : `<span class="chip" title="子代理档案平台">${esc(r.platform)}</span><span class="chip">子代理</span>`;
  const hash = (r.kind === 'loop' && r.org_repo
      ? '#p=' + encodeURIComponent(r.org_repo) + '&' : '#')
    + 'view=run&exec=' + encodeURIComponent(r.id);
  return `<tr role="button" tabindex="0" data-hist-hash="${esc(hash)}"
    title="点击进入运行详情（${r.kind === 'loop' ? '循环轮次' : '子代理会话直读'}）">
    <td class="mono small">${r.started_at ? fmtShort(r.started_at) : '—'}</td>
    <td class="small">${esc(r.kind === 'loop' ? orgRepoFull(r.org_repo) : displayProject(r.project))}</td>
    <td>${src}</td>
    <td class="small" style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        title="${esc(r.title)}">${esc(r.title)}</td>
    <td>${zhChip(EXEC_CLS[r.state] || 'mut', r.state)}</td>
    <td class="small">${esc(r.agent || '—')}</td>
    <td class="mono small">${histDur(r.duration_s)}</td></tr>`;
}
function renderHistory(){
  const rows = HIST.rows || [];
  $('#hist-cnt').textContent = `共 ${HIST.total ?? rows.length} 条`;
  if (!rows.length){
    $('#hist-table').innerHTML = '<div class="empty">没有匹配的执行记录——放宽筛选，或点右上「立即采集」。</div>';
    return;
  }
  // 同父会话任务组：本页内 ≥2 条共享 parent_ref 的子代理归为一组（点组头折叠）
  const bucket = {};
  const gkeyOf = r => (r.kind === 'agent' && r.parent_ref
    && (bucket[r.parent_ref] = (bucket[r.parent_ref] || 0) + 1) ? r.parent_ref : null);
  rows.forEach(gkeyOf);                       // 先计数
  const gkeyOf2 = r => (r.kind === 'agent' && r.parent_ref
    && bucket[r.parent_ref] >= 2 ? r.parent_ref : null);
  const out = [], done = {};
  rows.forEach(r => {
    const k = gkeyOf2(r);
    if (!k){ out.push(histRowHtml(r)); return; }
    if (done[k]) return;
    done[k] = true;
    const open = !HIST_COLLAPSED.has(k);
    out.push(`<tr class="hist-group" role="button" tabindex="0" data-hist-group="${esc(k)}"
      title="点击${open ? '折叠' : '展开'}该任务组（父会话 ${esc(trunc(k, 36))}，共 ${bucket[k]} 条）">
      <td colspan="7" class="small mut" style="cursor:pointer">${open ? '▾' : '▸'} 任务组 · 父会话
        <span class="mono">${esc(trunc(k, 36))}</span>（${bucket[k]} 条子代理）</td></tr>`);
    if (!open) return;
    rows.forEach(m => { if (gkeyOf2(m) === k) out.push(histRowHtml(m)); });
  });
  $('#hist-table').innerHTML = `<div class="card" style="padding:0;overflow:hidden">
    <div class="table-responsive"><table class="table table-sm table-hover align-middle mb-0">
    <thead><tr><th>开始时间</th><th>项目</th><th>来源</th>
    <th style="min-width:280px">任务 / 轮次</th><th>状态</th><th>agent</th><th>耗时</th></tr></thead>
    <tbody>${out.join('')}</tbody></table></div></div>`;
}
document.addEventListener('click', e => {
  const g = e.target.closest('tr[data-hist-group]');
  if (g){
    const k = g.dataset.histGroup;
    HIST_COLLAPSED.has(k) ? HIST_COLLAPSED.delete(k) : HIST_COLLAPSED.add(k);
    renderHistory();
  }
});
$('#hist-src').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  document.querySelectorAll('#hist-src button').forEach(x => x.classList.toggle('active', x === b));
  loadHistory();
});
$('#hist-state').addEventListener('change', loadHistory);
$('#hist-project').addEventListener('change', loadHistory);
$('#hist-agent').addEventListener('change', loadHistory);
let HIST_QT = null;
$('#hist-q').addEventListener('input', () => {
  clearTimeout(HIST_QT); HIST_QT = setTimeout(loadHistory, 350);
});
document.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target instanceof Element
      && e.target.matches('tr[data-hist-hash]')){
    e.preventDefault(); location.hash = e.target.dataset.histHash;
  }
});
document.addEventListener('click', e => {
  if (e.target.closest('details')) return;   // 折叠区展开/收起不触发行导航（issue #328 repair）
  const tr = e.target.closest('tr[data-hist-hash]');
  if (tr) location.hash = tr.dataset.histHash;
});

// ---- Operator 干预台账（view=operator；后端 GET /api/operator，spec 2026-09-30-cp-operator-agent）
const OP_OC_LABEL = {RESOLVED: '已解决', MITIGATED_WATCHING: '已缓解·观察中',
  ESCALATED_HUMAN: '已升级人工', FALSE_POSITIVE_NOOP: '误报未动',
  RUNNING: '进行中', FAILED: '失败'};
const OP_OC_CLS = {RESOLVED: 'ok', MITIGATED_WATCHING: 'warn',
  ESCALATED_HUMAN: 'vio', FALSE_POSITIVE_NOOP: 'mut', RUNNING: 'acc', FAILED: 'err'};
const OP_SRC_LABEL = {alert: '告警', cp_event: 'CP 事件', watchdog: 'watchdog',
  manual: '手动'};
const OP_STATE_CLS = {SUCCEEDED: 'ok', RUNNING: 'acc', FAILED: 'err',
  VERIFYING: 'acc'};
let OP_ROWS = [];
let OP_OC = '';
async function loadOperator(){
  const data = await jget('/api/operator');
  OP_ROWS = data.rows || [];
}
function opOcKey(r){
  if (r.run_state === 'RUNNING' || r.run_state === 'VERIFYING') return 'RUNNING';
  if (r.run_state === 'FAILED') return 'FAILED';
  return r.outcome || '';
}
function opOcChip(key){
  if (!key) return '<span class="chip mut">待执行</span>';
  return `<span class="chip ${OP_OC_CLS[key] || 'mut'}" title="${esc(key)}">${esc(OP_OC_LABEL[key] || key)}</span>`;
}
function renderOperator(){
  const src = $('#op-src').value, q = ($('#op-q').value || '').trim();
  let n = 0;
  const rows = OP_ROWS.filter(r => {
    if (src && r.signal.source !== src) return false;
    if (OP_OC && opOcKey(r) !== OP_OC) return false;
    if (q){
      const hay = [r.signal.kind, r.signal.message, r.signal.org_repo,
        r.diagnosis, (r.actions || []).map(a => a.verb + ' ' + (a.target || '')).join(' ')]
        .join(' ').toLowerCase();
      if (!hay.includes(q.toLowerCase())) return false;
    }
    return true;
  }).map(r => {
    n++;
    const hash = '#view=run&exec=' + encodeURIComponent('operator:' + r.id);
    const acts = (r.actions || []).map(a =>
      `<span class="op-act${a.verb === 'escalate_human' ? ' human' : ''}"
         title="${esc(a.detail || '')}">${esc(a.verb)}${a.target ? ' · ' + esc(a.target) : ''}</span>`).join('');
    // 证据结构化：ref 长串截断为摘要，完整内容进 title；kind 作前缀
    const ev = (r.evidence || []).map(e =>
      `<span class="small mut" title="${esc(e.ref || '')}">${esc(e.kind || '')}: ${esc(trunc(e.ref || '', 60))}</span>`).join(' ');
    const dur = r.started_at && r.ended_at
      ? histDur(Math.max(1, Math.round(r.ended_at - r.started_at))) : '—';
    // 诊断摘要：长文本截断为一行，完整内容收进 <details> 展开区
    const diag = r.diagnosis || '';
    const diagHtml = diag
      ? (diag.length > 120
        ? `<details class="diag-fold"><summary onclick="event.stopPropagation()">${esc(trunc(diag, 120))}</summary>
            <div class="small" style="max-width:460px;margin-top:.3rem">${esc(diag)}</div></details>`
        : `<div class="small" style="max-width:460px">${esc(diag)}</div>`)
      : '';
    return `<tr role="button" tabindex="0" data-hist-hash="${esc(hash)}"
        title="点击进入运行详情（operator 轮 · 会话流=轮日志）">
      <td class="mono small">${r.started_at ? fmtShort(r.started_at) : '—'}<br>
        <span class="mut">${esc(dur)}</span></td>
      <td><span class="chip mut">${esc(OP_SRC_LABEL[r.signal.source] || r.signal.source)}</span></td>
      <td class="small"><b>${esc(r.signal.kind)}</b>${r.signal.org_repo
        ? ' · <span class="mut">' + esc(orgRepoFull(r.signal.org_repo)) + '</span>'
        : ' · <span class="mut">机器全局</span>'}
        <div class="mut" style="max-width:440px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(r.signal.message || '')}">${esc(r.signal.message || '')}</div>
        ${diagHtml}
        ${r.signal.note ? `<div class="vio small">手动说明：${esc(r.signal.note)}</div>` : ''}</td>
      <td>${acts || '<span class="small mut">—</span>'}
        ${ev ? '<div>' + ev + '</div>' : ''}</td>
      <td>${opOcChip(opOcKey(r))}</td>
      <td class="small">${esc(r.agent || '—')}${r.model
        ? '<br><span class="mut mono">' + esc(r.model) + '</span>' : ''}</td></tr>`;
  }).join('');
  $('#op-table').innerHTML = rows
    ? `<div class="card" style="padding:0;overflow:hidden"><div class="table-responsive">
      <table class="table mb-0"><thead><tr><th style="width:108px">时间</th>
      <th style="width:78px">来源</th><th>信号与诊断</th>
      <th style="width:250px">动作与证据</th><th style="width:118px">结局</th>
      <th style="width:88px">agent</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`
    : '<div class="empty">没有匹配的干预记录——放宽筛选再试。</div>';
  $('#op-cnt').textContent = '共 ' + n + ' 条';
}
$('#op-trigger-btn').addEventListener('click', e =>
  openTriggerDialog(e.currentTarget, '', '', 'operator'));
$('#op-src').addEventListener('change', renderOperator);
$('#op-oc').addEventListener('click', e => {
  const btn = e.target.closest('button'); if (!btn) return;
  document.querySelectorAll('#op-oc .btn').forEach(x =>
    x.classList.toggle('active', x === btn));
  OP_OC = btn.dataset.oc || '';
  renderOperator();
});
let OP_QT = null;
$('#op-q').addEventListener('input', () => {
  clearTimeout(OP_QT); OP_QT = setTimeout(renderOperator, 300);
});

const CP_PY = '%USERPROFILE%\\.zcode\\skills\\code-loop\\scripts\\control_plane.py';// outbox 未决的人工 reconcile 命令（动作能力已并入看板 blocked 列的 outbox 卡）
const reconcileCmd = cp_id =>
  `python "${CP_PY}" reconcile --operation ${cp_id} --receipt-file "<远端回执 JSON>"`;
const reconcileExtra = cp_id => `<div class="s"><button class="btn btn-sm btn-outline-primary"
  onclick="copyCmd(${regCmd(reconcileCmd(cp_id))}, this)">复制 reconcile 命令</button>
  <span class="small mut">需先核实远端回执；无 receipt 时 reconcile 会被拒绝</span></div>`;

// 动作视图已并入看板：状态条只承担「有没有要我做的」一眼判读，
// 具体动作挂在 ready / blocked 列的卡片上（复制合并命令 / 刷新验证 / 结论全文 / reconcile）
function renderActionStrip(){ renderActionStripBody(); fitKanban(); }  // 看板条会推移看板
function renderActionStripBody(){
  const all = DATA.q || {merges: [], decisions: [], exceptions: []};
  const q = {merges: all.merges.filter(inScope), decisions: all.decisions.filter(inScope),
             exceptions: all.exceptions.filter(inScope)};
  const total = q.merges.length + q.decisions.length + q.exceptions.length;
  const el = $('#kanban-strip');
  if (total){
    el.innerHTML = `<div class="strip">先做这些：<b>${q.merges.length}</b> 待合并 ·
      <b>${q.decisions.length}</b> 待决策 · <b>${q.exceptions.length}</b> 异常
      <span class="mut small">（分别对应看板待合并 / 阻塞列与告警条）</span></div>`;
    return;
  }
  const totalOpen = (DATA.it?.issues || []).filter(i => i.state === 'open' && inScope(i)).length;
  const reviewing = Object.entries(DATA.ov?.funnels || {})
    .filter(([org]) => inScope({org_repo: org}))
    .reduce((n, [,f]) => n + f.awaiting_review, 0);
  el.innerHTML = DATA.q ? `<div class="strip ok">✓ 无待人工事项
    <span class="mut">· ${reviewing} 个 PR 评审中 · ${totalOpen} 个 Issue 开放</span></div>` : '';
}

function renderProjects(){
  const ps = DATA.ov?.projects || [];
  const countChip = (label, n, cls, tip) =>
    `<span class="chip ${n ? cls : 'mut'}"${tip ? ` title="${tip}"` : ''}>${label} ${n}</span>`;
  $('#project-cards').innerHTML = ps.length ? ps.map(p => `
    <div class="card${p.org_repo === FILTER.project ? ' focused' : ''}" data-proj="${esc(p.org_repo)}"
      title="点击聚焦该项目（再点下拉选「全部项目」还原）"><div class="t">${esc(orgRepoFull(p.org_repo))}
      ${p.enabled ? zhChip('ok', 'enabled') : zhChip('mut', 'disabled')}</div>
    <div class="s"><span>@${esc(p.enabled_host||'—')}</span>
      <span>${esc(p.platform_url)}</span></div>
    <div class="s">${countChip('开放 Issue', p.counts.open_issues, 'acc', '开放中的 Issue 数（点击项目卡可聚焦查看）')}
      ${countChip('开放 PR', p.counts.open_prs, 'acc', '开放中的 PR 数（点击项目卡可聚焦查看）')}
      ${countChip('待合并', p.counts.pending_merges, 'acc', '评审通过待人工合并的 PR 数')}
      ${countChip('待决策', p.counts.pending_decisions, 'warn', '等待人工决策的 Issue 数')}
      ${countChip('异常', p.counts.exceptions, 'err', '告警条目数')}</div>
    <div class="s small">上次采集成功 ${relWhen(p.last_ok_collect_at)}${p.last_error ?
      ` <span class="err-text">${esc(trunc(p.last_error,140))}</span>` : ''}</div>
    <div class="s small"><a class="proj-link" role="button" tabindex="0" data-proj="${esc(p.org_repo)}">查看动作</a></div>
    </div>`).join('') : '<div class="empty">暂无项目 · 先运行 collect</div>';
}

function renderPipeline(){
  const orgs = FILTER.project ? [FILTER.project] : PROJECTS();
  const FUNNEL_CLS = {open_issues: 'acc', in_development: 'acc', awaiting_review: 'acc',
                      changes_requested: 'warn', blocked: 'err', ready_merge: 'ok'};
  $('#flow-strip').innerHTML = orgs.map(org => {
    const f = (DATA.ov?.funnels || {})[org] || {};
    const stages = [['open_issues', '打开'], ['in_development', '开发中'],
      ['awaiting_review', '待评审'], ['changes_requested', '需修改'],
      ['blocked', '阻塞'], ['ready_merge', '可合并']];
    const chip = (k, label) =>
      `<span class="chip ${f[k] ? (FUNNEL_CLS[k] || 'acc') : 'mut'}">${label} ${f[k] || 0}</span>`;
    return `<div class="funnel-row">
      <strong class="small">${esc(orgRepoShort(org))}</strong>`
      + stages.map(([k, label], i) =>
        (i ? '<span class="funnel-arrow">→</span>' : '') + chip(k, label)).join('')
      + '</div>';
  }).join('');
  const cutoff = ((DATA.ov?.generated_at) || Date.now()/1000) - 7*86400;
  const iso = v => (Date.parse(v || '') || 0) / 1000;
  const updated = x => Math.max(iso(x.updated_at), iso(x.closed_at), iso(x.merged_at), iso(x.created_at));
  const fresh = x => updated(x) >= cutoff;

  window.__prs = (DATA.it?.prs || []).filter(inScope).slice().sort(
    (a,b) => (a.state==='open'?0:1) - (b.state==='open'?0:1) || updated(b) - updated(a));
  const prs = window.__SHOW_ALL_PRS ? window.__prs : window.__prs.filter(fresh);
  const showProjCol = !FILTER.project;   // 聚焦单项目时项目列全同值，隐藏
  const projCell = p => showProjCol
    ? `<td class="col-proj"><a class="proj-link" role="button" tabindex="0" data-proj="${esc(p.org_repo)}" title="点击聚焦该项目">${esc(orgRepoShort(p.org_repo))}</a></td>` : '';
  const verdictCell = p => {
    if (p.merged) return '<span class="small ok">✓ 已合并</span>';
    if (p.state !== 'open') return '<span class="chip mut">已关闭</span>';
    return p.verdict
      ? zhChip(VERDICT_STYLE[p.verdict] || 'mut', p.verdict)
      : '<span class="chip mut">待评审</span>';
  };
  const prRow = p => `<tr>${projCell(p)}<td class="num">
      <a href="${esc(p.html_url)}" target="_blank">${p.number}</a></td>
      <td>${esc(trunc(p.title,70))}</td>
      <td>${verdictCell(p)}</td>
      <td>${p.merged ? '' : zhChip(p.state==='open'?'acc':'mut', p.state)}</td>
      <td class="small mut mono">${esc(trunc(p.head_sha,8))}</td>
      <td class="small mut">${relWhen(p.updated_at)}</td></tr>`;
  $('#pr-table').innerHTML = `<div class="table-responsive"><table class="table table-sm table-hover align-middle">
    <thead><tr>${showProjCol ? '<th class="col-proj">项目</th>' : ''}<th class="num">#</th><th>标题</th>
    <th>结论</th><th>状态</th><th>Head</th><th>更新</th></tr></thead><tbody>`
    + prs.map(prRow).join('')
    + ((!window.__SHOW_ALL_PRS && window.__prs.length > prs.length)
      ? `<tr class="hiddenrow"><td colspan="${showProjCol ? 7 : 6}"><a class="more" role="button" tabindex="0" onclick="togglePRs()">↓ 显示全部 ${window.__prs.length} 行（含 ${window.__prs.length - prs.length} 行 7 天前历史）</a></td></tr>`
      : '') + '</tbody></table></div>';

  const allIssues = (DATA.it?.issues || []).filter(inScope).slice().sort(
    (a,b) => (a.state==='open'?0:1) - (b.state==='open'?0:1) || updated(b) - updated(a));
  window.__issues = allIssues;
  const issues = window.__SHOW_ALL_ISSUES ? allIssues : allIssues.filter(i =>
    i.state === 'open' || iso(i.closed_at) >= cutoff);
  const issueRow = i => `<tr>
    ${showProjCol ? `<td class="col-proj"><a class="proj-link" role="button" tabindex="0" data-proj="${esc(i.org_repo)}" title="点击聚焦该项目">${esc(orgRepoShort(i.org_repo))}</a></td>` : ''}<td class="num">
    <a href="${esc(i.html_url)}" target="_blank">${i.number}</a></td>
    <td>${esc(trunc(i.title,70))}</td>
    <td class="small">${esc((JSON.parse(i.labels||'[]')||[]).join(', '))}</td>
    <td>${zhChip(i.state==='open'?'acc':'mut', i.state)}</td>
    <td class="small mut">${relWhen(i.updated_at)}</td></tr>`;
  $('#issue-table').innerHTML = `<div class="table-responsive"><table class="table table-sm table-hover align-middle">
    <thead><tr>${showProjCol ? '<th class="col-proj">项目</th>' : ''}<th class="num">#</th><th>标题</th>
    <th>标签</th><th>状态</th><th>更新</th></tr></thead><tbody>`
    + issues.map(issueRow).join('')
    + ((!window.__SHOW_ALL_ISSUES && allIssues.length > issues.length)
      ? `<tr class="hiddenrow"><td colspan="${showProjCol ? 6 : 5}"><a class="more" role="button" tabindex="0" onclick="toggleIssues()">↓ 显示全部 ${allIssues.length} 行（含 ${allIssues.length - issues.length} 行已关闭历史）</a></td></tr>`
      : '') + '</tbody></table></div>';
}
window.togglePRs = () => { window.__SHOW_ALL_PRS = true; renderPipeline(); };
window.toggleHistoryWorkers = () => { window.__SHOW_HISTORY_WORKERS = true; renderMachines(); };
window.toggleIssues = () => { window.__SHOW_ALL_ISSUES = true; renderPipeline(); };

function renderMachines(){
  const el = $('#machines');
  if (!el) return;
  const data = DATA.workers;
  if (!data){   // /api/workers 未加载或失败：退回注册表 chips，保持基线可见性
    const machines = DATA.ov?.machines || [];
    if (!machines.length) { el.innerHTML = '<span class="text-secondary small">未配置控制面机器注册（单机模式）</span>'; return; }
    el.innerHTML = `<div style="grid-column:1/-1;display:flex;flex-wrap:wrap;gap:.5rem">` + machines.map(m => {
      const live = m.enabled && !m.revoked;
      const stale = !m.last_seen || (Date.now()/1000 - m.last_seen) > 7200;
      const color = m.revoked ? 'var(--err)' : !live ? 'var(--warn)' : stale ? 'var(--warn)' : 'var(--ok)';
      const projects = (m.projects || '').split(',').filter(Boolean)
        .map(p => displayProject(p)).join('、') || '无授权项目';
      return `<span class="chip" title="平台 ${esc(m.last_addr || '-')} · agent ${esc(m.agent_version || '-')} · ${projects}"
        style="border-color:${color}"><span style="color:${color}">●</span> ${esc(m.name)}${m.revoked ? '（已吊销）' : live ? '' : '（停用）'} · ${m.last_seen ? ago(Math.max(0, Date.now()/1000 - m.last_seen)) + '前在线' : '从未上线'} · ${esc(projects)}</span>`;
    }).join('') + `</div>`;
    return;
  }
  const now = Date.now()/1000;
  const statusOf = m => {
    if (!m.registered) return ['mut', '未注册'];   // 仅见于执行记录，永不告警
    if (m.revoked) return ['err', '已吊销'];
    if (!m.enabled) return ['warn', '已停用'];
    if (m.stalled_kinds.length) return ['warn', '应动未动'];
    if (m.last_seen && (now - m.last_seen) > 7200) return ['warn', '久未在线'];
    return ['ok', '活跃'];
  };
  const cardOf = m => {
    const [cls, label] = statusOf(m);
    const seen = m.last_seen ?? m.last_claim;
    const kindChips = Object.entries(m.kinds).map(([k, s]) =>
      `<span class="chip ${s.stalled ? 'err' : 'mut'}"
        title="${esc(k)} 最近领取：${s.last_claim == null ? '—' : fmtShort(s.last_claim)} · 停摆阈值 ${ago(data.thresholds[k] || 0)}">${esc(k)} ${s.last_claim == null ? '—' : ago(s.age_seconds) + '前'}${s.stalled ? ' · 应动未动' : ''}</span>`).join('');
    const runRows = m.running.map(r =>
      `<div class="s small">${esc(orgRepoFull(r.org_repo))} · ${esc(r.kind || '—')} · ${esc(trunc(r.key || '', 48))}
        <span title="租约至 ${r.expires_at == null ? '—' : fmtShort(r.expires_at)}">运行 ${r.created_at == null ? '—' : ago(now - r.created_at)}前</span>
        ${r.lease_expired ? zhChip('err', 'lease_expired') : ''}</div>`).join('');
    // issue #334: 取消率超阈值（近 window_days 天 CANCELLED / 终态总数 > 20%）着色为
    // err 并附统计口径 tooltip——28% 取消率与成功数同中性样式平铺无法判断异常。
    const done = Object.entries(m.outcomes).filter(([, n]) => n > 0);
    const cancelled = m.outcomes.CANCELLED || 0;
    const total = done.reduce((a, [, n]) => a + n, 0);
    const cancelRate = total ? cancelled / total : 0;
    const cancelBad = cancelRate > 0.2;
    const outcomeChips = done.map(([s, n]) => {
      const cls = (s === 'CANCELLED' && cancelBad) ? 'err' : (EXEC_CLS[s] || 'mut');
      const tip = s === 'CANCELLED' && cancelBad
        ? `近 ${data.window_days} 天取消率 ${(cancelRate * 100).toFixed(0)}%（${cancelled}/${total}），超过 20% 阈值`
        : `近 ${data.window_days} 天 ${esc(s)}`;
      return `<span class="chip ${cls}" title="${tip}">${ZH_MAP[s] || esc(s)} ${n}</span>`;
    }).join('');
    return `<div class="card"${m.registered ? '' : ' style="border-style:dashed"'}>
      <div class="t"><span style="color:var(--${cls})">●</span> ${esc(m.name)}
        <span class="chip ${cls}">${label}</span>
        <span class="small mut" style="font-weight:400">agent ${esc(m.agent_version || '—')}</span></div>
      <div class="s small">${m.registered
        ? `最后心跳 ${seen == null ? '从未' : ago(now - seen) + '前'} · ${esc(m.last_addr || 'addr —')}`
        : '仅见于执行记录（历史执行或未注册 worker）'}</div>
      <div class="s small">授权项目 ${esc(projectsOf(m.projects))}</div>
      <div class="s">${kindChips || '<span class="mut small">无领取记录</span>'}</div>
      ${runRows}
      ${outcomeChips ? `<div class="s">${outcomeChips}</div>` : ''}
    </div>`;
  };
  // 注册机器优先；未注册历史 worker（命名机制退役前的遗留，永不告警）默认折叠
  // 成一张汇总卡，点击单向展开——与 PR 列表 togglePRs 的展示预算惯用法一致。
  const registered = data.machines.filter(m => m.registered);
  const history = data.machines.filter(m => !m.registered);
  let html = registered.map(cardOf).join('');
  if (history.length) {
    if (window.__SHOW_HISTORY_WORKERS) {
      html += history.map(cardOf).join('');
    } else {
      const lastActive = Math.max(0, ...history.map(m => m.last_claim || 0));
      html += `<div class="card" role="button" tabindex="0" onclick="toggleHistoryWorkers()"
        style="border-style:dashed;cursor:pointer"
        title="worker 全局模式切换前的历史执行命名（每会话唯一），永不参与停摆告警；点击展开">
        <div class="t"><span style="color:var(--muted)">●</span> 历史 worker
          <span class="chip mut">${history.length}</span></div>
        <div class="s small">未注册执行记录 · 最近活动 ${lastActive ? ago(now - lastActive) + '前' : '—'} · 点击展开</div>
      </div>`;
    }
  }
  el.innerHTML = html || '<div class="empty">暂无机器 · 控制面注册表为空</div>';
}

function renderLoops(){
  const loops = (DATA.ov?.loops || []).filter(inScope);
  const stallSec = DATA.ov?.config?.loop_stall_seconds || 27000;
  // 列固定为 code-loop 的三种循环（与 store.py LOOP_KINDS 联动）
  const kinds = ['discover', 'handler', 'reviewer'];
  const byKey = {};
  loops.forEach(l => { byKey[l.org_repo + '|' + l.kind] = l; });
  const orgs = [...new Set(loops.map(l => l.org_repo))];
  const cell = l => {
    if (!l) return '<span class="mut">—</span>';
    const stalled = l.age_seconds != null && l.age_seconds > stallSec;
    const act = l.last_activity == null
      ? '<span class="mut">无记录</span>'
      : `<span title="上次活动 ${ago(l.age_seconds)}前">${ago(l.age_seconds)}前</span>`;
    return act +
      (l.running ? ` <span class="chip acc" title="running×${esc(l.running)}">运行×${esc(l.running)}</span>` : '') +
      (stalled ? ' <span class="chip err">疑似停摆</span>' : '');
  };
  $('#loop-cards').innerHTML = orgs.length
    ? `<div class="table-responsive"><table class="table table-sm table-hover align-middle loop-matrix">
        <thead><tr><th>项目</th>${kinds.map(k => `<th>${k}</th>`).join('')}</tr></thead><tbody>` +
      orgs.map(o => `<tr><td><a class="proj-link" role="button" tabindex="0" data-proj="${esc(o)}" title="点击聚焦该项目">${esc(orgRepoShort(o))}</a></td>` +
        kinds.map(k => `<td>${cell(byKey[o + '|' + k])}</td>`).join('') + '</tr>').join('') +
      '</tbody></table></div>'
    : '<div class="empty">无循环活动记录</div>';
  const sel = $('#rt-project');
  const projects = (DATA.ov?.projects || []).map(p => p.org_repo);
  sel.style.display = (projects.length > 1 && !FILTER.project) ? '' : 'none';
  if (sel.dataset.v !== JSON.stringify(projects)){
    sel.dataset.v = JSON.stringify(projects);
    sel.innerHTML = projects.map(p => `<option value="${esc(p)}">${esc(orgRepoFull(p))}</option>`).join('');
    if (projects.includes(RT_PROJECT)) sel.value = RT_PROJECT;
  }
  if (FILTER.project && projects.includes(FILTER.project)) sel.value = FILTER.project;
  RT_PROJECT = sel.value || RT_PROJECT;
  const rt = DATA.rt;
  if (!rt){ $('#runtime').innerHTML = '<div class="empty">选择项目加载运行时</div>'; return; }
  const table = (head, rows) => `<div class="table-responsive"><table class="table table-sm table-hover align-middle mb-2"><thead><tr>${head.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>` +
    (rows || []).map(r => `<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('') + '</tbody></table></div>';
  const execChip = s => zhChip(EXEC_CLS[s] || 'mut', s);
  const jsonNote = (raw, keys) => {
    let o = null;
    try { o = JSON.parse(raw || ''); } catch (e) {}
    if (o && typeof o === 'object'){
      const bits = keys.map(k => o[k]).filter(x => x != null && x !== '')
        .map(x => trunc(String(x), 50));
      if (bits.length) return esc(bits.join(' · '));
      if (Object.keys(o).length === 0) return '<span class="mut">（空）</span>';
    }
    return esc(trunc(raw || '—', 60));
  };
  const outboxTarget = o => {
    let j = null;
    try { j = JSON.parse(o.receipt || ''); } catch (e) {}
    if (j && typeof j === 'object'){
      if (j.number != null) return '#' + esc(String(j.number)) + ' ';
      const url = j.pull_request_url || j.issue_url || j.html_url || '';
      const m = String(url).match(/\/(\d+)(?:$|\?)/);
      if (m) return '#' + m[1] + ' ';
      if (j.title) return esc(trunc(String(j.title), 24)) + ' ';
    }
    return '';
  };
  $('#runtime').innerHTML =
    '<details class="cp-box"><summary>Control Plane 明细（高级：租约 / Outbox / 执行 / Work Items）</summary>' +
    '<h2 class="section">租约</h2>' + table(['资源','到期','状态'],
      rt.leases.map(l => {
        const e = epochOf(l.expires_at);
        const remain = e == null ? null : e - Date.now()/1000;
        const full = e == null ? '' : new Date(e*1000).toLocaleString('zh-CN', {hour12:false});
        const cell = e == null ? '—'
          : remain < 0 ? `<span title="${full}">过期于 ${fmtShort(e)}</span>`
          : `<span title="${full}">${ago(remain)}后到期</span>`;
        const state = remain == null ? '<span class="chip mut">未知</span>'
          : remain < 0 ? '<span class="chip err">已过期</span>'
          : remain < 3600 ? '<span class="chip warn">临期</span>'
          : '<span class="chip ok">有效</span>';
        return [esc(l.resource), cell, state];
      })) +
    '<h2 class="section">Outbox</h2>' + table(['Action','目标','状态','详情'],
      rt.outbox.map(o => [esc(o.action), outboxTarget(o) + jsonNote(o.receipt, ['title', 'body']),
        zhChip(o.state==='SENT'?'ok':(o.state==='SENDING'||o.state==='UNKNOWN'?'err':'mut'), o.state),
        o.receipt ? `<details class="fold"><summary>原始 JSON</summary><pre>${esc(trunc(o.receipt, 2000))}</pre></details>` : '<span class="mut">—</span>'])) +
    '<h2 class="section">最近执行</h2>' + table(['Worker','状态','分支','创建','结果'],
      rt.executions.slice(0,20).map(e => [esc(e.worker), execChip(e.state),
        esc(e.branch||'—'), fmtCell(e.created_at),
        jsonNote(e.result, ['decision', 'action', 'summary', 'state'])])) +
    '<h2 class="section">Work Items</h2>' + table(['Kind','状态','Key','创建'],
      rt.work_items.slice(0,20).map(w => [esc(w.kind),
        zhChip(w.state==='RUNNING'?'acc':'mut', w.state||'—'),
        esc(trunc(w.key,40)), fmtCell(w.created_at)])) +
    '</details>';
}

// Okabe-Ito 色盲安全色板：四条序列在浅底上可区分（2026-09-10 评审 P0 跟进）
const SERIES = [
  ['issues_created','建 Issue','#0072B2'], ['issues_closed','关 Issue','#009E73'],
  ['prs_created','建 PR','#E69F00'], ['prs_merged','合 PR','#CC79A7']];

function renderTrends(){
  const tr = DATA.tr || {daily:[],cycle:{}};
  const cyc = tr.cycle || {};
  const card = (t, c) => `<div class="card"><div class="t">${t}</div>
    <div class="kpi">${c.p50 ?? '—'}<small>小时 · P50</small></div>
    <div class="s small mut"><span>P90 ${c.p90 ?? '—'} 小时</span><span>n=${c.n ?? 0}</span></div></div>`;
  $('#cycle-cards').innerHTML =
    card('Issue → 首个 PR', cyc.issue_to_pr || {}) +
    card('PR → Approved', cyc.pr_to_approved || {});
  const daily = tr.daily || [];
  // 宽度按容器实测，铺满 #chart-wrap（避免 viewBox 等比缩放的两侧死白）
  const cw = ($('#chart-wrap').clientWidth || 900) - 28;
  const W = Math.max(600, cw), H = 240, pad = 30;
  const max = Math.max(1, ...daily.flatMap(d => SERIES.map(s => d[s[0]] || 0)));
  const plot = W - 2*pad;
  const group = daily.length ? plot / daily.length : plot;
  const bw = Math.min(14, Math.max(5, group * 0.18));
  let svg = '';
  for (let gy = 0; gy <= 4; gy++){
    const y = H - pad - (H - 2*pad) * gy / 4;
    svg += `<line x1="${pad}" y1="${y}" x2="${W-pad}" y2="${y}"
      stroke="#e3e9ee" stroke-width="1"/>
      <text x="4" y="${y+4}" fill="#64748b" font-size="11">${Math.round(max*gy/4)}</text>`;
  }
  daily.forEach((d, i) => {
    const gx = pad + i * group + (group - 4*bw) / 2;
    SERIES.forEach((s, si) => {
      const v = d[s[0]] || 0;
      const h = (H - 2*pad) * v / max;
      svg += `<rect x="${gx + si*bw}" y="${H-pad-h}" width="${bw-3}" height="${Math.max(v?1:0,h)}"
        fill="${s[2]}"><title>${d.date} ${s[1]}: ${v}</title></rect>`;
    });
    if (daily.length <= 16 || i % 2 === 0)
      svg += `<text x="${pad + i * group + group / 2}" y="${H-8}" fill="#64748b"
        font-size="11" text-anchor="middle">${d.date.slice(5)}</text>`;
  });
  $('#chart').setAttribute('viewBox', `0 0 ${W} ${H}`);
  $('#chart').innerHTML = svg;
  $('#chart-legend').innerHTML = SERIES.map(s =>
    `<span><i style="background:${s[2]}"></i>${s[1]}</span>`).join('');
}
let _rsT;
window.addEventListener('resize', () => {   // 容器宽度变化后按新宽度重排图表
  clearTimeout(_rsT);
  _rsT = setTimeout(() => {
    if (CURRENT_VIEW === 'trends') renderTrends();
    fitKanban();
  }, 200);
});
// 板高上限随「板以上」的高度重算：导航换行、聚焦条/错误条出现、看板条后到都会改变它
if (window.ResizeObserver){
  const _ro = new ResizeObserver(() => fitKanban());
  ['#navbar-wrap', '#focus-bar', '#data-errors', '#kanban-strip'].forEach(s => {
    const el = $(s);
    if (el) _ro.observe(el);
  });
}

// ---- kanban (agent development task board) view
// 台账 fail_reason → 一句人话（按前导 CODE 查表，未命中原样截断）
const triggerHintOf = reason =>
  REVIEW_TRIGGER_HINT[String(reason || '').split(':')[0].split(' ')[0]]
  || trunc(String(reason || ''), 60);
const PREFLIGHT = {};   // org_repo -> {online:[],registered:[]} | null(取数失败,按钮保持乐观)
async function loadPreflight(){
  if (!DATA.kb) return;
  const keys = new Set();
  for (const col of KANBAN_COLS) for (const c of (DATA.kb[col[0]] || [])) keys.add(c.org_repo);
  const want = [...keys].filter(k => PREFLIGHT[k] === undefined);
  if (!want.length) return;
  try {
    const data = await jget('/api/review/preflight?projects=' + encodeURIComponent(want.join(',')));
    for (const k of want) PREFLIGHT[k] = data[k] || {online: [], registered: []};
  } catch (e) { for (const k of want) PREFLIGHT[k] = null; }
}
function triggerButton(c){
  const active = (DATA.kb?.reviewing || []).some(
    x => x.org_repo === c.org_repo && x.number === c.number);
  if (active) return '<button class="btn btn-sm btn-outline-primary" disabled title="reviewer 执行中，见审核中列">审核中…</button>';
  // 范围外的卡：控制面永远不会为它派生评审项（只派生 base==项目 base 的开放
  // 非草稿 PR），按钮点了必然失败——说清为什么，好过给一个假按钮。这是静态事实
  // （采集到的 PR base vs 项目配置 base），不是已删除的忙闲预检：claim 时控制面
  // 仍是唯一裁决者。
  if (c.review_scope === false)
    return '<button class="btn btn-sm btn-outline-primary" disabled title="' + esc(c.review_scope_note)
      + '">不在评审范围</button>';
  const tr = c.trigger;
  if (tr && (tr.state === 'PENDING' || tr.state === 'CLAIMED'))
    return '<button class="btn btn-sm btn-outline-primary" disabled title="指令已下发，等待执行机 agent 领取（超时未领自动过期可重试）">排队中…</button>';
  if (tr && tr.state === 'SPAWNED')
    return '<button class="btn btn-sm btn-outline-primary" disabled title="reviewer 会话已拉起，见审核中列">运行中…</button>';
  const pf = PREFLIGHT[c.org_repo];
  if (pf && pf.online.length && !pf.registered.length){
    const hint = pf.online.join('、') + ' 在线但未注册此项目。在该机执行： dashboard.py register --project '
      + c.org_repo + ' --repo <检出路径>，并重启该机 worker agent 后解锁';
    return '<button class="btn btn-sm btn-outline-primary" disabled title="' + esc(hint)
      + '">暂不可触发</button>';
  }
  const queued = (pf && !pf.online.length)
    ? '（暂无 agent 在线：下发后将排队等待，agent 上线即领取）' : '';
  return '<button class="btn btn-sm btn-outline-primary" title="立即拉起一轮 reviewer' + esc(queued)
    + '" onclick="reviewTrigger(this, \'' + esc(c.org_repo) + '\', ' + c.number + ')">触发审核</button>';
}
function triggerFailbar(c){
  const tr = c.trigger;
  // 范围外的卡不显示失败条：永久原因（base 不匹配）优先于瞬时症状，否则那条
  // 「领取不到（进行中 / 已完成 …）」会被读成「等会儿再试」。
  if (!tr || tr.state !== 'FAILED' || c.review_scope === false) return '';
  return '<div class="s"><div class="failbar" title="' + esc(tr.fail_reason || '')
    + '"><b>上次触发失败</b><span>' + esc(triggerHintOf(tr.fail_reason))
    + '</span><span class="at">' + relWhen(tr.at) + '</span></div></div>';
}
// ---- 有冲突 PR 的「触发修复」：repair_round 指令，与触发审核同一套链路 ----
// 按钮只在平台报告有冲突（mergeable=false）且没有 live 修复执行时可点；
// 占用中的卡给「修复中…」，冲突翻绿后按钮整个消失（repair_trigger 同步静默）。
function repairButton(c){
  if (c.repair_busy)
    // 占用中的卡给出直达内链：不用先去 处理中 列找那张执行卡。
    return '<button class="btn btn-sm btn-outline-primary" disabled title="修复会话运行中">修复中…</button>'
      + (c.repair_exec ? ` <a class="verify-link" href="#p=${encodeURIComponent(c.org_repo)}&view=run&exec=${encodeURIComponent(c.repair_exec)}">查看过程 ›</a>` : '');
  const tr = c.repair_trigger;
  if (tr && (tr.state === 'PENDING' || tr.state === 'CLAIMED'))
    return '<button class="btn btn-sm btn-outline-primary" disabled title="修复指令已下发，等待执行机 agent 领取（超时未领自动过期可重试）">排队中…</button>';
  // 控制面对 base≠项目 base 的 PR 既不派生评审项也不派生 repair 项——同一颗
  // 假按钮问题，卡面上的「不在评审范围」chip 已说明原因，不再给触发修复。
  if (c.mergeable !== false || c.review_scope === false) return '';
  return '<button class="btn btn-sm btn-outline-primary" title="立即拉起一轮冲突修复：merge 基线 → 解冲突（保留双方意图）→ 验证 → push 回原 PR 分支'
    + '" onclick="repairTrigger(this, \'' + esc(c.org_repo) + '\', ' + c.number + ')">触发修复</button>';
}
function repairFailbar(c){
  const tr = c.repair_trigger;
  if (!tr || tr.state !== 'FAILED') return '';
  return '<div class="s"><div class="failbar" title="' + esc(tr.fail_reason || '')
    + '"><b>上次修复触发失败</b><span>' + esc(triggerHintOf(tr.fail_reason || ''))
    + '</span><span class="at">' + relWhen(tr.at) + '</span></div></div>';
}
// fresh=true：操作者动作后的那次重取，绕开 serve 的 last-good 快照——
// 刚写下的台账状态必须当场可见，否则失败条要等下一次自动刷新才出现。
async function loadKanban(fresh){
  const qs = new URLSearchParams();
  if (FILTER.project) qs.set('project', FILTER.project);
  if (fresh) qs.set('fresh', '1');
  const q = qs.toString() ? '?' + qs.toString() : '';
  try { DATA.kb = await jget('/api/kanban' + q); } catch (e) { DATA.kb = null; }
  await loadPreflight();
}
const KANBAN_COLS = [
  ['todo', '待处理', 'k-todo'], ['running', '处理中', 'k-run'],
  ['review', '待评审', 'k-review'], ['reviewing', '审核中', 'k-reviewing'],
  ['ready', '待人工合并', 'k-ready'], ['blocked', '阻塞', 'k-blocked']];
function kanbanLabels(js){
  let ls = [];
  try { ls = JSON.parse(js || '[]'); } catch (e) {}
  return ls.map(l => `<span class="chip mut">${esc(l)}</span>`).join('');
}
function kanbanPrCard(c, col){
  const mergeable = c.mergeable == null ? '' :
    c.mergeable ? '<span class="chip ok" title="平台报告可与基线合并">可合并</span>'
                : '<span class="chip err" title="平台报告存在合并冲突，先 rebase">有冲突</span>';
  const linked = (JSON.parse(c.issue_numbers || '[]') || [])
    .map(n => `<span class="chip acc">#${n}</span>`).join('');
  const badge = c.verdict ? zhChip(VERDICT_STYLE[c.verdict] || 'mut', c.verdict)
    : (col === 'review' && c.review_scope !== false) ? '<span class="chip mut">待评审</span>' : '';
  const when = col === 'ready' ? `结论后等 ${ago(c.waiting_seconds)}` : relWhen(c.updated_at);
  const meta = `${projSpan(c.org_repo)}<span class="mono">${esc(c.head_ref || '')}</span><span>${when}</span>`
    + (col === 'ready' ? `<span>结论人 ${esc(c.verdict_poster || '—')}</span>` : '');
  let extra = triggerFailbar(c) + repairFailbar(c);
  // 范围外的卡把原因写在卡面上（不只挂在 title 里）：PR 的 base 与项目控制面
  // base 都在卡片上有据可查，读一眼就知道为什么没有按钮。
  const scope = c.review_scope === false
    ? `<span class="chip warn" title="${esc(c.review_scope_note)}">不在评审范围 · base=${esc(c.base_ref || '?')}</span>`
    : '';
  extra += (badge || mergeable || linked || scope)
    ? `<div class="s">${scope}${badge}${mergeable}${linked}</div>` : '';
  if (col === 'review' && c.review_scope !== false){   // 一键拉起一轮只评审这张卡的 reviewer（定向指令进 prompt；唯一载荷=校验过的 PR 号）
    // 范围外卡不渲染按钮：scope chip 已说明原因（issue #322 提示收敛）
    extra += `<div class="s">${triggerButton(c)}${repairButton(c)}</div>`;
  }
  if (col === 'ready'){   // ready 列 = 人工合并的行动队列，动作能力就地挂卡；
    // 有冲突 = 合不动，「复制合并命令」是假按钮，一并隐藏（翻绿恢复），
    // 改给「触发修复」——推进入口，不是那条必失败的合并命令。
    const cmd = 'gh pr merge ' + c.number + ' --repo ' + repoOf(c.org_repo);
    const mergeBtn = c.mergeable === false ? '' :
      `<button class="btn btn-sm btn-outline-primary" title="${esc(cmd)}" onclick="copyCmd(${regCmd(cmd)}, this)">复制合并命令</button>`;
    extra += `<div class="s">${repairButton(c)}${mergeBtn}
      <a class="verify-link" role="button" tabindex="0" onclick="collectVerify(this)" data-org="${esc(c.org_repo)}" data-number="${esc(c.number)}">${esc(verifyText(c.org_repo, c.number))}</a></div>
      <details class="fold"><summary>评审结论全文</summary><pre>${esc(trunc(c.verdict_body||'（无正文）',800))}</pre></details>`;
  }
  if (col === 'blocked' && c.verdict && c.verdict_body)
    extra += `<details class="fold"><summary>阻塞原因（评审结论）</summary><pre>${esc(trunc(c.verdict_body, 600))}</pre></details>`;
  return queueCard('#' + c.number + ' ' + c.title, meta, c.html_url, extra);
}
function kanbanCard(c, col){
  if (c.card === 'pr') return kanbanPrCard(c, col);
  if (c.card === 'exec'){
    if (col === 'reviewing'){   // reviewer 执行卡：整卡点击进运行详情，PR 外链降为卡内图标
      const title = c.number != null ? '#' + c.number + ' ' + (c.title || '')
        : (c.key || '评审中');
      const runLink = c.execution
        ? '#p=' + encodeURIComponent(c.org_repo) + '&view=run&exec=' + encodeURIComponent(c.execution)
        : (c.html_url || '');   // 无 execution id 的旧镜像行退回 PR 外链
      const prIcon = c.html_url
        ? `<a href="${esc(c.html_url)}" target="_blank" rel="noopener noreferrer" title="在 GitHub 打开 PR" onclick="event.stopPropagation()">↗</a>`
        : '';
      return queueCard(title,
        `${projSpan(c.org_repo)}<span>@${esc(c.worker || '—')}</span><span>${ago(agoAbs(epochOf(c.created_at)))}前</span>${prIcon}${c.lease_expired ? zhChip('err', 'lease_expired') : ''}`,
        runLink,
        `<div class="s"><span class="chip acc mono" title="PR 分支">${esc(c.branch || '')}</span>${c.worktree ? `<span class="small mut" title="worktree：${esc(c.worktree)}">📂</span>` : ''}</div>`, true,
        '', `location.href=this.querySelector('a').getAttribute('href')`);
    }
    const title = c.issue_number != null ? '#' + c.issue_number + ' ' + (c.title || '')
      : (c.key || '执行中');
    // 开发执行卡带 issue → 决策详情；修复执行卡（repair:<n>，无 issue）→
    // 运行详情：整卡点击看这轮修复的会话流与控制面时间线。
    const detailLink = c.issue_number != null && c.has_issue
      ? '#p=' + encodeURIComponent(c.org_repo) + '&view=decision&n=' + c.issue_number
      : (c.execution
        ? '#p=' + encodeURIComponent(c.org_repo) + '&view=run&exec=' + encodeURIComponent(c.execution)
        : '');
    const keyChip = (c.key && c.issue_number == null)
      ? `<span class="chip acc mono" title="工作项">${esc(c.key)}</span>` : '';
    // 整卡点击面与「审核中」执行卡同款（queueCard 第 7 参）：处理中的卡
    // 视觉上就该能点进去看过程——只留标题锚点可点等于没有入口。无 detailLink
    // 的卡（既无 issue 也无 execution）不挂 handler，防止 querySelector 落空。
    return queueCard(title,
      `${projSpan(c.org_repo)}<span>@${esc(c.worker || '—')}</span><span>${ago(agoAbs(epochOf(c.created_at)))}前</span>${c.lease_expired ? zhChip('err', 'lease_expired') : ''}`,
      detailLink,
      `<div class="s"><span class="chip acc mono" title="开发分支">${esc(c.branch || '')}</span>${keyChip}${c.worktree ? `<span class="small mut" title="worktree：${esc(c.worktree)}">📂</span>` : ''}</div>`, true, '',
      detailLink ? `location.href=this.querySelector('a').getAttribute('href')` : '');
  }
  if (c.card === 'issue'){
    return queueCard('#' + c.number + ' ' + c.title,
      `${projSpan(c.org_repo)}<span>${relWhen(c.created_at)}</span>`,
      '#p=' + encodeURIComponent(c.org_repo) + '&view=decision&n=' + c.number,
      c.labels ? `<div class="s">${kanbanLabels(c.labels)}</div>` : '', true);
  }
  if (c.card === 'decision'){
    return queueCard('#' + c.number + ' ' + c.title,
      `${projSpan(c.org_repo)}<span>积压 ${ago(c.waiting_seconds)}（自创建）</span>`,
      '#p=' + encodeURIComponent(c.org_repo) + '&view=decision&n=' + c.number,
      c.body ? `<div class="s small mut">${esc(cardSummary(c.body))}</div>` : '', true);
  }
  if (c.card === 'outbox'){
    return queueCard('outbox #' + c.cp_id + ' ' + c.action,
      `${projSpan(c.org_repo)}${zhChip('err', c.state)}<span>${relWhen(c.created_at)}</span>`, '',
      reconcileExtra(c.cp_id));
  }
}
// 看板整屏：列内独立滚动先要知道「列有多高」，而板以上是可变高的导航/告警/聚焦条，
// 所以高度必须实测；视图隐藏或无数据就不设高度，退回整页滚动（失败态不占整屏）
function fitKanban(){
  const board = $('#kanban-board');
  if (!board || !board.offsetParent || !DATA.kb) return;
  // 文档坐标：与当前滚动位置无关。板高定好后整页正好一屏，浏览器自己会把滚动归零
  const top = board.getBoundingClientRect().top + window.scrollY;
  const h = Math.max(280, window.innerHeight - top - 14) + 'px';
  if (board.style.getPropertyValue('--kanban-h') !== h)   // 幂等：同值不写，避免观察器自激
    board.style.setProperty('--kanban-h', h);
}
// 自动刷新会重建列 DOM：列序恒定，按 index 记住各列滚动位置，重绘后还原。
// 只在同一数据集（同项目聚焦）内还原——换项目时旧像素偏移没有意义
const colTops = el => [...el.children].map(c => c.querySelector('.stack')?.scrollTop || 0);
let KANBAN_KEY = null;
function renderKanban(){
  const el = $('#kanban-board');
  const board = DATA.kb;
  if (!board){
    el.style.removeProperty('--kanban-h');   // 失败态不占整屏
    el.innerHTML = '<div class="empty">加载失败或无数据</div>';
    return;
  }
  const tops = KANBAN_KEY === FILTER.project ? colTops(el) : [];
  el.innerHTML = KANBAN_COLS.map(([key, label, cls]) => {
    const cards = board[key] || [];
    // issue #322：review 列把「不在评审范围」（base≠项目 base / 已合并）的卡
    // 折叠进独立分组——列头计数只计可操作卡，范围外卡数单独小字，信噪比恢复。
    let body;
    if (key === 'review'){
      const inScope = cards.filter(c => c.review_scope !== false);
      const outScope = cards.filter(c => c.review_scope === false);
      const count = `<span class="count ${cls}">${inScope.length}</span>`
        + (outScope.length ? `<span class="count mut" title="不在评审范围（base≠项目 base 或已合并）">+${outScope.length}</span>` : '');
      const head = `<h2 class="col-head">${label} ${count}</h2>`;
      const stack = inScope.length ? inScope.map(c => kanbanCard(c, key)).join('') : '';
      const folded = outScope.length
        ? `<details class="scope-fold"><summary>不在评审范围 · ${outScope.length} 张（base≠项目 base / 已合并）</summary>`
          + outScope.map(c => kanbanCard(c, key)).join('') + '</details>'
        : '';
      // 两组都空才显示占位符（issue #322 修复：无范围外卡时不得在非空列下方误渲染「空」）
      const empty = (!inScope.length && !outScope.length)
        ? '<div class="empty" style="padding:.4rem;font-size:.78rem">空</div>' : '';
      body = `<div class="stack">${stack}${folded}${empty}</div>`;
      return `<div class="kanban-col ${cls}">${head}${body}</div>`;
    }
    const head = `<h2 class="col-head">${label}
         <span class="count ${cls}">${cards.length}</span></h2>`;
    return `<div class="kanban-col ${cls}">${head}
      <div class="stack">${cards.length ? cards.map(c => kanbanCard(c, key)).join('')
        : '<div class="empty" style="padding:.4rem;font-size:.78rem">空</div>'}</div></div>`;
  }).join('');
  [...el.children].forEach((c, i) => {
    const st = c.querySelector('.stack');
    if (st && tops[i]) st.scrollTop = tops[i];
  });
  KANBAN_KEY = FILTER.project;
  fitKanban();
}

// ---- 一键派发 reviewer 轮（POST /api/review/trigger → 指令台账 + WS 直推）
// 点击应答即含去向（已派发到 <machine> / 排队等待 worker 上线）；此后短轮询
// 状态。真正的评审在 worker agent 机器上执行。
// 结果不靠按钮文案记忆：卡片上的「最近一次下发结果」读自项目指令台账
// （/api/kanban 的 trigger 字段），刷新页面仍在——按钮文案只是即时回执。
const REVIEW_TRIGGER_HINT = {
  INVALID_COMMAND: '指令不合法（PR 号或动作名）',
  COMMAND_STATE: '指令状态已被其他事件改变，可直接重试',
  CLAIM_UNAVAILABLE: '领取不到：该 PR 不可领取（进行中 / 已完成 / 处于修复流程）',
  WORKTREE_FAILED: '执行机工作区准备失败（残留 worktree 或 fetch 失败，见该机日志）',
  BIND_FAILED: '绑定失败（工作区与 PR Head 不一致或输入已换代，可直接重试）',
  NOT_REGISTERED: '目标机器未注册执行信息：dashboard.py register --project <key> --repo <路径>',
  REGISTER_INVALID: '注册的路径已失效：重新执行 dashboard.py register',
  SPAWN_FAILED: '拉起进程失败：该轮将由租约超时回队，稍后可重试',
  COMMAND_UNACKED: '派发后无 agent 确认（机器离线？）已回队',
  COMMAND_TTL: '指令长时间无 worker 领取，已过期',
  DONE: '上一轮已结束（可再次下发）',
  AGENT_ERROR: '执行机 agent 出错（见该机 agent 日志）',
  MODEL_UNAVAILABLE: '所选模型不在执行机可用列表（换模型，或在该机安装/登录）',
  AGENT_MISSING: '执行机未安装该 agent CLI（opencode 不在本机 PATH）'
};
// 点击卡片按钮：先弹选择框（执行 agent + 模型），确认后才下发。
// agent/model 只让执行机的代码选 argv 预设（门铃不是信封，永不进 prompt）；
// agent 卡片与模型下拉都来自 GET /api/spawn/options?project=<org__repo>——卡片由
// 服务端注册表下发，所以新增一个 coding agent 不必改这里（图标查不到就用通用兜底）；
// 模型列表来自执行机的能力投影（worker 每 600s 上报，scope 标来源），无投影时回退
// serve 主机检测并在来源行警示；执行机 spawn 前还会用自己的列表复检，不过即 FAIL
// MODEL_UNAVAILABLE，不浪费会话。本地/远程项目同一条组装路径，前端不分支。
const RT_GENERIC_ICON = '<svg class="rt-ic" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="2.5" y="4.5" width="19" height="15" rx="3" stroke="#5b6b78" stroke-width="1.6"/><path d="M7 9.5l3 2.5-3 2.5" stroke="#5b6b78" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M12.5 15H17" stroke="#5b6b78" stroke-width="1.6" stroke-linecap="round"/></svg>';
const RT_ICONS = {
  '': RT_GENERIC_ICON,
  claude: '<svg class="rt-ic" viewBox="0 0 24 24" aria-hidden="true"><path fill="#D97757" d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></svg>',
  opencode: '<svg class="rt-ic" viewBox="0 0 512 512" aria-hidden="true"><rect width="512" height="512" rx="110" fill="#131010"/><path d="M320 224V352H192V224H320Z" fill="#5A5858"/><path fill-rule="evenodd" clip-rule="evenodd" d="M384 416H128V96H384V416ZM320 160H192V352H320V160Z" fill="#fff"/></svg>'
};
// 固定首卡：不是插件——不带 agent 字段 = 用该机注册的 spawn 命令。
const RT_DEFAULT_AGENT = {id: '', label: '默认', sub: '本机注册'};
let RT_OPTIONS = null;   // 本次开窗取到的插件列表（投影在服务端内存，无请求期检测）
let RT_SCOPE = null;     // 载荷的 scope：{kind:'machine',machine,reported_at} | {kind:'serve-host'}
let RT_TARGET = null;   // {btn, org, number} — 确认时消费，取消即弃

window.reviewTrigger = function(btn, org, number){
  if (btn.disabled) return;
  openTriggerDialog(btn, org, number, 'review');
};
window.repairTrigger = function(btn, org, number){
  if (btn.disabled) return;
  openTriggerDialog(btn, org, number, 'repair');
};

// 两种触发共用一个对话框与一条下发链路，只换标题/确认按钮/端点和说明文案；
// agent 卡片与模型下拉完全一致（同一 /api/spawn/options、同一套降级）。
const RT_KINDS = {
  review: {label: '触发审核', go: '开始评审', post: '/api/review/trigger',
           status: '/api/review/trigger/status', what: ''},
  repair: {label: '触发修复', go: '开始修复', post: '/api/repair/trigger',
           status: '/api/repair/trigger/status',
           what: '这轮 agent 将按 handler《PR 冲突解决（兜底）》契约：认领 repair:<n> → '
               + '在修复 worktree 里 merge 基线分支解决冲突（保留双方真实意图）→ '
               + '项目验证 → push 回原 PR 分支。判不了的冲突会 BLOCKED 并留痕评论，不硬猜。'},
  operator: {label: '手动触发协调巡检', go: '开始巡检', post: '/api/operator/trigger',
             status: null, operator: true,
             what: '协调轮将在面板服务主机执行：查证信号 → L1 白名单内处置 → 结构化报告。'
                 + '危险动作一律升级人工（[❓needs-human]），绝不自行执行。'}
};

function ensureTriggerDialog(){
  if ($('#rt-backdrop')) return;
  document.body.insertAdjacentHTML('beforeend', `
  <div class="rt-backdrop" id="rt-backdrop">
    <div class="rt-dlg" role="dialog" aria-modal="true" aria-labelledby="rt-title">
      <header id="rt-title"><span id="rt-kind">触发审核</span> · <span class="rt-pr"></span></header>
      <div class="rt-body">
        <div class="rt-hint" id="rt-what" style="display:none"></div>
        <span class="rt-label">执行 agent</span>
        <div class="rt-agents" id="rt-agents" role="radiogroup" aria-label="执行 agent"></div>
        <div class="rt-scope" id="rt-scope" style="display:none"></div>
        <div id="rt-model-field" style="display:none">
          <span class="rt-label">模型 <span id="rt-model-state"></span></span>
          <select id="rt-model-sel"></select>
        </div>
        <div id="rt-note-field" style="display:none">
          <span class="rt-label">情况说明（可选，≤2000 字——会话据此定向排查）</span>
          <textarea id="rt-note" maxlength="2000" rows="3"
            placeholder="例：GPU-02 的 reviewer 两轮没动静，帮我看下是不是又卡租约了"></textarea>
          <div class="small mut text-end"><span id="rt-note-cnt">0</span>/2000</div>
        </div>
        <div class="rt-hint" id="rt-hint"></div>
      </div>
      <footer>
        <button type="button" class="btn btn-sm" id="rt-cancel">取消</button>
        <button type="button" class="btn btn-sm btn-primary" id="rt-go">开始评审</button>
      </footer>
    </div>
  </div>`);
  $('#rt-cancel').addEventListener('click', closeTriggerDialog);
  $('#rt-go').addEventListener('click', confirmTriggerDialog);
  $('#rt-backdrop').addEventListener('click', e => {
    if (e.target === $('#rt-backdrop')) closeTriggerDialog();
  });
  document.addEventListener('keydown', rtEscape);
}
function rtEscape(e){ if (e.key === 'Escape') closeTriggerDialog(); }
function rtSelectedAgent(){
  const el = document.querySelector('#rt-agents .rt-agcard[aria-checked="true"]');
  return el ? el.dataset.agent : '';
}
function rtAgentCard(a){
  const title = a.id === '' ? '使用项目注册的 spawn 命令'
    : (a.models === null ? a.label + '（模型走注册缺省）'
                         : a.label + '（模型可下拉选择）');
  return `<button type="button" class="rt-agcard" role="radio"
            aria-checked="${a.id === ''}" data-agent="${esc(a.id)}" title="${esc(title)}">
    ${RT_ICONS[a.id] || RT_GENERIC_ICON}${esc(a.label)}<span class="rt-sub">${esc(a.sub)}</span>
  </button>`;
}
function rtPaintScope(scope){
  const el = $('#rt-scope');
  if (!el) return;
  if (!scope){ el.style.display = 'none'; return; }
  el.style.display = 'block';
  if (scope.kind === 'machine'){
    el.className = 'rt-scope';
    const t = new Date(scope.reported_at * 1000).toLocaleTimeString('zh-CN',
      {hour: '2-digit', minute: '2-digit'});
    el.innerHTML = '<span class="dot"></span>模型列表来自执行机 '
      + esc(scope.machine) + ' 上报 · ' + esc(t);
  } else {
    el.className = 'rt-scope warn';
    el.innerHTML = '<span class="dot"></span>模型列表来自面板主机检测（执行机未上报，可能与执行机不符）';
  }
}
function rtPaintAgents(rows, hintText, warn){
  const list = $('#rt-agents');
  list.innerHTML = rows.map(rtAgentCard).join('');
  list.querySelectorAll('.rt-agcard').forEach(card =>
    card.addEventListener('click', () => {
      list.querySelectorAll('.rt-agcard').forEach(x =>
        x.setAttribute('aria-checked', x === card ? 'true' : 'false'));
      renderTriggerModelField();
    }));
  const hint = $('#rt-hint');
  hint.className = warn ? 'rt-hint rt-warn' : 'rt-hint';
  hint.textContent = hintText || '';
}
function closeTriggerDialog(){
  const bd = $('#rt-backdrop');
  if (bd) bd.classList.remove('open');
  RT_TARGET = null;
  RT_SCOPE = null;
  const scope = $('#rt-scope');
  if (scope) scope.style.display = 'none';
}
async function openTriggerDialog(btn, org, number, kind){
  kind = kind || 'review';
  ensureTriggerDialog();
  const K = RT_KINDS[kind];
  RT_TARGET = {btn, org, number, kind};
  $('#rt-kind').textContent = K.label;
  $('#rt-backdrop .rt-pr').textContent = K.operator ? '协调巡检' : '#' + number;
  $('#rt-go').textContent = K.go;
  const what = $('#rt-what');
  what.style.display = K.what ? '' : 'none';
  what.textContent = K.what || '';
  $('#rt-model-field').style.display = 'none';
  $('#rt-note-field').style.display = K.operator ? 'grid' : 'none';
  if (K.operator){
    const note = $('#rt-note');
    if (note) note.value = '';
    const cnt = $('#rt-note-cnt');
    if (cnt) cnt.textContent = '0';
  }
  $('#rt-hint').textContent = ''; $('#rt-hint').className = 'rt-hint';
  $('#rt-agents').innerHTML = '<span class="rt-loading">检测执行 agent…</span>';
  $('#rt-backdrop').classList.add('open');
  RT_OPTIONS = null;
  RT_SCOPE = null;
  rtPaintScope(null);
  if (K.operator){   // 协调 agent 走 operator options：机器默认预选，scope 恒 serve-host（真实范围，非降级）
    try{
      const resp = await fetch('/api/operator/options',
        {headers: {'X-Requested-With': 'dashboard-fetch'}});
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) throw new Error(data.error || ('HTTP ' + resp.status));
      if (data.enabled === false) throw new Error('OPERATOR_DISABLED——在机器配置 operator.enabled 开启后可用');
      RT_OPTIONS = data.agents || [];
      const scope = $('#rt-scope');
      scope.style.display = 'block'; scope.className = 'rt-scope';
      scope.innerHTML = '<span class="dot"></span>协调轮恒在面板服务主机执行'
        + '——serve-host 即真实范围，非降级 · 默认 ' + esc(data.default_agent || '（未配置）');
      const pre = data.default_agent || '';
      const rows = (RT_OPTIONS || []).map(a => Object.assign({}, a));
      rtPaintAgents(rows, '');
      document.querySelectorAll('#rt-agents .rt-agcard').forEach(c =>
        c.setAttribute('aria-checked', c.dataset.agent === pre ? 'true' : 'false'));
      renderTriggerModelField();
    }catch(err){
      rtPaintAgents([], err.message, true);
      renderTriggerModelField();
    }
    return;
  }
  try{
    const resp = await fetch('/api/spawn/options?project=' + encodeURIComponent(org),
      {headers: {'X-Requested-With': 'dashboard-fetch'}});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    RT_OPTIONS = data.agents || [];
    RT_SCOPE = data.scope || null;
    rtPaintScope(RT_SCOPE);
  }catch(err){
    // 接口不可用：降级为只剩「默认」卡——默认轮照常可下发，不是死路
    RT_OPTIONS = [];
    RT_SCOPE = null;
    rtPaintScope(null);
    rtPaintAgents([RT_DEFAULT_AGENT],
      'agent 列表获取失败：' + err.message + '（可按默认继续）', true);
    renderTriggerModelField();
    return;
  }
  rtPaintAgents([RT_DEFAULT_AGENT].concat(RT_OPTIONS), '');
  renderTriggerModelField();
}
function renderTriggerModelField(){
  const agent = rtSelectedAgent();
  const field = $('#rt-model-field'), sel = $('#rt-model-sel'),
        state = $('#rt-model-state'), hint = $('#rt-hint');
  if (hint.className.indexOf('rt-warn') < 0){
    hint.textContent = ''; hint.className = 'rt-hint';
  }
  const row = (RT_OPTIONS || []).find(a => a.id === agent);
  if (!agent || !row){ field.style.display = 'none'; return; }
  field.style.display = 'grid';
  if (row.models === null){   // 该插件没有检测命令：不渲染禁用下拉（禁用控件
    // 看起来像 UI 故障，issue #333），改显示一行说明——用户能区分
    // 「正常无选项」与「加载失败」
    sel.style.display = 'none';
    sel.innerHTML = '';
    state.textContent = '该 agent 未上报模型列表，将使用注册缺省模型';
    return;
  }
  if (!row.models.length){    // 检测失败：说明原因，而不是造数据
    sel.innerHTML = '<option value="">默认</option>';
    sel.disabled = true; sel.style.display = '';
    // scope=machine：列表来自执行机，空=执行机视角没装/失败；无 scope 维持原文案
    state.textContent = RT_SCOPE && RT_SCOPE.kind === 'machine'
      ? '（执行机未装 ' + row.label + ' 或检测失败）'
      : '（检测失败或未装 ' + row.label + '）';
    return;
  }
  sel.style.display = '';
  sel.innerHTML = '<option value="">默认（' + esc(row.label) + ' 自身配置）</option>'
    + row.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
  sel.disabled = false; state.textContent = '';
}
function confirmTriggerDialog(){
  const t = RT_TARGET;
  if (!t) return;
  const agent = rtSelectedAgent();
  const model = $('#rt-model-sel') ? $('#rt-model-sel').value : '';
  const opts = {};
  if (agent) opts.agent = agent;
  if (agent && model) opts.model = model;
  if (RT_KINDS[t.kind] && RT_KINDS[t.kind].operator){
    // operator 触发：无 PR 语义，note 一起下发，终态即已派发（台账见 Operator 视图）
    const note = $('#rt-note') ? $('#rt-note').value.trim() : '';
    if (note) opts.note = note;
    closeTriggerDialog();
    dispatchOperatorTrigger(t.btn, opts);
    return;
  }
  closeTriggerDialog();
  dispatchReviewTrigger(t.btn, t.org, t.number, opts, t.kind);
}
async function dispatchOperatorTrigger(btn, opts){
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '下发中…';
  try{
    // operator 无项目语义：machine-global 轮用当前看板项目位满足共享契约
    // （shared POST 块 400 project required —— review MAJOR-1）。
    const body = Object.assign({project: FILTER.project || 'machine-global'}, opts || {});
    const resp = await fetch('/api/operator/trigger', {method: 'POST',
      headers: {'Content-Type': 'application/json',
                'X-Requested-With': 'dashboard-fetch'},
      body: JSON.stringify(body)});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) throw new Error(data.error || data.decision || ('HTTP ' + resp.status));
    btn.textContent = '已派发 ✓';
    if (data.id) location.hash = '#view=run&exec=' + encodeURIComponent(data.id);
    else if (CURRENT_VIEW === 'operator') loadOperator().then(renderOperator);
  }catch(err){
    btn.title = err.message || String(err);
    btn.textContent = '失败';
  }finally{
    setTimeout(() => { btn.textContent = old; btn.disabled = false;
      btn.title = ''; }, 4000);
  }
}
async function dispatchReviewTrigger(btn, org, number, opts, kind){
  if (btn.disabled) return;
  kind = kind || 'review';
  const K = RT_KINDS[kind];
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '下发中…';
  const hintOf = triggerHintOf;
  // 终态立刻重取看板：警示条 / 预检三态当场落地，不等下一次 60s 自动刷新。
  const refreshBoard = () => { if (CURRENT_VIEW === 'kanban') loadKanban(true).then(renderKanban); };
  try{
    const resp = await fetch(K.post, {method: 'POST',
      headers: {'Content-Type': 'application/json',
                'X-Requested-With': 'dashboard-fetch'},
      body: JSON.stringify(Object.assign({project: org, number: number}, opts || {}))});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) throw new Error(data.error || 'HTTP ' + resp.status);
    btn.textContent = data.machine ? ('已派发 ' + data.machine + ' ✓') : '已下发（排队）';
    refreshBoard();   // POST 已清快照：这一次取数即见
    // 状态短轮询（≤6 次 × 5s）：排队→已派发；终态→原因/完成。
    for (let i = 0; i < 6; i++){
      await new Promise(r => setTimeout(r, 5000));
      const st = await fetch(K.status + '?project='
        + encodeURIComponent(org) + '&number=' + number,
        {headers: {'X-Requested-With': 'dashboard-fetch'}})
        .then(r => r.json()).catch(() => null);
      if (!st || !st.command) continue;
      if (st.state === 'SPAWNED'){
        btn.textContent = '运行中 ✓';
        // claim 已落项目 CP 库：立即刷新看板——评审轮把卡片移入「审核中」列，
        // 修复轮把执行卡移入「处理中」列（整卡点击进 view=run 运行详情），
        // 都不等下一次 60s 自动刷新。
        refreshBoard();
        break;
      }
      if (st.state === 'DONE'){ btn.textContent = '上一轮已结束'; btn.title = triggerHintOf('DONE'); refreshBoard(); break; }
      if (st.state === 'EXPIRED'){ btn.textContent = '已过期（可重试）'; btn.title = triggerHintOf('COMMAND_TTL'); refreshBoard(); break; }
      if (st.state === 'FAILED'){
        btn.textContent = '失败';
        btn.title = hintOf(st.fail_reason);
        PREFLIGHT[org] = undefined;   // failure may mean registration changed: re-probe next render
        refreshBoard();               // 失败条与新的预检当场出现
        break;
      }
      if (st.state === 'CLAIMED' && st.machine) btn.textContent = '已派发 ' + st.machine + ' ✓';
    }
  }catch(err){
    btn.title = triggerHintOf(err.message || err);
    btn.textContent = '失败';
  }finally{
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 4000);
  }
};

// ---- 看板横向拖动：按住左键平移（≥6px 才算拖）；滚动条/按钮/链接不受影响
(function(){
  const board = $('#kanban-board');
  let drag = null, panned = false;
  board.addEventListener('pointerdown', e => {
    if (e.button !== 0 || e.pointerType === 'touch') return;  // 触屏走原生滚动
    if (!e.target.closest('.kanban-col')) return;  // 滚动条/列间空隙让位原生行为
    if (e.target.closest('button,a,details,summary,input,textarea,select,[role=button]'))
      return;
    drag = {x: e.clientX, sl: board.scrollLeft};
    panned = false;
  });
  window.addEventListener('pointermove', e => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    if (!panned && Math.abs(dx) > 6){
      panned = true;
      board.classList.add('dragging');
    }
    if (panned) board.scrollLeft = drag.sl - dx;
  });
  const release = () => {
    if (drag && panned) setTimeout(() => { panned = false; }, 0);  // 吞掉紧随的一次 click
    drag = null;
    board.classList.remove('dragging');
  };
  window.addEventListener('pointerup', release);
  window.addEventListener('pointercancel', release);
  board.addEventListener('click', e => {
    if (panned){
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
})();

// ---- events (activity timeline) view
let EV_KIND = '', EV_KINDS = [];
async function loadEvents(){
  const params = new URLSearchParams({days: $('#ev-days').value});
  if (EV_KIND) params.set('kind', EV_KIND);
  if (FILTER.project) params.set('project', FILTER.project);
  try { DATA.ev = await jget('/api/events?' + params); } catch (e) { DATA.ev = null; }
}
const EV_NOTE_KEYS = ['decision', 'summary', 'title', 'state', 'action', 'worker',
                      'branch', 'loop', 'key', 'number'];
// 事件 kind 中文化：内部枚举 → 用户可读标签（未收录的保留原名）
const EV_KIND_ZH = {
  'item.created': '工作项创建', 'sync.completed': '同步完成', 'evidence.added': '证据入库',
  'execution.claimed': '领取执行', 'execution.reported': '上报结果', 'execution.derived': '派生执行',
  'execution.bound': '绑定分支', 'publication.sent': '发布已发', 'execution.stale_cancelled': '过期取消',
  'sync.failed': '同步失败', 'execution.swept': '清扫执行', 'execution.finished': '执行完成',
  'handler.blocked.cooldown': '冷却阻塞', 'command.acked': '命令确认', 'command.enqueued': '命令入队',
  'command.dispatched': '命令派发', 'command.swept': '命令清扫', 'command.progress_done': '进度完成',
  'execution.abandoned': '执行弃置', 'execution.recovered': '执行恢复', 'item.revived': '工作项复活',
  'machine.enabled': '机器启用', 'exam.touched_by_pr': '考题被 PR 触及', 'exam.source': '考题来源',
  'handler.blocked.recovered': 'handler 解除阻塞',
};
const evKindZh = k => EV_KIND_ZH[k] || k;
function renderEvents(){
  const ev = DATA.ev;
  if (!ev){ $('#ev-list').innerHTML = '<div class="empty">加载失败或无数据</div>'; return; }
  $('#ev-total').textContent = '共 ' + ev.total + ' 条事件';
  const kinds = [...new Set((ev.recent || []).map(e => e.kind))].sort();
  if (!EV_KIND && kinds.length) EV_KINDS = kinds;  // only the unfiltered response sees all kinds
  const sel = $('#ev-kind');
  const keep = sel.value;
  sel.innerHTML = '<option value="">全部类型</option>' +
    (EV_KINDS.length ? EV_KINDS : kinds).map(k => `<option value="${esc(k)}"${k === EV_KIND ? ' selected' : ''}>${esc(evKindZh(k))}</option>`).join('');
  sel.value = keep;
  const counts = ev.daily.map(d => d.count), max = Math.max(1, ...counts);
  // sqrt 刻度：峰值日不再把其余天数压成不可见的细线；8% 下限保证低计数天肉眼可见
  // 每根条下加日期标签（MM-DD），hover 显示完整日期与计数
  $('#ev-chart').innerHTML = ev.daily.map(d => {
    const [, m, dd] = (d.date || '').split('-');
    const label = m && dd ? `${m}-${dd}` : d.date;
    return `<i style="height:${d.count ? Math.max(8, Math.round(100 * Math.sqrt(d.count) / Math.sqrt(max))) : 0}%" title="${d.date}：${d.count} 条"><b>${d.count || ''}</b><span class="ev-bar-date">${label}</span></i>`;
  }).join('');
  // 无详情事件聚合：同 kind 且无 note 的行合并为一行计数，避免详情列「—」刷屏
  const groups = {};
  (ev.recent || []).forEach(e => {
    const d = new Date(e.epoch * 1000);
    const key = `${two(d.getMonth()+1)}-${two(d.getDate())}`;
    (groups[key] = groups[key] || []).push(e);
  });
  const noteOf = e => {
    let note = '';
    try {
      const o = JSON.parse(e.payload || '{}');
      note = EV_NOTE_KEYS.map(k => o[k]).filter(x => x != null && x !== '' && HUMANISH(x))
        .map(x => trunc(String(x), 60)).join(' · ');
      if (!note && o.pr && o.pr.number) note = 'PR #' + o.pr.number;
      if (!note) note = '';
    } catch (err) { note = trunc(e.payload || '', 90); }
    return note;
  };
  const rowsHtml = rows => {
    const detailed = [], blank = {};
    rows.forEach(e => {
      const note = noteOf(e);
      if (note) detailed.push({e, note});
      else { const k = e.kind; blank[k] = (blank[k] || 0) + 1; }
    });
    const outHtml = Object.entries(blank).map(([k, n]) =>
      `<div class="ev-row"><span class="t">${fmtShort(rows.find(r => r.kind === k).epoch).slice(-5)}</span>
        <span class="proj">${esc(orgRepoShort(rows.find(r => r.kind === k).org_repo))}</span>
        <span class="kind" title="${esc(k)}">${esc(evKindZh(k))}</span>
        <span class="note mut">×${n} 条（无详情）</span></div>`).join('');
    return detailed.map(({e, note}) =>
      `<div class="ev-row"><span class="t">${fmtShort(e.epoch).slice(-5)}</span>
        <span class="proj">${esc(orgRepoShort(e.org_repo))}</span>
        <span class="kind" title="${esc(e.kind)}">${esc(evKindZh(e.kind))}</span>
        <span class="note">${esc(note)}</span></div>`).join('') + outHtml;
  };
  $('#ev-list').innerHTML = Object.keys(groups).length ? Object.entries(groups).map(
    ([day, rows]) => `<h3 class="ev-day">${day}（${rows.length}）</h3>` + rowsHtml(rows)).join('')
    : '<div class="empty">✓ 窗口内没有事件</div>';
}
$('#ev-days').addEventListener('change', () => loadEvents().then(renderEvents));
$('#ev-kind').addEventListener('change', e => { EV_KIND = e.target.value;
  loadEvents().then(renderEvents); });

function renderStatus(){
  const errs = DATA.errors || [];
  const errBox = $('#data-errors');
  errBox.style.display = errs.length ? 'flex' : 'none';
  if (errs.length) errBox.innerHTML =
    '<span class="chip err">数据获取失败</span>' +
    `<span title="${esc(errs.join('; '))}">${errs.length} 个接口未更新（其余视图仍显示既有数据）</span>`;
  const collected = DATA.ov?.collected_at;
  if (FOCUS_BUSY) {   // 切回页面触发的即时采集进行中：提示不被并发渲染覆盖
    $('#fresh').textContent = '即时采集中…';
    $('#fresh').title = '切回页面触发的即时采集中，完成后自动刷新';
  } else if (collected) {
    const age = Math.max(0, Date.now()/1000 - collected);
    $('#fresh').textContent = errs.length ? '部分数据获取失败' : '采集于 ' + ago(age) + '前';
    $('#fresh').title = '数据采集于 ' + fmt(collected) + ' · 每 60s 自动刷新'
      + (errs.length ? '；失败：' + errs.join('; ') : '');
    $('#fresh').style.color = errs.length ? 'var(--err)'
      : age > 5400 ? 'var(--err)' : age > 2700 ? 'var(--warn)' : '';
    $('#collect-btn').style.display = 'inline-block';
  } else {   // issue #336：从未成功采集（collected_at 空）时按钮必须可见——
    // 全新环境首采前 / 采集全失败时，UI 是唯一的采集与重试入口（鸡生蛋）。
    $('#fresh').textContent = errs.length ? '部分数据获取失败' : '尚未采集';
    $('#fresh').title = errs.length ? '失败：' + errs.join('; ')
      : '项目尚未成功采集过，点击「立即采集」开始';
    $('#fresh').style.color = errs.length ? 'var(--err)' : 'var(--warn)';
    $('#collect-btn').style.display = 'inline-block';
  }
  fitKanban();   // 失败条出现/消失会整体推移看板
}

// 分端点渲染（issue #130）：每个端点 resolve 后只重绘依赖它的区块，互不等待，
// 首绘不再被最慢端点阻塞。跨端点区块（actionStrip/pipeline 混用 ov/q/it）
// 在其任一依赖到达时重绘。
const SECTION_RENDER = {
  ov(){ renderFilterBar(); renderStatus(); alertBar(); renderActionStrip();
        renderProjects(); renderMachines(); renderLoops();
        loadRuntime().then(renderLoops); },
  q(){ renderActionStrip(); renderPipeline(); },
  it(){ renderActionStrip(); renderPipeline(); },
  tr(){ renderTrends(); },
};

function updateBadge(){
  const q = DATA.q || {merges:[],decisions:[],exceptions:[]};
  const alerts = DATA.ov?.alerts || [];
  // 口径统一：标题徽章 = 待办总数（待合并 + 待决策 + 全部告警），与告警条三档计数同源
  const n = q.merges.length + q.decisions.length + alerts.length;
  document.title = n ? '(' + n + ' 待办) code-loop dashboard' : 'code-loop dashboard';
  const c = document.createElement('canvas'); c.width = c.height = 64;
  const x = c.getContext('2d');
  x.fillStyle = '#0e7490'; x.beginPath(); x.arc(32, 32, 28, 0, 7); x.fill();
  if (n) {
    x.fillStyle = '#dc2626'; x.beginPath(); x.arc(47, 17, 16, 0, 7); x.fill();
    x.fillStyle = '#fff'; x.font = 'bold 19px sans-serif'; x.textAlign = 'center';
    x.fillText(n > 9 ? '9' : String(n), 47, 24);
  }
  let link = document.querySelector('link[rel="icon"]');
  if (!link) { link = document.createElement('link'); link.rel = 'icon';
    document.head.appendChild(link); }
  link.href = c.toDataURL();
}

async function loadWorkers(){
  // 失败保留旧值：机器卡片比回退 chips 新，别因一次抖动降级视图
  try { DATA.workers = await jget('/api/workers'); } catch (e) {}
}

async function loadRuntime(){
  if (!RT_PROJECT) {
    const ps = (DATA.ov?.projects || []).map(p => p.org_repo);
    if (!ps.length) return;
    RT_PROJECT = $('#rt-project').value || ps[0];
  }
  try { DATA.rt = await jget('/api/runtime?project=' + encodeURIComponent(RT_PROJECT)); }
  catch (e) { DATA.rt = null; }
}

async function refresh(){
  // 分端点渲染：每个端点 resolve 即渲染其区块，互不等待；按端点独立容错，
  // 任一失败只冻结该端点数据（保留旧值），不再拖垮整页。
  const trendsQ = FILTER.project ? '/api/trends?project=' + encodeURIComponent(FILTER.project)
                                 : '/api/trends';
  const endpoints = {ov: '/api/overview', q: '/api/queue', it: '/api/items', tr: trendsQ};
  const errors = [];
  // 当前视图的主内容先发请求：看板是落地视图，不等 overview/队列返回
  if (CURRENT_VIEW === 'kanban') loadKanban().then(() => paintIfChanged('kanban', JSON.stringify(DATA.kb), renderKanban));
  if (CURRENT_VIEW === 'events') loadEvents().then(() => paintIfChanged('events', JSON.stringify(DATA.ev), renderEvents));
  if (CURRENT_VIEW === 'loops') loadWorkers().then(() => paintIfChanged('loops', JSON.stringify(DATA.workers), renderMachines));
  await Promise.allSettled(Object.entries(endpoints).map(async ([k, url]) => {
    try {
      DATA[k] = await jget(url);
      const raw = JSON.stringify(DATA[k]);   // issue #314：载荷未变就跳过重绘
      if (raw !== LAST_PAINT[k]) { LAST_PAINT[k] = raw;
        try { SECTION_RENDER[k]?.(); }
        catch (e) { $('#fresh').textContent = '刷新失败: ' + (e?.message || e); $('#fresh').style.color = 'var(--err)'; }
      }
    } catch (e) {
      errors.push(String(e?.message || e).replace(/^Error:\s*/, ''));
    }
  }));
  DATA.errors = errors;
  renderStatus();
  updateBadge();
}

$('#tabs').addEventListener('click', e => {
  const btn = e.target.closest('button'); if (!btn) return;
  // 离开运行详情（含「← 返回看板」都经 showView 走这里）：停轮询与秒表，
  // 不能只靠 hashchange 的异步兜底——它到达时 CURRENT_VIEW 已被本处改掉
  if (CURRENT_VIEW === 'run' && btn.dataset.v !== 'run') stopRunPoll();
  document.querySelectorAll('#tabs button').forEach(b =>
    b.classList.toggle('active', b === btn));
  document.querySelectorAll('.view').forEach(v =>
    v.classList.toggle('active', v.id === 'view-' + btn.dataset.v));
  CURRENT_VIEW = btn.dataset.v;
  unmountViews(CURRENT_VIEW);   // 切走即卸载，切回由下方 load+render 重载
  syncHash();
  if (CURRENT_VIEW === 'events') loadEvents().then(renderEvents);
  if (CURRENT_VIEW === 'kanban') { fitKanban(); loadKanban().then(renderKanban); }
  if (CURRENT_VIEW === 'history') loadHistory();
  if (CURRENT_VIEW === 'operator') loadOperator().then(renderOperator);
  // 视图隐藏时 clientWidth=0，图表按回退宽度画；切入可见后立刻按实测宽度重画
  if (CURRENT_VIEW === 'trends') requestAnimationFrame(renderTrends);
  if (CURRENT_VIEW === 'loops') loadWorkers().then(renderMachines);
  if (CURRENT_VIEW === 'projects') renderProjects();   // 这两个 tab 无进入即渲染分支，卸载后切回会空白
  if (CURRENT_VIEW === 'pipeline') renderPipeline();
});
$('#rt-project').addEventListener('change', () => { RT_PROJECT = $('#rt-project').value; loadRuntime().then(renderLoops); });
$('#proj-filter').addEventListener('change', e => setFilter(e.target.value));
document.addEventListener('click', e => {
  const el = e.target.closest('[data-proj]');
  if (el) setFilter(el.dataset.proj);
});
function applyViewFromHash(){
  const h = HASH();
  if (h.view === 'decision' && h.n) {   // deep link / history into a decision detail
    if (CURRENT_VIEW !== 'decision' || DETAIL_N !== String(h.n)) openDetail(h);
    return;
  }
  if (h.view === 'run' && h.exec) {     // deep link / 审核中卡片点击：运行详情
    if (CURRENT_VIEW !== 'run' || RUN_EXEC !== String(h.exec)) openRun(h);
    return;
  }
  // 旧 webhook 深链仍带 view=queue：归一化到看板，别让历史消息落成死链
  const view = h.view === 'queue' ? 'kanban' : h.view;
  if (view && document.querySelector(`#tabs button[data-v="${view}"]`)
      && CURRENT_VIEW !== view)
    showView(view);
}
window.addEventListener('hashchange', () => {
  const h = HASH();
  // Leaving a detail via history: reset state BEFORE setFilter's syncHash runs,
  // otherwise the stale decision view gets re-anchored into the new location.
  if (CURRENT_VIEW === 'decision' && h.view !== 'decision') {
    CURRENT_VIEW = 'kanban';
    DETAIL_N = '';
    DETAIL_ORG = '';
    showView('kanban');
  }
  if (CURRENT_VIEW === 'run' && h.view !== 'run') {
    stopRunPoll();   // 离开运行详情：停轮询，别让定时器追着已隐藏的视图跑
    CURRENT_VIEW = 'kanban';
    RUN_EXEC = '';
    RUN_ORG = '';
    showView('kanban');
  }
  applyViewFromHash();
  const target = h.p || '';
  if (target !== FILTER.project) setFilter(target);
});
$('#collect-btn').addEventListener('click', async () => {
  const b = $('#collect-btn');
  b.disabled = true;
  b.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> 采集中…';
  await collectStart();   // 后台单飞：提交即返回，轮询到完成再复位按钮
  b.disabled = false; b.textContent = '立即采集';
  refresh();
});
// 键盘可达：所有 role=button 的伪链接（告警条/折叠列头/proj-link/验证链…）支持 Enter/Space
document.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target instanceof Element
      && e.target.matches('a[role="button"][tabindex], div[role="button"][tabindex]')) {
    e.preventDefault();
    e.target.click();
  }
});
// 即时采集：用户去 GitHub 合并完切回本页时，visibilitychange/focus 触发一次
// 「采集→刷新」，待合并卡秒级出列，不再等 schtasks 下一轮（≤2min）+ 60s 轮询。
// 防重入 + 30s 去抖；与定时采集并发由服务端 mkdir 锁兜底（skipped 照常刷新）。
let FOCUS_BUSY = false, FOCUS_AT = Date.now();   // 初值=加载时刻：首次打开不算「切回」
async function focusCollect(){
  if (FOCUS_BUSY || Date.now() - FOCUS_AT < 30000) return;
  FOCUS_BUSY = true; FOCUS_AT = Date.now();
  renderStatus();   // 立刻给出「即时采集中…」反馈，不等下一轮轮询
  try { await collectStart(); } catch (e) {}   // 失败不阻塞：refresh 拉现有快照
  finally { FOCUS_BUSY = false; refresh(); }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') focusCollect();
});
window.addEventListener('focus', focusCollect);   // 与上一条互为兜底，去抖挡重入
window.focusCollect = focusCollect;   // QA 入口（惯例同 window.collectVerify）

refresh();
applyViewFromHash();
setInterval(refresh, 60000);
