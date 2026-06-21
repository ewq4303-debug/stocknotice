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

  function renderFutures(data) {
    const card = $('invFuturesCard');
    const futures = (data && Array.isArray(data.futures)) ? data.futures : [];
    if (!futures.length) { card.style.display = 'none'; return; }
    card.style.display = '';

    const totPnl = (data.total_futures_pnl != null) ? data.total_futures_pnl : futures.reduce((s, f) => s + (f.pnl || 0), 0);
    $('invFutPnl').innerHTML = '未平倉損益合計 <span class="inv-num ' + signCls(totPnl) + '">' + signStr(totPnl, 0) + '</span>';

    let rows = '';
    futures.forEach((f) => {
      rows +=
        `<tr>
          <td class="inv-name">${f.name || '—'}</td>
          <td class="${f.side === '買進' ? 'up' : (f.side === '賣出' ? 'down' : '')}">${f.side || '—'}</td>
          <td class="inv-num">${fmt(f.lots, 0)}</td>
          <td class="inv-num">${fmt(f.avg_price, 2)}</td>
          <td class="inv-num">${fmt(f.price, 2)}</td>
          <td class="inv-num ${signCls(f.pnl)}">${signStr(f.pnl, 0)}</td>
        </tr>`;
    });
    $('invFuturesWrap').innerHTML =
      `<table class="inv-table"><thead><tr>
        <th>商品</th><th>買賣</th><th>口數</th><th>成交均價</th><th>即時價</th><th>未平倉損益</th>
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
