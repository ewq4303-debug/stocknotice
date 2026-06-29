/* ===== Dashboard shared interactions ===== */
(function () {
  const CONFIG = window.DASHBOARD_CONFIG || {};
  const STOCK_PATTERN = /^(?:\d{4,6}|\d{4,5}[A-Z])$/i;
  const TOKEN_KEY = 'stocknoticeWebhookToken';
  const COOLDOWN_MS = 30000;
  let lastWebhookAt = 0;

  function normalizeStockId(value) {
    return String(value || '').trim().toUpperCase();
  }

  function isValidStockId(stockId) {
    return STOCK_PATTERN.test(stockId);
  }

  function getWebhookToken() {
    let token = sessionStorage.getItem(TOKEN_KEY) || '';
    if (!token) {
      token = window.prompt('請輸入本次操作授權碼（需與 Apps Script 後端驗證值一致）') || '';
      token = token.trim();
      if (token) sessionStorage.setItem(TOKEN_KEY, token);
    }
    return token;
  }

  function ensureWebhookAllowed() {
    const now = Date.now();
    if (now - lastWebhookAt < COOLDOWN_MS) {
      alert('操作太頻繁，請稍候再試。');
      return null;
    }
    if (!CONFIG.gasUrl || !CONFIG.triggerUrl) {
      alert('尚未設定 webhook URL，請先設定 window.DASHBOARD_CONFIG。');
      return null;
    }
    const token = getWebhookToken();
    if (!token) {
      alert('未提供授權碼，已取消操作。');
      return null;
    }
    lastWebhookAt = now;
    return token;
  }

  window.toggleCard = function toggleCard(stockId) {
    if (window.innerWidth > 980) return;
    document.querySelectorAll('.stock-card').forEach(function (card) {
      if (card.id === 'card_' + stockId) card.classList.toggle('expanded');
      else card.classList.remove('expanded');
    });
    const card = document.getElementById('card_' + stockId);
    if (card && card.classList.contains('expanded')) {
      window.resizeAllCharts();
      setTimeout(function () { card.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 100);
    }
  };

  window.confirmDelete = function confirmDelete(event, stockId, btn) {
    event.stopPropagation();
    if (confirm('確定要取消追蹤 ' + stockId + ' 嗎？')) window.manageStock('remove', stockId, null, btn);
  };

  window.showCountdownToast = function showCountdownToast(message, totalSeconds) {
    const existing = document.getElementById('custom-toast');
    if (existing) existing.remove();
    const toast = document.createElement('div');
    toast.id = 'custom-toast';
    toast.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);background:rgba(10,14,21,.96);color:#dde3ec;padding:20px 30px;border-radius:12px;z-index:9999;font-size:16px;box-shadow:0 8px 30px rgba(0,0,0,.5);text-align:center;min-width:280px;backdrop-filter:blur(6px);border:1px solid #1e2632;';
    document.body.appendChild(toast);
    let secondsLeft = totalSeconds;
    const updateText = function () {
      toast.innerHTML = '<div style="margin-bottom:10px;line-height:1.5;">' + message + '</div><div style="font-size:36px;font-weight:900;color:#1ed992;font-variant-numeric:tabular-nums;margin:5px 0;">' + secondsLeft + '</div><div style="font-size:13px;color:#8b95a5;">秒後自動重新整理...</div>';
    };
    updateText();
    const timer = setInterval(function () {
      secondsLeft -= 1;
      if (secondsLeft <= 0) {
        clearInterval(timer);
        toast.innerHTML = "<div style='font-size:18px;font-weight:bold;color:#1ed992;'>🔄 重新整理中...</div>";
        window.location.reload();
      } else updateText();
    }, 1000);
  };

  window.manageStock = function manageStock(action, stockIdOverride, inputId, btn) {
    let stockId = normalizeStockId(stockIdOverride);
    if (!stockId) {
      const inputField = document.getElementById(inputId);
      if (inputField) stockId = normalizeStockId(inputField.value);
    }
    if (!stockId) { alert('請輸入股票代號！'); return; }
    if (!isValidStockId(stockId)) { alert('股票代號格式不正確，請輸入 4-6 碼數字或 ETF 代號。'); return; }
    const token = ensureWebhookAllowed();
    if (!token) return;

    let originalText = '';
    if (btn) { originalText = btn.innerText; btn.innerText = '⏳'; btn.style.pointerEvents = 'none'; }
    fetch(CONFIG.gasUrl, {
      method: 'POST',
      body: JSON.stringify({ action: action, stock: stockId, authToken: token }),
      headers: { 'Content-Type': 'text/plain;charset=utf-8', 'X-Dashboard-Token': token }
    })
      .then(function (response) { return response.text(); })
      .then(function (text) {
        if (text.includes('Error')) {
          alert('❌ 伺服器內部錯誤：\n' + text);
          if (btn) { btn.innerText = originalText; btn.style.pointerEvents = 'auto'; }
        } else if (text.includes('Success') || text.includes('No changes')) {
          const input = document.getElementById('stockInput');
          if (action === 'add' && input) input.value = '';
          fetch(CONFIG.triggerUrl, { method: 'POST', mode: 'no-cors', body: JSON.stringify({ action: 'refresh', authToken: token }) }).catch(function (e) { console.log(e); });
          const actionStr = (action === 'add') ? '新增' : '刪除';
          window.showCountdownToast('✅ 股票 ' + stockId + ' 已' + actionStr + '！<br>系統已自動觸發重新抓取資料', 150);
        } else {
          alert('⚠️ 收到未知回應，請確認權限設定。');
          if (btn) { btn.innerText = originalText; btn.style.pointerEvents = 'auto'; }
        }
      })
      .catch(function (err) {
        alert('❌ 網路請求失敗：' + err.message);
        if (btn) { btn.innerText = originalText; btn.style.pointerEvents = 'auto'; }
      });
  };

  window.resizeAllCharts = function resizeAllCharts() {
    setTimeout(function () { window.dispatchEvent(new Event('resize')); }, 50);
  };

  window.switchTab = function switchTab(tabId, btnElement) {
    document.querySelectorAll('.tab-btn').forEach(function (btn) { btn.classList.remove('active'); });
    btnElement.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(function (content) { content.classList.remove('active'); });
    document.getElementById(tabId).classList.add('active');
    window.resizeAllCharts();
  };

  window.showStock = function showStock(stockId) {
    if (window.innerWidth <= 980) return;
    document.querySelectorAll('.sidebar-item').forEach(function (el) { el.classList.remove('active'); });
    document.querySelectorAll('.stock-card').forEach(function (el) { el.classList.remove('active'); });
    document.getElementById('nav_' + stockId).classList.add('active');
    document.getElementById('card_' + stockId).classList.add('active');
    window.resizeAllCharts();
    window.scrollTo({ top: document.querySelector('.app-layout').offsetTop - 20, behavior: 'smooth' });
  };

  window.triggerAction = function triggerAction(btn) {
    const token = ensureWebhookAllowed();
    if (!token) return;
    btn.innerText = '⏳ 觸發中...';
    btn.style.pointerEvents = 'none';
    btn.style.opacity = '0.7';
    fetch(CONFIG.triggerUrl, { method: 'POST', mode: 'no-cors', body: JSON.stringify({ action: 'refresh', authToken: token }) })
      .then(function () { window.showCountdownToast('✅ 重新執行指令已發送！<br>系統正在抓取最新資料', 150); })
      .catch(function () { alert('❌ 發生錯誤，請檢查網路。'); })
      .finally(function () { btn.innerText = '⟳ 重新抓取最新資料'; btn.style.pointerEvents = 'auto'; btn.style.opacity = '1'; });
  };

  window.onscroll = function () {
    const btn = document.getElementById('backToTop');
    if (btn) btn.style.display = (document.body.scrollTop > 400 || document.documentElement.scrollTop > 400) ? 'block' : 'none';
  };
}());
