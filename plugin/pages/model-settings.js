(function () {
  'use strict';
  const el = id => document.getElementById(id);
  const fields = {model:'model-name',max_tokens:'model-tokens',timeout_ms:'model-timeout',retries:'model-retries',temperature:'model-temperature'};
  function show(data) {
    for (const [name,id] of Object.entries(fields)) el(id).value = data.config[name];
    el('model-thinking').value = data.config.thinking?.type || '';
    el('model-effort').value = data.config.reasoning_effort || '';
    el('model-tokens').max = data.limits.max_tokens || '';
    el('model-extra').textContent = '配置来源：' + (data.source === 'live' ? '本地覆盖' : '项目初始配置') +
      '\n温度分档：' + JSON.stringify(data.temperature_tiers, null, 2);
    el('model-fields').disabled = false;
    el('model-save').disabled = false;
  }
  async function request(path, body) {
    const response = await panelFetch(path, {method:body ? 'POST' : 'GET',
      ...(body ? {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (response.status !== 200 || !data.ok) throw Error(data.message || '读取失败');
    return data;
  }
  async function refresh() {
    el('model-save').disabled = true;
    el('model-fields').disabled = true;
    try { show(await request('/model/status')); el('model-status').textContent = '已读取当前调用配置。'; }
    catch (error) { el('model-status').textContent = '参数暂不可用：' + error.message; }
  }
  el('model-refresh').addEventListener('click', refresh);
  el('model-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (el('model-save').disabled) return;
    el('model-save').disabled = true;
    const body = Object.fromEntries(Object.entries(fields).map(([name,id]) =>
      [name,name === 'model' ? el(id).value : Number(el(id).value)]));
    body.thinking = el('model-thinking').value ? {type:el('model-thinking').value} : null;
    body.reasoning_effort = el('model-effort').value || null;
    try { show(await request('/model/config',body)); el('model-status').textContent = '已保存，后续调用读取新参数；同值保存不会增加变化记录。'; }
    catch (error) { el('model-status').textContent = '没有保存：' + error.message; el('model-save').disabled = false; }
  });
  refresh();
}());
