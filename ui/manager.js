'use strict';
const $ = (id) => document.getElementById(id);
const CORE = '@deepseek-ai/dsh';
let state = null;
let busy = false;
const queries = Object.fromEntries(['core', 'plugin'].map(k => [k, { list: 0, detail: 0, loadingList: false, loadingDetail: false, package: '', tags: {} }]));
const invoke = (command, args) => window.__TAURI__.core.invoke(command, args);
const manage = (request) => invoke('manage', { request });
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function notice(text, kind = '') { $('notice').textContent = text; $('notice').className = kind; }
function show(page) {
  document.querySelectorAll('.page').forEach(el => el.hidden = el.id !== page);
  document.querySelectorAll('.nav').forEach(el => el.classList.toggle('active', el.dataset.page === page));
}
function controls() {
  document.querySelectorAll('.action').forEach(el => el.disabled = busy);
  $('open-normal').disabled = busy || !state?.core;
  $('open-safe').disabled = busy || !state?.core;
  $('stop-safe').disabled = busy || !state?.safeRunning;
  $('stop-normal').disabled = busy || !state?.normalRunning;
  $('plugin-install').disabled = busy || !$('profile').value;
  $('profile').disabled = busy;
  $('plugin-name').disabled = busy;
  for (const kind of ['core', 'plugin']) {
    const q = queries[kind];
    $(kind + '-fetch').disabled = busy || q.loadingList;
    $(kind + '-details').disabled = busy || q.loadingDetail || q.loadingList;
    $(kind + '-versions').disabled = busy || q.loadingList || !q.package;
    $(kind + '-version').disabled = busy;
  }
}
function actionButton(text, callback, danger = false) {
  const b = node('button', text, 'action' + (danger ? ' danger' : ''));
  b.addEventListener('click', callback); b.disabled = busy; return b;
}
const statusLabels = { match: '声明匹配', mismatch: '不匹配', invalid: '声明无效', unknown: '未核验', undeclared: '未声明内核范围' };
function statusBadge(status) {
  return node('span', statusLabels[status] || '未核验', 'badge ' + (status === 'match' ? 'on' : ['mismatch','invalid'].includes(status) ? 'fail' : 'warn'));
}
function rangeTable(rows) {
  const wrap = node('div', undefined, 'compat-table-wrap');
  const table = node('table'); const head = node('thead'); const hr = node('tr');
  ['依赖 / 环境', '作者声明的范围', '比较版本', '结果'].forEach(text => hr.append(node('th', text)));
  head.append(hr); table.append(head); const body = node('tbody');
  for (const row of rows) {
    const tr = node('tr');
    tr.append(node('td', row.name + (row.optional ? '（可选）' : '')), node('td', typeof row.range === 'string' ? row.range : JSON.stringify(row.range)), node('td', row.version || '—'));
    const result = node('td'); result.append(statusBadge(row.status));
    if (row.reason) result.append(node('p', row.reason, 'error-text'));
    tr.append(result); body.append(tr);
  }
  table.append(body); wrap.append(table); return wrap;
}
function renderCompatibility(kind, info) {
  const root = $(kind + '-compat'); root.replaceChildren();
  root.append(node('h3', info.package + ' @ ' + info.version));
  if (info.deprecated) root.append(node('p', '发布者已标记弃用：' + info.deprecated, 'error-text'));
  if (kind === 'plugin') {
    const title = node('div', undefined, 'compat-title');
    title.append(node('span', '当前内核：' + (info.currentCore || '未安装 / 不可用')), statusBadge(info.coreStatus)); root.append(title);
    if (!info.isBundle) root.append(node('p', '此版本没有声明 dsh.bundle，不能作为 Harness 插件安装。', 'error-text'));
    const dsh = info.checks.filter(r => r.kind === 'dsh');
    if (dsh.length) root.append(rangeTable(dsh));
    else root.append(node('p', '发布者未声明 DSH peerDependencies，无法据此判断兼容性。', 'muted'));
    if (!info.currentCore && dsh.length) root.append(node('p', '仍可查看作者声明；安装内核后可重新核验。', 'muted'));
  } else {
    root.append(node('p', '已装插件与候选内核 ' + info.targetCore + ' 的声明检查', 'muted'));
    if (!info.installedPlugins.length) root.append(node('p', '未检测到第三方插件。', 'muted'));
    for (const p of info.installedPlugins) {
      const details = node('details'); const summary = node('summary');
      summary.append(node('span', `${p.name} @ ${p.version || '未安装'} · ${p.profile} · ${p.enabled ? '已启用' : '已停用'} `), statusBadge(p.coreStatus));
      details.append(summary);
      if (p.checks.length) details.append(rangeTable(p.checks));
      else details.append(node('p', '没有可读取的内核兼容声明，不能据此确认兼容。', 'muted'));
      root.append(details);
    }
    for (const p of info.profileErrors) root.append(node('p', p.profile + ' 无法检查：' + p.error, 'error-text'));
  }
  root.append(node('h3', '运行环境声明（engines）'));
  if (info.engines.length) root.append(rangeTable(info.engines));
  else root.append(node('p', '此版本未声明 engines。当前 Node.js：' + (info.nodeVersion || '未检测到'), 'muted'));
  const other = info.checks.filter(r => r.kind !== 'dsh');
  if (other.length) {
    const details = node('details'); details.append(node('summary', '其他依赖声明（' + other.length + ' 项，未核验本机安装）'), rangeTable(other)); root.append(details);
  }
  root.append(node('p', '来源：所选精确版本的 npm 包元数据。内核范围包含预发布版本；声明匹配不代表实际启动已通过。', 'footnote'));
}
function resetDetails(kind, text = '版本已变化，请查看此版本的兼容声明。') {
  const q = queries[kind]; q.detail++; q.loadingDetail = false;
  $(kind + '-compat').replaceChildren(node('p', text, 'muted'));
}
function resetVersions(kind) {
  const q = queries[kind]; q.list++; q.package = ''; q.loadingList = false; q.tags = {};
  $(kind + '-versions').replaceChildren(node('option', '尚未获取版本'));
  $(kind + '-version-count').textContent = '点击“获取可用版本”加载';
  resetDetails(kind, '尚未选择要查看的版本。'); controls();
}
async function loadDetails(kind) {
  if (busy) return;
  const name = kind === 'core' ? CORE : $('plugin-name').value.trim();
  const version = $(kind + '-version').value.trim();
  const q = queries[kind]; const id = ++q.detail;
  q.loadingDetail = true; controls();
  const root = $(kind + '-compat'); root.replaceChildren(node('p', `正在读取 ${name} @ ${version} 的兼容声明…`, 'muted'));
  try {
    const result = await manage({ action: 'version_info', package: name, version });
    if (q.detail !== id) return;
    renderCompatibility(kind, result);
  } catch (e) {
    if (q.detail === id) root.replaceChildren(node('p', '读取兼容声明失败：' + String(e), 'error-text'));
  } finally { if (q.detail === id) { q.loadingDetail = false; controls(); } }
}
async function fetchVersions(kind) {
  if (busy) return;
  const name = kind === 'core' ? CORE : $('plugin-name').value.trim();
  resetVersions(kind);
  const q = queries[kind]; const id = ++q.list;
  q.loadingList = true; controls();
  $(kind + '-version-count').textContent = '查询中…';
  $(kind + '-versions').replaceChildren(node('option', '正在从 npm 获取版本…'));
  try {
    const result = await manage({ action: 'versions', package: name });
    if (q.list !== id) return;
    if (!result.versions?.length) throw new Error('此包没有可用的已发布版本。');
    q.package = name; q.tags = result.tags || {};
    const list = $(kind + '-versions');
    list.replaceChildren(...result.versions.map(v => {
      const tags = Object.entries(q.tags).filter(([, value]) => value === v).map(([tag]) => tag);
      const label = v + (tags.length ? ' · ' + tags.join(' / ') : '') + (v.includes('-') ? ' · 预发布' : '');
      const opt = node('option', label); opt.value = v; return opt;
    }));
    const previous = $(kind + '-version').value.trim();
    const current = kind === 'core' ? state?.core?.version : state?.profiles.find(p => p.name === $('profile').value)?.plugins.find(p => p.name === name)?.version;
    list.value = [previous, current, q.tags.latest, result.versions[0]].find(v => result.versions.includes(v));
    $(kind + '-version').value = list.value;
    $(kind + '-version-count').textContent = `共 ${result.versions.length} 个版本，按版本号从新到旧`;
    notice(`已获取 ${name} 的 ${result.versions.length} 个版本，请在列表中选择。`, 'success');
    q.loadingList = false; controls();
    await loadDetails(kind);
  } catch (e) {
    if (q.list !== id) return;
    $(kind + '-version-count').textContent = '获取失败，可重试';
    $(kind + '-versions').replaceChildren(node('option', '获取失败'));
    $(kind + '-compat').replaceChildren(node('p', String(e), 'error-text'));
    notice('获取版本失败：' + String(e), 'error');
  } finally { if (q.list === id) { q.loadingList = false; controls(); } }
}
function renderPlugins() {
  const root = $('plugin-list'); root.replaceChildren();
  const profile = state?.profiles.find(p => p.name === $('profile').value);
  if (!profile) { root.append(node('p', '还没有可管理的 profile。首次普通启动后可在这里管理插件。', 'empty')); return; }
  if (profile.error) { root.append(node('p', profile.error, 'error-text')); return; }
  if (!profile.plugins.length) root.append(node('p', '此环境没有第三方插件。', 'empty'));
  for (const plugin of profile.plugins) {
    const item = node('article', undefined, 'plugin'); const heading = node('div', undefined, 'plugin-heading');
    heading.append(node('strong', plugin.name, 'plugin-name'), node('span', plugin.enabled ? '已启用' : '已停用', 'badge' + (plugin.enabled ? ' on' : '')));
    item.append(heading, node('p', `${plugin.version || '未安装 / 链接失效'} · ${plugin.local ? '本地源码' : '版本声明 ' + (plugin.spec || '未声明')}`, 'plugin-meta'));
    const bar = node('div', undefined, 'plugin-controls');
    if (!plugin.local) bar.append(actionButton('查看 / 切换版本', () => {
      $('plugin-name').value = plugin.name; $('plugin-version').value = plugin.version || '';
      $('plugin-form-title').scrollIntoView({ behavior: 'smooth', block: 'start' }); fetchVersions('plugin');
    }));
    bar.append(actionButton(plugin.enabled ? '停用' : '启用', () => perform({action:'plugin_toggle', profile:profile.name, package:plugin.name, enabled:!plugin.enabled}, '正在更新插件启用状态…')));
    const remove = actionButton('移除', () => {
      if (remove.dataset.confirm === 'yes') perform({action:'plugin_remove', profile:profile.name, package:plugin.name}, '正在移除插件…');
      else { remove.dataset.confirm = 'yes'; remove.textContent = '确认移除'; remove.title = '再次点击移除插件，不删除会话或本地源码'; }
    }, true);
    bar.append(remove); item.append(bar);
    const peers = Object.entries(plugin.peers || {}); const details = node('details');
    details.append(node('summary', peers.length ? '已装版本的内核兼容声明（' + peers.length + ' 项）' : '未声明内核兼容范围'));
    const dl = node('dl');
    for (const [name, range] of peers) dl.append(node('dt', name), node('dd', range));
    details.append(dl); item.append(details); root.append(item);
  }
  controls();
}
function render() {
  $('current-version').textContent = state.core?.version || '未安装 / 不可用';
  $('normal-status').textContent = state.normalRunning ? '普通后端运行中' : '普通后端未运行';
  $('normal-status').className = 'badge' + (state.normalRunning ? ' on' : '');
  $('log').textContent = state.logTail || '暂无管理操作。';
  $('core-error').hidden = !state.coreError; $('core-error').textContent = state.coreError || '';
  const cores = $('core-list'); cores.replaceChildren();
  if (!state.cores.length) cores.append(node('p', '未检测到可用内核，可以在下方安装。', 'empty'));
  for (const core of state.cores) {
    const item = node('div', undefined, 'version-item'); const desc = node('div');
    desc.className = 'version-description';
    desc.append(node('strong', core.version), node('small', core.entry)); item.append(desc);
    if (core.uninstallKind) desc.append(node('small', core.uninstallKind === 'managed' ? '管理器安装' : 'npm 全局安装'));
    const bar = node('div', undefined, 'version-actions');
    if (core.current) bar.append(node('span', '当前使用', 'badge on'));
    else bar.append(actionButton('切换到此版本', () => perform({action:'switch_core', version:core.version, entry:core.entry}, '正在切换内核…')));
    if (core.canUninstall && !core.current) {
      const confirmation = node('div', undefined, 'uninstall-confirmation'); confirmation.hidden = true;
      confirmation.append(node('strong', `卸载内核 ${core.version}？`),
        node('p', '将先停止普通和安全后端，再删除此安装目录。会话、配置和插件数据会保留。'),
        node('code', core.uninstallPath));
      const actions = node('div', undefined, 'button-row');
      actions.append(actionButton('确认卸载', () => perform({action:'uninstall_core', version:core.version, entry:core.entry}, `正在卸载内核 ${core.version}…`), true),
        actionButton('取消', () => { confirmation.hidden = true; remove.focus(); }));
      confirmation.append(actions);
      const remove = actionButton('卸载', () => { confirmation.hidden = false; }, true);
      remove.setAttribute('aria-label', `卸载内核 ${core.version}`);
      bar.append(remove); item.append(bar, confirmation);
    } else {
      item.append(bar);
      if (core.uninstallReason) desc.append(node('small', core.uninstallReason));
    }
    cores.append(item);
  }
  const chosen = $('profile').value;
  $('profile').replaceChildren(...state.profiles.map(p => { const opt = node('option', p.name); opt.value = p.name; return opt; }));
  if (state.profiles.some(p => p.name === chosen)) $('profile').value = chosen;
  else if (state.profiles.some(p => p.name === 'web')) $('profile').value = 'web';
  renderPlugins(); controls();
}
async function refresh(silent = false) {
  try { state = await manage({action:'status'}); render(); }
  catch (e) { if (!silent) notice(String(e), 'error'); }
}
async function perform(request, message) {
  if (busy) return;
  busy = true; controls(); notice(message, 'busy');
  for (const kind of ['core', 'plugin']) {
    // Invalidate in-flight metadata before a mutation can change installed versions.
    queries[kind].list++; queries[kind].loadingList = false;
    resetDetails(kind, '安装状态可能变化，请重新查看兼容声明。');
  }
  const timer = setInterval(() => refresh(true), 3000);
  try { const result = await manage(request); notice(result.message || '操作完成。', 'success'); }
  catch (e) { notice(String(e), 'error'); }
  finally { clearInterval(timer); busy = false; await refresh(true); controls(); }
}
document.querySelectorAll('[data-page]').forEach(b => b.addEventListener('click', () => show(b.dataset.page)));
document.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => show(b.dataset.go)));
$('profile').addEventListener('change', renderPlugins);
$('refresh').addEventListener('click', async () => {
  for (const kind of ['core','plugin']) resetDetails(kind, '状态已刷新，请重新查看兼容声明。');
  await refresh(); controls();
});
$('open-normal').addEventListener('click', () => perform({action:'open_normal'}, '正在连接普通后端…管理页面会保持可用。'));
$('open-safe').addEventListener('click', () => perform({action:'open_safe'}, '正在启动安全模式…首次启动可能需要一些时间。'));
$('stop-normal').addEventListener('click', () => perform({action:'stop_normal'}, '正在停止普通后端…'));
$('stop-safe').addEventListener('click', () => perform({action:'stop_safe'}, '正在停止安全模式…'));
for (const kind of ['core', 'plugin']) {
  $(kind + '-fetch').addEventListener('click', () => fetchVersions(kind));
  $(kind + '-details').addEventListener('click', () => loadDetails(kind));
  $(kind + '-versions').addEventListener('change', () => { $(kind + '-version').value = $(kind + '-versions').value; loadDetails(kind); });
  $(kind + '-version').addEventListener('input', () => { $(kind + '-versions').value = $(kind + '-version').value.trim(); resetDetails(kind); controls(); });
}
$('core-install').addEventListener('click', () => perform({action:'install_core', version:$('core-version').value.trim()}, '正在下载、校验和安装内核。当前内核会在新版本安装成功后才切换，可在操作日志查看进度。'));
$('plugin-install').addEventListener('click', () => perform({action:'plugin_install', profile:$('profile').value, package:$('plugin-name').value.trim(), version:$('plugin-version').value.trim()}, '正在安装插件版本…普通后端将保持停止。'));
$('plugin-name').addEventListener('input', () => { resetVersions('plugin'); $('plugin-version').value = ''; });
(async () => {
  if (!window.__TAURI__?.core?.invoke) { notice('请通过桌面外壳打开此管理页面。', 'error'); controls(); return; }
  await refresh();
  if (state) notice(state.core ? '管理器已就绪。' : '管理器已就绪；安装内核后即可启动。');
  try {
    const mode = await invoke('startup_mode');
    if (mode === 'safe' || mode === 'normal') await perform({action:'open_' + mode}, '正在打开 ' + (mode === 'safe' ? '安全' : '普通') + '模式…');
  } catch (e) { notice(String(e), 'error'); }
})();
