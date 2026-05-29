"""
股票監控機器人 v4.2 - 穩定雙棲版 (整合動態大戶資金分級與籌碼可視化)
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

def calculate_atr(high, low, close, period=7):
    hl = high - low
    hc = (high - close.shift()).abs()
    lc = (low - close.shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calculate_supertrend(df, period=7, multiplier=3):
    high  = df['High'].astype(float).values
    low   = df['Low'].astype(float).values
    close = df['Close'].astype(float).values
    
    atr = calculate_atr(df['High'], df['Low'], df['Close'], period).values
    hl2 = (high + low) / 2
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr
    
    n = len(df)
    final_upper = [0.0] * n
    final_lower = [0.0] * n
    supertrend  = [0.0] * n
    direction   = [0] * n
    
    for i in range(n):
        bu, bl, c = basic_upper[i], basic_lower[i], close[i]
        
        if pd.isna(bu) or pd.isna(bl):
            final_upper[i] = bu
            final_lower[i] = bl
            supertrend[i] = bu
            direction[i] = -1
            continue
        
        if i == 0 or pd.isna(final_upper[i-1]):
            final_upper[i] = bu
            final_lower[i] = bl
            supertrend[i]  = bu
            direction[i]   = -1
            continue
        
        if bu < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            final_upper[i] = bu
        else:
            final_upper[i] = final_upper[i-1]
            
        if bl > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            final_lower[i] = bl
        else:
            final_lower[i] = final_lower[i-1]
            
        prev_st = supertrend[i-1]
        if prev_st == final_upper[i-1]:
            if c <= final_upper[i]:
                supertrend[i], direction[i] = final_upper[i], -1
            else:
                supertrend[i], direction[i] = final_lower[i], 1
        else:
            if c >= final_lower[i]:
                supertrend[i], direction[i] = final_lower[i], 1
            else:
                supertrend[i], direction[i] = final_upper[i], -1
                
    return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)

# =========================================================
# 資料抓取
# =========================================================

def get_stock_data_yf(stock_id: str, days: int = 60):
    df = None
    suffix_used = None
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days+20)
    
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
        df = df.tail(days)
        df = df[df["Close"] > 0].copy()
        df['SMA_5']  = calculate_sma(df['Close'], 5)
        df['SMA_10'] = calculate_sma(df['Close'], 10)
        df['SMA_20'] = calculate_sma(df['Close'], 20)
        df['SMA_60'] = calculate_sma(df['Close'], 60)
        df['RSI_14'] = calculate_rsi(df['Close'], 14)
        df['MACD'], df['MACD_Signal'] = calculate_macd(df['Close'])
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
        df['K'], df['D'] = calculate_stochastic(df['High'], df['Low'], df['Close'])
        df['ST'], df['ST_DIR'] = calculate_supertrend(df, period=7, multiplier=3)
        df['Vol_MA20']    = df['Volume'].rolling(20).mean()
        df['Vol_MA5']     = df['Volume'].rolling(5).mean()
        df['High_20']     = df['Close'].rolling(20).max()
        
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

def get_institution_data(stock_id: str, days: int = 80):
    start = (datetime.now() - timedelta(days=days+30)).strftime("%Y-%m-%d")
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
            by_date[date] = {"date": date, "foreign": 0, "trust": 0, "dealer": 0}
        
        name = d.get("name", "")
        if name:
            diff = float(d.get("buy", 0)) - float(d.get("sell", 0))
            name_low = name.lower()
            if "外" in name or "foreign" in name_low: by_date[date]["foreign"] += diff
            elif "投信" in name or "trust" in name_low: by_date[date]["trust"] += diff
            elif "自營" in name or "dealer" in name_low: by_date[date]["dealer"] += diff
        else:
            by_date[date]["foreign"] += float(d.get("Foreign_Investor_diff", d.get("foreign_investor_diff", 0)))
            by_date[date]["trust"]   += float(d.get("Investment_Trust_diff", d.get("investment_trust_diff", 0)))
            by_date[date]["dealer
