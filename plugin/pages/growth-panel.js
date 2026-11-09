/* 魂和今天共用一份成长读写面；所有模型行为仍由后端具名入口执行。 */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  var labels = {likes:'成长兴趣',dislikes:'不喜欢',views:'当前看法',self_description:'怎样看自己',stance:'待人倾向',sleep_notice:'睡眠提示'};
  var states = {active:'进行中',paused:'暂停',done:'完成',abandoned:'已放下'};
  var current = null;
  var limitsForm = el('growth-limits');
  function localTime(stamp) {
    return stamp ? new Date(stamp).toLocaleString('zh-CN', {hour12:false}) : '时间未记录';
  }
  async function request(path, body) {
    var response = await panelFetch('/day/growth' + path, body === undefined ? {method:'GET'} :
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    var data = await response.json();
    if (response.status !== 200 || data.ok === false) throw new Error(data.message || '读取失败');
    return data;
  }
  function profile(value) {
    return Object.keys(labels).map(function (key) {
      return labels[key] + '：' + (Object.prototype.hasOwnProperty.call(value,key) ? (value[key] || '已清空') : (key === 'sleep_notice' ? '睡觉啦，明天说（默认，每次深睡对每位私聊联系人提示一次）' : key === 'likes' ? '尚未形成新兴趣；标志性喜好保留' : '沿用初始设定'));
    }).join('\n');
  }
  async function refresh() {
    el('growth-toggle').disabled = true;
    if (limitsForm) el('growth-limits-save').disabled = true;
    try {
      var data = await request('');
      current = data;
      if (el('solitude-card')) el('solitude-card').hidden = data.settings.enabled;
      el('growth-state').textContent = (data.settings.enabled ? '自主成长已开启' : '自主成长未开启') +
        ' · 每 ' + data.interval_minutes + ' 分钟一次机会，每个生活日最多 ' + data.daily_cap +
        ' 次；闲着和失败也计入。 ' + (data.phone ? data.phone.reason : '手机连接状态未确认') + '。';
      if (data.usage) el('growth-state').textContent += '\n本生活日（' + data.usage.day + '）已用 ' +
        data.usage.used + ' 次，剩余 ' + data.usage.remaining + ' 次机会。剩余机会不代表一定执行。';
      el('growth-toggle').textContent = data.settings.enabled ? '关闭并恢复原有模式' : '开启自主成长';
      el('growth-toggle').disabled = false;
      if (limitsForm) {
        el('growth-interval').value = String(data.interval_minutes);
        el('growth-cap').value = String(data.daily_cap);
        el('growth-limits-save').disabled = false;
      }
      el('growth-current').textContent = profile(data.profile);
      el('growth-plans').textContent = data.intentions.length ? data.intentions.map(function (p) {
        return p.name + ' · ' + (states[p.status] || p.status) + (p.next_step ? ' · ' + p.next_step : '');
      }).join('\n') : '还没有自己的打算。不必为了填满列表而新建。';
      el('growth-log').textContent = data.log.length ? data.log.slice(-8).reverse().map(function (r) {
        return localTime(r.at) + ' · ' + (r.action || '选择机会') + ' · ' + (r.error_detail || r.reason || r.error || r.status || '已开始');
      }).join('\n') : '还没有新模式的活动记录。';
      if (el('growth-version')) {
        el('growth-version').replaceChildren();
        var versions = data.versions.filter(function (v) { return v.profile; });
        versions.slice().reverse().forEach(function (v) {
          var option = document.createElement('option'); option.value = String(v.version);
          option.textContent = '第 ' + v.version + ' 版 · ' + v.note; el('growth-version').appendChild(option);
        });
        var history = el('growth-versions');
        history.replaceChildren();
        if (!versions.length) history.textContent = '还没有结构化成长版本；旧文字自述仍保留在记忆记录中。';
        versions.slice().reverse().forEach(function (v, index) {
          var record = document.createElement('details'); record.className = 'growth-version-record';
          var title = document.createElement('summary');
          title.textContent = '第 ' + v.version + ' 版 · ' + localTime(v.ts) + ' · ' + v.note;
          record.appendChild(title);
          var previous = versions[versions.length - index - 2];
          var changed = Object.keys(labels).filter(function (key) {
            return previous ? previous.profile[key] !== v.profile[key] : v.profile[key] !== undefined;
          }).map(function (key) {
            var after = v.profile[key] === undefined ? '未设置（沿用初始或默认）' : (v.profile[key] || '已清空');
            if (!previous) return labels[key] + '：' + after;
            var before = previous.profile[key] === undefined ? '未设置（沿用初始或默认）' : (previous.profile[key] || '已清空');
            return labels[key] + '：' + before + ' → ' + after;
          });
          var changes = document.createElement('p'); changes.className = 'growth-version-changes';
          changes.textContent = !previous ? '首次成长记录\n' + (changed.join('\n') || '尚未设置性情字段。') :
            changed.length ? '本次变化\n' + changed.join('\n') : '本次没有性情字段变化。';
          record.appendChild(changes);
          var content = document.createElement('p');
          content.textContent = '这一版完整内容\n' + profile(v.profile) + '\n出处编号：' + ((v.sources || []).join('、') || '未引用具体经历');
          record.appendChild(content);
          if (v.model_turn_id) {
            var link = document.createElement('a');
            link.textContent = '查看当时的素材、回答与思考记录';
            link.href = window.PANEL_BASE + '/observe?source=model&turn_id=' + encodeURIComponent(v.model_turn_id);
            record.appendChild(link);
          } else {
            var note = document.createElement('p'); note.className = 'hint';
            note.textContent = '未关联模型记录。人工纠正、回退或缺少回执的旧版本不会借用其他回合。';
            record.appendChild(note);
          }
          history.appendChild(record);
        });
        el('growth-value').value = data.profile[el('growth-field').value] || (el('growth-field').value === 'sleep_notice' ? '睡觉啦，明天说' : '');
        el('growth-restore').disabled = !versions.length;
      }
    } catch (error) {
      current = null;
      if (limitsForm) el('growth-limits-save').disabled = true;
      if (el('solitude-card')) el('solitude-card').hidden = true;
      el('growth-state').textContent = '成长状态暂不可用：' + error.message;
    }
  }
  async function write(path, body) {
    try { await request(path, body); await refresh(); }
    catch (error) { el('growth-state').textContent = '没有保存：' + error.message; }
  }
  el('growth-refresh').addEventListener('click', refresh);
  if (limitsForm) limitsForm.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (!current || el('growth-limits-save').disabled) return;
    el('growth-limits-save').disabled = true;
    await write('/config', {enabled:current.settings.enabled,
      interval_minutes:Number(el('growth-interval').value), daily_cap:Number(el('growth-cap').value)});
    el('growth-limits-save').disabled = !current;
  });
  el('growth-toggle').addEventListener('click', async function () {
    if (!current) return;
    el('growth-toggle').disabled = true;
    await write('/config', {enabled: !current.settings.enabled});
    el('growth-toggle').disabled = !current;
  });
  if (el('growth-save')) {
    el('growth-field').addEventListener('change', function () {
      el('growth-value').value = current ? current.profile[this.value] || (this.value === 'sleep_notice' ? '睡觉啦，明天说' : '') : '';
    });
    el('growth-save').addEventListener('click', async function () {
      var patch = {}; patch[el('growth-field').value] = el('growth-value').value;
      await write('/profile', {patch:patch,note:el('growth-note').value});
    });
    el('growth-restore').addEventListener('click', async function () {
      await write('/profile', {version:Number(el('growth-version').value),note:el('growth-note').value});
    });
  }
  refresh();
}());
