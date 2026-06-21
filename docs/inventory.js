/* ===== 庫存與持股比例（唯讀顯示，資料來自 docs/inventory.json）===== */
(function () {
  const PALETTE = ['#4d7fff', '#1ed992', '#e0a83c', '#ff525b', '#9d7bff', '#5fd3ff',
    '#ff8a6b', '#3ddca0', '#f2c14e', '#7f9fff', '#ff6fa5', '#46c2c2', '#c9a227', '#8b95a5'];

  let pieChart = null;
  const $ = (id) => document.getElementById(id);

  function fmt(n, d) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toLocaleString('en-US', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function signCls(n) { return (n === null || n === undefined || isNaN(n)) ? '' : (n >= 0 ? 'up' : 'down'); }
  function signStr(n, d) { if (n === null || n === undefined || isNaN(n)) return '—'; return (n >= 0 ? '+' : '') + fmt(n, d); }

  async function loadJson() {
    try {
      const r = await fetch('inventory.json?t=' + Date.now());
      if (!r.ok) return null;
      return await r.json();
    } catch (e) { return null; }
  }

  function metric(cls, label, value, sub) {
    return `<div class="metric ${cls}"><div class="label">${label}</div>
      <div class="value inv-num ${cls === 'up' || cls === 'down' ? cls : ''}">${value}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
  }

  function render(data) {
    const holdings = (data && Array.isArray(data.holdings)) ? data.holdings.slice() : [];
    $('invUpdated').textContent = (data && data.updated_at) ? ('更新於 ' + data.updated_at) : '';

    if (!holdings.length) {
      $('invSummary').innerHTML = '';
      $('invTableWrap').innerHTML = '<div class="inv-empty">尚無庫存資料。</div>';
      if (pieChart) pieChart.clear();
    } else {
      holdings.sort((a, b) => (b.market_value || 0) - (a.market_value || 0));

      const totMv = data.total_market_value != null ? data.total_market_value : holdings.reduce((s, h) => s + (h.market_value || 0), 0);
      const totCost = data.total_cost != null ? data.total_cost : holdings.reduce((s, h) => s + ((h.avg_cost && h.shares) ? h.avg_cost * h.shares : 0), 0);
      const totPnl = data.total_pnl != null ? data.total_pnl : holdings.reduce((s, h) => s + (h.pnl || 0), 0);
      const totPct = (data.total_pnl_pct != null) ? data.total_pnl_pct : (totCost ? totPnl / totCost * 100 : null);

      $('invSummary').innerHTML =
        metric('accent', '總市值 (' + (data.currency || 'TWD') + ')', fmt(totMv, 0), '持股 ' + holdings.length + ' 檔') +
        metric('', '總成本', fmt(totCost, 0), '') +
        metric(signCls(totPnl), '未實現損益', signStr(totPnl, 0), '') +
        metric(signCls(totPnl), '報酬率', (totPct == null ? '—' : signStr(totPct, 2) + '%'), '');

      const pieData = holdings.map((h, i) => ({
        name: (h.stock_id ? h.stock_id + ' ' : '') + (h.name || ''),
        value: +(h.market_value || 0).toFixed(0),
        itemStyle: { color: PALETTE[i % PALETTE.length] }
      }));
      if (!pieChart) pieChart = echarts.init($('invPie'));
      pieChart.setOption({
        tooltip: { trigger: 'item', formatter: (p) => `${p.name}<br/>市值 ${fmt(p.value, 0)}<br/>占比 <b>${p.percent}%</b>` },
        series: [{
          type: 'pie', radius: ['46%', '74%'], center: ['50%', '50%'], avoidLabelOverlap: true,
          itemStyle: { borderColor: '#0a0e15', borderWidth: 2 },
          label: { color: '#8b95a5', fontSize: 11, formatter: '{b}\n{d}%' },
          labelLine: { lineStyle: { color: '#2b3645' } },
          data: pieData
        }]
      }, true);
      pieChart.resize();

      let rows = '';
      holdings.forEach((h, i) => {
        const color = PALETTE[i % PALETTE.length];
        const weight = (totMv && h.market_value) ? (h.market_value / totMv * 100) : (h.weight || 0);
        const pnlPct = (h.pnl_pct != null) ? h.pnl_pct : ((h.avg_cost && h.shares && h.pnl != null) ? h.pnl / (h.avg_cost * h.shares) * 100 : null);
        rows +=
          `<tr>
            <td class="inv-code">${h.stock_id || '—'}</td>
            <td class="inv-name"><span class="inv-dot" style="background:${color}"></span>${h.name || '—'}</td>
            <td class="inv-num">${fmt(h.shares, 0)}</td>
            <td class="inv-num">${fmt(h.avg_cost, 2)}</td>
            <td class="inv-num">${fmt(h.price, 2)}</td>
            <td class="inv-num">${fmt(h.market_value, 0)}</td>
            <td class="inv-num ${signCls(h.pnl)}">${signStr(h.pnl, 0)}</td>
            <td class="inv-num ${signCls(pnlPct)}">${pnlPct == null ? '—' : signStr(pnlPct, 2) + '%'}</td>
            <td><div class="inv-wbar"><span class="inv-num">${weight.toFixed(1)}%</span>
              <div class="inv-wbar-track"><div class="inv-wbar-fill" style="width:${Math.min(weight, 100)}%;background:${color}"></div></div></div></td>
          </tr>`;
      });
      $('invTableWrap').innerHTML =
        `<table class="inv-table"><thead><tr>
          <th>股號</th><th>股名</th><th>股數</th><th>成本均價</th><th>現價</th><th>市值</th><th>未實現損益</th><th>報酬率</th><th>占比</th>
        </tr></thead><tbody>${rows}</tbody></table>`;
    }

    renderFutures(data);
  }

  function futRiskLevel(equity, initial, maint) {
    if (equity == null) return { color: '#8b95a5', label: '待輸入權益數' };
    if (initial && equity >= initial) return { color: '#1ed992', label: '充足' };
    if (maint && equity >= maint) return { color: '#e0a83c', label: '注意（低於原始保證金）' };
    if (maint && equity >= maint * 0.25) return { color: '#ff8a4d', label: '追繳（低於維持保證金）' };
    return { color: '#ff525b', label: '強制平倉風險' };
  }

  function renderFutures(data) {
    const card = $('invFuturesCard');
    const futures = (data && Array.isArray(data.futures)) ? data.futures : [];
    if (!futures.length) { card.style.display = 'none'; return; }
    card.style.display = '';

    const acc = (data && data.futures_account) ? data.futures_account : {};
    const rInit = (acc.margin_rate_initial != null) ? acc.margin_rate_initial : 0.135;
    const rMaint = (acc.margin_rate_maintenance != null) ? acc.margin_rate_maintenance : 0.1035;

    let totNotional = 0, totEstInit = 0, totEstMaint = 0, totPnl = 0;
    const enriched = futures.map((f) => {
      const mult = f.multiplier || 100;
      const notional = (f.price || 0) * mult * (f.lots || 0);
      totNotional += notional;
      totEstInit += notional * rInit;
      totEstMaint += notional * rMaint;
      totPnl += (f.pnl || 0);
      return { f, notional };
    });

    const totFutPnl = (data.total_futures_pnl != null) ? data.total_futures_pnl : totPnl;
    $('invFutPnl').innerHTML = '未平倉損益合計 <span class="inv-num ' + signCls(totFutPnl) + '">' + signStr(totFutPnl, 0) + '</span>';

    // ---- 風險控管面板 ----
    const isEstInit = (acc.initial_margin == null);
    const isEstMaint = (acc.maintenance_margin == null);
    const initMargin = isEstInit ? totEstInit : acc.initial_margin;
    const maintMargin = isEstMaint ? totEstMaint : acc.maintenance_margin;
    const equity = (acc.equity != null) ? acc.equity : null;
    const ri = (acc.risk_indicator != null) ? acc.risk_indicator
      : (equity != null && maintMargin) ? (equity / maintMargin * 100) : null;
    const riBasis = (acc.risk_indicator != null) ? '券商提供' : '權益數 ÷ 維持保證金（估）';
    const available = (acc.available_margin != null) ? acc.available_margin : (equity != null ? equity - initMargin : null);
    const surplus = (acc.surplus_margin != null) ? acc.surplus_margin : (equity != null ? equity - initMargin : null);
    const lv = futRiskLevel(equity, initMargin, maintMargin);

    const rk = (label, value, sub, vcolor) =>
      `<div class="rk"><div class="label">${label}</div>
        <div class="value inv-num"${vcolor ? ` style="color:${vcolor}"` : ''}>${value}</div>
        ${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;

    let panel =
      `<div class="rk span2"><div class="label">風險指標（${riBasis}）</div>
        <div class="value inv-num" style="color:${lv.color}">${ri == null ? '—' : Math.round(ri) + '%'}
          <span class="rk-badge" style="background:${lv.color}">${lv.label}</span></div>
        <div class="rk-bar"><div class="rk-bar-fill" style="width:${ri == null ? 0 : Math.min(ri, 300) / 300 * 100}%;background:${lv.color}"></div></div></div>`;
    panel += rk('權益數', equity == null ? '請提供' : fmt(equity, 0), equity == null ? '貼期貨權益截圖或給我數字' : '', equity == null ? '#8b95a5' : '');
    panel += rk('原始保證金' + (isEstInit ? '（估）' : ''), fmt(initMargin, 0), '');
    panel += rk('維持保證金' + (isEstMaint ? '（估）' : ''), fmt(maintMargin, 0), '');
    panel += rk('可動用(出金)保證金', available == null ? '—' : fmt(available, 0), '', available == null ? '' : (available >= 0 ? '#1ed992' : '#ff525b'));
    panel += rk('超額/追繳保證金', surplus == null ? '—' : signStr(surplus, 0),
      surplus == null ? '' : (surplus >= 0 ? '超額（可出金）' : '需追繳'), surplus == null ? '' : (surplus >= 0 ? '#1ed992' : '#ff525b'));
    panel += rk('契約總值（名目）', fmt(totNotional, 0), '槓桿約 ' + (equity ? (totNotional / equity).toFixed(2) + 'x' : '—'));
    panel += `<div class="inv-risk-note">⚠️ 權益數 < 維持保證金即需追繳；依規定風險指標過低（約 ≤ 25%）期貨商將強制平倉。${(isEstInit || isEstMaint) ? '部分保證金為依契約價值估算（股票期貨級距1），實際以券商為準。' : '保證金與風險指標數值取自券商。'}</div>`;
    $('invFutRisk').innerHTML = panel;

    // ---- 留倉明細（含名目價值）----
    let rows = '';
    enriched.forEach(({ f, notional }) => {
      rows +=
        `<tr>
          <td class="inv-name">${f.name || '—'}</td>
          <td class="${f.side === '買進' ? 'up' : (f.side === '賣出' ? 'down' : '')}">${f.side || '—'}</td>
          <td class="inv-num">${fmt(f.lots, 0)}</td>
          <td class="inv-num">${fmt(f.avg_price, 2)}</td>
          <td class="inv-num">${fmt(f.price, 2)}</td>
          <td class="inv-num">${fmt(notional, 0)}</td>
          <td class="inv-num ${signCls(f.pnl)}">${signStr(f.pnl, 0)}</td>
        </tr>`;
    });
    $('invFuturesWrap').innerHTML =
      `<table class="inv-table"><thead><tr>
        <th>商品</th><th>買賣</th><th>口數</th><th>成交均價</th><th>即時價</th><th>名目價值</th><th>未平倉損益</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  }

  function init() {
    // 切換到庫存頁時重繪圓餅圖（修正隱藏時寬度為 0 的問題）
    const tabBtn = $('tabbtn-inventory');
    if (tabBtn) tabBtn.addEventListener('click', () => setTimeout(() => { if (pieChart) pieChart.resize(); }, 60));
    loadJson().then((d) => render(d || { holdings: [] }));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
