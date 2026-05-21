"""
股票監控機器人 v4.0
功能:
  1. 每日股票分析 (個股 + 大盤)
  2. 生成完整 HTML 網站 (GitHub Pages)
  3. 保留 Telegram 推播
  4. AI 綜合分析

資料源: FinMind API
輸出: docs/index.html + Telegram 訊息
"""

import os
import json
import requests
import subprocess
from datetime import datetime, timedelta
from collections import defaultdict
import anthropic

# ===== 設定 =====
STOCKS = os.getenv("STOCKS", "2330,2454,2317").split(",")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
OUTPUT_DIR = "docs"
OUTPUT_FILE = f"{OUTPUT_DIR}/index.html"


# =========================================================
# 資料抓取
# =========================================================

def fetch_finmind(dataset: str, **params):
    """通用 FinMind API 抓取"""
    try:
        p = {"dataset": dataset, "token": FINMIND_TOKEN, **params}
        r = requests.get(FINMIND_URL, params=p, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
        return data
    except Exception as e:
        print(f"  ⚠️ {dataset} 抓取失敗: {e}")
        return []


def get_stock_data(stock_id: str, days: int = 60):
    """取得個股資料 (價格、成交量、60日K線)"""
    start = (datetime.now() - timedelta(days=days+10)).strftime("%Y-%m-%d")
    data = fetch_finmind("TaiwanStockPrice", data_id=stock_id, start_date=start)
    
    if not data:
        return None
    
    # 只保留最近 days 天
    data = sorted(data, key=lambda x: x["date"])[-days:]
    
    return {
        "stock_id": stock_id,
        "kline": data,  # 完整K線資料
        "latest": data[-1] if data else {},
        "prev": data[-2] if len(data) > 1 else {},
    }


def get_institution_data(stock_id: str, days: int = 30):
    """取得三大法人買賣超"""
    start = (datetime.now() - timedelta(days=days+10)).strftime("%Y-%m-%d")
    data = fetch_finmind("TaiwanStockInstitutionalInvestors", 
                        data_id=stock_id, start_date=start)
    
    # 計算累計
    foreign_5d = sum(float(d.get("Foreign_Investor_diff", 0)) for d in data[-5:])
    foreign_20d = sum(float(d.get("Foreign_Investor_diff", 0)) for d in data[-20:])
    trust_5d = sum(float(d.get("Investment_Trust_diff", 0)) for d in data[-5:])
    trust_20d = sum(float(d.get("Investment_Trust_diff", 0)) for d in data[-20:])
    
    latest = data[-1] if data else {}
    
    return {
        "latest": latest,
        "foreign_today": float(latest.get("Foreign_Investor_diff", 0)) / 1000,  # 張
        "foreign_5d": foreign_5d / 1000,
        "foreign_20d": foreign_20d / 1000,
        "trust_today": float(latest.get("Investment_Trust_diff", 0)) / 1000,
        "trust_5d": trust_5d / 1000,
        "trust_20d": trust_20d / 1000,
        "dealer_today": float(latest.get("Dealer_diff", 0)) / 1000,
        "history": data,  # 30日完整資料
    }


def get_margin_data(stock_id: str, days: int = 30):
    """取得融資融券資料"""
    start = (datetime.now() - timedelta(days=days+10)).strftime("%Y-%m-%d")
    data = fetch_finmind("TaiwanStockMarginPurchaseShortSale", 
                        data_id=stock_id, start_date=start)
    
    if not data:
        return {}
    
    latest = data[-1]
    prev = data[-2] if len(data) > 1 else latest
    
    # 計算變化
    margin_balance = int(latest.get("MarginPurchaseBuy", 0))
    margin_change = margin_balance - int(prev.get("MarginPurchaseBuy", 0))
    
    short_balance = int(latest.get("ShortSaleBuy", 0))
    short_change = short_balance - int(prev.get("ShortSaleBuy", 0))
    
    return {
        "margin_balance": margin_balance,
        "margin_change": margin_change,
        "short_balance": short_balance,
        "short_change": short_change,
        "history": data,  # 30日完整資料
    }


def get_market_overview():
    """取得大盤資料"""
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
    
    # 加權指數
    taiex = fetch_finmind("TaiwanStockPrice", data_id="TAIEX", start_date=start)
    taiex = sorted(taiex, key=lambda x: x["date"])[-60:] if taiex else []
    
    # 大盤三大法人
    institution = fetch_finmind("TaiwanStockInstitutionalInvestorsSummary", 
                               start_date=start)
    
    # 期貨留倉
    futures = fetch_finmind("TaiwanFuturesInstitutionalInvestors", 
                           data_id="TX", start_date=start)
    
    # 匯率
    fx = fetch_finmind("TaiwanExchangeRate", start_date=start)
    usd_twd = [d for d in fx if d.get("currency") == "USD"]
    
    # 散戶多空比
    retail = fetch_finmind("TaiwanFuturesRetailPosition", 
                          data_id="MXF", start_date=start)
    
    # 台指期未平倉
    futures_oi = fetch_finmind("TaiwanFuturesDaily", 
                              data_id="TX", start_date=start)
    
    # 大盤融資
    total_margin = fetch_finmind("TaiwanStockTotalMarginPurchaseShortSale", 
                                start_date=start)
    
    return {
        "taiex": taiex,
        "institution": institution[-30:] if institution else [],
        "futures": futures[-30:] if futures else [],
        "usd_twd": usd_twd[-30:] if usd_twd else [],
        "retail": retail[-30:] if retail else [],
        "futures_oi": futures_oi[-30:] if futures_oi else [],
        "total_margin": total_margin[-30:] if total_margin else [],
    }


def get_news(stock_id: str, limit: int = 5):
    """取得股票新聞"""
    start = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    news = fetch_finmind("TaiwanStockNews", data_id=stock_id, start_date=start)
    
    # 按時間排序，取最新5條
    news = sorted(news, key=lambda x: x.get("date", ""), reverse=True)[:limit]
    return news


def calculate_technical_indicators(kline):
    """計算技術指標 (簡化版)"""
    if len(kline) < 60:
        return {}
    
    closes = [float(d["close"]) for d in kline]
    highs = [float(d["max"]) for d in kline]
    lows = [float(d["min"]) for d in kline]
    
    # MA
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    
    # KD (簡化)
    rsv = ((closes[-1] - min(lows[-9:])) / (max(highs[-9:]) - min(lows[-9:]))) * 100 if max(highs[-9:]) != min(lows[-9:]) else 50
    k = 50  # 簡化，實際需要遞迴計算
    d = 50
    
    # RSI (簡化)
    gains = [max(closes[i] - closes[i-1], 0) for i in range(-14, 0)]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(-14, 0)]
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    rs = avg_gain / avg_loss if avg_loss != 0 else 0
    rsi = 100 - (100 / (1 + rs))
    
    return {
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "k": round(rsv, 1),
        "d": round(k, 1),
        "rsi": round(rsi, 1),
    }


# =========================================================
# AI 分析
# =========================================================

def generate_ai_analysis(stock_id: str, stock_name: str, data: dict, indicators: dict, institution: dict, margin: dict):
    """呼叫 Claude API 生成股票分析"""
    
    if not ANTHROPIC_API_KEY:
        return "AI 分析功能未啟用（請設定 ANTHROPIC_API_KEY）"
    
    latest = data["latest"]
    prev = data["prev"]
    
    prompt = f"""請分析 {stock_id} {stock_name} 的當前狀況，提供專業建議。

## 技術面
- 收盤價: {latest.get('close')} (前日: {prev.get('close')})
- 成交量: {latest.get('Trading_Volume')} 張
- MA5: {indicators.get('ma5')}, MA20: {indicators.get('ma20')}, MA60: {indicators.get('ma60')}
- KD: K={indicators.get('k')}, D={indicators.get('d')}
- RSI: {indicators.get('rsi')}

## 籌碼面
- 外資今日: {institution['foreign_today']:.0f}張, 5日: {institution['foreign_5d']:.0f}張, 20日: {institution['foreign_20d']:.0f}張
- 投信今日: {institution['trust_today']:.0f}張, 5日: {institution['trust_5d']:.0f}張
- 融資餘額: {margin.get('margin_balance', 0):,}張 (變化: {margin.get('margin_change', 0):+})
- 融券餘額: {margin.get('short_balance', 0):,}張 (變化: {margin.get('short_change', 0):+})

請用繁體中文回答，分三部分：
1. **技術面分析** (150字內): K線位置、均線排列、技術指標訊號、支撐壓力
2. **籌碼面分析** (150字內): 法人動向、融資融券變化、籌碼健康度
3. **操作建議** (100字內): 短線策略、進出場點位、風險提示

請使用條列式，清楚明瞭。"""
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        analysis = response.content[0].text
        return analysis
        
    except Exception as e:
        print(f"  ⚠️ AI 分析失敗: {e}")
        return f"AI 分析暫時無法使用: {str(e)}"


def generate_market_ai_analysis(market_data: dict):
    """生成大盤 AI 分析"""
    
    if not ANTHROPIC_API_KEY:
        return "AI 分析功能未啟用"
    
    taiex_latest = market_data["taiex"][-1] if market_data["taiex"] else {}
    inst_latest = market_data["institution"][-1] if market_data["institution"] else {}
    
    prompt = f"""請分析台股大盤當前狀況。

## 大盤數據
- 加權指數: {taiex_latest.get('close', 'N/A')}
- 成交量: {taiex_latest.get('Trading_Volume', 'N/A')}
- 外資買賣超: {inst_latest.get('Foreign_Investor_diff', 0)} 千元
- 投信買賣超: {inst_latest.get('Investment_Trust_diff', 0)} 千元

請用繁體中文，分四部分（各100字）：
1. **技術面分析**: 指數位置、均線、技術指標
2. **籌碼面分析**: 法人動向、期貨留倉
3. **市場情緒**: 漲跌家數、類股表現
4. **後市展望**: 短線策略、風險提示"""
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        print(f"  ⚠️ 大盤 AI 分析失敗: {e}")
        return f"AI 分析暫時無法使用: {str(e)}"


# =========================================================
# HTML 生成
# =========================================================

def generate_html(stocks_data: dict, market_data: dict):
    """生成完整 HTML 網站"""
    
    # 生成時間
    update_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 個股卡片 HTML
    stock_cards_html = ""
    for stock_id, data in stocks_data.items():
        stock_cards_html += generate_stock_card_html(stock_id, data)
    
    # 大盤數據
    market_html = generate_market_html(market_data)
    
    # 市場指標時序
    timeseries_html = generate_timeseries_html(market_data)
    
    # 完整 HTML
    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股票監控儀表板</title>
<style>
{get_css_styles()}
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <div class="header-top">
      <h1>📊 股票監控儀表板</h1>
      <div class="update-time">
        <i class="ti ti-clock"></i>
        最後更新: {update_time} (台灣時間)
      </div>
    </div>
  </div>

  {market_html}
  
  {timeseries_html}
  
  <div class="section-header">
    <i class="ti ti-star"></i>
    追蹤個股分析
  </div>
  
  <div class="stock-grid">
    {stock_cards_html}
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-chart-financial@0.1.1/dist/chartjs-chart-financial.min.js"></script>
<script>
{get_chart_scripts(stocks_data, market_data)}
</script>

</body>
</html>"""
    
    return html


def generate_stock_card_html(stock_id: str, data: dict):
    """生成個股卡片 HTML"""
    
    stock = data["stock_data"]
    indicators = data["indicators"]
    institution = data["institution"]
    margin = data["margin"]
    news = data["news"]
    ai_analysis = data["ai_analysis"]
    
    latest = stock["latest"]
    prev = stock["prev"]
    
    # 計算漲跌
    close = float(latest.get("close", 0))
    prev_close = float(prev.get("close", close))
    change = close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
    
    change_class = "positive" if change > 0 else "negative"
    change_arrow = "↑" if change > 0 else "↓"
    
    # 新聞HTML
    news_html = ""
    for n in news[:5]:
        news_html += f"""
        <div class="news-item">
          <div class="news-time">{n.get('date', '')}</div>
          <div class="news-title">{n.get('title', '')}</div>
        </div>"""
    
    return f"""
    <div class="card">
      <div class="card-header">
        <h3 class="card-title">{stock_id} {data.get('name', '')}</h3>
        <div class="card-price {change_class}">${close:.2f}</div>
      </div>
      
      <div class="metrics-grid">
        <div class="metric">
          <div class="metric-label">漲跌</div>
          <div class="metric-value {change_class}">{change_arrow} {change:+.2f} ({change_pct:+.2f}%)</div>
        </div>
        <div class="metric">
          <div class="metric-label">成交量</div>
          <div class="metric-value">{int(latest.get('Trading_Volume', 0)):,} 張</div>
        </div>
      </div>
      
      <div class="chart-container" style="height: 200px;">
        <canvas id="kline_{stock_id}"></canvas>
      </div>
      
      <div class="analysis-box">
        <div class="analysis-title"><i class="ti ti-sparkles"></i> AI 分析</div>
        <div class="analysis-text">{ai_analysis.replace(chr(10), '<br>')}</div>
      </div>
      
      <div class="card-section">
        <div class="section-title">三大法人 (張)</div>
        <table class="table">
          <tr>
            <th>法人</th><th>今日</th><th>5日</th><th>20日</th>
          </tr>
          <tr>
            <td>外資</td>
            <td class="{'positive' if institution['foreign_today'] > 0 else 'negative'}">{institution['foreign_today']:+.0f}</td>
            <td class="{'positive' if institution['foreign_5d'] > 0 else 'negative'}">{institution['foreign_5d']:+.0f}</td>
            <td class="{'positive' if institution['foreign_20d'] > 0 else 'negative'}">{institution['foreign_20d']:+.0f}</td>
          </tr>
          <tr>
            <td>投信</td>
            <td class="{'positive' if institution['trust_today'] > 0 else 'negative'}">{institution['trust_today']:+.0f}</td>
            <td class="{'positive' if institution['trust_5d'] > 0 else 'negative'}">{institution['trust_5d']:+.0f}</td>
            <td class="{'positive' if institution['trust_20d'] > 0 else 'negative'}">{institution['trust_20d']:+.0f}</td>
          </tr>
        </table>
      </div>
      
      <div class="card-section">
        <div class="section-title">融資融券</div>
        <div class="metrics-row">
          <div class="metric-small">
            <div class="metric-small-label">融資餘額</div>
            <div class="metric-small-value">{margin.get('margin_balance', 0):,}</div>
            <div class="metric-small-change {'positive' if margin.get('margin_change', 0) > 0 else 'negative'}">{margin.get('margin_change', 0):+,}</div>
          </div>
          <div class="metric-small">
            <div class="metric-small-label">融券餘額</div>
            <div class="metric-small-value">{margin.get('short_balance', 0):,}</div>
            <div class="metric-small-change {'positive' if margin.get('short_change', 0) > 0 else 'negative'}">{margin.get('short_change', 0):+,}</div>
          </div>
        </div>
      </div>
      
      <div class="card-section">
        <div class="section-title">相關新聞</div>
        <div class="news-list">
          {news_html}
        </div>
      </div>
    </div>"""


def generate_market_html(market_data: dict):
    """生成大盤資訊 HTML"""
    
    taiex_latest = market_data["taiex"][-1] if market_data["taiex"] else {}
    inst_latest = market_data["institution"][-1] if market_data["institution"] else {}
    
    close = float(taiex_latest.get("close", 0))
    prev_close = float(market_data["taiex"][-2].get("close", close)) if len(market_data["taiex"]) > 1 else close
    change = close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
    change_class = "positive" if change > 0 else "negative"
    change_arrow = "↑" if change > 0 else "↓"
    
    return f"""
    <div class="section-header">
      <i class="ti ti-chart-line"></i>
      大盤總覽
    </div>
    
    <div class="metrics-grid-4">
      <div class="metric">
        <div class="metric-label">加權指數</div>
        <div class="metric-value {change_class}">{close:.2f}</div>
        <div class="metric-change {change_class}">{change_arrow} {change:+.2f} ({change_pct:+.2f}%)</div>
      </div>
      <div class="metric">
        <div class="metric-label">成交量 (億股)</div>
        <div class="metric-value">{float(taiex_latest.get('Trading_Volume', 0))/100000:.1f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">外資買賣超 (億)</div>
        <div class="metric-value {'positive' if float(inst_latest.get('Foreign_Investor_diff', 0)) > 0 else 'negative'}">
          {float(inst_latest.get('Foreign_Investor_diff', 0))/100000:.1f}
        </div>
      </div>
      <div class="metric">
        <div class="metric-label">投信買賣超 (億)</div>
        <div class="metric-value {'positive' if float(inst_latest.get('Investment_Trust_diff', 0)) > 0 else 'negative'}">
          {float(inst_latest.get('Investment_Trust_diff', 0))/100000:.1f}
        </div>
      </div>
    </div>
    
    <div class="card" style="margin-top: 16px;">
      <div class="chart-container" style="height: 320px;">
        <canvas id="taiexChart"></canvas>
      </div>
    </div>"""


def generate_timeseries_html(market_data: dict):
    """生成市場指標時序分析 HTML"""
    
    return """
    <div class="section-header">
      <i class="ti ti-chart-dots"></i>
      市場指標綜合研判 (近30日)
    </div>
    
    <div class="grid-2">
      <div class="card">
        <div class="card-title">微台散戶多空比</div>
        <div class="chart-container">
          <canvas id="retailChart"></canvas>
        </div>
      </div>
      
      <div class="card">
        <div class="card-title">美元兌新台幣</div>
        <div class="chart-container">
          <canvas id="fxChart"></canvas>
        </div>
      </div>
    </div>
    
    <div class="card">
      <div class="card-title">三大法人現貨 vs 期貨</div>
      <div class="chart-container" style="height: 320px;">
        <canvas id="institutionChart"></canvas>
      </div>
    </div>"""


def get_css_styles():
    """回傳 CSS 樣式"""
    return """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f5f5f5;
  padding: 16px;
  line-height: 1.5;
  color: #333;
}
.container { max-width: 1400px; margin: 0 auto; }
.header {
  background: white;
  border-radius: 12px;
  border: 0.5px solid #e0e0e0;
  padding: 1.25rem;
  margin-bottom: 16px;
}
.header-top { display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 22px; font-weight: 500; }
.update-time { font-size: 13px; color: #666; }
.section-header {
  font-size: 18px;
  font-weight: 500;
  margin: 24px 0 16px 0;
  display: flex;
  align-items: center;
  gap: 8px;
}
.card {
  background: white;
  border-radius: 12px;
  border: 0.5px solid #e0e0e0;
  padding: 1rem;
  margin-bottom: 16px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 0.5px solid #e0e0e0;
}
.card-title { font-size: 18px; font-weight: 500; }
.card-price { font-size: 20px; font-weight: 500; }
.positive { color: #d32f2f; }
.negative { color: #388e3c; }
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}
.metrics-grid-4 {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}
.metric {
  background: #f5f5f5;
  border-radius: 8px;
  padding: 12px;
}
.metric-label { font-size: 11px; color: #666; margin-bottom: 4px; }
.metric-value { font-size: 16px; font-weight: 500; }
.metric-change { font-size: 12px; margin-top: 2px; }
.chart-container { position: relative; height: 280px; margin-bottom: 12px; }
.stock-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 16px;
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.analysis-box {
  background: #f5f5f5;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.analysis-title {
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 8px;
}
.analysis-text {
  font-size: 13px;
  color: #666;
  line-height: 1.6;
}
.card-section { margin-bottom: 16px; }
.section-title {
  font-size: 13px;
  font-weight: 500;
  margin-bottom: 8px;
}
.table {
  width: 100%;
  font-size: 12px;
  border-collapse: collapse;
}
.table th {
  text-align: left;
  padding: 8px;
  background: #f5f5f5;
  color: #666;
  font-weight: 500;
}
.table td {
  padding: 8px;
  border-bottom: 0.5px solid #e0e0e0;
}
.metrics-row {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}
.metric-small {
  background: #f5f5f5;
  border-radius: 8px;
  padding: 10px;
  text-align: center;
}
.metric-small-label { font-size: 11px; color: #666; margin-bottom: 4px; }
.metric-small-value { font-size: 14px; font-weight: 500; }
.metric-small-change { font-size: 11px; margin-top: 2px; }
.news-list { max-height: 200px; overflow-y: auto; }
.news-item {
  padding: 8px 0;
  border-bottom: 0.5px solid #e0e0e0;
}
.news-item:last-child { border-bottom: none; }
.news-time { font-size: 11px; color: #666; margin-bottom: 4px; }
.news-title { font-size: 13px; line-height: 1.4; }
@media (max-width: 1024px) {
  .grid-2 { grid-template-columns: 1fr; }
  .metrics-grid-4 { grid-template-columns: repeat(2, 1fr); }
  .stock-grid { grid-template-columns: 1fr; }
}
"""


def get_chart_scripts(stocks_data: dict, market_data: dict):
    """生成 Chart.js 腳本"""
    
    scripts = []
    
    # 個股 K 線圖
    for stock_id, data in stocks_data.items():
        kline = data["stock_data"]["kline"]
        dates = [d["date"][-5:] for d in kline[-30:]]  # MM-DD
        closes = [float(d["close"]) for d in kline[-30:]]
        
        scripts.append(f"""
new Chart(document.getElementById('kline_{stock_id}'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(dates)},
    datasets: [{{
      label: '收盤價',
      data: {json.dumps(closes)},
      borderColor: '#378ADD',
      borderWidth: 2,
      tension: 0.3,
      pointRadius: 2,
      fill: false
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ ticks: {{ font: {{ size: 11 }} }} }},
      x: {{ ticks: {{ font: {{ size: 10 }}, maxRotation: 0, autoSkip: true }} }}
    }}
  }}
}});""")
    
    # 大盤 K 線
    taiex = market_data["taiex"][-60:]
    taiex_dates = [d["date"][-5:] for d in taiex]
    taiex_closes = [float(d["close"]) for d in taiex]
    
    scripts.append(f"""
new Chart(document.getElementById('taiexChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(taiex_dates)},
    datasets: [{{
      label: '加權指數',
      data: {json.dumps(taiex_closes)},
      borderColor: '#378ADD',
      borderWidth: 2,
      tension: 0.3,
      fill: false
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }}
  }}
}});""")
    
    # 散戶多空比
    retail = market_data["retail"][-30:]
    retail_dates = [d["date"][-5:] for d in retail]
    retail_ratio = [float(d.get("long_volume", 0)) / (float(d.get("long_volume", 0)) + float(d.get("short_volume", 1))) * 100 for d in retail]
    
    scripts.append(f"""
new Chart(document.getElementById('retailChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(retail_dates)},
    datasets: [{{
      label: '散戶多單比例 (%)',
      data: {json.dumps(retail_ratio)},
      borderColor: '#EF5350',
      backgroundColor: 'rgba(239, 83, 80, 0.1)',
      borderWidth: 2,
      fill: true,
      tension: 0.3
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }}
  }}
}});""")
    
    # 匯率
    fx = market_data["usd_twd"][-30:]
    fx_dates = [d["date"][-5:] for d in fx]
    fx_values = [float(d.get("close", 0)) for d in fx]
    
    scripts.append(f"""
new Chart(document.getElementById('fxChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(fx_dates)},
    datasets: [{{
      label: 'USD/TWD',
      data: {json.dumps(fx_values)},
      borderColor: '#378ADD',
      borderWidth: 2,
      tension: 0.3
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }}
  }}
}});""")
    
    # 三大法人
    inst = market_data["institution"][-30:]
    inst_dates = [d["date"][-5:] for d in inst]
    foreign = [float(d.get("Foreign_Investor_diff", 0)) / 100000 for d in inst]  # 億元
    trust = [float(d.get("Investment_Trust_diff", 0)) / 100000 for d in inst]
    
    scripts.append(f"""
new Chart(document.getElementById('institutionChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(inst_dates)},
    datasets: [
      {{
        label: '外資現貨 (億)',
        data: {json.dumps(foreign)},
        backgroundColor: 'rgba(55, 138, 221, 0.7)',
        yAxisID: 'y'
      }},
      {{
        label: '投信現貨 (億)',
        data: {json.dumps(trust)},
        backgroundColor: 'rgba(29, 158, 117, 0.7)',
        yAxisID: 'y'
      }}
    ]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: true, position: 'top' }} }},
    scales: {{
      y: {{
        position: 'left',
        title: {{ display: true, text: '買賣超 (億)' }}
      }}
    }}
  }}
}});""")
    
    return "\n".join(scripts)


# =========================================================
# Telegram 推播
# =========================================================

def send_telegram(text: str):
    """發送 Telegram 訊息"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"  ⚠️ Telegram 推播失敗: {e}")


# =========================================================
# 主流程
# =========================================================

def main():
    print(f"=== 股票監控機器人 v4.0 ({datetime.now().strftime('%Y-%m-%d %H:%M')}) ===\n")
    
    # 建立輸出目錄
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # 1. 抓取個股資料
    print("[1/5] 抓取個股資料...")
    stocks_data = {}
    
    for stock_id in STOCKS:
        print(f"  處理 {stock_id}...")
        
        stock_data = get_stock_data(stock_id)
        if not stock_data:
            print(f"    ⚠️ {stock_id} 無資料，跳過")
            continue
        
        institution = get_institution_data(stock_id)
        margin = get_margin_data(stock_id)
        news = get_news(stock_id)
        indicators = calculate_technical_indicators(stock_data["kline"])
        
        # AI 分析
        print(f"    生成 AI 分析...")
        ai_analysis = generate_ai_analysis(
            stock_id, 
            stock_data["latest"].get("stock_id", stock_id),
            stock_data, 
            indicators, 
            institution, 
            margin
        )
        
        stocks_data[stock_id] = {
            "stock_data": stock_data,
            "institution": institution,
            "margin": margin,
            "news": news,
            "indicators": indicators,
            "ai_analysis": ai_analysis,
            "name": stock_data["latest"].get("stock_id", stock_id)
        }
    
    # 2. 抓取大盤資料
    print("\n[2/5] 抓取大盤資料...")
    market_data = get_market_overview()
    
    # 3. 生成 HTML
    print("\n[3/5] 生成 HTML 網站...")
    html = generate_html(stocks_data, market_data)
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    
    print(f"  ✓ HTML 已生成: {OUTPUT_FILE}")
    
    # 4. Git commit + push
    print("\n[4/5] 更新 GitHub Pages...")
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", OUTPUT_DIR], check=True)
        
        result = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if result.returncode == 0:
            print("  ⓘ 無新變動")
        else:
            subprocess.run(["git", "commit", "-m", f"Update stock analysis {datetime.now().strftime('%Y-%m-%d %H:%M')}"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("  ✓ 已更新 GitHub Pages")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ Git 操作失敗: {e}")
    
    # 5. Telegram 推播（簡化版）
    print("\n[5/5] 發送 Telegram 通知...")
    
    tg_msg = f"📊 *股票監控日報* ({datetime.now().strftime('%Y-%m-%d')})\n\n"
    
    for stock_id, data in stocks_data.items():
        latest = data["stock_data"]["latest"]
        close = float(latest.get("close", 0))
        foreign = data["institution"]["foreign_today"]
        
        tg_msg += f"*{stock_id}* ${close:.2f} | 外資 {foreign:+.0f}張\n"
    
    tg_msg += f"\n🌐 完整分析: https://你的用戶名.github.io/stocknotice/"
    
    send_telegram(tg_msg)
    print("  ✓ Telegram 已送出")
    
    print("\n✅ 完成！")


if __name__ == "__main__":
    main()
