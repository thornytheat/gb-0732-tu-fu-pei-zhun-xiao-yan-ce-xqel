import {drawResidual} from './plots.js';

export const labels = {
  PASS: '通过', FAIL: '超限', BLOCKED: '待核', FIT: '已拟合', READY: '可签', HOLD: '暂缓', RETURN: '退修',
  SIGNED: '已签', OUTLIER: '离群', ACCEPTED: '采信', RECEIVED: '已收',
  EMPTY_FILE: '空档', BLANK_FILE: '空行档', HEADER_ONLY: '仅表头',
  AGREE: '结论一致', CHANGED: '结论改变', UNVERIFIABLE: '待补原件',
  CURRENT: '现规则签审', STALE: '旧规签审·待补签', INVALIDATED: '旧规签审·失效', ABSENT: '未签审',
};
const reasons = JSON.parse(document.getElementById('reason-dictionary').textContent);
const number = new Intl.NumberFormat('zh-CN', {minimumFractionDigits: 3, maximumFractionDigits: 3, useGrouping: false});
export const fmt = value => value == null ? '—' : number.format(value);
export const reason = code => (code && (reasons[code] || code)) || '—';
const evidence = source => source ? `${source.file} #${source.line}${source.end_line > source.line ? `–${source.end_line}` : ''}` : '—';

export function el(tag, value, className) {
  const node = document.createElement(tag);
  if (value != null) node.textContent = value;
  if (className) node.className = className;
  return node;
}

export function badge(value) {
  const node = el('span', labels[value] || value, 'status');
  node.dataset.status = value;
  return node;
}

function table(columns, rows) {
  const frame = el('div', null, 'table-wrap');
  const ledger = el('table', null, 'ledger');
  const thead = ledger.createTHead().insertRow();
  for (const [label, className] of columns) {
    const th = el('th', label, className);
    th.scope = 'col';
    thead.append(th);
  }
  const body = ledger.createTBody();
  for (const record of rows) {
    const row = body.insertRow();
    for (const [column, content] of record.entries()) {
      const cell = row.insertCell();
      cell.className = columns[column][1] || '';
      if (content instanceof Node) cell.append(content);
      else cell.textContent = content ?? '—';
      if (cell.classList.contains('raw')) cell.title = cell.textContent;
    }
  }
  frame.append(ledger);
  return rows.length ? frame : el('p', '当前没有记录。');
}

function quarantined(rows) {
  return table([['图幅 / 拼缝'], ['点名'], ['原因码'], ['判定'], ['源行'], ['原始字段', 'raw']], rows.map(row => [
    row.sheet || row.edge, row.name, row.reason, reason(row.reason), evidence(row.source), row.source.raw.join(' | '),
  ]));
}

function group(root, title, content) {
  root.append(el('h3', title), content);
}

function intake(root, tables) {
  const files = Object.values(tables);
  root.append(table([['原件'], ['编码'], ['状态'], ['逻辑记录', 'num'], ['空行', 'num'], ['重复表头', 'num']], files.map(file => [
    file.file, file.encoding, badge(file.state), file.records, file.blank, file.headers,
  ])));
  group(root, '原始记录', table([['来源'], ['原始字段', 'raw']], files.flatMap(file => file.rows.map(row => [
    evidence(row.source), row.source.raw.join(' | '),
  ]))));
  group(root, '结构隔离', quarantined(files.flatMap(file => file.quarantine)));
}

function coordinates(root, data) {
  const converted = [...data.pixels.map(row => ({...row, unit: 'px'})), ...data.ground.map(row => ({...row, unit: 'm'}))];
  root.append(table([['图幅'], ['点名'], ['横坐标', 'num'], ['纵坐标', 'num'], ['单位'], ['源行'], ['原值', 'raw']],
    converted.map(row => [row.sheet, row.name, ...row.xy.map(fmt), row.unit, evidence(row.source), row.source.raw.join(' | ')])));
  group(root, '换算隔离', quarantined(data.quarantine));
}

function pairing(root, data) {
  root.append(table([['图幅'], ['归并点名'], ['像素 u', 'num'], ['像素 v', 'num'], ['东 m', 'num'], ['北 m', 'num'],
    ['像素源行'], ['地面源行']], data.pairs.map(row => [row.sheet, row.name, ...row.pixel.map(fmt),
    ...row.ground.map(fmt), evidence(row.pixel_source), evidence(row.ground_source)])));
  group(root, '配对隔离', quarantined(data.quarantine));
}

function affine(root, sheets, pairs) {
  root.append(table([['图幅'], ['拟合'], ['点数', 'num'], ['均方根 m', 'num'], ['最大 m', 'num'], ['条件数', 'num'],
    ['系数 E [u, v, 1]'], ['系数 N [u, v, 1]'], ['原因']], Object.entries(sheets).map(([code, fit]) => [
    code, badge(fit.status), fit.count, fmt(fit.rms_m), fmt(fit.max_m), fmt(fit.model?.condition),
    fit.model?.east.map(x => x.toFixed(9)).join(' / '), fit.model?.north.map(x => x.toFixed(9)).join(' / '), reason(fit.reason),
  ])));
  const lines = new Map(pairs.map(pair => [pair.id, pair.pixel_source]));
  group(root, '初算残差', table([['图幅'], ['点名'], ['ΔE m', 'num'], ['ΔN m', 'num'], ['距离 m', 'num'], ['像素源行']],
    Object.entries(sheets).flatMap(([code, fit]) => (fit.residuals || []).map(row => [
      code, row.name, fmt(row.dx_m), fmt(row.dy_m), fmt(row.distance_m), lines.has(row.id) ? evidence(lines.get(row.id)) : row.id,
    ]))));
}

function refit(root, data, selected, limit) {
  const rows = [...data.accepted, ...data.outliers].filter(row => !selected || row.sheet === selected)
    .sort((a, b) => a.sheet.localeCompare(b.sheet) || a.name.localeCompare(b.name));
  if (rows.length) {
    const wrap = el('div', null, 'residual-wrap');
    const legend = el('p', null, 'plot-legend');
    legend.append(el('span', '灰线 初算'), el('span', '绿线 采信'), el('span', '红线 离群'));
    const canvas = el('canvas');
    canvas.id = 'residual-plot';
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', '各控制点的初算与重估残差对照');
    wrap.append(legend, canvas);
    root.append(wrap);
    drawResidual(canvas, rows, limit);
  }
  root.append(table([['图幅'], ['控制点'], ['初算 m', 'num'], ['重估 m', 'num'], ['判定'], ['原因码'], ['像素源行'], ['地面源行']],
    rows.map(row => [row.sheet, row.name, fmt(row.before?.distance_m), fmt(row.after.distance_m),
      badge(row.reason ? 'OUTLIER' : 'ACCEPTED'), row.reason, evidence(row.pixel_source), evidence(row.ground_source)])));
  group(root, '控制点隔离', quarantined(data.quarantine.filter(row => !selected || row.sheet === selected)));
}

function seams(root, data, activeEdge) {
  root.append(table([['拼缝'], ['相邻图幅'], ['状态'], ['见证点', 'num'], ['覆盖率 A / B'], ['最大 m', 'num'], ['中位 m', 'num'], ['原因']],
    data.edges.map(edge => [edge.code, `${edge.a} ${edge.a_side} · ${edge.b} ${edge.b_side}`, badge(edge.status), edge.count,
      edge.coverage.map(n => `${(n * 100).toFixed(1)}%`).join(' / '), fmt(edge.max_m), fmt(edge.median_m),
      edge.reasons.length ? edge.reasons.map(reason).join(' / ') : '—'])));
  const active = data.edges.find(edge => edge.code === activeEdge) || data.edges[0];
  if (active) {
    group(root, `${active.code} 见证点`, table([['证点'], ['甲侧像素'], ['乙侧像素'], ['法向 m', 'num'], ['切向 m', 'num'],
      ['平面偏差 m', 'num'], ['源行']], active.stations.map(row => [row.name, row.a_pixel.join(' / '), row.b_pixel.join(' / '),
      fmt(row.normal_m), fmt(row.tangent_m), fmt(row.gap_m), evidence(row.source)])));
  }
  group(root, '见证行隔离', quarantined(data.quarantine));
}

function review(root, snapshot) {
  const signed = new Map(snapshot.signatures.map(item => [item.sheet, item]));
  const totals = snapshot.ledger;
  const width = [totals.controls, totals.ground, totals.witnesses];
  const sums = [
    `${width[0].accepted} 采信 + ${width[0].outlier} 离群 + ${width[0].isolated} 隔离`,
    `${width[1].paired} 已配对 + ${width[1].isolated} 隔离`,
    `${width[2].eligible} 可用 + ${width[2].isolated} 隔离`,
  ];
  root.append(table([['图幅'], ['校验结论'], ['拟合均方根 m', 'num'], ['关联拼缝'], ['理由'], ['签审员'], ['签审时间']],
    snapshot.reviews.map(row => {
      const signature = signed.get(row.code);
      return [`${row.code} ${row.title}`, badge(signature ? 'SIGNED' : row.status), fmt(row.fit_rms_m),
        row.edges.join(' / '), reason(row.reason), signature?.reviewer, signature?.signed_at];
    })));
  group(root, '原件对账', table([['档件'], ['原件记录', 'num'], ['归属'], ['核对']],
    width.map((entry, index) => [['控制点', '地面点', '拼缝见证'][index], entry.input, sums[index], badge('PASS')])));
}

function migration(root, data) {
  if (!data) {
    root.append(el('p', '选择左栏「规则迁移复核」后生成复核册。'));
    return;
  }
  const summary = data.summary;
  root.append(table([['旧批次'], ['源规则'], ['现行规则'], ['复算可重验'], ['原件状态'], ['生成时间']], [[
    `${data.source.kind === 'archive' ? '归档' : '批次'} ${data.source.id.slice(0, 12)}`,
    data.source.rule_version, data.rule_version,
    String(data.sheets.filter(row => row.recomputed).length) + ' / ' + data.sheets.length,
    data.source_state === 'MATCHED' ? '摘要相符' : data.source_state === 'CHANGED' ? '摘要不符' : '档件缺失',
    data.created_at,
  ]]));
  root.append(el('p', data.note, 'caption'));
  root.append(table([['图幅'], ['旧结论'], ['新口径复算'], ['对照'], ['原签审'], ['签审状态'], ['待补 / 待办']],
    data.sheets.map(row => [
      `${row.code} ${row.title}`,
      row.legacy ? `${labels[row.legacy.status] || row.legacy.status}${row.legacy.reason ? ' · ' + reason(row.legacy.reason) : ''}` : '—',
      row.recomputed ? `${labels[row.recomputed.status] || row.recomputed.status}${row.recomputed.reason ? ' · ' + reason(row.recomputed.reason) : ''}` : '无法复算',
      badge(row.verdict),
      row.signature ? `${row.signature.reviewer} · ${row.signature.signed_at}` : '—',
      badge(row.signature_state),
      row.pending.map(item => `${item.label}${item.detail && item.detail.missing ? `（缺 ${item.detail.missing.join('、')}）` : ''}`).join('；') || '—',
    ])));
  for (const row of data.sheets) {
    group(root, `${row.code} ${row.title} · 可重验依据`, table([['依据'], ['来源'], ['内容']],
      row.basis.map(item => [item.label, item.origin === 'archived' ? '旧证据' : item.origin === 'recomputed' ? '新复算' : '原件',
        basisText(item)])));
    group(root, `${row.code} ${row.title} · 待补 / 待办`, table([['原因'], ['说明'], ['细节']],
      row.pending.map(item => [item.label, item.code,
        item.detail ? Object.entries(item.detail).map(([key, value]) => `${key}=${Array.isArray(value) ? value.join('、') : value}`).join('；') : '—'])));
  }
}

function basisText(item) {
  if (item.kind === 'input') return `${item.file} · ${item.sha256 ? item.sha256.slice(0, 16) : '—'} · ${item.match ? '相符' : '不符'}`;
  if (item.kind === 'controls') return `${item.accepted} 采信 / ${item.outlier} 离群 / ${item.isolated} 隔离 · ${item.accepted_sources.slice(0, 4).join('、')}${item.accepted_sources.length > 4 ? ' …' : ''}`;
  if (item.kind === 'model') return `${item.status} · ${item.count} 点 · 均方根 ${fmt(item.rms_m)} m · 条件数 ${fmt(item.condition)}`;
  if (item.kind === 'seam') return `${item.edge} · ${item.status} · ${item.count} 站 · 最大 ${fmt(item.max_m)} m · 限 ${fmt(item.limit_m)} m`;
  if (item.kind === 'signature') return `${item.reviewer} · ${item.signed_at} · 版本 ${item.version} · 规则 ${item.rule_version}`;
  if (item.kind === 'ledger') return item.balanced ? '三表行号多重集对账通过' : '对账未通过';
  return '—';
}

export function renderLedger(root, section, {snapshot, preview, selected, edgeId, limit, recheck}) {
  root.replaceChildren();
  if (section === 'migration') {
    migration(root, recheck);
    return;
  }
  const value = preview || (section === 'review' ? snapshot : snapshot?.[section]);
  if (!value) {
    root.append(el('p', '运行校验后查看。'));
    return;
  }
  const renderers = {intake, coordinates, pairing, affine, refit, seams, review};
  if (section === 'intake') intake(root, preview ? value.tables : value);
  else if (section === 'affine') affine(root, preview ? value.sheets : value, snapshot?.pairing.pairs || []);
  else if (section === 'refit') refit(root, value, selected, limit);
  else if (section === 'seams') seams(root, value, edgeId);
  else renderers[section](root, value);
}
