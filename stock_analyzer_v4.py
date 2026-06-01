"""
股票監控機器人 v4.3 - 終極版 (整合動態大戶資金分級、籌碼可視化、基本面與法人參與率)
"""

import os
import json
import requests
import subprocess
from datetime import datetime, timedelta, timezone
import yfinance as yf
import pandas as pd
import concurrent.futures

_gemini_quota_exhausted = False

# 台灣時區 (UTC+8)
TW_TZ = timezone(timedelta(hours=8))

def now_tw():
    return datetime.now(TW_TZ)

# ===== 設定 =====
def load_stocks():
    if os.path.exists("stocks.txt"):
        with open("stocks.txt", "r", encoding="utf-8") as f:
            stocks = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        return stocks if stocks else ["2330", "2454", "2317"]
    else:
        return os.getenv("STOCKS", "2330,2454,2317").split(",")

STOCKS = load_stocks()
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
AI_PROVIDER    = os.getenv("AI_PROVIDER", "claude").lower()
CLAUDE_MODEL   = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20240620")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
OUTPUT_DIR = "docs"
OUTPUT_FILE = f"{OUTPUT_DIR}/index.html"


# =========================================================
# 技術指標計算
# =========================================================

def calculate_sma(series, period):
    return series.rolling(window=period).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

def calculate_stochastic(high, low, close, period=14, smooth_k=3, smooth_d=3):
    lowest_low = low.rolling(window=period).min()
    highest_high = high.rolling(window=period).max()
    k = 100 * ((close - lowest_low) / (highest_high - lowest_low))
    k = k.rolling(window=smooth_k).mean()
    d = k.rolling(window=smooth_d).mean()
    return k, d

def calculate_atr(high, low, close, period=10):
    hl = high - low
    hc = (high - close.shift(1)).abs()
    lc = (low - close.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    
    atr = [float('nan')] * len(tr)
    tr_vals = tr.tolist()
    
    sma = tr.rolling(window=period).mean().tolist()
    
    for i in range(len(tr_vals)):
        if pd.notna(sma[i]) and pd.isna(atr[i-1] if i > 0 else float('nan')):
            atr[i] = sma[i]
        elif i > 0 and pd.notna(atr[i-1]):
            atr[i] = (tr_vals[i] + (period - 1) * atr[i-1]) / period
            
    return pd.Series(atr, index=tr.index)

def calculate_supertrend(df, period=10, multiplier=3):
    high  = df['High'].tolist()
    low   = df['Low'].tolist()
    close = df['Close'].tolist()
    
    atr = calculate_atr(df['High'], df['Low'], df['Close'], period).tolist()
    
    n = len(df)
    basic_upper = [float('nan')] * n
    basic_lower = [float('nan')] * n
    
    for i in range(n):
        if pd.notna(atr[i]):
            hl2 = (high[i] + low[i]) / 2
            basic_upper[i] = hl2 + multiplier * atr[i]
            basic_lower[i] = hl2 - multiplier * atr[i]
            
    final_upper = [float('nan')] * n
    final_lower = [float('nan')] * n
    supertrend  = [float('nan')] * n
    direction   = [0] * n
    
    for i in range(1, n):
        if pd.isna(atr[i]):
            continue
        
        if pd.isna(final_upper[i-1]):
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            supertrend[i]  = basic_upper[i]
            direction[i]   = -1
            continue
            
        if basic_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i-1]
            
        if basic_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i-1]
            
        if supertrend[i-1] == final_upper[i-1]:
            if close[i] > final_upper[i]:
                direction[i] = 1
                supertrend[i] = final_lower[i]
            else:
                direction[i] = -1
                supertrend[i] = final_upper[i]
        elif supertrend[i-1] == final_lower[i-1]:
            if close[i] < final_lower[i]:
                direction[i] = -1
                supertrend[i] = final_upper[i]
            else:
                direction[i] = 1
                supertrend[i] = final_lower[i]
                
    return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)

# =========================================================
# 資料抓取
# =========================================================

def get_stock_data_yf(stock_id: str, days: int = 90):
    df = None
    suffix_used = None
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days+200)
    
    for suffix in (".TW", ".TWO"):
        try:
            ticker = yf.Ticker(f"{stock_id}{suffix}")
            temp_df = ticker.history(start=start_date, end=end_date)
            if not temp_df.empty:
                df = temp_df
                suffix_used = suffix
                break
        except Exception:
            continue
    
    if df is None or df.empty:
        return None
    
    try:
        df = df[df["Close"] > 0].copy()
        
        df['SMA_5']  = calculate_sma(df['Close'], 5)
        df['SMA_10'] = calculate_sma(df['Close'], 10)
        df['SMA_20'] = calculate_sma(df['Close'], 20)
        df['SMA_60'] = calculate_sma(df['Close'], 60)
        df['RSI_14'] = calculate_rsi(df['Close'], 14)
        df['MACD'], df['MACD_Signal'] = calculate_macd(df['Close'])
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
        df['K'], df['D'] = calculate_stochastic(df['High'], df['Low'], df['Close'])
        
        df['ST'], df['ST_DIR'] = calculate_supertrend(df, period=10, multiplier=3)
        df['Vol_MA20']    = df['Volume'].rolling(20).mean()
        df['Vol_MA5']     = df['Volume'].rolling(5).mean()
        df['High_20']     = df['Close'].rolling(20).max()

        df = df.tail(days)
        latest = df.iloc[-1]
        prev   = df.iloc[-2] if len(df) > 1 else latest
        
        def _f(v, default=0.0):
            return float(v) if pd.notna(v) else default
        
        return {
            "stock_id": stock_id,
            "df": df,
            "latest": {
                "close":  _f(latest["Close"]),
                "volume": int(latest["Volume"]) if pd.notna(latest["Volume"]) else 0,
                "high":   _f(latest["High"]),
                "low":    _f(latest["Low"]),
                "open":   _f(latest["Open"]),
            },
            "prev": {"close": _f(prev["Close"])},
            "indicators": {
                "ma5":         _f(latest.get("SMA_5")),
                "ma10":        _f(latest.get("SMA_10")),
                "ma20":        _f(latest.get("SMA_20")),
                "ma60":        _f(latest.get("SMA_60")),
                "rsi":         _f(latest.get("RSI_14"), 50),
                "k":           _f(latest.get("K"), 50),
                "d":           _f(latest.get("D"), 50),
                "macd":        _f(latest.get("MACD")),
                "macd_signal": _f(latest.get("MACD_Signal")),
                "macd_hist":       _f(latest.get("MACD_Hist")),
                "macd_hist_prev":  _f(prev.get("MACD_Hist")),
                "supertrend":      _f(latest.get("ST")),
                "supertrend_dir":  int(latest.get("ST_DIR")) if pd.notna(latest.get("ST_DIR")) else 0,
                "vol_ma5":     _f(latest.get("Vol_MA5")),
                "vol_ma20":    _f(latest.get("Vol_MA20")),
                "high_20":     _f(latest.get("High_20")),
            },
        }
    except Exception:
        return None

def fetch_finmind(dataset: str, **params):
    if not FINMIND_TOKEN: return []
    try:
        p = {"dataset": dataset, "token": FINMIND_TOKEN, **params}
        r = requests.get(FINMIND_URL, params=p, timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception:
        return []

def get_institution_data(stock_id: str, days: int = 120):
    start = (datetime.now() - timedelta(days=days+60)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    
    raw = []
    for api_name in ["TaiwanStockInstitutionalInvestorsBuySell", "TaiwanStockInstitutionalInvestors"]:
        raw = fetch_finmind(api_name, data_id=stock_id, start_date=start, end_date=end)
        if raw: break
    
    if not raw:
        return {"latest": {}, "foreign_today": 0, "foreign_5d": 0, "foreign_20d": 0, "trust_today": 0, "trust_5d": 0, "trust_20d": 0, "dealer_today": 0, "history": []}
    
    by_date = {}
    for d in raw:
        date = d.get("date", "")
        if not date: continue
        if date not in by_date:
            by_date[date] = {
                "date": date, "foreign": 0, "trust": 0, "dealer": 0,
                "foreign_buy": 0, "foreign_sell": 0, "trust_buy": 0, "trust_sell": 0, "dealer_buy": 0, "dealer_sell": 0
            }
        
        name = d.get("name", "")
        if name:
            buy = float(d.get("buy", 0))
            sell = float(d.get("sell", 0))
            diff = buy - sell
            name_low = name.lower()
            if "外" in name or "foreign" in name_low: 
                by_date[date]["foreign"] += diff
                by_date[date]["foreign_buy"] += buy
                by_date[date]["foreign_sell"] += sell
            elif "投信" in name or "trust" in name_low: 
                by_date[date]["trust"] += diff
                by_date[date]["trust_buy"] += buy
                by_date[date]["trust_sell"] += sell
            elif "自營" in name or "dealer" in name_low: 
                by_date[date]["dealer"] += diff
                by_date[date]["dealer_buy"] += buy
                by_date[date]["dealer_sell"] += sell
        else:
            by_date[date]["foreign"] += float(d.get("Foreign_Investor_diff", d.get("foreign_investor_diff", 0)))
            by_date[date]["trust"]   += float(d.get("Investment_Trust_diff", d.get("investment_trust_diff", 0)))
            by_date[date]["dealer"]  += float(d.get("Dealer_diff", d.get("dealer_diff", 0)))
    
    history = sorted(by_date.values(), key=lambda x: x["date"])[-days:]
    if not history:
        return {"latest": {}, "foreign_today": 0, "foreign_5d": 0, "foreign_20d": 0, "trust_today": 0, "trust_5d": 0, "trust_20d": 0, "dealer_today": 0, "history": []}
    
    latest = history[-1]
    return {
        "latest": latest,
        "foreign_today": latest["foreign"] / 1000,
        "foreign_5d":    sum(h["foreign"] for h in history[-5:])  / 1000,
        "foreign_20d":   sum(h["foreign"] for h in history[-20:]) / 1000,
        "trust_today":   latest["trust"] / 1000,
        "trust_5d":      sum(h["trust"] for h in history[-5:])  / 1000,
        "trust_20d":     sum(h["trust"] for h in history[-20:]) / 1000,
        "dealer_today":  latest["dealer"] / 1000,
        "dealer_5d":     sum(h["dealer"] for h in history[-5:]) / 1000,
        "dealer_20d":    sum(h["dealer"] for h in history[-20:]) / 1000,
        "history": history,
    }

def get_margin_data(stock_id: str, days: int = 120):
    start = (datetime.now() - timedelta(days=days+60)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    data = fetch_finmind("TaiwanStockMarginPurchaseShortSale", data_id=stock_id, start_date=start, end_date=end)
    
    if not data:
        return {"margin_balance": 0, "margin_change": 0, "margin_5d": 0, "margin_20d": 0, "short_balance": 0, "short_change": 0, "short_5d": 0, "short_20d": 0, "history": []}
    
    data = sorted(data, key=lambda x: x.get("date", ""))
    
    def get_balance(d, kind):
        if kind == "margin": return float(d.get("MarginPurchaseTodayBalance", d.get("margin_purchase_today_balance", d.get("MarginPurchaseBuy", 0))))
        else: return float(d.get("ShortSaleTodayBalance", d.get("short_sale_today_balance", d.get("ShortSaleBuy", 0))))
    
    history = []
    for i in range(len(data)):
        d = data[i]
        date, mb, sb = d.get("date", ""), int(get_balance(d, "margin")), int(get_balance(d, "short"))
        mdiff, sdiff = 0, 0
        if i > 0:
            mdiff = mb - int(get_balance(data[i-1], "margin"))
            sdiff = sb - int(get_balance(data[i-1], "short"))
        history.append({"date": date, "margin_bal": mb, "margin_diff": mdiff, "short_bal": sb, "short_diff": sdiff})
        
    latest = data[-1]
    margin_balance = int(get_balance(latest, "margin"))
    short_balance  = int(get_balance(latest, "short"))
    
    def diff_n_days_ago(n, kind):
        if len(data) <= n: return 0
        return int(get_balance(data[-1], kind) - get_balance(data[-n-1], kind))
        
    return {
        "margin_balance": margin_balance, "margin_change": diff_n_days_ago(1, "margin"), "margin_5d": diff_n_days_ago(5, "margin"), "margin_20d": diff_n_days_ago(20, "margin"),
        "short_balance": short_balance, "short_change": diff_n_days_ago(1, "short"), "short_5d": diff_n_days_ago(5, "short"), "short_20d": diff_n_days_ago(20, "short"),
        "history": history[-days:]
    }

def get_borrowing_data(stock_id: str):
    end = now_tw().date()
    start = end - timedelta(days=180)
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": "TaiwanDailyShortSaleBalances", "data_id": stock_id, "start_date": start.isoformat(), "end_date": end.isoformat()}
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}

    def _empty_borrow():
        return {"borrow_balance": 0, "borrow_change":  0, "borrow_5d": 0, "borrow_20d": 0, "history": []}

    try: j = requests.get(url, params=params, headers=headers, timeout=15).json()
    except Exception: return _empty_borrow()
    rows = j.get("data") or []
    if not rows or "SBLShortSalesCurrentDayBalance" not in rows[0]: return _empty_borrow()

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    df["lots"] = (pd.to_numeric(df["SBLShortSalesCurrentDayBalance"], errors="coerce").fillna(0) / 1000).round().astype(int)

    history = []
    for i in range(len(df)):
        date = str(df.iloc[i]["date"])
        bal = int(df.iloc[i]["lots"])
        diff = bal - int(df.iloc[i-1]["lots"]) if i > 0 else 0
