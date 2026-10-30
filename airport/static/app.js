'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let state = null;
let setupRequired = false;
let toastTimer;
let pollTimer;
let dirty = false;
let loadingState = false;

function text(selector, value) { $(selector).textContent = value; }
function toast(message, error = false) {
  const box = $('#toast'); box.textContent = message; box.className = error ? 'error' : ''; box.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { box.hidden = true; }, error ? 8000 : 4500);
}
function errors(root, fields = {}) {
  $$('[data-error]', root).forEach(node => { node.textContent = fields[node.dataset.error] || ''; });
  $$('input', root).forEach(node => { if (fields[node.name]) node.setAttribute('aria-invalid', 'true'); else node.removeAttribute('aria-invalid'); });
}
async function api(path, data) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch('/api/' + path, {method: data === undefined ? 'GET' : 'POST',
      credentials: 'same-origin', headers: data === undefined ? {} : {'Content-Type': 'application/json'},
      body: data === undefined ? undefined : JSON.stringify(data), signal: controller.signal});
    const body = await response.json();
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('auth/')) await showAuth();
      const error = new Error(body.error || '操作失败，请稍后重试。'); error.fields = body.fields || {}; throw error;
    }
    return body;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，请检查 NAS 网络。操作可能仍在进行，刷新状态后再试。');
    if (error instanceof TypeError) throw new Error('无法连接 NAS 面板，请检查网络和容器状态。');
    throw error;
  } finally { clearTimeout(timer); }
}
async function busy(button, action, errorBox) {
  if (button.disabled) return;
  button.disabled = true;
  const old = button.textContent; button.textContent = '处理中…';
  if (errorBox) text(errorBox, '');
  try { await action(); }
  catch (error) { if (errorBox) text(errorBox, error.message); toast(error.message, true); }
  finally { button.disabled = false; button.textContent = old; }
}
function configData() { return Object.fromEntries(new FormData($('#config-form'))); }
function badge(selector, label, kind) { const node = $(selector); node.textContent = label; node.className = 'badge ' + kind; }
function node(tag, value, className) { const n = document.createElement(tag); if (value !== undefined) n.textContent = value; if (className) n.className = className; return n; }
function formatBytes(value) { return value > 1048576 ? (value / 1048576).toFixed(1) + ' MB' : (value / 1024).toFixed(1) + ' KB'; }

function providerFields() {
  const provider = $('#dns-provider').value;
  const spec = {
    cloudflare: {fields: [['CF_DNS_API_TOKEN', 'DNS API Token']], hint: '为该域名区域授予 Zone → DNS → Edit 和 Zone → Zone → Read。解析记录使用“仅 DNS”（灰色云朵）。'},
    alidns: {fields: [['ALICLOUD_ACCESS_KEY', 'AccessKey ID'], ['ALICLOUD_SECRET_KEY', 'AccessKey Secret']], hint: '使用具有该域名 DNS 管理权限的 RAM 子账号，不建议使用主账号密钥。'},
    tencentcloud: {fields: [['TENCENTCLOUD_SECRET_ID', 'SecretId'], ['TENCENTCLOUD_SECRET_KEY', 'SecretKey']], hint: '使用具有对应 DNSPod 域名解析管理权限的腾讯云子账号密钥。'},
    dnspod: {fields: [['DNSPOD_API_KEY', 'DNSPod API Key（ID,Token）']], hint: '填写 DNSPod 个人令牌的 ID,Token，中间使用英文逗号；不是腾讯云 SecretId / SecretKey。'}
  }[provider];
  const container = $('#dns-fields'); container.replaceChildren();
  for (const [name, label] of spec.fields) {
    const item = node('label', label); const input = node('input'); input.name = name; input.type = 'password'; input.autocomplete = 'off';
    input.placeholder = state?.acme.credentials_saved && state.acme.provider === provider ? '已保存，留空保留' : '仅保存在 NAS，不会返回浏览器';
    item.append(input); container.append(item);
  }
  text('#dns-help', spec.hint + ' 凭据保存在受限数据目录中，备份整个目录时请妥善保管。');
}
function render(fillForms = false) {
  const {config, gateway, certificate, acme, certificate_job: job} = state;
  text('#version', 'airport v' + state.version);
  text('#listen-control', gateway.ports.control); text('#listen-pcm', gateway.ports.pcm);
  if (fillForms && config) for (const [key, value] of Object.entries(config)) if ($('#config-form').elements[key]) $('#config-form').elements[key].value = value;
  if (fillForms) { $('#dns-provider').value = acme.provider; $('#acme-form').elements.email.value = acme.email; providerFields(); }
  text('#gateway-status', gateway.running ? '正在运行' : '尚未启动');
  text('#gateway-detail', gateway.running ? '控制与音频 TLS 监听已开启' : '保存配置与证书后，点击应用启动');
  badge('#top-status', gateway.running ? '● 网关运行中' : '○ 网关未启动', gateway.running ? 'ok' : 'neutral');
  text('#connections', (gateway.active.control + gateway.active.pcm) + ' 个');
  text('#traffic', `控制 ${gateway.active.control} · 音频 ${gateway.active.pcm} · 已转发 ${formatBytes(gateway.bytes.up + gateway.bytes.down)}`);
  text('#cert-status', certificate ? (certificate.days_left >= 0 ? `剩余 ${certificate.days_left} 天` : '证书已过期') : '尚未配置');
  text('#cert-detail', certificate ? certificate.domain : '自动申请或导入 NAS 已有证书');
  const certOK = certificate && certificate.days_left >= 0 && config && certificate.domain === config.domain;
  badge('#certificate-badge', certificate ? (certOK ? '证书已就绪' : '证书需要更新') : '等待证书', certOK ? 'ok' : 'warning');
  text('#acme-job', job.message + (!acme.available ? ' 当前环境未安装 lego；Docker 镜像已内置。' : ''));
  $('#acme-start').disabled = job.state === 'running' || !acme.available;
  $('#stop-gateway').disabled = !gateway.running;
  $('#step-1').classList.toggle('done', !!config); $('#step-2').classList.toggle('done', !!certOK); $('#step-3').classList.toggle('done', gateway.running);
  const pending = gateway.running && JSON.stringify(config) !== JSON.stringify(gateway.config);
  text('#apply-hint', pending ? '已保存的配置尚未应用，当前连接仍使用之前的配置。' : gateway.running ? '正在运行。修改配置后再次应用，已有连接保留原目标。' : '先保存连接配置并准备证书，再应用启动。');
  text('#phone-domain', config?.domain || '尚未设置'); text('#phone-control', config?.public_control_port || '—'); text('#phone-pcm', config?.public_pcm_port || '—');
  const rows = $('#router-rules'); rows.replaceChildren();
  for (const [label, publicPort, localPort] of [['控制', config?.public_control_port, gateway.ports.control], ['音频', config?.public_pcm_port, gateway.ports.pcm]]) {
    const tr = node('tr'); [label, publicPort || '待设置', localPort, 'TCP'].forEach(value => tr.append(node('td', value))); rows.append(tr);
  }
  const events = $('#events'); events.replaceChildren();
  if (!gateway.events.length) events.append(node('p', '尚无运行事件。', 'muted'));
  for (const event of gateway.events) {
    const row = node('div', undefined, 'event ' + event.level);
    row.append(node('time', new Date(event.time).toLocaleString('zh-CN', {hour12: false})), node('span', event.message)); events.append(row);
  }
}
async function loadState(fillForms = false) {
  if (loadingState) return;
  loadingState = true;
  try { state = await api('state'); render(fillForms); }
  finally { loadingState = false; }
}
async function showAuth() {
  clearInterval(pollTimer);
  $('#shell').hidden = true; $('#auth-screen').hidden = false;
  const auth = await api('auth/status'); setupRequired = auth.setup_required;
  if (auth.authenticated) return enter();
  text('#auth-title', setupRequired ? '欢迎，设置你的网关' : '欢迎回来');
  text('#auth-description', setupRequired ? '输入首次初始化码，再创建一个管理密码。下一步开始配置连接。' : '输入管理密码，查看连接与证书状态。');
  $('#setup-code-label').hidden = !setupRequired;
  $('#auth-form').elements.password.autocomplete = setupRequired ? 'new-password' : 'current-password';
  text('#auth-submit', setupRequired ? '创建管理员 →' : '进入面板 →');
}
async function enter() {
  $('#auth-screen').hidden = true; $('#shell').hidden = false;
  await loadState(true); clearInterval(pollTimer);
  pollTimer = setInterval(() => { if (!document.hidden) loadState().catch(error => toast(error.message, true)); }, 6000);
}
$('#auth-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('#auth-submit'), async () => {
    errors(form);
    try { await api(setupRequired ? 'auth/setup' : 'auth/login', Object.fromEntries(new FormData(form))); form.reset(); await enter(); }
    catch (error) { errors(form, error.fields); throw error; }
  }, '#auth-error');
});
$('#logout').addEventListener('click', () => busy($('#logout'), async () => { await api('auth/logout', {}); state = null; dirty = false; await showAuth(); }));
$('#config-form').addEventListener('input', () => { dirty = true; text('#config-save-state', '有未保存的修改'); });
$('#config-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('button[type=submit]', form), async () => {
    errors(form);
    try { const response = await api('config', configData()); dirty = false; text('#config-save-state', '已保存，待应用'); toast(response.message); await loadState(); }
    catch (error) { errors(form, error.fields); $('[aria-invalid=true]', form)?.focus(); throw error; }
  }, '#config-error');
});
$('#validate-config').addEventListener('click', () => busy($('#validate-config'), async () => {
  errors($('#config-form'));
  try { await api('config/validate', configData()); toast('填写格式正确。保存后继续检查证书与网络。'); }
  catch (error) { errors($('#config-form'), error.fields); throw error; }
}, '#config-error'));
$$('[data-tab]').forEach(button => button.addEventListener('click', () => {
  $$('[data-tab]').forEach(item => item.setAttribute('aria-selected', String(item === button)));
  ['upload', 'auto', 'mounted'].forEach(name => { $('#tab-' + name).hidden = name !== button.dataset.tab; });
  text('#certificate-error', '');
}));
$('#upload-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('button', form), async () => {
    const cert = form.elements.certificate.files[0], key = form.elements.private_key.files[0];
    if (!cert || !key) throw new Error('请选择完整证书链和私钥文件。');
    if (cert.size + key.size > 180000) throw new Error('证书文件过大，请选择 PEM 文本文件，不要选择压缩包。');
    const response = await api('certificate/upload', {certificate: await cert.text(), private_key: await key.text()});
    form.reset(); toast(response.message); await loadState();
  }, '#certificate-error');
});
$('#load-mounted').addEventListener('click', () => busy($('#load-mounted'), async () => { const r = await api('certificate/mounted', {}); toast(r.message); await loadState(); }, '#certificate-error'));
$('#dns-provider').addEventListener('change', providerFields);
$('#acme-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('button[type=submit]', form), async () => {
    errors(form); const all = Object.fromEntries(new FormData(form));
    const credentials = Object.fromEntries(Object.entries(all).filter(([key]) => !['provider', 'email', 'agree'].includes(key)));
    try { const response = await api('acme/settings', {provider: all.provider, email: all.email, agree: form.elements.agree.checked, credentials});
      $$('input[type=password]', form).forEach(input => { input.value = ''; }); toast(response.message); await loadState(); providerFields();
    } catch (error) { errors(form, error.fields); throw error; }
  }, '#certificate-error');
});
$('#acme-start').addEventListener('click', () => busy($('#acme-start'), async () => { const r = await api('acme/start', {}); toast(r.message); await loadState(); }, '#certificate-error'));
$('#start-gateway').addEventListener('click', () => busy($('#start-gateway'), async () => {
  if (dirty) throw new Error('连接设置还有未保存的修改，请先保存。');
  const r = await api('gateway/start', {}); toast(r.message); await loadState();
}));
$('#stop-gateway').addEventListener('click', () => {
  if (!confirm('停止网关会关闭现有控制与音频连接，正在进行的通话会中断。确定停止？')) return;
  busy($('#stop-gateway'), async () => { const r = await api('gateway/stop', {}); toast(r.message); await loadState(); });
});
$('#run-diagnostics').addEventListener('click', () => busy($('#run-diagnostics'), async () => {
  if (dirty) throw new Error('请先保存修改后的连接配置。');
  const container = $('#diagnostic-results'); container.replaceChildren(node('div', '正在检查域名、安卓端口与证书，请稍候…', 'empty-state'));
  try {
    const result = await api('diagnostics', {public: $('#test-public').checked}); container.replaceChildren();
    for (const check of result.checks) {
      const row = node('div', undefined, 'diagnostic-item');
      row.append(node('span', check.status === 'ok' ? '✓' : check.status === 'error' ? '×' : '!', 'dot ' + check.status), node('b', check.name));
      const content = node('div'); content.append(node('p', check.message)); if (check.detail) content.append(node('small', check.detail)); row.append(content); container.append(row);
    }
  } catch (error) { container.replaceChildren(node('div', error.message, 'error-message')); throw error; }
}));
$('#copy-settings').addEventListener('click', () => busy($('#copy-settings'), async () => {
  const config = state?.config; if (!config) throw new Error('请先保存连接配置。');
  const value = `AirSIM 自建域名直连\n域名：${config.domain}\nHTTPS 控制端口：${config.public_control_port}\nTLS 音频端口：${config.public_pcm_port}\n先完成局域网 VoWLAN 配对，再验证并保存。`;
  if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
  else { const area = node('textarea'); area.value = value; document.body.append(area); area.select(); const copied = document.execCommand('copy'); area.remove(); if (!copied) throw new Error('浏览器不允许自动复制，请手动复制上面的三项配置。'); }
  toast('手机配置已复制。');
}));
$('#password-toggle').addEventListener('click', () => { $('#password-form').hidden = !$('#password-form').hidden; });
$('#password-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy($('button', form), async () => { await api('auth/password', Object.fromEntries(new FormData(form))); form.reset(); form.hidden = true; toast('管理密码已更新，其他会话已退出。'); });
});
$$('.nav-link').forEach(link => link.addEventListener('click', () => { $$('.nav-link').forEach(item => item.classList.toggle('active', item === link)); }));
window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
showAuth().catch(error => { $('#auth-screen').hidden = false; text('#auth-title', '暂时无法连接'); text('#auth-description', error.message); });
