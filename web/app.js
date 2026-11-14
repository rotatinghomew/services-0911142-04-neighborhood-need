const state = {context: null, current: null, estimate: null};
const element = identifier => document.getElementById(identifier);

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败');
  return value;
}

function message(value, error = false) {
  element('status').textContent = value;
  element('status').className = error ? 'error' : '';
}

function estimateMessage(value, error = false) {
  element('estimate-status').textContent = value;
  element('estimate-status').className = error ? 'error' : 'hint';
}

async function guarded(action) {
  try { await action(); } catch (error) { message(error.message, true); }
}

function selected() {
  const selection = {};
  for (const control of state.context.product.options) selection[control[0]] = element('select-' + control[0]).value;
  return {revision: element('revision').value, selection};
}

function renderControls() {
  const revision = element('revision').value || state.context.default_revision;
  element('revision').replaceChildren();
  for (const dataset of state.context.datasets) {
    const option = document.createElement('option');
    option.value = dataset.revision;
    option.textContent = dataset.label + ' · ' + dataset.revision.slice(0, 9);
    element('revision').append(option);
  }
  element('revision').value = revision;
  if (!element('revision').value) element('revision').value = state.context.default_revision;
  if (element('controls').children.length) return;
  for (const [key, labelText, choices] of state.context.product.options) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const select = document.createElement('select');
    select.id = 'select-' + key;
    for (const choice of choices) {
      const option = document.createElement('option');
      option.value = choice; option.textContent = choice;
      select.append(option);
    }
    select.value = state.context.product.selection[key];
    label.append(select);
    element('controls').append(label);
  }
}

function displayValue(value) {
  if (value === null || value === undefined) return '未观测';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function drawChart(series) {
  const canvas = element('chart');
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  const values = series.filter(point => typeof point.y === 'number' && Number.isFinite(point.y));
  context.font = '12px sans-serif';
  if (!values.length) { context.fillText('该范围没有可定量的描述值', 30, 50); return; }
  const low = Math.min(0, ...values.map(point => point.y));
  const high = Math.max(1, ...values.map(point => point.y));
  const scale = value => 250 - (value - low) / (high - low || 1) * 210;
  context.strokeStyle = '#90a5ad';
  context.beginPath(); context.moveTo(45, 20); context.lineTo(45, 250); context.lineTo(940, 250); context.stroke();
  context.fillStyle = '#0a7168';
  const scatter = values.every(point => typeof point.x === 'number');
  const minimumX = scatter ? Math.min(...values.map(point => point.x)) : 0;
  const maximumX = scatter ? Math.max(...values.map(point => point.x)) : 1;
  values.forEach((point, index) => {
    const position = scatter ? 60 + (point.x - minimumX) / (maximumX - minimumX || 1) * 850 : 55 + index * 875 / values.length;
    if (scatter) {
      context.beginPath(); context.arc(position, scale(point.y), 3.5, 0, Math.PI * 2); context.fill();
    } else {
      context.fillRect(position, Math.min(scale(point.y), scale(0)), Math.max(3, 800 / values.length), Math.max(1, Math.abs(scale(point.y) - scale(0))));
    }
    if (values.length < 12) context.fillText(point.label, position, 275);
  });
  context.fillStyle = '#29434e';
  context.fillText(high.toFixed(2), 0, 45); context.fillText(low.toFixed(2), 0, 250);
  context.fillText(scatter ? '所选资料的数值坐标（仅为原始描述）' : '按表中顺序展示描述值；缺测不绘制为零', 50, 20);
}

function render(result, heading) {
  // 实时接口返回 {view: 描述}；保存记录读取返回 {result: {view: 描述}}
  if (result.result && result.result.view) result = result.result;
  state.current = result;
  element('summary').replaceChildren();
  for (const [label, value] of Object.entries(result.view.summary)) {
    const card = document.createElement('article');
    const title = document.createElement('span'); title.textContent = label;
    const amount = document.createElement('strong'); amount.textContent = displayValue(value);
    card.append(title, amount); element('summary').append(card);
  }
  element('notes').replaceChildren();
  for (const note of result.view.notes) {
    const paragraph = document.createElement('p'); paragraph.textContent = note; element('notes').append(paragraph);
  }
  const rows = result.view.rows;
  const fields = rows.length ? Object.keys(rows[0]) : [];
  const header = document.createElement('tr');
  for (const field of fields) { const cell = document.createElement('th'); cell.textContent = field; header.append(cell); }
  element('columns').replaceChildren(header); element('rows').replaceChildren();
  for (const row of rows) {
    const line = document.createElement('tr'); line.tabIndex = 0;
    for (const field of fields) { const cell = document.createElement('td'); cell.textContent = displayValue(row[field]); line.append(cell); }
    line.onclick = () => { element('details').textContent = JSON.stringify(row, null, 2); };
    line.onkeydown = event => { if (event.key === 'Enter') line.click(); };
    element('rows').append(line);
  }
  drawChart(result.view.series);
  element('details').textContent = JSON.stringify({revision: result.revision, selection: result.selection,
    history: result.view.history || null}, null, 2);
  message((heading || '已更新描述') + ' · 数据 ' + result.revision.slice(0, 12));
}

const fmt = (value, digits = 1) => (value === null || value === undefined) ? '未观测'
  : Number.isFinite(value) ? Number(value).toFixed(digits) : '未观测';
const pct = (value, digits = 1) => (value === null || value === undefined) ? '未观测' : (value * 100).toFixed(digits) + '%';

function readScenario() {
  const scenario = {
    population_multiplier: parseFloat(element('sc-multiplier').value) || 1,
    unknown_policy: element('sc-policy').value,
    monte_carlo_draws: parseInt(element('sc-draws').value, 10),
    random_seed: parseInt(element('sc-seed').value, 10),
    loco: element('sc-loco').checked,
  };
  const positionText = element('sc-position').value.trim();
  scenario.crosswalk_position = positionText === '' ? null : parseFloat(positionText);
  if (scenario.unknown_policy === 'manual') {
    try {
      const parsed = JSON.parse(element('sc-manual').value || '{}');
      if (typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('应为对象');
      scenario.unknown_counts = parsed;
    } catch (error) {
      throw new Error('手工缺口人口不是合法 JSON 对象：' + error.message);
    }
  }
  return scenario;
}

function renderEstimate(result) {
  // 实时接口返回 {view: 估计结果}；保存记录读取返回 {result: {view: 估计结果}}
  if (result.result && result.result.view) result = result.result;
  state.estimate = result;
  const view = result.view;
  element('estimate-cards').replaceChildren();
  const cards = [
    ['模型需求总数', fmt(view.total.need_count) + ' 人'],
    ['总数95%区间', fmt(view.total.q025) + ' ~ ' + fmt(view.total.q975)],
    ['情景总人口', fmt(view.total.population) + ' 人'],
    ['无样本社区', view.model.areas_without_sample.join('、') || '无'],
    ['随机效应SD', fmt(view.model.random_effect_sd, 3)],
    ['蒙特卡洛次数', String(view.meta.monte_carlo_draws)],
  ];
  for (const [label, value] of cards) {
    const card = document.createElement('article');
    const title = document.createElement('span'); title.textContent = label;
    const amount = document.createElement('strong'); amount.textContent = value;
    card.append(title, amount); element('estimate-cards').append(card);
  }
  element('estimate-note').textContent =
    '模型列为后分层需求人数与95%区间；直接列为旧边界问卷直接加权率（新边界按流向混合），仅作并排对照。'
    + ' 可靠程度规则：' + Object.entries(view.meta.reliability_rules).map(([k, v]) => k + '=' + v).join('；')
    + ' 借用占比=随机截距中先验信息的权重，1.0 表示完全依赖借用。' + view.total.basis;

  const head = ['社区', '情景人口', '模型需求人数', '95%区间', '模型需求率', '直接估计率', '直接估计人数',
    '借用占比', '可靠程度', '证据性质'];
  const tr = document.createElement('tr');
  for (const name of head) { const th = document.createElement('th'); th.textContent = name; tr.append(th); }
  element('estimate-head').replaceChildren(tr);
  element('estimate-rows').replaceChildren();
  for (const row of view.areas) {
    const line = document.createElement('tr');
    line.tabIndex = 0;
    if (!row.model.has_direct_sample) line.classList.add('no-sample');
    const values = [
      row.area, fmt(row.scenario_population), fmt(row.model.need_count),
      fmt(row.model.q025) + ' ~ ' + fmt(row.model.q975), pct(row.model.rate, 2),
      row.direct ? pct(row.direct.rate, 2) : '无本地样本',
      row.direct ? fmt(row.direct.count) : '不适用',
      pct(row.model.borrowed_share, 1), row.model.reliability, row.model.evidence,
    ];
    for (const value of values) { const td = document.createElement('td'); td.textContent = value; line.append(td); }
    line.onclick = () => { element('details').textContent = JSON.stringify(row, null, 2); };
    line.onkeydown = event => { if (event.key === 'Enter') line.click(); };
    element('estimate-rows').append(line);
  }

  const dependency = element('dependency-view');
  dependency.replaceChildren();
  const impact = view.crosswalk_impact;
  const impactTitle = document.createElement('p');
  impactTitle.textContent = impact.uncertain
    ? '存在不确定流向。' + impact.note
    : impact.note;
  dependency.append(impactTitle);
  if (impact.uncertain && impact.affected_areas.length) {
    const list = document.createElement('ul');
    for (const item of impact.affected_areas) {
      const li = document.createElement('li');
      li.textContent = item.area + '：模型人数角点范围 ' + fmt(item.model_count_low) + '~' + fmt(item.model_count_high)
        + (item.model_count_zero_span ? '（本数据下为0跨度）' : '')
        + (item.direct_rate_low !== null ? '；直接率角点范围 ' + pct(item.direct_rate_low, 2) + '~' + pct(item.direct_rate_high, 2) : '')
        + '；涉及映射 ' + item.mappings.join('、');
      list.append(li);
    }
    dependency.append(list);
  }
  const depTitle = document.createElement('p');
  depTitle.textContent = '各社区信息来源（流向中点、来源社区样本量与借用占比）：';
  dependency.append(depTitle);
  const depList = document.createElement('ul');
  for (const row of view.areas) {
    const li = document.createElement('li');
    li.textContent = row.area + ' ← ' + row.dependency.map(d =>
      d.old_area + '(流向' + pct(d.flow_midpoint, 1) + ',样本n=' + d.sample_n + ',借用' + pct(d.borrowed_share, 1) + ')').join(' + ')
      + (row.crosswalk_uncertain_mappings.length ? '；不确定映射：' + row.crosswalk_uncertain_mappings.join('、') : '');
    depList.append(li);
  }
  dependency.append(depList);

  const loco = element('loco-view');
  loco.replaceChildren();
  if (view.loco) {
    const intro = document.createElement('p');
    intro.textContent = view.loco.held_out_kind + '。' + view.loco.note;
    loco.append(intro);
    const metricLine = document.createElement('ul');
    for (const [name, metrics] of Object.entries(view.loco.metrics)) {
      const li = document.createElement('li');
      li.textContent = name + '：bias=' + fmt(metrics.bias, 4) + '，MAE=' + fmt(metrics.mae, 4)
        + '，RMSE=' + fmt(metrics.rmse, 4)
        + ('interval_coverage' in metrics ? '，95%区间覆盖率=' + pct(metrics.interval_coverage, 1) : '');
      metricLine.append(li);
    }
    loco.append(metricLine);
    const table = document.createElement('table');
    table.className = 'inner-table';
    const lhead = document.createElement('tr');
    for (const name of ['留出社区', '样本n', '直接率', 'MRP预测率', '95%区间', '覆盖', '调整后分层率', '街道均值率']) {
      const th = document.createElement('th'); th.textContent = name; lhead.append(th);
    }
    table.append(lhead);
    for (const row of view.loco.areas) {
      const trr = document.createElement('tr');
      for (const value of [row.area, row.direct_n, pct(row.direct_rate, 2), pct(row.model_rate_mean, 2),
        pct(row.model_rate_q025, 2) + '~' + pct(row.model_rate_q975, 2), row.covered ? '是' : '否',
        pct(row.adjusted_rate_mean, 2), pct(row.benchmark_rate, 2)]) {
        const td = document.createElement('td'); td.textContent = value; trr.append(td);
      }
      table.append(trr);
    }
    loco.append(table);
  } else {
    loco.textContent = '本次计算已关闭 LOCO 检验。';
  }

  const marginal = element('marginal-view');
  marginal.replaceChildren();
  for (const [dim, rows] of Object.entries(view.marginals)) {
    const box = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = {age: '年龄', living: '家庭结构', mobility: '行动便利程度'}[dim] || dim;
    box.append(title);
    const ul = document.createElement('ul');
    for (const row of rows) {
      const li = document.createElement('li');
      li.textContent = row[dim] + '：人口 ' + fmt(row.population) + '，需求 ' + fmt(row.need_count)
        + ' 人（95%区间 ' + fmt(row.q025) + '~' + fmt(row.q975) + '）';
      ul.append(li);
    }
    box.append(ul);
    marginal.append(box);
  }

  const cellHead = document.createElement('tr');
  for (const name of ['单元', '状态', '处理方式', '情景人口', '模型率', '需求人数', '95%区间']) {
    const th = document.createElement('th'); th.textContent = name; cellHead.append(th);
  }
  element('cell-head').replaceChildren(cellHead);
  element('cell-rows').replaceChildren();
  for (const row of view.cells) {
    const trr = document.createElement('tr');
    if (row.status === 'unknown') trr.classList.add('unknown-cell');
    if (row.status === 'impossible') trr.classList.add('impossible-cell');
    for (const value of [row.cell, row.status, row.treatment, fmt(row.scenario_count),
      pct(row.model_rate_mean, 2), fmt(row.need_count_mean), fmt(row.need_q025) + '~' + fmt(row.need_q975)]) {
      const td = document.createElement('td'); td.textContent = value; trr.append(td);
    }
    trr.onclick = () => { element('details').textContent = JSON.stringify(row, null, 2); };
    element('cell-rows').append(trr);
  }
  element('details').textContent = JSON.stringify({
    revision: result.revision, selection: result.selection, scenario: result.scenario,
    model: view.model, audit: view.audit,
  }, null, 2);
  estimateMessage('已计算小域需求估计 · 数据 ' + result.revision.slice(0, 12)
    + ' · 指纹 ' + view.audit.data_sha256.slice(0, 10));
}

async function refresh() {
  state.context = await request('/api/context');
  renderControls();
  element('archive').replaceChildren();
  for (const notebook of state.context.notebooks) {
    const button = document.createElement('button'); button.textContent = notebook.title + ' #' + notebook.id;
    button.onclick = () => guarded(async () => {
      const saved = await request('/api/notebooks/' + notebook.id);
      element('revision').value = saved.revision;
      for (const [key, value] of Object.entries(saved.selection)) {
        const control = element('select-' + key); if (control) control.value = value;
      }
      if (saved.result && saved.result.view && saved.result.view.kind === 'small_area_need_estimate') {
        switchTab('estimate');
        renderEstimate(saved);
        estimateMessage('已读取保存的估计，不随新数据变化');
      } else {
        switchTab('browse');
        render(saved, '已读取保存的描述，不随新数据变化');
      }
    });
    element('archive').append(button);
  }
}

function switchTab(which) {
  element('browse-panel').hidden = which !== 'browse';
  element('estimate-panel').hidden = which !== 'estimate';
  element('tab-browse').classList.toggle('active', which === 'browse');
  element('tab-estimate').classList.toggle('active', which === 'estimate');
}

element('tab-browse').onclick = () => switchTab('browse');
element('tab-estimate').onclick = () => switchTab('estimate');
element('sc-policy').onchange = () => {
  element('sc-manual-wrap').hidden = element('sc-policy').value !== 'manual';
};
element('run-estimate').onclick = () => guarded(async () => {
  estimateMessage('正在拟合模型并做蒙特卡洛后分层，请稍候…');
  const body = {...selected(), scenario: readScenario()};
  const result = await request('/api/estimate', body);
  renderEstimate(result);
});
element('inspect').onclick = () => guarded(async () => render(await request('/api/explore', selected())));
element('save').onclick = () => guarded(async () => {
  const saved = await request('/api/notebooks', {...selected(), title: element('title').value});
  await refresh(); render(saved, '已保存记录 #' + saved.id);
});
element('save-estimate').onclick = () => guarded(async () => {
  if (!state.estimate) throw new Error('请先计算一次小域需求估计');
  const body = {...selected(), kind: 'estimate', title: element('title').value || '小域需求估计记录',
    scenario: state.estimate.scenario || readScenario()};
  const saved = await request('/api/notebooks', body);
  await refresh();
  renderEstimate(saved);
  estimateMessage('已保存估计记录 #' + saved.id);
});
element('raw').onclick = () => guarded(async () => {
  const revision = state.current ? state.current.revision : element('revision').value;
  element('details').textContent = JSON.stringify(await request('/api/data?revision=' + encodeURIComponent(revision)), null, 2);
});
element('upload').onchange = () => guarded(async () => {
  const file = element('upload').files[0]; if (!file) return;
  const data = JSON.parse(await file.text());
  const uploaded = await request('/api/datasets', {label: file.name, data});
  await refresh(); element('revision').value = uploaded.revision;
  render(await request('/api/explore', selected()), '已导入独立数据版本');
});
element('export').onclick = () => guarded(async () => {
  if (!state.current) throw new Error('请先查看一个范围');
  const data = await request('/api/data?revision=' + encodeURIComponent(state.current.revision));
  const blob = new Blob([JSON.stringify({description: state.current, data}, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = 'analysis-' + state.current.revision.slice(0, 12) + '.json'; link.click();
  URL.revokeObjectURL(url);
});
guarded(async () => { await refresh(); render(await request('/api/explore', selected())); });
