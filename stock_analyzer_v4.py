"""
股票監控機器人 v4.2 - 穩定版
改進:
  - 使用 yfinance 抓股價（更穩定）
  - 自己計算技術指標（不依賴 pandas-ta）
  - 修正所有圖表數據生成
  - 完善錯誤處理
"""

import os
import json
import requests
import subprocess
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
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
# 技術指標計算（純 pandas，不依賴外部套件）
# =========================================================

def calculate_sma(series, period):
    """計算簡單移動平均"""
    return series.rolling(window=period).mean()

def calculate_rsi(series, period=14):
    """計算 RSI 指標"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(series, fast=12, slow=26, signal=9):
    """計算 MACD 指標"""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

def calculate_stochastic(high, low, close, period=14, smooth_k=3, smooth_d=3):
    """計算 KD 指標"""
    lowest_low = low.rolling(window=period).min()
    highest_high = high.rolling(window=period).max()
    k = 100 * ((close - lowest_low) / (highest_high - lowest_low))
    k = k.rolling(window=smooth_k).mean()
    d = k.rolling(window=smooth_d).mean()
    return k, d


# =========================================================
# 資料抓取 - yfinance (股價、K線)
# =========================================================

def get_stock_data_yf(stock_id: str, days: int = 60):
    """使用 yfinance 取得台股股價資料"""
    try:
        # 台股代號需要加 .TW
        ticker = yf.Ticker(f"{stock_id}.TW")
        
        # 抓取歷史資料
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days+20)
        
        df = ticker.history(start=start_date, end=end_date)
        
        if df.empty:
            print(f"    ⚠️ {stock_id} yfinance 無資料")
            return None
        
        # 只保留最近 days 天
        df = df.tail(days)
        
        # 計算技術指標（使用自己的函數）
        df['SMA_5'] = calculate_sma(df['Close'], 5)
        df['SMA_20'] = calculate_sma(df['Close'], 20)
        df['SMA_60'] = calculate_sma(df['Close'], 60)
        df['RSI_14'] = calculate_rsi(df['Close'], 14)
        df['MACD'], df['MACD_Signal'] = calculate_macd(df['Close'])
        df['K'], df['D'] = calculate_stochastic(df['High'], df['Low'], df['Close'])
        
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest
        
        return {
            "stock_id": stock_id,
            "df": df,  # 完整 DataFrame
            "latest": {
                "close": float(latest["Close"]),
                "volume": int(latest["Volume"]),
                "high": float(latest["High"]),
                "low": float(latest["Low"]),
                "open": float(latest["Open"]),
            },
            "prev": {
                "close": float(prev["Close"]),
            },
            "indicators": {
                "ma5": float(latest.get("SMA_5", 0)) if pd.notna(latest.get("SMA_5")) else 0,
                "ma20": float(latest.get("SMA_20", 0)) if pd.notna(latest.get("SMA_20")) else 0,
                "ma60": float(latest.get("SMA_60", 0)) if pd.notna(latest.get("SMA_60")) else 0,
                "rsi": float(latest.get("RSI_14", 50)) if pd.notna(latest.get("RSI_14")) else 50,
                "k": float(latest.get("K", 50)) if pd.notna(latest.get("K")) else 50,
                "d": float(latest.get("D", 50)) if pd.notna(latest.get("D")) else 50,
                "macd": float(latest.get("MACD", 0)) if pd.notna(latest.get("MACD")) else 0,
                "macd_signal": float(latest.get("MACD_Signal", 0)) if pd.notna(latest.get("MACD_Signal")) else 0,
            }
        }
        
    except Exception as e:
        print(f"    ⚠️ {stock_id} yfinance 錯誤: {e}")
        return None


# =========================================================
# 資料抓取 - FinMind (法人、融資融券)
# =========================================================

def fetch_finmind(dataset: str, **params):
    """通用 FinMind API 抓取"""
    if not FINMIND_TOKEN:
        print(f"  ⚠️ FINMIND_TOKEN 未設定，跳過 {dataset}")
        return []
    
    try:
        p = {"dataset": dataset, "token": FINMIND_TOKEN, **params}
        r = requests.get(FINMIND_URL, params=p, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", [])
        return data
    except Exception as e:
        print(f"  ⚠️ {dataset} 抓取失敗: {e}")
        return []


def get_institution_data(stock_id: str, days: int = 30):
    """取得三大法人買賣超"""
    start = (datetime.now() - timedelta(days=days+10)).strftime("%Y-%m-%d")
    data = fetch_finmind("TaiwanStockInstitutionalInvestors", 
                        data_id=stock_id, start_date=start)
    
    if not data:
        return {"latest": {}, "foreign_today": 0, "foreign_5d": 0, "foreign_20d": 0,
                "trust_today": 0, "trust_5d": 0, "trust_20d": 0, "dealer_today": 0, "history": []}
    
    # 排序並取最近 days 天
    data = sorted(data, key=lambda x: x.get("date", ""))[-days:]
    
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
        "history": data,
    }


def get_margin_data(stock_id: str, days: int = 30):
    """取得融資融券資料"""
    start = (datetime.now() - timedelta(days=days+10)).strftime("%Y-%m-%d")
    data = fetch_finmind("TaiwanStockMarginPurchaseShortSale", 
                        data_id=stock_id, start_date=start)
    
    if not data:
        return {"margin_balance": 0, "margin_change": 0, "short_balance": 0, "short_change": 0, "history": []}
    
    data = sorted(data, key=lambda x: x.get("date", ""))[-days:]
    latest = data[-1]
    prev = data[-2] if len(data) > 1 else latest
    
    margin_balance = int(latest.get("MarginPurchaseBuy", 0))
    margin_change = margin_balance - int(prev.get("MarginPurchaseBuy", 0))
    
    short_balance = int(latest.get("ShortSaleBuy", 0))
    short_change = short_balance - int(prev.get("ShortSaleBuy", 0))
    
    return {
        "margin_balance": margin_balance,
        "margin_change": margin_change,
        "short_balance": short_balance,
        "short_change": short_change,
        "history": data,
    }


def get_market_overview():
    """取得大盤資料"""
    start = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
    
    # 加權指數 (使用 yfinance)
    try:
        taiex_ticker = yf.Ticker("^TWII")
        taiex_df = taiex_ticker.history(period="3mo")
        taiex = taiex_df.tail(60).reset_index()
        taiex_data = []
        for _, row in taiex.iterrows():
            taiex_data.append({
                "date": row["Date"].strftime("%Y-%m-%d"),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]),
            })
    except Exception as e:
        print(f"  ⚠️ 大盤指數抓取失敗: {e}")
        taiex_data = []
    
    # 大盤三大法人
    institution = fetch_finmind("TaiwanStockInstitutionalInvestorsSummary", start_date=start)
    institution = sorted(institution, key=lambda x: x.get("date", ""))[-30:] if institution else []
    
    # 期貨留倉
    futures = fetch_finmind("TaiwanFuturesInstitutionalInvestors", data_id="TX", start_date=start)
    futures = sorted(futures, key=lambda x: x.get("date", ""))[-30:] if futures else []
    
    # 匯率
    fx = fetch_finmind("TaiwanExchangeRate", start_date=start)
    usd_twd = [d for d in fx if d.get("currency") == "USD"]
    usd_twd = sorted(usd_twd, key=lambda x: x.get("date", ""))[-30:] if usd_twd else []
    
    # 散戶多空比
    retail = fetch_finmind("TaiwanFuturesRetailPosition", data_id="MXF", start_date=start)
    retail = sorted(retail, key=lambda x: x.get("date", ""))[-30:] if retail else []
    
    # 台指期未平倉
    futures_oi = fetch_finmind("TaiwanFuturesDaily", data_id="TX", start_date=start)
    futures_oi = sorted(futures_oi, key=lambda x: x.get("date", ""))[-30:] if futures_oi else []
    
    # 大盤融資
    total_margin = fetch_finmind("TaiwanStockTotalMarginPurchaseShortSale", start_date=start)
    total_margin = sorted(total_margin, key=lambda x: x.get("date", ""))[-30:] if total_margin else []
    
    return {
        "taiex": taiex_data,
        "institution": institution,
        "futures": futures,
        "usd_twd": usd_twd,
        "retail": retail,
        "futures_oi": futures_oi,
        "total_margin": total_margin,
    }


def get_news(stock_id: str, limit: int = 5):
    """取得股票新聞"""
    start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    news = fetch_finmind("TaiwanStockNews", data_id=stock_id, start_date=start)
    
    news = sorted(news, key=lambda x: x.get("date", ""), reverse=True)[:limit]
    return news


def get_stock_name(stock_id: str):
    """取得股票名稱"""
    try:
        ticker = yf.Ticker(f"{stock_id}.TW")
        info = ticker.info
        return info.get("longName", stock_id)
    except:
        return stock_id


# =========================================================
# AI 分析
# =========================================================

def generate_ai_analysis(stock_id: str, stock_name: str, data: dict, institution: dict, margin: dict):
    """呼叫 Claude API 生成股票分析"""
    
    if not ANTHROPIC_API_KEY:
        return "AI 分析功能未啟用（請設定 ANTHROPIC_API_KEY）"
    
    latest = data["latest"]
    prev = data["prev"]
    ind = data["indicators"]
    
    prompt = f"""請分析 {stock_id} {stock_name} 的當前狀況，提供專業建議。

## 技術面
- 收盤價: {latest['close']:.2f} (前日: {prev['close']:.2f})
- 成交量: {latest['volume']:,} 股
- MA5: {ind['ma5']:.2f}, MA20: {ind['ma20']:.2f}, MA60: {ind['ma60']:.2f}
- KD: K={ind['k']:.1f}, D={ind['d']:.1f}
- RSI: {ind['rsi']:.1f}
- MACD: {ind['macd']:.2f} (訊號線: {ind['macd_signal']:.2f})

## 籌碼面
- 外資今日: {institution['foreign_today']:.0f}張, 5日: {institution['foreign_5d']:.0f}張, 20日: {institution['foreign_20d']:.0f}張
- 投信今日: {institution['trust_today']:.0f}張, 5日: {institution['trust_5d']:.0f}張
- 融資餘額: {margin['margin_balance']:,}張 (變化: {margin['margin_change']:+,})
- 融券餘額: {margin['short_balance']:,}張 (變化: {margin['short_change']:+,})

請用繁體中文回答，分三部分（每部分約100字）：
1. **技術面分析**: K線位置、均線排列、技術指標訊號、支撐壓力
2. **籌碼面分析**: 法人動向、融資融券變化、籌碼健康度
3. **操作建議**: 短線策略、進出場點位、風險提示"""
    
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
        return "AI 分析暫時無法使用"


# =========================================================
# HTML 生成（簡化版，包含所有修正）
# =========================================================

def generate_html(stocks_data: dict, market_data: dict):
    """生成完整 HTML"""
    
    update_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 生成個股卡片
    stock_cards = ""
    for stock_id, data in stocks_data.items():
        stock_cards += generate_stock_card(stock_id, data)
    
    # 生成大盤區塊
    market_section = generate_market_section(market_data)
    
    # 生成時序圖表區塊
    timeseries_section = generate_timeseries_section(market_data)
    
    # Chart.js 腳本
    chart_scripts = generate_chart_scripts(stocks_data, market_data)
    
    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股票監控儀表板</title>
<style>{get_css()}</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📊 股票監控儀表板</h1>
    <div class="update-time">最後更新: {update_time}</div>
  </div>
  
  {market_section}
  {timeseries_section}
  
  <div class="section-header">追蹤個股分析</div>
  <div class="stock-grid">{stock_cards}</div>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>{chart_scripts}</script>
</body>
</html>"""
    
    return html


def generate_stock_card(stock_id: str, data: dict):
    """生成個股卡片"""
    latest = data["latest"]
    prev = data["prev"]
    ind = data["indicators"]
    inst = data["institution"]
    margin = data["margin"]
    news = data["news"]
    ai = data["ai_analysis"]
    
    close = latest["close"]
    prev_close = prev["close"]
    change = close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
    
    change_class = "positive" if change > 0 else "negative"
    arrow = "↑" if change > 0 else "↓"
    
    news_html = ""
    for n in news[:5]:
        news_html += f'<div class="news-item"><div class="news-time">{n.get("date", "")}</div><div class="news-title">{n.get("title", "")}</div></div>'
    
    return f"""
<div class="card">
  <div class="card-header">
    <h3>{stock_id} {data.get('name', '')}</h3>
    <div class="card-price {change_class}">${close:.2f}</div>
  </div>
  <div class="metrics-grid">
    <div class="metric">
      <div class="metric-label">漲跌</div>
      <div class="metric-value {change_class}">{arrow} {change:+.2f} ({change_pct:+.2f}%)</div>
    </div>
    <div class="metric">
      <div class="metric-label">成交量</div>
      <div class="metric-value">{int(latest['volume']/1000):,} 張</div>
    </div>
  </div>
  <div class="chart-container" style="height:200px;">
    <canvas id="k{stock_id}"></canvas>
  </div>
  <div class="analysis-box">
    <div class="analysis-title">AI 分析</div>
    <div class="analysis-text">{ai.replace(chr(10), '<br>')}</div>
  </div>
  <div class="section-title">技術指標</div>
  <div class="metrics-row">
    <div class="metric-sm"><div class="metric-sm-label">MA5</div><div class="metric-sm-value">{ind['ma5']:.2f}</div></div>
    <div class="metric-sm"><div class="metric-sm-label">MA20</div><div class="metric-sm-value">{ind['ma20']:.2f}</div></div>
    <div class="metric-sm"><div class="metric-sm-label">RSI</div><div class="metric-sm-value">{ind['rsi']:.1f}</div></div>
    <div class="metric-sm"><div class="metric-sm-label">KD</div><div class="metric-sm-value">{ind['k']:.0f}/{ind['d']:.0f}</div></div>
  </div>
  <div class="section-title">三大法人 (張)</div>
  <table class="table">
    <tr><th>法人</th><th>今日</th><th>5日</th><th>20日</th></tr>
    <tr><td>外資</td><td class="{'positive' if inst['foreign_today']>0 else 'negative'}">{inst['foreign_today']:+.0f}</td><td class="{'positive' if inst['foreign_5d']>0 else 'negative'}">{inst['foreign_5d']:+.0f}</td><td class="{'positive' if inst['foreign_20d']>0 else 'negative'}">{inst['foreign_20d']:+.0f}</td></tr>
    <tr><td>投信</td><td class="{'positive' if inst['trust_today']>0 else 'negative'}">{inst['trust_today']:+.0f}</td><td class="{'positive' if inst['trust_5d']>0 else 'negative'}">{inst['trust_5d']:+.0f}</td><td class="{'positive' if inst['trust_20d']>0 else 'negative'}">{inst['trust_20d']:+.0f}</td></tr>
  </table>
  <div class="section-title">融資融券</div>
  <div class="metrics-row">
    <div class="metric-sm"><div class="metric-sm-label">融資</div><div class="metric-sm-value">{margin['margin_balance']:,}</div><div class="metric-sm-change {'positive' if margin['margin_change']>0 else 'negative'}">{margin['margin_change']:+,}</div></div>
    <div class="metric-sm"><div class="metric-sm-label">融券</div><div class="metric-sm-value">{margin['short_balance']:,}</div><div class="metric-sm-change {'positive' if margin['short_change']>0 else 'negative'}">{margin['short_change']:+,}</div></div>
  </div>
  <div class="section-title">相關新聞</div>
  <div class="news-list">{news_html}</div>
</div>"""


def generate_market_section(market_data: dict):
    """生成大盤區塊"""
    taiex = market_data["taiex"]
    if not taiex:
        return "<p>大盤資料載入中...</p>"
    
    latest = taiex[-1]
    prev = taiex[-2] if len(taiex) > 1 else latest
    
    close = latest["close"]
    prev_close = prev["close"]
    change = close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
    
    change_class = "positive" if change > 0 else "negative"
    arrow = "↑" if change > 0 else "↓"
    
    inst = market_data["institution"]
    inst_latest = inst[-1] if inst else {}
    
    foreign = float(inst_latest.get("Foreign_Investor_diff", 0)) / 100000  # 億
    trust = float(inst_latest.get("Investment_Trust_diff", 0)) / 100000
    
    return f"""
<div class="section-header">大盤總覽</div>
<div class="metrics-grid-4">
  <div class="metric">
    <div class="metric-label">加權指數</div>
    <div class="metric-value {change_class}">{close:.2f}</div>
    <div class="metric-change {change_class}">{arrow} {change:+.2f} ({change_pct:+.2f}%)</div>
  </div>
  <div class="metric">
    <div class="metric-label">成交量(億股)</div>
    <div class="metric-value">{latest['volume']/100000:.1f}</div>
  </div>
  <div class="metric">
    <div class="metric-label">外資(億)</div>
    <div class="metric-value {'positive' if foreign>0 else 'negative'}">{foreign:+.1f}</div>
  </div>
  <div class="metric">
    <div class="metric-label">投信(億)</div>
    <div class="metric-value {'positive' if trust>0 else 'negative'}">{trust:+.1f}</div>
  </div>
</div>
<div class="card">
  <div class="chart-container" style="height:300px;">
    <canvas id="taiex"></canvas>
  </div>
</div>"""


def generate_timeseries_section(market_data: dict):
    """生成時序分析區塊"""
    return """
<div class="section-header">市場指標綜合研判 (近30日)</div>
<div class="grid-2">
  <div class="card">
    <div class="card-title">微台散戶多空比</div>
    <div class="chart-container"><canvas id="retail"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">美元兌新台幣</div>
    <div class="chart-container"><canvas id="fx"></canvas></div>
  </div>
</div>
<div class="card">
  <div class="card-title">三大法人現貨 vs 期貨</div>
  <div class="chart-container" style="height:320px;">
    <canvas id="inst"></canvas>
  </div>
</div>"""


def generate_chart_scripts(stocks_data: dict, market_data: dict):
    """生成所有圖表腳本"""
    scripts = []
    
    # 個股K線
    for stock_id, data in stocks_data.items():
        df = data["df"]
        dates = [d.strftime("%m-%d") for d in df.index[-30:]]
        closes = df["Close"].tail(30).tolist()
        
        scripts.append(f"""
new Chart(document.getElementById('k{stock_id}'),{{
  type:'line',
  data:{{labels:{json.dumps(dates)},datasets:[{{label:'收盤',data:{json.dumps(closes)},borderColor:'#378ADD',borderWidth:2,tension:0.3,pointRadius:2,fill:false}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{y:{{ticks:{{font:{{size:11}}}}}},x:{{ticks:{{font:{{size:10}},maxRotation:0,autoSkip:true}}}}}}}}
}});""")
    
    # 大盤K線
    taiex = market_data["taiex"]
    if taiex:
        taiex_dates = [d["date"][-5:] for d in taiex]
        taiex_closes = [d["close"] for d in taiex]
        
        scripts.append(f"""
new Chart(document.getElementById('taiex'),{{
  type:'line',
  data:{{labels:{json.dumps(taiex_dates)},datasets:[{{label:'加權指數',data:{json.dumps(taiex_closes)},borderColor:'#378ADD',borderWidth:2,tension:0.3,fill:false}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}}}}
}});""")
    
    # 散戶多空比
    retail = market_data["retail"]
    if retail:
        retail_dates = [d["date"][-5:] for d in retail]
        retail_ratio = []
        for d in retail:
            long_vol = float(d.get("long_volume", 0))
            short_vol = float(d.get("short_volume", 1))
            ratio = (long_vol / (long_vol + short_vol) * 100) if (long_vol + short_vol) > 0 else 50
            retail_ratio.append(ratio)
        
        scripts.append(f"""
new Chart(document.getElementById('retail'),{{
  type:'line',
  data:{{labels:{json.dumps(retail_dates)},datasets:[{{label:'散戶多單%',data:{json.dumps(retail_ratio)},borderColor:'#EF5350',backgroundColor:'rgba(239,83,80,0.1)',borderWidth:2,fill:true,tension:0.3}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}}}}
}});""")
    
    # 匯率
    fx = market_data["usd_twd"]
    if fx:
        fx_dates = [d["date"][-5:] for d in fx]
        fx_values = [float(d.get("close", 0)) for d in fx]
        
        scripts.append(f"""
new Chart(document.getElementById('fx'),{{
  type:'line',
  data:{{labels:{json.dumps(fx_dates)},datasets:[{{label:'USD/TWD',data:{json.dumps(fx_values)},borderColor:'#378ADD',borderWidth:2,tension:0.3}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}}}}
}});""")
    
    # 三大法人
    inst = market_data["institution"]
    if inst:
        inst_dates = [d["date"][-5:] for d in inst]
        foreign = [float(d.get("Foreign_Investor_diff", 0))/100000 for d in inst]
        trust = [float(d.get("Investment_Trust_diff", 0))/100000 for d in inst]
        
        scripts.append(f"""
new Chart(document.getElementById('inst'),{{
  type:'bar',
  data:{{labels:{json.dumps(inst_dates)},datasets:[{{label:'外資(億)',data:{json.dumps(foreign)},backgroundColor:'rgba(55,138,221,0.7)'}},{{label:'投信(億)',data:{json.dumps(trust)},backgroundColor:'rgba(29,158,117,0.7)'}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:true,position:'top'}}}}}}
}});""")
    
    return "\n".join(scripts)


def get_css():
    """CSS樣式"""
    return """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f5;padding:16px;line-height:1.5;color:#333}
.container{max-width:1400px;margin:0 auto}
.header{background:white;border-radius:12px;border:0.5px solid #e0e0e0;padding:1.25rem;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:22px;font-weight:500}
.update-time{font-size:13px;color:#666}
.section-header{font-size:18px;font-weight:500;margin:24px 0 16px 0}
.card{background:white;border-radius:12px;border:0.5px solid #e0e0e0;padding:1rem;margin-bottom:16px}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding-bottom:12px;border-bottom:0.5px solid #e0e0e0}
.card-header h3{font-size:18px;font-weight:500}
.card-price{font-size:20px;font-weight:500}
.card-title{font-size:15px;font-weight:500;margin-bottom:12px}
.positive{color:#d32f2f}
.negative{color:#388e3c}
.metrics-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:16px}
.metrics-grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.metrics-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
.metric{background:#f5f5f5;border-radius:8px;padding:12px}
.metric-label{font-size:11px;color:#666;margin-bottom:4px}
.metric-value{font-size:16px;font-weight:500}
.metric-change{font-size:12px;margin-top:2px}
.metric-sm{background:#f5f5f5;border-radius:8px;padding:10px;text-align:center}
.metric-sm-label{font-size:11px;color:#666;margin-bottom:4px}
.metric-sm-value{font-size:14px;font-weight:500}
.metric-sm-change{font-size:11px;margin-top:2px}
.chart-container{position:relative;height:280px;margin-bottom:12px}
.stock-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.analysis-box{background:#f5f5f5;border-radius:8px;padding:12px;margin-bottom:12px}
.analysis-title{font-size:13px;font-weight:500;margin-bottom:8px}
.analysis-text{font-size:13px;color:#666;line-height:1.6}
.section-title{font-size:13px;font-weight:500;margin:12px 0 8px 0}
.table{width:100%;font-size:12px;border-collapse:collapse}
.table th{text-align:left;padding:8px;background:#f5f5f5;color:#666;font-weight:500}
.table td{padding:8px;border-bottom:0.5px solid #e0e0e0}
.news-list{max-height:200px;overflow-y:auto}
.news-item{padding:8px 0;border-bottom:0.5px solid #e0e0e0}
.news-item:last-child{border-bottom:none}
.news-time{font-size:11px;color:#666;margin-bottom:4px}
.news-title{font-size:13px;line-height:1.4}
@media (max-width:1024px){.grid-2{grid-template-columns:1fr}.metrics-grid-4{grid-template-columns:repeat(2,1fr)}.stock-grid{grid-template-columns:1fr}}
"""


# =========================================================
# Telegram 推播
# =========================================================

def send_telegram(text: str):
    """發送 Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"  ⚠️ Telegram 失敗: {e}")


# =========================================================
# 主流程
# =========================================================

def main():
    print(f"=== 股票監控機器人 v4.1 ({datetime.now().strftime('%Y-%m-%d %H:%M')}) ===\n")
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # 1. 抓個股
    print("[1/4] 抓取個股資料...")
    stocks_data = {}
    
    for stock_id in STOCKS:
        print(f"  處理 {stock_id}...")
        
        stock_data = get_stock_data_yf(stock_id)
        if not stock_data:
            continue
        
        institution = get_institution_data(stock_id)
        margin = get_margin_data(stock_id)
        news = get_news(stock_id)
        name = get_stock_name(stock_id)
        
        print(f"    生成 AI 分析...")
        ai_analysis = generate_ai_analysis(stock_id, name, stock_data, institution, margin)
        
        stocks_data[stock_id] = {
            "latest": stock_data["latest"],
            "prev": stock_data["prev"],
            "df": stock_data["df"],
            "indicators": stock_data["indicators"],
            "institution": institution,
            "margin": margin,
            "news": news,
            "ai_analysis": ai_analysis,
            "name": name,
        }
    
    # 2. 抓大盤
    print("\n[2/4] 抓取大盤資料...")
    market_data = get_market_overview()
    
    # 3. 生成 HTML
    print("\n[3/4] 生成 HTML...")
    html = generate_html(stocks_data, market_data)
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    
    print(f"  ✓ {OUTPUT_FILE}")
    
    # 4. Git push
    print("\n[4/4] 更新 GitHub Pages...")
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", OUTPUT_DIR], check=True)
        
        result = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if result.returncode == 0:
            print("  無變動")
        else:
            subprocess.run(["git", "commit", "-m", f"Update {datetime.now().strftime('%Y-%m-%d %H:%M')}"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("  ✓ 已更新")
    except Exception as e:
        print(f"  ⚠️ Git 失敗: {e}")
    
    # Telegram
    tg_msg = f"📊 *股票監控* ({datetime.now().strftime('%m-%d')})\n\n"
    for stock_id, data in stocks_data.items():
        close = data["latest"]["close"]
        foreign = data["institution"]["foreign_today"]
        tg_msg += f"*{stock_id}* ${close:.2f} | 外資{foreign:+.0f}張\n"
    tg_msg += f"\n🌐 https://ewq4303-debug.github.io/stocknotice/"
    
    send_telegram(tg_msg)
    
    print("\n✅ 完成！")


if __name__ == "__main__":
    main()
