/* ===== 庫存與持股比例：上傳截圖 → GitHub → AI 辨識 → 顯示 ===== */
(function () {
  const REPO = 'ewq4303-debug/stocknotice';
  const BRANCH = 'main';
  const UPLOAD_PATH = 'inventory_uploads/upload.jpg';
  const TOKEN_KEY = 'gh_inv_token';
  const PALETTE = ['#4d7fff', '#1ed992', '#e0a83c', '#ff525b', '#9d7bff', '#5fd3ff',
    '#ff8a6b', '#3ddca0', '#f2c14e', '#7f9fff', '#ff6fa5', '#46c2c2', '#c9a227', '#8b95a5'];

  let pieChart = null;
  let pollTimer = null;
  let selectedBlob = null;
  let lastUpdatedAt = null;

  const $ = (id) => document.getElementById(id);

  function fmt(n, d) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toLocaleString('en-US', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function signCls(n) { return (n === null || n === undefined || isNaN(n)) ? '' : (n >= 0 ? 'up' : 'down'); }
  function signStr(n, d) { if (n === null || n === undefined || isNaN(n)) return '—'; return (n >= 0 ? '+' : '') + fmt(n, d); }

  /* ---------- Token ---------- */
  window.invSaveToken = function () {
    const t = ($('invToken').value || '').trim();
    if (!t) { alert('請先輸入 GitHub Token'); return; }
    localStorage.setItem(TOKEN_KEY, t);
    $('invHint').innerHTML = '✅ Token 已儲存在這台裝置的瀏覽器（不會上傳到任何伺服器）。';
  };

  /* ---------- 選檔 / 拖曳 ---------- */
  function downscale(file) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        const maxW = 1600;
        let w = img.width, h = img.height;
        if (w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        cv.getContext('2d').drawImage(img, 0, 0, w, h);
        cv.toBlob((b) => resolve(b || file), 'image/jpeg', 0.9);
      };
      img.onerror = () => resolve(file);
      img.src = URL.createObjectURL(file);
    });
  }

  async function onFile(file) {
    if (!file || !file.type.startsWith('image/')) { alert('請選擇圖片檔'); return; }
    selectedBlob = await downscale(file);
    const url = URL.createObjectURL(selectedBlob);
    const pv = $('invPreview');
    pv.src = url; pv.style.display = 'block';
    $('invUploadBtn').disabled = false;
  }

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(',')[1]);
      r.onerror = reject;
      r.readAsDataURL(blob);
    });
  }

  /* ---------- 上傳到 GitHub ---------- */
  window.invUpload = async function (btn) {
    const token = (localStorage.getItem(TOKEN_KEY) || ($('invToken').value || '').trim());
    if (!token) { alert('請先輸入並儲存 GitHub Token'); return; }
    if (!selectedBlob) { alert('請先選擇庫存截圖'); return; }
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ 上傳中...';
    try {
      const content = await blobToBase64(selectedBlob);
      const headers = { Authorization: 'token ' + token, Accept: 'application/vnd.github+json' };

      // 取得既有檔案 sha（覆蓋時需要）
      let sha;
      const getRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${UPLOAD_PATH}?ref=${BRANCH}`, { headers });
      if (getRes.status === 200) { sha = (await getRes.json()).sha; }
      else if (getRes.status === 401) { throw new Error('Token 無效或權限不足（需要 repo / contents 寫入權限）'); }

      const body = { message: 'inventory: upload screenshot ' + new Date().toISOString(), content, branch: BRANCH };
      if (sha) body.sha = sha;
      const putRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${UPLOAD_PATH}`, {
        method: 'PUT', headers: Object.assign({ 'Content-Type': 'application/json' }, headers), body: JSON.stringify(body)
      });
      if (!putRes.ok) { throw new Error('GitHub 回應 ' + putRes.status + '：' + (await putRes.text())); }

      btn.textContent = '✅ 已上傳，辨識中...';
      $('invHint').innerHTML = '📤 截圖已上傳，GitHub Action 正在用 AI 辨識並產生庫存 JSON，約 1–2 分鐘後此頁會自動更新…';
      startPolling();
    } catch (e) {
      alert('❌ 上傳失敗：' + e.message);
      btn.textContent = orig; btn.disabled = false;
    }
  };

  function startPolling() {
    const baseline = lastUpdatedAt;
    let tries = 0;
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      tries++;
      const data = await loadJson();
      if (data && data.updated_at && data.updated_at !== baseline) {
        clearInterval(pollTimer);
        render(data);
        $('invHint').innerHTML = '✅ 庫存已更新（' + data.updated_at + '）！';
        resetUploadBtn();
      } else if (tries > 30) { // 約 5 分鐘
        clearInterval(pollTimer);
        $('invHint').innerHTML = '⏱️ 辨識時間較久，稍後可手動重新整理頁面查看結果。';
        resetUploadBtn();
      }
    }, 10000);
  }
  function resetUploadBtn() {
    const b = $('invUploadBtn');
    b.textContent = '上傳並自動辨識'; b.disabled = false;
  }

  /* ---------- 載入 / 渲染 ---------- */
  async function loadJson() {
    try {
      const r = await fetch('inventory.json?t=' + Date.now());
      if (!r.ok) return null;
      return await r.json();
    } catch (e) { return null; }
  }

  function render(data) {
    const holdings = (data && Array.isArray(data.holdings)) ? data.holdings.slice() : [];
    lastUpdatedAt = data ? data.updated_at : null;
    $('invUpdated').textContent = (data && data.updated_at) ? ('更新於 ' + data.updated_at) : '';

    if (!holdings.length) {
      $('invSummary').innerHTML = '';
      $('invTableWrap').innerHTML = '<div class="inv-empty">尚無庫存資料，請於上方上傳券商庫存截圖。</div>';
      if (pieChart) { pieChart.clear(); }
      return;
    }

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

    // 圓餅圖
    const pieData = holdings.map((h, i) => ({
      name: (h.stock_id ? h.stock_id + ' ' : '') + (h.name || ''),
      value: +(h.market_value || 0).toFixed(0),
      itemStyle: { color: PALETTE[i % PALETTE.length] }
    }));
    if (!pieChart) { pieChart = echarts.init($('invPie')); }
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

    // 明細表
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

  function metric(cls, label, value, sub) {
    return `<div class="metric ${cls}"><div class="label">${label}</div>
      <div class="value inv-num ${cls === 'up' || cls === 'down' ? cls : ''}">${value}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
  }

  /* ---------- 初始化 ---------- */
  function init() {
    const saved = localStorage.getItem(TOKEN_KEY);
    if (saved && $('invToken')) $('invToken').value = saved;

    const fileInput = $('invFile');
    if (fileInput) fileInput.addEventListener('change', (e) => onFile(e.target.files[0]));

    const drop = $('invDrop');
    if (drop) {
      ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('drag'); }));
      ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('drag'); }));
      drop.addEventListener('drop', (e) => { if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]); });
    }

    // 切換到庫存頁時重繪圖表（修正隱藏時寬度為 0 的問題）
    const tabBtn = $('tabbtn-inventory');
    if (tabBtn) tabBtn.addEventListener('click', () => setTimeout(() => { if (pieChart) pieChart.resize(); }, 60));

    loadJson().then((d) => render(d || { holdings: [] }));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
