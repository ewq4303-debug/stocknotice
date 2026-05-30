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

def calculate_atr(high, low, close, period=10):
    hl = high - low
    hc = (high - close.shift(1)).abs()
    lc = (low - close.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    
    atr = [float('nan')] * len(tr)
    tr_vals = tr.tolist()
    
    # 完全模擬 TradingView 的 ta.rma()：第一筆使用 SMA 初始化，後續使用 RMA 遞迴
    sma = tr.rolling(window=period).mean().tolist()
    
    for i in range(len(tr_vals)):
        if pd.notna(sma[i]) and pd.isna(atr[i-1] if i > 0 else float('nan')):
            atr[i] = sma[i]  # 初始值
        elif i > 0 and pd.notna(atr[i-1]):
            atr[i] = (tr_vals[i] + (period - 1) * atr[i-1]) / period  # RMA公式
            
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
        
        # 第一筆有效資料初始化
        if pd.isna(final_upper[i-1]):
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            supertrend[i]  = basic_upper[i]
            direction[i]   = -1
            continue
            
        # 計算 Final Upper Band
        if basic_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i-1]
            
        # 計算 Final Lower Band
        if basic_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i-1]
            
        # 決定趨勢方向與指標數值
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

def get_stock_data_yf(stock_id: str, days: int = 90):  # 這裡改為 90
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
        # 【修改 2】先清理資料
        df = df[df["Close"] > 0].copy()
        
        # 【修改 3】在「完整資料」上計算所有指標
        df['SMA_5']  = calculate_sma(df['Close'], 5)
        df['SMA_10'] = calculate_sma(df['Close'], 10)
        df['SMA_20'] = calculate_sma(df['Close'], 20)
        df['SMA_60'] = calculate_sma(df['Close'], 60)
        df['RSI_14'] = calculate_rsi(df['Close'], 14)
        df['MACD'], df['MACD_Signal'] = calculate_macd(df['Close'])
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
        df['K'], df['D'] = calculate_stochastic(df['High'], df['Low'], df['Close'])
        
        # 確認這裡帶入的參數是 (10, 3)
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
        history.append({"date": date, "balance": bal, "diff": diff})

    today_lots = int(df.iloc[-1]["lots"])
    def diff_n_days_ago(n_days):
        if len(df) < n_days + 1: return 0
        return today_lots - int(df.iloc[-(n_days + 1)]["lots"])

    return {"borrow_balance": today_lots, "borrow_change": diff_n_days_ago(1), "borrow_5d": diff_n_days_ago(5), "borrow_20d": diff_n_days_ago(20), "history": history[-120:]}

def get_tdcc_holding(stock_id: str, close_price: float):
    if close_price <= 0: close_price = 100
    retail_shares = 5_000_000 / close_price
    large_shares = 50_000_000 / close_price

    LEVEL_MAX = {
        "1": 999, "2": 5000, "3": 10000, "4": 15000, "5": 20000,
        "6": 30000, "7": 40000, "8": 50000, "9": 100000, "10": 200000,
        "11": 400000, "12": 600000, "13": 800000, "14": 1000000, "15": float('inf')
    }
    
    retail_levels = {k for k, v in LEVEL_MAX.items() if v <= max(retail_shares, 999)}
    if not retail_levels: retail_levels = {"1"}
    large_levels = {k for k, v in LEVEL_MAX.items() if v >= large_shares}
    if not large_levels: large_levels = {"15"}

    GITHUB_BASE = "https://raw.githubusercontent.com/ewq4303-debug/stocknotice/main/tdcc_history"
    
    def fetch_csv(date_str):
        url = f"{GITHUB_BASE}/{date_str}.csv"
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200 or len(r.content) < 1000: return None
            text = r.content.decode("utf-8-sig", errors="replace")
            ret_ratio, lrg_ratio, total_holders, total_shares = 0.0, 0.0, 0, 0
            for line in text.splitlines()[1:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 6: continue
                if parts[1] != stock_id: continue
                level = parts[2]
                try:
                    ratio = float(parts[5])
                    holders = int(parts[3])
                    shares = int(parts[4])
                    if level in retail_levels: ret_ratio += ratio
                    if level in large_levels: lrg_ratio += ratio
                    if level == "17": 
                        total_holders = holders
                        total_shares = shares
                except ValueError: continue
            if total_holders > 0:
                return {"date": date_str, "retail_ratio": ret_ratio, "large_ratio": lrg_ratio, "total_holders": total_holders, "total_shares": total_shares}
            return None
        except: return None

    today = datetime.now()
    cursor = today
    while cursor.weekday() != 4: cursor -= timedelta(days=1)
        
    results = []
    # 改為抓 10 週，且拿掉原本的 break，以保留所有歷史資料
    for _ in range(10):
        data = fetch_csv(cursor.strftime("%Y%m%d"))
        if data:
            results.append(data)
        cursor -= timedelta(days=7)
        
    if not results:
        return {"retail_ratio": 0, "retail_change": 0, "large_ratio": 0, "large_change": 0, "big_holder_change": 0, "total_holders": 0, "holders_change": 0, "avg_lots": 0, "avg_lots_change": 0, "retail_threshold_shares": retail_shares, "large_threshold_shares": large_shares, "history": []}
        
    # 計算所有歷史資料的平均張數
    for r in results:
        r["avg_lots"] = (r["total_shares"] / 1000) / r["total_holders"] if r["total_holders"] else 0

    latest = results[0]
    prev = results[1] if len(results) >= 2 else latest
    
    return {
        "retail_ratio": latest["retail_ratio"],
        "retail_change": latest["retail_ratio"] - prev["retail_ratio"],
        "large_ratio": latest["large_ratio"],
        "large_change": latest["large_ratio"] - prev["large_ratio"],
        "big_holder_change": latest["large_ratio"] - prev["large_ratio"],
        "total_holders": latest["total_holders"],
        "holders_change": latest["total_holders"] - prev["total_holders"],
        "avg_lots": latest["avg_lots"],
        "avg_lots_change": latest["avg_lots"] - prev["avg_lots"],
        "retail_threshold_shares": retail_shares,
        "large_threshold_shares": large_shares,
        "history": results
    }

def _get(d, *keys, default=0.0):
    for k in keys:
        v = d.get(k)
        if v is not None:
            try: return float(v)
            except (ValueError, TypeError): continue
    return float(default)

def get_market_overview():
    start = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    taiex_data = []
    
    fm_taiex = fetch_finmind("TaiwanStockPrice", data_id="TAIEX", start_date=start, end_date=end)
    if fm_taiex:
        for r in sorted(fm_taiex, key=lambda x: x.get("date", ""))[-90:]:
            taiex_data.append({
                "date":  r.get("date", ""),
                "open":  float(r.get("open", r.get("Open", 0))),
                "high":  float(r.get("max",  r.get("High", r.get("high", 0)))),
                "low":   float(r.get("min",  r.get("Low",  r.get("low", 0)))),
                "close": float(r.get("close", r.get("Close", 0))),
                "volume": int(float(r.get("Trading_Volume", r.get("trading_volume", 0)) or 0)),
                "money":  float(r.get("Trading_money", r.get("trading_money", 0)) or 0),
            })
        if len(taiex_data) >= 10:
            taiex_df = pd.DataFrame([{"Open": d["open"], "High": d["high"], "Low": d["low"], "Close": d["close"]} for d in taiex_data])
            taiex_df["SMA_20"] = calculate_sma(taiex_df["Close"], 20)
            st_series, st_dir_series = calculate_supertrend(taiex_df, period=10, multiplier=3)
            k_series, d_series = calculate_stochastic(taiex_df["High"], taiex_df["Low"], taiex_df["Close"], 14, 3, 3)
            for i, d in enumerate(taiex_data):
                v, dr = st_series.iloc[i], st_dir_series.iloc[i]
                d["supertrend"]     = float(v) if pd.notna(v) else None
                d["supertrend_dir"] = int(dr) if pd.notna(dr) else 0
                sma = taiex_df["SMA_20"].iloc[i]
                d["ma20"] = round(float(sma), 2) if pd.notna(sma) else None
                
                k_val, d_val = k_series.iloc[i], d_series.iloc[i]
                d["k"] = round(float(k_val), 2) if pd.notna(k_val) else None
                d["d"] = round(float(d_val), 2) if pd.notna(d_val) else None
              
    need_yf = (not taiex_data) or any(d["open"] == 0 for d in taiex_data[-5:])
    if need_yf:
        try:
            taiex_ticker = yf.Ticker("^TWII")
            temp_df = taiex_ticker.history(period="6mo")
            temp_df["SMA_20"] = calculate_sma(temp_df["Close"], 20)
            st_series, st_dir_series = calculate_supertrend(temp_df, period=10, multiplier=3)
            temp_df["ST"] = st_series
            temp_df["ST_DIR"] = st_dir_series
            taiex_df = temp_df.tail(90).reset_index()
            
            if not taiex_data:
                money_by_date = {r.get("date", ""): float(r.get("Trading_money", 0) or 0) for r in (fm_taiex or [])}
                for _, row in taiex_df.iterrows():
                    date_str = row["Date"].strftime("%Y-%m-%d")
                    sma = row["SMA_20"]
                    v, dr = row["ST"], row["ST_DIR"]
                    taiex_data.append({
                        "date":  date_str, "open": float(row["Open"]), "high": float(row["High"]),
                        "low":   float(row["Low"]), "close": float(row["Close"]), "volume": int(row["Volume"] or 0),
                        "money":  money_by_date.get(date_str, 0),
                        "ma20": round(float(sma), 2) if pd.notna(sma) else None,
                        "supertrend": float(v) if pd.notna(v) else None,
                        "supertrend_dir": int(dr) if pd.notna(dr) else 0
                    })
        except Exception: pass
    
    inst_raw = fetch_finmind("TaiwanStockTotalInstitutionalInvestors", start_date=start, end_date=end)
    inst_by_date = {}
    for d in inst_raw:
        date = d.get("date", "")
        name = d.get("name", "")
        diff = float(d.get("buy", 0)) - float(d.get("sell", 0))
        if date not in inst_by_date:
            inst_by_date[date] = {"date": date, "Foreign_Investor_diff": 0, "Investment_Trust_diff": 0, "Dealer_diff": 0}
        name_low = name.lower()
        if "外" in name or "foreign" in name_low: inst_by_date[date]["Foreign_Investor_diff"] += diff
        elif "投信" in name or "trust" in name_low: inst_by_date[date]["Investment_Trust_diff"] += diff
        elif "自營" in name or "dealer" in name_low: inst_by_date[date]["Dealer_diff"] += diff
    
    institution = sorted(inst_by_date.values(), key=lambda x: x["date"])[-90:]
    futures = fetch_finmind("TaiwanFuturesInstitutionalInvestors", data_id="TX", start_date=start, end_date=end)
    futures = sorted(futures, key=lambda x: x.get("date", ""))[-90:] if futures else []
    usd_twd = get_usd_twd_rate(days=150)[-90:]
    
    tmf_inst = fetch_finmind("TaiwanFuturesInstitutionalInvestors", data_id="TMF", start_date=start, end_date=end)
    tmf_daily = fetch_finmind("TaiwanFuturesDaily", data_id="TMF", start_date=start, end_date=end)
    inst_net_by_date = {}
    for r in tmf_inst:
        date = r.get("date", "")
        if not any(k in r.get("institutional_investors", r.get("name", "")) for k in {"外資及陸資", "外資", "投信", "自營商"}): continue
        inst_net_by_date[date] = inst_net_by_date.get(date, 0) + (float(r.get("long_open_interest_balance_volume", 0)) - float(r.get("short_open_interest_balance_volume", 0)))
    
    total_oi_by_date = {}
    for r in tmf_daily:
        total_oi_by_date[r.get("date", "")] = total_oi_by_date.get(r.get("date", ""), 0) + float(r.get("open_interest", 0))
    
    retail = []
    for date in sorted(total_oi_by_date.keys()):
        total_oi = total_oi_by_date[date]
        if total_oi == 0: continue
        retail_net = -inst_net_by_date.get(date, 0)
        retail.append({"date": date, "retail_ratio": round((retail_net / total_oi) * 100, 2)})
    retail = retail[-90:]
    
    margin_raw = fetch_finmind("TaiwanStockTotalMarginPurchaseShortSale", start_date=start, end_date=end)
    total_margin = []
    if margin_raw:
        margin_df = pd.DataFrame(margin_raw)
        margin_df = margin_df[margin_df['name'].astype(str).str.lower() == 'marginpurchasemoney']
        if not margin_df.empty:
            total_margin = [{"date": str(r["date"]), "TodayBalance": int(float(r["TodayBalance"]))} for _, r in margin_df.sort_values("date").tail(90).iterrows()]
    
    return {"taiex": taiex_data, "institution": institution, "futures": futures, "usd_twd": usd_twd, "retail": retail, "total_margin": total_margin}

def get_usd_twd_rate(days: int = 150):
    result = []
    today = datetime.now()
    cutoff = today - timedelta(days=days)
    years_needed = list({cutoff.year, today.year})
    
    for year in sorted(years_needed):
        url = f"https://www.tpefx.com.tw/uploads/service/tw/{year}nt.csv"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            content = None
            for enc in ("utf-8", "big5", "cp950", "utf-8-sig"):
                try: content = r.content.decode(enc); break
                except UnicodeDecodeError: continue
            if not content: continue
            
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if not lines: continue
            
            header = None
            data_start = 0
            for i, line in enumerate(lines):
                cols = [c.strip().lower() for c in line.split(",")]
                if any("close" in c for c in cols):
                    header = [c.strip() for c in line.split(",")]
                    data_start = i + 1
                    break
            if not header:
                header = [c.strip() for c in lines[0].split(",")]
                data_start = 1
            
            close_idx, date_idx = None, 0
            for i, h in enumerate(header):
                if h.lower() in ("close", "收盤", "收盤價"): close_idx = i
                if h.lower() in ("date", "日期", "交易日"): date_idx = i
            if close_idx is None: close_idx = len(header) - 1
            
            for line in lines[data_start:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) <= close_idx: continue
                raw_date, raw_close = parts[date_idx], parts[close_idx]
                if not raw_close or raw_close in ("-", "N/A", ""): continue
                try:
                    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                        try:
                            dt = datetime.strptime(raw_date, fmt)
                            date_str = dt.strftime("%Y-%m-%d")
                            break
                        except ValueError: continue
                    else: continue
                    if dt < cutoff: continue
                    result.append({"date": date_str, "close": float(raw_close.replace(",", ""))})
                except: continue
        except: continue
    
    seen, unique = set(), []
    for d in sorted(result, key=lambda x: x["date"]):
        if d["date"] not in seen:
            seen.add(d["date"])
            unique.append(d)
    return unique[-days:]

def get_news(stock_id: str, limit: int = 5):
    start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    news = fetch_finmind("TaiwanStockNews", data_id=stock_id, start_date=start)
    return sorted(news, key=lambda x: x.get("date", ""), reverse=True)[:limit]

def get_stock_name(stock_id: str):
    try:
        resp = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockInfo","data_id": stock_id, "token": FINMIND_TOKEN})
        return resp.json()["data"][0]["stock_name"] 
    except: return stock_id

def get_fundamentals(stock_id: str):
    result = {"trailing_pe": None, "forward_pe": None, "peg": None, "eps_ttm": None, "eps_forward": None, "eps_growth": None, "revenue_growth": None, "market_cap": None, "dividend_yield": None, "roe": None}
    for suffix in (".TW", ".TWO"):
        try:
            info = yf.Ticker(f"{stock_id}{suffix}").info
            if not info or len(info) < 5: continue
            result["trailing_pe"]    = info.get("trailingPE")
            result["forward_pe"]     = info.get("forwardPE")
            result["peg"]            = info.get("trailingPegRatio") or info.get("pegRatio")
            result["eps_ttm"]        = info.get("trailingEps") or info.get("epsTrailingTwelveMonths")
            result["eps_forward"]    = info.get("forwardEps")
            result["eps_growth"]     = info.get("earningsGrowth")
            result["revenue_growth"] = info.get("revenueGrowth")
            result["market_cap"]     = info.get("marketCap")
            div_y                    = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
            if div_y is not None: result["dividend_yield"] = div_y * 100 if div_y < 1 else div_y
            result["roe"]            = info.get("returnOnEquity")
            return result
        except: continue
    return result

# =========================================================
# AI 分析
# =========================================================
def _call_claude(prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return client.messages.create(model=CLAUDE_MODEL, max_tokens=1200, messages=[{"role": "user", "content": prompt}]).content[0].text

def _call_gemini(prompt: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY)
    return client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=types.GenerateContentConfig(max_output_tokens=4096, temperature=0.7)).text
  
def generate_ai_analysis(stock_id: str, stock_name: str, data: dict, institution: dict, margin: dict, borrow: dict, tdcc: dict):
    global _gemini_quota_exhausted
    if AI_PROVIDER == "gemini":
        if not GEMINI_API_KEY: return "未設定 API", "未設定 API", "未設定 API"
        if _gemini_quota_exhausted: return "配額用完", "配額用完", "配額用完"
        import time; time.sleep(4)
    else:
        if not ANTHROPIC_API_KEY: return "未設定 API", "未設定 API", "未設定 API"
    
    latest, prev, ind = data["latest"], data["prev"], data["indicators"]
    prompt = f"""請分析 {stock_id} {stock_name} 的當前狀況。

## 技術面
- 收盤: {latest['close']:.2f} (前日: {prev['close']:.2f})
- 5/10/20/60MA: {ind.get('ma5',0):.2f} / {ind.get('ma10',0):.2f} / {ind.get('ma20',0):.2f} / {ind.get('ma60',0):.2f}
- Supertrend(7,3): {ind.get('supertrend',0):.2f} ({'↑上升' if ind.get('supertrend_dir')==1 else '↓下降'})
- MACD 柱體: {ind.get('macd_hist',0):+.3f}
- 5日均量 vs 20日均量: {ind.get('vol_ma5',0):,.0f} vs {ind.get('vol_ma20',0):,.0f}

## 籌碼面
- 外資 今日/5日/20日: {institution.get('foreign_today',0):+.0f} / {institution.get('foreign_5d',0):+.0f} / {institution.get('foreign_20d',0):+.0f} 張
- 投信 今日/5日/20日: {institution.get('trust_today',0):+.0f} / {institution.get('trust_5d',0):+.0f} / {institution.get('trust_20d',0):+.0f} 張
- 融資 餘額/今日/5日: {margin.get('margin_balance',0):,} / {margin.get('margin_change',0):+,} / {margin.get('margin_5d',0):+,} 張
- 借券 餘額/5日: {borrow.get('borrow_balance',0):,} / {borrow.get('borrow_5d',0):+,} 張
- 集保 資金大戶比例: {tdcc.get('large_ratio',0):.2f}% (週變化: {tdcc.get('large_change',0):+.2f}%)

請用繁體中文嚴格按照以下格式輸出三段分析,不要輸出額外說明。
=== 技術面 ===
[60字]
=== 籌碼面 ===
[60字]
=== 操作建議 ===
[60字]"""
    try:
        text = _call_gemini(prompt) if AI_PROVIDER == "gemini" else _call_claude(prompt)
        sections = {"技術面": "", "籌碼面": "", "操作建議": ""}
        current = None
        for line in text.split("\n"):
            stripped = line.strip()
            if "===" in stripped:
                for key in sections:
                    if key in stripped: current = key; break
            elif current and stripped: sections[current] += line + "\n"
        return sections["技術面"].strip() or text, sections["籌碼面"].strip(), sections["操作建議"].strip()
    except Exception as e:
        if AI_PROVIDER == "gemini" and ("429" in str(e) or "quota" in str(e).lower()): _gemini_quota_exhausted = True
        return "分析失敗", "分析失敗", "分析失敗"

# =========================================================
# HTML 生成
# =========================================================

def calculate_stock_rating(data: dict) -> dict:
    ind, inst, margin, borrow, tdcc, latest = data.get("indicators",{}), data.get("institution",{}), data.get("margin",{}), data.get("borrow",{}), data.get("tdcc",{}), data.get("latest",{})
    tech = 0.0
    close, ma20, ma60, ma5, ma10 = latest.get("close",0), ind.get("ma20",0), ind.get("ma60",0), ind.get("ma5",0), ind.get("ma10",0)
    if close > ma20 > 0: tech += 1
    if ma20 > ma60 > 0: tech += 1
    if ma5 > ma10 > 0: tech += 1
    if ind.get("high_20",0) > 0 and close >= ind.get("high_20",0)*0.97: tech += 1.5
    if latest.get("volume",0) > ind.get("vol_ma5",0) > 0: tech += 1.5
    if ind.get("macd_hist",0) > 0: tech += 2
    if ind.get("vol_ma5",0) > ind.get("vol_ma20",0) > 0: tech += 2
    
    chip = 0.0
    last3 = inst.get("history", [])[-3:]
    if sum(h.get("foreign",0) for h in last3) > 0: chip += 1.5
    if sum(h.get("trust",0) for h in last3) > 0: chip += 1.5
    if inst.get("foreign_today",0) > 0 and inst.get("trust_today",0) > 0: chip += 1
    if margin.get("margin_change",0) < 0: chip += 1.5
    if borrow.get("borrow_change",0) < 0: chip += 1.5
    if tdcc.get("large_change",0) > 0: chip += 3
    
    total = tech + chip
    if total >= 14: rating, rk = "強力加碼", "strong-buy"
    elif total >= 10: rating, rk = "加碼", "buy"
    elif total >= 6: rating, rk = "中性觀望", "neutral"
    elif total >= 3: rating, rk = "減碼", "sell"
    else: rating, rk = "強力減碼", "strong-sell"
    return {"tech": round(tech,1), "chip": round(chip,1), "total": round(total,1), "rating": rating, "rating_key": rk}

def generate_rating_table(stocks_data: dict) -> str:
    groups = {"strong-buy": {"label": "強力加碼", "stocks": [], "icon": "ti-arrow-big-up-filled"}, "buy": {"label": "加碼", "stocks": [], "icon": "ti-arrow-up"}, "neutral": {"label": "中性", "stocks": [], "icon": "ti-minus"}, "sell": {"label": "減碼", "stocks": [], "icon": "ti-arrow-down"}, "strong-sell": {"label": "強力減碼", "stocks": [], "icon": "ti-arrow-big-down-filled"}}
    for sid, data in stocks_data.items():
        r = data.get("rating", {})
        key = r.get("rating_key", "neutral")
        if key in groups: groups[key]["stocks"].append({"stock_id": sid, "name": data.get("name",""), "change": data.get("change_pct",0), "tech": r.get("tech",0), "chip": r.get("chip",0), "total": r.get("total",0)})
    for g in groups.values(): g["stocks"].sort(key=lambda s: -s["total"])
    cols_html = ""
    for key, g in groups.items():
        stocks = g["stocks"]
        chips_html = ""
        for s in stocks:
            chips_html += f'<div class="stock-chip"><div class="chip-top"><span class="chip-name">{s["stock_id"]} {s["name"]}</span><span class="chip-change {"up" if s["change"]>=0 else "down"}">{"+" if s["change"]>=0 else ""}{s["change"]:.2f}%</span></div><div class="chip-meta"><span class="chip-tag">技{s["tech"]:g}</span><span class="chip-tag">籌{s["chip"]:g}</span></div></div>'
        if not stocks: chips_html = '<div class="empty-hint">無</div>'
        cols_html += f'<div class="rating-col rating-col-{key}"><div class="rating-col-header"><span class="rating-col-label rating-label-{key}"><i class="ti {g["icon"]}" style="font-size:11px;"></i>{g["label"]}</span><span class="rating-col-count">{len(stocks)}</span></div>{chips_html}</div>'
    return f"""<div class="rating-section"><div class="rating-header"><div class="rating-title">📊 個股操作建議綜合評等</div><div class="rating-update">更新於 {now_tw().strftime("%Y-%m-%d %H:%M")}</div></div><div class="rating-grid">{cols_html}</div><details class="compact-legend"><summary>ℹ️ 點此查看評分邏輯與門檻</summary><div class="legend-content"><div class="legend-row"><span class="legend-badge l-tech">技術(10)</span><span>站月線(+1) / 月>季(+1) / 5>10(+1) / 創高(+1.5) / 量>5均(+1.5) / MACD紅(+2) / 5日量>20日量(+2)</span></div><div class="legend-row"><span class="legend-badge l-chip">籌碼(10)</span><span>外資3日(+1.5) / 投信3日(+1.5) / 同買(+1) / 融資減(+1.5) / 借券減(+1.5) / 大戶增(+3)</span></div><div class="legend-row"><span class="legend-badge l-total">總分門檻</span><span>≥14 強力加碼 | 10~13 加碼 | 6~9 觀望 | 3~5 減碼 | ≤2 強力減碼</span></div></div></details></div>"""

def generate_stock_card(stock_id: str, data: dict, is_first: bool = False) -> str:
    latest, ind, inst, margin, borrow, tdcc, news, r = data["latest"], data["indicators"], data["institution"], data["margin"], data.get("borrow", {}), data.get("tdcc", {}), data.get("news", []), data["rating"]
    c_cls = "up" if data.get("change_pct",0) >= 0 else "down"
    c_sign = "+" if data.get("change_pct",0) >= 0 else ""
    change_str = f"{c_sign}{data.get('change_pct',0):.2f}%"
    close_price = latest.get('close',0)
    tt_today, tt_5d, tt_20d = inst.get("foreign_today",0)+inst.get("trust_today",0)+inst.get("dealer_today",0), inst.get("foreign_5d",0)+inst.get("trust_5d",0)+inst.get("dealer_5d",0), inst.get("foreign_20d",0)+inst.get("trust_20d",0)+inst.get("dealer_20d",0)
    
    news_html = ""
    for n in news[:5]: news_html += f'<div style="padding:10px 0; border-bottom:1px dashed #eee;"><div style="font-size:11px; color:#999; margin-bottom:4px;">{n.get("date","")}</div><a href="{n.get("url", n.get("link", "#"))}" target="_blank" style="font-size:13px; font-weight:500; color:#333; text-decoration:none; display:block;">{n.get("title","")}</a></div>'
    if not news: news_html = "<div style='padding:10px 0; color:#999; font-size:12px;'>近期無相關新聞</div>"

    # 【新增邏輯】建立法人日資料的字典，方便查找當週四、五的資料 (單位：張)
    inst_history = inst.get("history", [])
    inst_dict = {}
    for d in inst_history:
        date_key = d.get("date", "").replace("-", "")
        net_lots = (d.get("foreign", 0) + d.get("trust", 0) + d.get("dealer", 0)) / 1000
        inst_dict[date_key] = net_lots

    # --- TDCC 多週變化表格 ---
    tdcc_hist = tdcc.get("history", [])
    tdcc_table_rows = ""
    
    # 【修正】獨立判斷邏輯，徹底解決字串 format 被 f-string 跳脫搞壞的問題
    def get_diff_html(diff, kind):
        if diff == 0: return ""
        cls = "up" if diff > 0 else "down"
        if kind == 'pct': return f'<span class="{cls}" style="font-size:11px; margin-left:4px;">({diff:+.2f}%)</span>'
        elif kind == 'int': return f'<span class="{cls}" style="font-size:11px; margin-left:4px;">({diff:+,})</span>'
        elif kind == 'float': return f'<span class="{cls}" style="font-size:11px; margin-left:4px;">({diff:+.1f})</span>'
        return ""

    for i in range(len(tdcc_hist)):
        curr = tdcc_hist[i]
        prev_r = tdcc_hist[i+1] if i+1 < len(tdcc_hist) else curr
        l_diff = curr['large_ratio'] - prev_r['large_ratio']
        r_diff = curr['retail_ratio'] - prev_r['retail_ratio']
        h_diff = curr['total_holders'] - prev_r['total_holders']
        a_diff = curr.get('avg_lots',0) - prev_r.get('avg_lots',0)
        
        # 找出當周四與週五的法人買賣加總 (TDCC 日期與前一天)
        date_str = curr.get('date', '')
        thu_fri_net = 0
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                fri_str = dt.strftime("%Y%m%d")
                thu_str = (dt - timedelta(days=1)).strftime("%Y%m%d")
                thu_fri_net = inst_dict.get(fri_str, 0) + inst_dict.get(thu_str, 0)
            except Exception:
                pass

        # 將四五法人插在大戶與散戶之間
        tdcc_table_rows += f'''<tr>
            <td style="text-align:center;">{curr['date']}</td>
            <td style="text-align:center;">{curr['large_ratio']:.2f}% {get_diff_html(l_diff, 'pct')}</td>
            <td style="text-align:center;" class="{'up' if thu_fri_net >= 0 else 'down'}">{thu_fri_net:+,.0f}</td>
            <td style="text-align:center;">{curr['retail_ratio']:.2f}% {get_diff_html(r_diff, 'pct')}</td>
            <td style="text-align:center;">{curr['total_holders']:,} {get_diff_html(h_diff, 'int')}</td>
            <td style="text-align:center;">{curr.get('avg_lots',0):.1f} {get_diff_html(a_diff, 'float')}</td>
        </tr>'''
        
    if not tdcc_table_rows:
        tdcc_table_rows = "<tr><td colspan='6' style='text-align:center;'>無集保資料</td></tr>"

    # 【新增】抓取大戶與散戶的門檻張數
    r_shares = tdcc.get("retail_threshold_shares", 0) / 1000
    l_shares = tdcc.get("large_threshold_shares", 0) / 1000
    threshold_info = f"大戶 >5千萬 ({l_shares:,.0f}張) / 散戶 <5百萬 ({r_shares:,.0f}張)"

    return f"""
    <div class="stock-card {'active' if is_first else ''} {'mobile-expanded' if is_first else ''}" id="card_{stock_id}">
      <div class="mobile-header mobile-only" onclick="toggleMobile('{stock_id}')">
        <div class="mh-top"><span class="mh-icon" id="icon_{stock_id}">{"▼" if is_first else "▶"}</span><span class="mh-name">{stock_id} {data.get('name','')}</span><span class="mh-price {c_cls}">${close_price:,.2f} ({change_str})</span></div>
        <div class="mh-bottom"><span class="mh-rating">🌟 {r.get('rating','')}</span><span class="mh-score">技 {r.get('tech',0):g} / 籌 {r.get('chip',0):g}</span></div>
      </div>
      <div class="stock-body">
        <div class="card-header-desktop desktop-only"><h2>{stock_id} {data.get('name','')} <span class="{c_cls}">${close_price:,.2f} ({change_str})</span></h2><div style="font-size:16px; font-weight:bold;">綜合評等: <span style="color:var(--primary);">{r.get('rating','')}</span> (技術: {r.get('tech',0):g} / 籌碼: {r.get('chip',0):g})</div></div>
        
        <div id="kline_{stock_id}" style="width: 100%; height: 800px; margin-bottom: 15px;"></div>
        
        <div class="grid-2-col">
          <div><h3 style="margin:0 0 10px 0; font-size:16px;">👥 三大法人買賣超 (張)</h3><table class="data-table"><tr><th>法人</th><th>今日</th><th>5日</th><th>20日</th></tr><tr><td>外資</td><td class="{'up' if inst.get('foreign_today',0)>=0 else 'down'}">{inst.get('foreign_today',0):+,.0f}</td><td class="{'up' if inst.get('foreign_5d',0)>=0 else 'down'}">{inst.get('foreign_5d',0):+,.0f}</td><td class="{'up' if inst.get('foreign_20d',0)>=0 else 'down'}">{inst.get('foreign_20d',0):+,.0f}</td></tr><tr><td>投信</td><td class="{'up' if inst.get('trust_today',0)>=0 else 'down'}">{inst.get('trust_today',0):+,.0f}</td><td class="{'up' if inst.get('trust_5d',0)>=0 else 'down'}">{inst.get('trust_5d',0):+,.0f}</td><td class="{'up' if inst.get('trust_20d',0)>=0 else 'down'}">{inst.get('trust_20d',0):+,.0f}</td></tr><tr><td>自營</td><td class="{'up' if inst.get('dealer_today',0)>=0 else 'down'}">{inst.get('dealer_today',0):+,.0f}</td><td class="{'up' if inst.get('dealer_5d',0)>=0 else 'down'}">{inst.get('dealer_5d',0):+,.0f}</td><td class="{'up' if inst.get('dealer_20d',0)>=0 else 'down'}">{inst.get('dealer_20d',0):+,.0f}</td></tr><tr class="row-total"><td>合計</td><td class="{'up' if tt_today>=0 else 'down'}">{tt_today:+,.0f}</td><td class="{'up' if tt_5d>=0 else 'down'}">{tt_5d:+,.0f}</td><td class="{'up' if tt_20d>=0 else 'down'}">{tt_20d:+,.0f}</td></tr></table></div>
          <div><h3 style="margin:0 0 10px 0; font-size:16px;">💰 融資 / 融券 / 借券 (張)</h3><table class="data-table"><tr><th>項目</th><th>餘額</th><th>今日</th><th>5日</th><th>20日</th></tr><tr><td>融資</td><td>{margin.get('margin_balance',0):,}</td><td class="{'up' if margin.get('margin_change',0)>=0 else 'down'}">{margin.get('margin_change',0):+,}</td><td class="{'up' if margin.get('margin_5d',0)>=0 else 'down'}">{margin.get('margin_5d',0):+,}</td><td class="{'up' if margin.get('margin_20d',0)>=0 else 'down'}">{margin.get('margin_20d',0):+,}</td></tr><tr><td>融券</td><td>{margin.get('short_balance',0):,}</td><td class="{'up' if margin.get('short_change',0)>=0 else 'down'}">{margin.get('short_change',0):+,}</td><td class="{'up' if margin.get('short_5d',0)>=0 else 'down'}">{margin.get('short_5d',0):+,}</td><td class="{'up' if margin.get('short_20d',0)>=0 else 'down'}">{margin.get('short_20d',0):+,}</td></tr><tr><td>借券</td><td>{borrow.get('borrow_balance',0):,}</td><td class="{'up' if borrow.get('borrow_change',0)>=0 else 'down'}">{borrow.get('borrow_change',0):+,}</td><td class="{'up' if borrow.get('borrow_5d',0)>=0 else 'down'}">{borrow.get('borrow_5d',0):+,}</td><td class="{'up' if borrow.get('borrow_20d',0)>=0 else 'down'}">{borrow.get('borrow_20d',0):+,}</td></tr></table></div>
        </div>

        <div style="margin-bottom: 15px; padding: 12px; background: #f8f9fa; border-radius: 8px; border: 1px solid #eee;">
           <h3 style="margin:0 0 10px 0; font-size:14px; color: var(--primary); display: flex; align-items: center; gap: 6px;">📊 大戶與散戶持股變動 (歷史多週) <span style="font-size:10px; font-weight:normal; color:#888; background:#eee; padding:2px 6px; border-radius:10px;">{threshold_info}</span></h3>
           <div style="overflow-x: auto;">
             <table class="data-table" style="min-width: 600px; margin-bottom: 0;">
               <tr>
                 <th style="text-align: center;">日期</th>
                 <th style="text-align: center;">大戶比例</th>
                 <th style="text-align: center;">四五法人(張)</th>
                 <th style="text-align: center;">散戶比例</th>
                 <th style="text-align: center;">總股東人數</th>
                 <th style="text-align: center;">平均每戶(張)</th>
               </tr>
               {tdcc_table_rows}
             </table>
           </div>
        </div>

        <div class="grid-2-col">
          <div class="ai-box"><h4>🧠 AI 技術與籌碼分析</h4><div>{data.get('ai_tech','').replace(chr(10), '<br>')}</div><div style="margin-top:10px; border-top:1px dashed #ccc; padding-top:10px;">{data.get('ai_chip','').replace(chr(10), '<br>')}</div></div>
          <div class="ai-box" style="border-left-color: #e65100; background: #fff3e0;"><h4 style="color: #e65100;">🎯 AI 操作建議</h4><div>{data.get('ai_oper','').replace(chr(10), '<br>')}</div></div>
        </div>

        <div style="margin-top: 5px; border: 1px solid #f0f0f0; border-radius: 8px; overflow: hidden;">
            <details><summary style="padding: 12px 15px; background: #fdfdfd; cursor: pointer; font-size: 13px; font-weight: bold; color: #555; list-style: none; user-select: none;">📰 近期相關新聞 ({len(news[:5])}) <span style="float:right; font-size:12px; color:#aaa; font-weight:normal;">點擊展開 ▼</span></summary><div style="padding: 0 15px 5px 15px; border-top: 1px solid #f0f0f0;">{news_html}</div></details>
        </div>
      </div>
    </div>"""

def generate_market_section(market_data: dict):
    taiex = market_data["taiex"]
    if not taiex: return "<p>大盤資料載入中...</p>"
    latest, prev = taiex[-1], taiex[-2] if len(taiex)>1 else taiex[-1]
    close, change = latest["close"], latest["close"] - prev["close"]
    change_pct, change_class, arrow = (change / prev["close"] * 100) if prev["close"] else 0, "positive" if change > 0 else "negative", "↑" if change > 0 else "↓"
    money_b, money_prev = latest.get("money",0) / 100_000_000, prev.get("money",0) / 100_000_000
    money_diff, money_pct = money_b - money_prev, ((money_b - money_prev) / money_prev * 100) if money_prev else 0
    
    inst_latest = market_data["institution"][-1] if market_data["institution"] else {}
    foreign = _get(inst_latest, "Foreign_Investor_diff", "foreign_investor_diff") / 100_000_000
    trust   = _get(inst_latest, "Investment_Trust_diff", "investment_trust_diff") / 100_000_000
    
    return f"""<div class="section-header">大盤總覽</div><div class="metrics-grid-4"><div class="metric"><div class="metric-label">加權指數</div><div class="metric-value {change_class}">{close:,.2f}</div><div class="metric-change {change_class}">{arrow} {change:+.2f} ({change_pct:+.2f}%)</div></div><div class="metric"><div class="metric-label">成交金額(億)</div><div class="metric-value">{money_b:,.0f}</div><div class="sub {'up' if money_diff>=0 else 'down'}">{'↑' if money_diff>=0 else '↓'} {money_diff:+,.0f} ({money_pct:+.2f}%)</div></div><div class="metric"><div class="metric-label">外資(億)</div><div class="metric-value {'positive' if foreign>0 else 'negative'}">{foreign:+.1f}</div></div><div class="metric"><div class="metric-label">投信(億)</div><div class="metric-value {'positive' if trust>0 else 'negative'}">{trust:+.1f}</div></div></div>
    <div class="card">
        <div class="card-title" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
            <span>加權指數與市場指標綜合研判 (近 90 個交易日)</span>
            <div style="font-size:12px; font-weight:normal; display:flex; gap:12px; align-items:center; background:#f8f9fa; padding:4px 10px; border-radius:6px; border:1px solid #eee;" id="taiex_toggles">
                <span style="color:#888;">顯示指標：</span>
                <label style="cursor:pointer;"><input type="checkbox" checked value="1"> 成交量</label>
                <label style="cursor:pointer;"><input type="checkbox" checked value="2"> 散戶多空</label>
                <label style="cursor:pointer;"><input type="checkbox" checked value="3"> USD/TWD</label>
                <label style="cursor:pointer;"><input type="checkbox" checked value="4"> 融資市值比</label>
                <label style="cursor:pointer;"><input type="checkbox" checked value="5"> 三大法人</label>
            </div>
        </div>
        <div id="taiex_kline" style="width:100%;height:850px;"></div>
    </div>"""
    
def generate_timeseries_section(market_data: dict):
    return """<div class="section-header">市場指標綜合研判 (近30日)</div><div class="grid-2"><div class="card"><div class="card-title">微台散戶多空比</div><div class="chart-container"><canvas id="retail"></canvas></div></div><div class="card"><div class="card-title">美元兌新台幣</div><div class="chart-container"><canvas id="fx"></canvas></div></div></div><div class="grid-2"><div class="card"><div class="card-title">融資市值比 (融資餘額 ÷ 台股總市值)</div><div class="chart-container"><canvas id="margin_ratio"></canvas></div></div><div class="card"><div class="card-title">三大法人現貨 vs 期貨</div><div class="chart-container" style="height:320px;"><canvas id="inst"></canvas></div></div></div>"""

def generate_chart_scripts(stocks_data: dict, market_data: dict):
    scripts = []
    
    for stock_id, data in stocks_data.items():
        df_tail = data["df"].tail(90)
        ind_dates = [d.strftime("%m-%d") for d in df_tail.index]
        ind_ohlc  = [[float(r["Open"]), float(r["Close"]), float(r["Low"]), float(r["High"])] for _, r in df_tail.iterrows()]
        ind_vol   = [int(r["Volume"]/1000) if r["Volume"] else 0 for _, r in df_tail.iterrows()]
        ind_ma20  = [round(v, 2) if v==v else None for v in df_tail["SMA_20"].tolist()]
        st_up   = [int(round(v)) if (d==1 and pd.notna(v)) else None for v, d in zip(df_tail["ST"].tolist(), df_tail["ST_DIR"].tolist())]
        st_down = [int(round(v)) if (d==-1 and pd.notna(v)) else None for v, d in zip(df_tail["ST"].tolist(), df_tail["ST_DIR"].tolist())]
        ind_vol_color = ["#ef5350" if r["Close"] >= r["Open"] else "#26a69a" for _, r in df_tail.iterrows()]
        
        cd = data.get("chart_data", {})
        
        scripts.append(f"""
var dates_{stock_id} = {json.dumps(ind_dates)};
var state_{stock_id} = {{ lastIdx: -1, lastMonth: null }};

var dateFmt_{stock_id} = function(value, index) {{
  if (index <= state_{stock_id}.lastIdx) {{ state_{stock_id}.lastMonth = null; }}
  state_{stock_id}.lastIdx = index;
  
  var currM = value.split('-')[0];
  var currD = value.split('-')[1];
  var isBoundary = false;
  
  if (index === 0) isBoundary = true;
  else if (dates_{stock_id}[index - 1].split('-')[0] !== currM) isBoundary = true;
  
  if (state_{stock_id}.lastMonth !== null && state_{stock_id}.lastMonth !== currM) isBoundary = true;
  if (state_{stock_id}.lastMonth === null) isBoundary = true;
  
  state_{stock_id}.lastMonth = currM;
  return isBoundary ? (currM + '/' + currD) : currD;
}};

var chartK_{stock_id} = echarts.init(document.getElementById('kline_{stock_id}'));

var currentMouseY_{stock_id} = 0;
chartK_{stock_id}.getZr().on('mousemove', function(e) {{
    currentMouseY_{stock_id} = e.offsetY;
}});

chartK_{stock_id}.setOption({{
  title: [
    {{ text: '日K線圖', left: '6%', top: '1%', textStyle: {{fontSize: 12, color: '#555'}} }},
    {{ text: '成交量(張)', left: '6%', top: '33%', textStyle: {{fontSize: 11, color: '#555'}} }},
    {{ text: '三大法人買賣超(億)與累計', left: '6%', top: '54%', textStyle: {{fontSize: 11, color: '#555'}} }},
    {{ text: '融資/券/借券 增減(張)與餘額', left: '6%', top: '75%', textStyle: {{fontSize: 11, color: '#555'}} }}
  ],
  tooltip: {{ 
    trigger: 'axis', 
    axisPointer: {{ type: 'cross' }},
    formatter: function(params) {{
      var h = chartK_{stock_id}.getHeight();
      var yPct = (currentMouseY_{stock_id} / h) * 100;
      var date = params[0].axisValue;
      var html = '<div style="font-size:12px; line-height:1.5;"><b>' + date + '</b><br/>';
      
      var hoveredGrid = 0; 
      if (yPct >= 34 && yPct < 55) hoveredGrid = 1;
      else if (yPct >= 55 && yPct < 77) hoveredGrid = 2;
      else if (yPct >= 77) hoveredGrid = 3;
      
      params.forEach(function(p) {{
        var isPrice = (p.seriesName === 'K線');
        var isVol = (p.seriesName === '成交量(張)');
        var axIdx = p.axisIndex || 0;
        
        if (isPrice) {{
          var val = p.data;
          html += p.marker + ' 開: <b>' + Math.round(val[0]).toLocaleString() + '</b> ' +
                  '高: <b>' + Math.round(val[3]).toLocaleString() + '</b> ' +
                  '低: <b>' + Math.round(val[2]).toLocaleString() + '</b> ' +
                  '收: <b>' + Math.round(val[1]).toLocaleString() + '</b><br/>';
        }} else if (isVol) {{
          var val = p.data;
          var v = (Array.isArray(val)) ? (val.length > 1 ? val[1] : val[0]) : val;
          if (v != null && !isNaN(v) && v !== '') {{
              html += p.marker + ' ' + p.seriesName + ': <b>' + Math.round(v).toLocaleString() + '</b><br/>';
          }}
        }} else if (axIdx === hoveredGrid) {{
          var val = p.data;
          var v = (Array.isArray(val)) ? (val.length > 1 ? val[1] : val[0]) : val;
          if (v == null || isNaN(v) || v === '') return; // 【防空值】ST 為空時跳過不顯示
          v = Math.round(v).toLocaleString();
          html += p.marker + ' ' + p.seriesName + ': <b>' + v + '</b><br/>';
        }}
      }});
      html += '</div>';
      return html;
    }}
  }},
  legend: [
    {{ data: ['MA20', 'ST指標'], top: '1%', right: '8%', textStyle: {{fontSize: 10}}, itemWidth:10, itemHeight:10 }},
    {{ data: ['外資', '投信', '自營', '法人累計(右)'], top: '54%', right: '8%', textStyle: {{fontSize: 10}}, itemWidth:10, itemHeight:10 }},
    {{ data: ['融資增減', '融券增減', '借券增減', '融資餘額(右)', '融券餘額(右)', '借券餘額(右)'], top: '75%', right: '8%', textStyle: {{fontSize: 10}}, itemWidth:10, itemHeight:10 }}
  ],
  axisPointer: {{ link: {{xAxisIndex: 'all'}} }},
  grid: [
    {{ left: '6%', right: '8%', top: '5%', height: '25%' }},
    {{ left: '6%', right: '8%', top: '37%', height: '14%' }},
    {{ left: '6%', right: '8%', top: '58%', height: '14%' }},
    {{ left: '6%', right: '8%', top: '79%', height: '14%' }}
  ],
  xAxis: [
    {{ type: 'category', gridIndex: 0, data: dates_{stock_id}, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '#666', formatter: dateFmt_{stock_id}, showMinLabel: true, showMaxLabel: true}}, axisLine: {{onZero: false}}, axisTick: {{show: true}} }},
    {{ type: 'category', gridIndex: 1, data: dates_{stock_id}, boundaryGap: true, axisLabel: {{show: false}}, axisLine: {{onZero: false}}, axisTick: {{show: false}} }},
    {{ type: 'category', gridIndex: 2, data: dates_{stock_id}, boundaryGap: true, axisLabel: {{show: false}}, axisLine: {{onZero: false}}, axisTick: {{show: false}} }},
    {{ type: 'category', gridIndex: 3, data: dates_{stock_id}, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '#666', formatter: dateFmt_{stock_id}, showMinLabel: true, showMaxLabel: true}}, axisLine: {{onZero: false}}, axisTick: {{show: true}} }}
  ],
  yAxis: [
    {{ scale: true, gridIndex: 0, splitArea: {{show: true}}, splitLine: {{lineStyle: {{color: '#eee'}}}}, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v).toLocaleString(); }}}} }},
    {{ scale: true, gridIndex: 1, axisLabel: {{fontSize: 9, formatter: function(value) {{ return value >= 10000 ? (value / 1000) + 'k' : Math.round(value).toLocaleString(); }} }}, splitLine: {{show: false}} }},
    {{ type: 'value', gridIndex: 2, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v).toLocaleString(); }}}}, splitLine: {{lineStyle: {{color: '#eee'}}}} }},
    {{ type: 'value', gridIndex: 2, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v).toLocaleString(); }}}}, splitLine: {{show: false}}, position: 'right' }},
    {{ type: 'value', gridIndex: 3, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v).toLocaleString(); }}}}, splitLine: {{lineStyle: {{color: '#eee'}}}} }},
    {{ type: 'value', gridIndex: 3, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v).toLocaleString(); }}}}, splitLine: {{show: false}}, scale: true, position: 'right' }}
  ],
  dataZoom: [
    {{ type: 'inside', xAxisIndex: [0, 1, 2, 3], start: 0, end: 100 }}, 
    {{ show: true, xAxisIndex: [0, 1, 2, 3], type: 'slider', bottom: 0, start: 0, end: 100, height: 15 }}
  ],
  series: [
    {{ name: 'K線', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(ind_ohlc)}, itemStyle: {{color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a'}} }},
    {{ name: 'MA20', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(ind_ma20)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '#fbc02d'}} }},
    
    // 【修改】這裡兩者名稱皆設為 ST指標，確保圖例完美合併
    {{ name: 'ST指標', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(st_up)}, smooth: false, showSymbol: false, lineStyle: {{width: 1.5, color: '#ef5350', type: 'dashed'}} }},
    {{ name: 'ST指標', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(st_down)}, smooth: false, showSymbol: false, lineStyle: {{width: 1.5, color: '#26a69a', type: 'dashed'}} }},
    
    {{ name: '成交量(張)', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: {json.dumps(ind_vol)}, itemStyle: {{color: function(params) {{ return {json.dumps(ind_vol_color)}[params.dataIndex]; }} }} }},
    {{ name: '外資', type: 'bar', stack: 'inst', xAxisIndex: 2, yAxisIndex: 2, data: {json.dumps(cd.get('inst_foreign', []))}, itemStyle: {{color: '#378ADD'}} }},
    {{ name: '投信', type: 'bar', stack: 'inst', xAxisIndex: 2, yAxisIndex: 2, data: {json.dumps(cd.get('inst_trust', []))}, itemStyle: {{color: '#1D9E75'}} }},
    {{ name: '自營', type: 'bar', stack: 'inst', xAxisIndex: 2, yAxisIndex: 2, data: {json.dumps(cd.get('inst_dealer', []))}, itemStyle: {{color: '#FF9800'}} }},
    {{ name: '法人累計(右)', type: 'line', xAxisIndex: 2, yAxisIndex: 3, data: {json.dumps(cd.get('inst_cum', []))}, itemStyle: {{color: '#E91E63'}}, smooth: true, showSymbol: false }},
    {{ name: '融資增減', type: 'bar', xAxisIndex: 3, yAxisIndex: 4, data: {json.dumps(cd.get('margin_diff', []))}, itemStyle: {{color: '#EF5350'}} }},
    {{ name: '融券增減', type: 'bar', xAxisIndex: 3, yAxisIndex: 4, data: {json.dumps(cd.get('short_diff', []))}, itemStyle: {{color: '#66BB6A'}} }},
    {{ name: '借券增減', type: 'bar', xAxisIndex: 3, yAxisIndex: 4, data: {json.dumps(cd.get('borrow_diff', []))}, itemStyle: {{color: '#AB47BC'}} }},
    {{ name: '融資餘額(右)', type: 'line', xAxisIndex: 3, yAxisIndex: 5, data: {json.dumps(cd.get('margin_bal', []))}, itemStyle: {{color: '#EF5350'}}, smooth: true, showSymbol: false }},
    {{ name: '融券餘額(右)', type: 'line', xAxisIndex: 3, yAxisIndex: 5, data: {json.dumps(cd.get('short_bal', []))}, itemStyle: {{color: '#66BB6A'}}, smooth: true, showSymbol: false }},
    {{ name: '借券餘額(右)', type: 'line', xAxisIndex: 3, yAxisIndex: 5, data: {json.dumps(cd.get('borrow_bal', []))}, itemStyle: {{color: '#AB47BC'}}, smooth: true, showSymbol: false }}
  ]
}});
window.addEventListener('resize', function() {{ chartK_{stock_id}.resize(); }});
""")

    # ==========================
    # 大盤綜合 JS 動態排版區塊
    # ==========================
    taiex = market_data.get("taiex", [])
    if taiex and len(taiex) > 0:
        taiex_dates = [d["date"][-5:] for d in taiex]
        taiex_ohlc  = [[d["open"], d["close"], d["low"], d["high"]] for d in taiex]
        taiex_vol = [round(d.get("money", 0) / 1e8, 1) for d in taiex]
        taiex_vol_color = ["#ef5350" if d["close"] >= d["open"] else "#26a69a" for d in taiex]
        
        taiex_ma20 = [d.get("ma20") for d in taiex]
        taiex_st_up = [int(round(d["supertrend"])) if (d.get("supertrend_dir")==1 and pd.notna(d.get("supertrend"))) else None for d in taiex]
        taiex_st_down = [int(round(d["supertrend"])) if (d.get("supertrend_dir")==-1 and pd.notna(d.get("supertrend"))) else None for d in taiex]
        
        retail = market_data.get("retail", [])
        retail_dict = {d['date'][-5:]: d['retail_ratio'] for d in retail}
        retail_aligned = [retail_dict.get(date, None) for date in taiex_dates]
        
        fx = market_data.get("usd_twd", [])
        fx_dict = {d.get("date","")[-5:]: float(d.get("close", d.get("Close", d.get("rate",0)))) for d in fx if d.get("close", d.get("Close", d.get("rate",0)))}
        fx_aligned = [fx_dict.get(date, None) for date in taiex_dates]
        
        total_margin = market_data.get("total_margin", [])
        margin_dict = {}
        if total_margin:
            margin_sorted = sorted(total_margin, key=lambda x: x.get("date", ""))
            taiex_by_date = {d["date"]: d for d in taiex}
            for d in margin_sorted:
                date = d.get("date", "")
                if date in taiex_by_date:
                    margin_now = _get(d, "MarginPurchaseTodayBalance", "TodayBalance", "MarginPurchaseMoney")
                    taiex_close = taiex_by_date[date]["close"]
                    if margin_now > 0 and taiex_close > 0:
                        margin_dict[date[-5:]] = round(margin_now / (taiex_close * 25 * 1e8) * 100, 3)
        margin_aligned = [margin_dict.get(date, None) for date in taiex_dates]
        
        inst = market_data.get("institution", [])
        inst_f_dict, inst_t_dict, inst_d_dict, fut_dict = {}, {}, {}, {}
        for d in inst:
            date_short = d.get("date", "")[-5:]
            inst_f_dict[date_short] = _get(d, "Foreign_Investor_diff") / 1e8
            inst_t_dict[date_short] = _get(d, "Investment_Trust_diff") / 1e8
            inst_d_dict[date_short] = _get(d, "Dealer_diff") / 1e8
            
        futures = market_data.get("futures", [])
        for f in futures:
            if "外資" in f.get("institutional_investors", ""):
                fut_dict[f.get("date", "")[-5:]] = float(f.get("long_open_interest_balance_volume", 0)) - float(f.get("short_open_interest_balance_volume", 0))
                
        inst_f_aligned = [inst_f_dict.get(date, 0) for date in taiex_dates]
        inst_t_aligned = [inst_t_dict.get(date, 0) for date in taiex_dates]
        inst_d_aligned = [inst_d_dict.get(date, 0) for date in taiex_dates]
        fut_aligned = [fut_dict.get(date, None) for date in taiex_dates]

        scripts.append(f"""
var taiexDates = {json.dumps(taiex_dates)};
var taiexState = {{ lastIdx: -1, lastMonth: null }};

var taiexDateFmt = function(value, index) {{
  if (index <= taiexState.lastIdx) {{ taiexState.lastMonth = null; }}
  taiexState.lastIdx = index;
  var currM = value.split('-')[0];
  var currD = value.split('-')[1];
  var isBoundary = false;
  if (index === 0) isBoundary = true;
  else if (taiexDates[index - 1].split('-')[0] !== currM) isBoundary = true;
  if (taiexState.lastMonth !== null && taiexState.lastMonth !== currM) isBoundary = true;
  if (taiexState.lastMonth === null) isBoundary = true;
  taiexState.lastMonth = currM;
  return isBoundary ? (currM + '/' + currD) : currD;
}};

var chartTaiex = echarts.init(document.getElementById('taiex_kline'));

var currentMouseY_taiex = 0;
chartTaiex.getZr().on('mousemove', function(e) {{
    currentMouseY_taiex = e.offsetY;
}});

function renderTaiex() {{
  var activeIdxs = [];
  document.querySelectorAll('#taiex_toggles input[type="checkbox"]').forEach(function(cb) {{
    if(cb.checked) activeIdxs.push(parseInt(cb.value));
  }});

  var grids = [], xAxes = [], yAxes = [], series = [], titles = [];
  var dataZooms = [
    {{ type: 'inside', xAxisIndex: [0], start: 0, end: 100 }}, 
    {{ show: true, xAxisIndex: [0], type: 'slider', bottom: 5, start: 0, end: 100, height: 15 }}
  ];

  var kHeight = activeIdxs.length > 0 ? 33 : 85;
  var startTop = 5 + kHeight + 6; 
  var totalAvailable = 92 - startTop; 

  grids.push({{ left: '8%', right: '5%', top: '5%', height: kHeight + '%' }});
  titles.push({{ text: '加權指數 (含 MA20, ST指標)', left: '8%', top: '1%', textStyle: {{ fontSize: 13, color: '#333' }} }});
  
  xAxes.push({{ type: 'category', gridIndex: 0, data: taiexDates, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '#666', formatter: taiexDateFmt, showMinLabel: true, showMaxLabel: true}}, axisLine: {{onZero: false}}, axisTick: {{show: true}} }});
  yAxes.push({{ scale: true, gridIndex: 0, splitArea: {{show: true}}, splitLine: {{lineStyle: {{color: '#eee'}}}}, axisLabel: {{fontSize: 10, formatter: function(v){{ return Math.round(v).toLocaleString(); }}}} }});
  
  series.push({{ name: '加權指數', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(taiex_ohlc)}, itemStyle: {{color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a'}} }});
  series.push({{ name: 'MA20', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(taiex_ma20)}, smooth: true, showSymbol: false, lineStyle: {{width: 1.5, color: '#fbc02d'}} }});
  
  // 【修改】大盤也將名字統稱為 ST指標 以合併圖例
  series.push({{ name: 'ST指標', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(taiex_st_up)}, smooth: false, showSymbol: false, lineStyle: {{width: 1.5, color: '#ef5350', type: 'dashed'}} }});
  series.push({{ name: 'ST指標', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(taiex_st_down)}, smooth: false, showSymbol: false, lineStyle: {{width: 1.5, color: '#26a69a', type: 'dashed'}} }});
  
  if(activeIdxs.length > 0) {{
    var eachBlock = totalAvailable / activeIdxs.length;
    
    activeIdxs.forEach(function(val, idx) {{
      var blockTop = startTop + idx * eachBlock;
      var gridIdx = idx + 1;
      
      var titleText = '';
      if(val===1) titleText = '成交金額(億)';
      if(val===2) titleText = '散戶多空比(%)';
      if(val===3) titleText = 'USD/TWD';
      if(val===4) titleText = '融資市值比(%)';
      if(val===5) titleText = '三大法人現貨(億) 與 外資期貨淨單(右軸)';
      
      titles.push({{ text: titleText, left: '8%', top: blockTop + '%', textStyle: {{ fontSize: 11, color: '#555' }} }});
      
      var gridTop = blockTop + 3.5; 
      var gridHeight = eachBlock - 4.5;
      grids.push({{ left: '8%', right: '5%', top: gridTop + '%', height: gridHeight + '%' }});
      
      var isLast = (idx === activeIdxs.length - 1);
      xAxes.push({{ type: 'category', gridIndex: gridIdx, data: taiexDates, boundaryGap: true, axisLabel: {{show: isLast, fontSize: 10, color: '#666', formatter: taiexDateFmt, showMinLabel: true, showMaxLabel: true}}, axisLine: {{onZero: false}}, axisTick: {{show: isLast}} }});
      
      dataZooms[0].xAxisIndex.push(gridIdx);
      dataZooms[1].xAxisIndex.push(gridIdx);

      if(val === 1) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, axisLabel: {{fontSize: 9, formatter: function(value) {{ return value >= 10000 ? (value / 1000) + 'k' : Math.round(value).toLocaleString(); }} }}, splitLine: {{show: false}} }});
        series.push({{ name: '成交金額(億)', type: 'bar', xAxisIndex: gridIdx, yAxisIndex: gridIdx, data: {json.dumps(taiex_vol)}, itemStyle: {{color: function(params){{ return ({json.dumps(taiex_ohlc)}[params.dataIndex][1] >= {json.dumps(taiex_ohlc)}[params.dataIndex][0]) ? '#ef5350' : '#26a69a'; }} }} }});
      }} else if(val === 2) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v) + '%'; }} }}, splitLine: {{show: false}} }});
        series.push({{ name: '散戶多空比(%)', type: 'line', xAxisIndex: gridIdx, yAxisIndex: gridIdx, data: {json.dumps(retail_aligned)}, itemStyle: {{color: '#EF5350'}}, areaStyle: {{color: 'rgba(239,83,80,0.1)'}}, smooth: true, showSymbol: false }});
      }} else if(val === 3) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v).toLocaleString(); }} }}, splitLine: {{show: false}} }});
        series.push({{ name: 'USD/TWD', type: 'line', xAxisIndex: gridIdx, yAxisIndex: gridIdx, data: {json.dumps(fx_aligned)}, itemStyle: {{color: '#378ADD'}}, areaStyle: {{color: 'rgba(55,138,221,0.1)'}}, smooth: true, showSymbol: false }});
      }} else if(val === 4) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, axisLabel: {{fontSize: 9, formatter: function(v){{ return parseFloat(v).toFixed(2) + '%'; }} }}, splitLine: {{show: false}} }});
        series.push({{ name: '融資市值比(%)', type: 'line', xAxisIndex: gridIdx, yAxisIndex: gridIdx, data: {json.dumps(margin_aligned)}, itemStyle: {{color: '#D4537E'}}, areaStyle: {{color: 'rgba(212,83,126,0.1)'}}, smooth: true, showSymbol: false }});
      }} else if(val === 5) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v).toLocaleString(); }} }}, splitLine: {{show: false}} }});
        yAxes.push({{ scale: true, gridIndex: gridIdx, axisLabel: {{fontSize: 9, formatter: function(v){{ return Math.round(v).toLocaleString(); }} }}, splitLine: {{show: false}}, position: 'right' }});
        var leftY = yAxes.length - 2;
        var rightY = yAxes.length - 1;
        series.push({{ name: '外資現貨', type: 'bar', stack: 'inst', xAxisIndex: gridIdx, yAxisIndex: leftY, data: {json.dumps(inst_f_aligned)}, itemStyle: {{color: '#378ADD'}} }});
        series.push({{ name: '投信現貨', type: 'bar', stack: 'inst', xAxisIndex: gridIdx, yAxisIndex: leftY, data: {json.dumps(inst_t_aligned)}, itemStyle: {{color: '#1D9E75'}} }});
        series.push({{ name: '自營現貨', type: 'bar', stack: 'inst', xAxisIndex: gridIdx, yAxisIndex: leftY, data: {json.dumps(inst_d_aligned)}, itemStyle: {{color: '#FF9800'}} }});
        series.push({{ name: '外資期貨', type: 'line', xAxisIndex: gridIdx, yAxisIndex: rightY, data: {json.dumps(fut_aligned)}, itemStyle: {{color: '#EF5350'}}, smooth: true, showSymbol: false }});
      }}
    }});
  }}

  chartTaiex.setOption({{
    title: titles,
    tooltip: {{ 
      trigger: 'axis', 
      axisPointer: {{ type: 'cross' }},
      formatter: function(params) {{
        var h = chartTaiex.getHeight();
        var yPct = (currentMouseY_taiex / h) * 100;
        var date = params[0].axisValue;
        var html = '<div style="font-size:12px; line-height:1.5;"><b>' + date + '</b><br/>';
        
        var hoveredGrid = 0;
        if (activeIdxs.length > 0) {{
            var eachBlock = totalAvailable / activeIdxs.length;
            activeIdxs.forEach(function(val, idx) {{
                var gridIdx = idx + 1;
                var blockTop = startTop + idx * eachBlock;
                if (yPct >= (blockTop - 2)) {{
                    hoveredGrid = gridIdx;
                }}
            }});
        }}
        
        params.forEach(function(p) {{
            var isPrice = (p.seriesName === '加權指數');
            var isVol = (p.seriesName.indexOf('成交金額') !== -1);
            var axIdx = p.axisIndex || 0;
            
            if (isPrice) {{
                var val = p.data;
                html += p.marker + ' 開: <b>' + Math.round(val[0]).toLocaleString() + '</b> ' +
                        '高: <b>' + Math.round(val[3]).toLocaleString() + '</b> ' +
                        '低: <b>' + Math.round(val[2]).toLocaleString() + '</b> ' +
                        '收: <b>' + Math.round(val[1]).toLocaleString() + '</b><br/>';
            }} else if (isVol) {{
                var val = p.data;
                var v = (Array.isArray(val)) ? (val.length > 1 ? val[1] : val[0]) : val;
                if (v != null && !isNaN(v) && v !== '') {{
                    html += p.marker + ' ' + p.seriesName + ': <b>' + Math.round(v).toLocaleString() + '</b><br/>';
                }}
            }} else if (axIdx === hoveredGrid) {{
                var val = p.data;
                var v = (Array.isArray(val)) ? (val.length > 1 ? val[1] : val[0]) : val;
                if (v == null || isNaN(v) || v === '') return; // 【防空值】ST 為空時跳過
                else {{
                    if (p.seriesName.indexOf('融資市值比') !== -1) {{
                        v = parseFloat(v).toFixed(2) + '%';
                    }} else {{
                        v = Math.round(v).toLocaleString();
                    }}
                }}
                html += p.marker + ' ' + p.seriesName + ': <b>' + v + '</b><br/>';
            }}
        }});
        html += '</div>';
        return html;
      }}
    }},
    // 【修改】大盤圖例修改為純 ST指標
    legend: {{ data: ['MA20', 'ST指標'], top: '1%', right: '5%', textStyle: {{fontSize: 10}}, itemWidth: 10, itemHeight: 10 }},
    axisPointer: {{ link: {{xAxisIndex: 'all'}} }},
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    dataZoom: dataZooms,
    series: series
  }}, true);
}}

renderTaiex();
document.querySelectorAll('#taiex_toggles input').forEach(function(cb) {{
  cb.addEventListener('change', renderTaiex);
}});
window.addEventListener('resize', function() {{ chartTaiex.resize(); }});
""")

    return "\n".join(scripts)

def get_css():
    return """
:root { --primary: #1565c0; --bg: #f5f5f5; --card-bg: #ffffff; --text: #333333; --up: #d32f2f; --down: #388e3c; --border: #e0e0e0; }
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);padding:16px;line-height:1.5;color:var(--text);scroll-behavior:smooth}
.container{max-width:1400px;margin:0 auto}
.header{background:white;border-radius:12px;border:0.5px solid var(--border);padding:1.25rem;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:22px;font-weight:500;margin:0;color:var(--primary)}
.update-time{font-size:13px;color:#666;margin-top:4px}
.btn-run{background:#2e7d32;color:white;border:none;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;display:inline-flex;align-items:center;text-decoration:none;box-shadow:0 2px 4px rgba(0,0,0,0.1);transition:0.2s}
.btn-run:hover{background:#1b5e20;transform:translateY(-1px)}
.section-header{font-size:18px;font-weight:500;margin:24px 0 16px 0;color:var(--primary);border-bottom:2px solid var(--primary);padding-bottom:4px;display:inline-block}
.card{background:white;border-radius:12px;border:0.5px solid var(--border);padding:1rem;margin-bottom:16px}
.card-title{font-size:15px;font-weight:500;margin-bottom:12px}
.positive, .up{color:var(--up)}
.negative, .down{color:var(--down)}
.metrics-grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.metric{background:#f8f9fa;border-radius:8px;padding:12px;border:1px solid #eee}
.metric-label{font-size:11px;color:#666;margin-bottom:4px}
.metric-value{font-size:16px;font-weight:500}
.metric-change{font-size:12px;margin-top:2px}
.sub{font-size:12px;margin-top:2px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.chart-container{position:relative;height:280px;margin-bottom:12px}
.rating-section{background:white;border-radius:12px;border:0.5px solid var(--border);padding:1.25rem;margin-bottom:16px}
.rating-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:10px;border-bottom:0.5px solid var(--border)}
.rating-title{font-size:16px;font-weight:500;display:flex;align-items:center;gap:6px}
.rating-update{font-size:11px;color:#999}
.rating-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.rating-col{background:#f8f9fa;border-radius:8px;padding:10px 8px;border-top:3px solid;border:1px solid #eee}
.rating-col-strong-buy{border-top-color:#c62828}
.rating-col-buy{border-top-color:#ef5350}
.rating-col-neutral{border-top-color:#888}
.rating-col-sell{border-top-color:#66bb6a}
.rating-col-strong-sell{border-top-color:#2e7d32}
.rating-col-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;padding-bottom:6px;border-bottom:0.5px solid var(--border)}
.rating-col-label{font-size:12px;font-weight:500;display:flex;align-items:center;gap:4px}
.rating-label-strong-buy{color:#c62828}
.rating-label-buy{color:#ef5350}
.rating-label-neutral{color:#555}
.rating-label-sell{color:#66bb6a}
.rating-label-strong-sell{color:#2e7d32}
.rating-col-count{font-size:11px;color:#999;background:white;border-radius:8px;padding:1px 6px;border:1px solid #eee}
.stock-chip{background:white;border:0.5px solid var(--border);border-radius:6px;padding:6px 8px;margin-bottom:5px;font-size:12px;box-shadow:0 1px 2px rgba(0,0,0,0.02)}
.chip-top{display:flex;justify-content:space-between;align-items:baseline}
.chip-name{font-weight:500;font-size:12px}
.chip-change{font-size:10px}
.chip-meta{font-size:10px;color:#999;margin-top:2px;display:flex;gap:6px}
.chip-tag{display:inline-block;font-size:9px;padding:1px 4px;border-radius:4px;background:#f5f5f5;color:#666}
.empty-hint{font-size:11px;color:#999;text-align:center;padding:14px 0}
.compact-legend { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 10px; }
.compact-legend summary { cursor: pointer; font-size: 12px; color: #888; font-weight: 500; display: inline-flex; align-items: center; gap: 4px; user-select: none; list-style: none; transition: 0.2s; }
.compact-legend summary::-webkit-details-marker { display: none; }
.compact-legend summary:hover { color: var(--primary); }
.legend-content { margin-top: 10px; background: #fafafa; border-radius: 6px; padding: 10px; font-size: 11.5px; color: #555; display: flex; flex-direction: column; gap: 8px; border: 1px solid #f0f0f0; }
.legend-row { display: flex; align-items: flex-start; gap: 8px; line-height: 1.4; }
.legend-badge { padding: 2px 6px; border-radius: 4px; font-weight: bold; white-space: nowrap; font-size: 10px; }
.l-tech { background: #e3f2fd; color: #1565c0; }
.l-chip { background: #e8f5e9; color: #2e7d32; }
.l-total { background: #fff3e0; color: #e65100; }
.data-table{width:100%;font-size:12px;border-collapse:collapse;margin-bottom:8px}
.data-table th{text-align:right;padding:6px 8px;background:#f8f9fa;color:#666;font-weight:500;font-size:11px;border:1px solid var(--border)}
.data-table th:first-child{text-align:left}
.data-table td{padding:6px 8px;border:1px solid var(--border);text-align:right}
.data-table td:first-child{text-align:left}
.data-table .row-total{background:#f8f9fa;font-weight:500}
.ai-box { background: #f8f9fa; border-left: 4px solid var(--primary); padding: 15px; margin-bottom: 15px; border-radius: 0 8px 8px 0; font-size: 13px; }
.ai-box h4 { margin: 0 0 8px 0; color: var(--primary); }
.app-layout { display: flex; gap: 20px; align-items: flex-start; }
.sidebar { width: 300px; position: sticky; top: 20px; display: flex; flex-direction: column; gap: 10px; }
.sidebar-title { font-size: 16px; font-weight: bold; color: #333; padding-bottom: 8px; border-bottom: 1px solid #ccc; margin-bottom: 5px; }
.sidebar-item { background: var(--card-bg); padding: 12px; border-radius: 8px; cursor: pointer; border: 2px solid transparent; box-shadow: 0 1px 3px rgba(0,0,0,0.05); transition: 0.2s; }
.sidebar-item:hover { border-color: #90caf9; }
.sidebar-item.active { border-color: var(--primary); background: #e3f2fd; }
.nav-top { display: flex; justify-content: space-between; font-weight: bold; font-size: 15px; margin-bottom: 4px; }
.nav-bottom { display: flex; justify-content: space-between; font-size: 12px; color: #555; }
.main-content { flex: 1; min-width: 0; }
.stock-card { display: none; background: var(--card-bg); border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); overflow: hidden; border:0.5px solid var(--border); }
.stock-card.active { display: block; }
.stock-body { padding: 20px; }
.card-header-desktop { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 1px solid var(--border); padding-bottom: 15px; margin-bottom: 15px; }
.card-header-desktop h2 { margin: 0; font-size: 22px; color: var(--primary); }
.grid-2-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 15px; }
.mobile-only { display: none; }
.desktop-only { display: block; }

@media (max-width: 900px) {
    .app-layout { flex-direction: column; gap: 12px; margin-top: 10px; }
    .desktop-only { display: none !important; }
    .mobile-only { display: flex; }
    .metrics-grid-4 { grid-template-columns: repeat(2, 1fr); }
    .grid-2 { grid-template-columns: 1fr; }
    .rating-grid { grid-template-columns: repeat(2, 1fr); }
    .sidebar { display: none; }
    .main-content { width: 100%; }
    .stock-card { display: block; margin-bottom: 12px; border-radius: 10px; }
    .mobile-header { padding: 15px; background: #fff; cursor: pointer; display: flex; flex-direction: column; gap: 8px; }
    .mh-top { display: flex; align-items: center; font-size: 16px; font-weight: bold; }
    .mh-icon { margin-right: 8px; color: var(--primary); font-size: 12px; }
    .mh-price { margin-left: auto; }
    .mh-bottom { display: flex; justify-content: space-between; font-size: 13px; color: #555; padding-left: 20px; }
    .stock-body { display: none; padding: 12px; border-top: 1px solid #eee; background: #fafafa; }
    .stock-card.mobile-expanded .stock-body { display: block; }
    .grid-2-col { grid-template-columns: 1fr; gap: 10px; }
}
"""

def generate_html(stocks_data: dict, market_data: dict) -> str:
    update_time = now_tw().strftime("%Y-%m-%d %H:%M")
    market_section = generate_market_section(market_data)
    rating_table = generate_rating_table(stocks_data)
    
    sidebar_items = ""
    stock_cards = ""
    is_first = True
    for stock_id, data in stocks_data.items():
        stock_cards += generate_stock_card(stock_id, data, is_first)
        r = data["rating"]
        sidebar_items += f'<div class="sidebar-item {"active" if is_first else ""}" id="nav_{stock_id}" onclick="showStock(\'{stock_id}\')"><div class="nav-top"><span>{stock_id} {data.get("name","")}</span><span class="{"up" if data.get("change_pct",0)>=0 else "down"}">{data.get("change_pct",0):+.2f}%</span></div><div class="nav-bottom"><span>⭐ {r.get("rating","")}</span><span>技{r.get("tech",0):g} / 籌{r.get("chip",0):g}</span></div></div>'
        is_first = False
        
    chart_scripts = generate_chart_scripts(stocks_data, market_data)
    
    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>股票監控儀表板</title>
<style>
{get_css()}
/* 分頁系統專用 CSS */
.tabs-container {{ display: flex; gap: 10px; margin-bottom: 20px; border-bottom: 2px solid #eee; padding-bottom: 10px; overflow-x: auto; scrollbar-width: none; }}
.tabs-container::-webkit-scrollbar {{ display: none; }}
.tab-btn {{ padding: 10px 20px; font-size: 16px; font-weight: bold; border: none; background: #f8f9fa; color: #555; border-radius: 8px; cursor: pointer; transition: all 0.2s; white-space: nowrap; }}
.tab-btn:hover {{ background: #e0e0e0; }}
.tab-btn.active {{ background: #1565c0; color: #fff; box-shadow: 0 2px 8px rgba(21,101,192,0.3); }}
.tab-content {{ display: none; animation: fadeIn 0.3s ease-in-out; }}
.tab-content.active {{ display: block; }}
@keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(5px); }} to {{ opacity: 1; transform: translateY(0); }} }}
</style>
</head>
<body>
<div class="container">
  <div class="header" id="top">
    <div><h1>📊 股票監控儀表板</h1><div class="update-time">最後更新: {update_time}</div></div>
    <button id="runBtn" class="btn-run" onclick="triggerAction()">▶ 立刻重新執行</button>
  </div>
  
  <div class="tabs-container">
      <button class="tab-btn active" onclick="switchTab('tab-market', this)">📈 大盤總覽</button>
      <button class="tab-btn" onclick="switchTab('tab-rating', this)">⭐ 綜合評等與建議</button>
      <button class="tab-btn" onclick="switchTab('tab-stocks', this)">📊 追蹤個股分析</button>
  </div>

  <div id="tab-market" class="tab-content active">
      {market_section}
  </div>

  <div id="tab-rating" class="tab-content">
      {rating_table}
  </div>

  <div id="tab-stocks" class="tab-content">
      <div class="app-layout">
        <div class="sidebar desktop-only"><div class="sidebar-title">個股清單</div>{sidebar_items}</div>
        <div class="main-content">{stock_cards}</div>
      </div>
  </div>
  
</div>
<button id="backToTop" onclick="window.scrollTo({{top: 0, behavior: 'smooth'}});" style="display:none; position:fixed; bottom:30px; right:30px; background:#1565c0; color:#fff; border:none; border-radius:50px; padding:10px 18px; cursor:pointer; box-shadow:0 4px 12px rgba(0,0,0,0.15); z-index:9999; font-weight:bold;">↑ 返回頂部</button>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script>
{chart_scripts}

/* 重新計算圖表大小 (非常重要：解決 ECharts 隱藏時寬高為 0 的 bug) */
function resizeAllCharts() {{ setTimeout(() => {{ window.dispatchEvent(new Event('resize')); }}, 50); }}

/* 分頁切換邏輯 */
function switchTab(tabId, btnElement) {{
    // 1. 移除所有按鈕的 active 狀態
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    // 2. 將點擊的按鈕設為 active
    btnElement.classList.add('active');
    
    // 3. 隱藏所有分頁內容
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
    // 4. 顯示目標分頁內容
    document.getElementById(tabId).classList.add('active');
    
    // 5. 切換分頁後，強制重新計算圖表尺寸
    resizeAllCharts();
}}

function showStock(stockId) {{
    if (window.innerWidth <= 900) return;
    document.querySelectorAll('.sidebar-item').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.stock-card').forEach(el => el.classList.remove('active'));
    document.getElementById('nav_' + stockId).classList.add('active');
    document.getElementById('card_' + stockId).classList.add('active');
    resizeAllCharts();
    window.scrollTo({{ top: document.querySelector('.app-layout').offsetTop - 20, behavior: 'smooth' }});
}}

function toggleMobile(stockId) {{
    const card = document.getElementById('card_' + stockId);
    const icon = document.getElementById('icon_' + stockId);
    if (card.classList.contains('mobile-expanded')) {{ card.classList.remove('mobile-expanded'); icon.innerText = '▶'; }}
    else {{ card.classList.add('mobile-expanded'); icon.innerText = '▼'; resizeAllCharts(); setTimeout(() => {{ card.scrollIntoView({{ behavior: 'smooth', block: 'start' }}); }}, 150); }}
}}

function triggerAction() {{
    const btn = document.getElementById('runBtn');
    btn.innerText = "⏳ 觸發中..."; btn.disabled = true; btn.style.opacity = "0.7";
    fetch('https://script.google.com/macros/s/你的專屬代碼/exec', {{ method: 'POST', mode: 'no-cors' }})
    .then(() => alert("✅ 指令已發送！請等待約 1~2 分鐘後重新整理網頁。"))
    .catch(err => alert("❌ 發生錯誤，請檢查網路。"))
    .finally(() => {{ btn.innerText = "▶ 立刻重新執行"; btn.disabled = false; btn.style.opacity = "1"; }});
}}

window.onscroll = function() {{ document.getElementById('backToTop').style.display = (document.body.scrollTop > 400 || document.documentElement.scrollTop > 400) ? 'block' : 'none'; }};
</script>
</body>
</html>"""

# =========================================================
# 主流程
# =========================================================

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "Markdown"}, timeout=10)
    except: pass

def process_single_stock(stock_id):
    print(f"  處理 {stock_id}...")
    stock_data = get_stock_data_yf(stock_id)
    if not stock_data: return None, None
    close_price = stock_data["latest"]["close"]
    
    institution = get_institution_data(stock_id, days=120)
    margin      = get_margin_data(stock_id, days=120)
    borrow      = get_borrowing_data(stock_id)
    tdcc        = get_tdcc_holding(stock_id, close_price)
    news        = get_news(stock_id)
    name        = get_stock_name(stock_id)
    
    df = stock_data["df"]
    inst_hist = {d["date"]: d for d in institution.get("history", [])}
    marg_hist = {d["date"]: d for d in margin.get("history", [])}
    borr_hist = {d["date"]: d for d in borrow.get("history", [])}
    chart_data = {"dates": [], "inst_foreign": [], "inst_trust": [], "inst_dealer": [], "inst_cum": [], "margin_diff": [], "margin_bal": [], "short_diff": [], "short_bal": [], "borrow_diff": [], "borrow_bal": []}
    
    cum_inst = 0
    for d_ts, row in df.tail(90).iterrows():
        d_str, d_short = d_ts.strftime("%Y-%m-%d"), d_ts.strftime("%m-%d")
        price = float(row["Close"])
        chart_data["dates"].append(d_short)
        
        i_data = inst_hist.get(d_str, {"foreign":0, "trust":0, "dealer":0})
        f_amt, t_amt, d_amt = (i_data["foreign"]*price)/1e8, (i_data["trust"]*price)/1e8, (i_data["dealer"]*price)/1e8
        cum_inst += (f_amt + t_amt + d_amt)
        chart_data["inst_foreign"].append(round(f_amt, 2)); chart_data["inst_trust"].append(round(t_amt, 2)); chart_data["inst_dealer"].append(round(d_amt, 2)); chart_data["inst_cum"].append(round(cum_inst, 2))
        
        m_data = marg_hist.get(d_str, {"margin_bal":0, "margin_diff":0, "short_bal":0, "short_diff":0})
        b_data = borr_hist.get(d_str, {"balance":0, "diff":0})
        chart_data["margin_bal"].append(m_data.get("margin_bal", 0)); chart_data["margin_diff"].append(m_data.get("margin_diff", 0))
        chart_data["short_bal"].append(m_data.get("short_bal", 0)); chart_data["short_diff"].append(m_data.get("short_diff", 0))
        chart_data["borrow_bal"].append(b_data.get("balance", 0)); chart_data["borrow_diff"].append(b_data.get("diff", 0))

    prev_close = stock_data["prev"]["close"]
    change_pct = ((close_price - prev_close) / prev_close * 100) if prev_close else 0
    
    ai_tech, ai_chip, ai_oper = generate_ai_analysis(stock_id, name, stock_data, institution, margin, borrow, tdcc)
    
    record = {
        "latest": stock_data["latest"], "prev": stock_data["prev"], "df": stock_data["df"], "indicators": stock_data["indicators"],
        "institution": institution, "margin": margin, "borrow": borrow, "tdcc": tdcc, "news": news,
        "change_pct": change_pct, "ai_tech": ai_tech, "ai_chip": ai_chip, "ai_oper": ai_oper, "name": name, "chart_data": chart_data
    }
    record["rating"] = calculate_stock_rating(record)
    print(f"    ✓ {stock_id} 評等: {record['rating']['rating']} (技{record['rating']['tech']:g}/籌{record['rating']['chip']:g})")
    return stock_id, record

def main():
    print(f'=== 股票監控機器人 v4.2 ({now_tw().strftime("%Y-%m-%d %H:%M")}) ===\n')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    print("[1/4] 平行抓取個股資料...")
    stocks_data = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_to_stock = {executor.submit(process_single_stock, sid): sid for sid in STOCKS}
        for future in concurrent.futures.as_completed(future_to_stock):
            sid = future_to_stock[future]
            try:
                res_id, record = future.result()
                if res_id and record: stocks_data[res_id] = record
            except Exception as exc: print(f"  ⚠️ {sid} 處理過程中發生錯誤: {exc}")
        
    print("\n[2/4] 抓取大盤資料...")
    market_data = get_market_overview()
    print(f"  ✓ 大盤指數: {len(market_data['taiex'])} 筆")
    print(f"  ✓ 三大法人: {len(market_data['institution'])} 筆")
    print(f"  ✓ 散戶多空: {len(market_data['retail'])} 筆")
    print(f"  ✓ 美元匯率: {len(market_data['usd_twd'])} 筆")
    print(f"  ✓ 期貨留倉: {len(market_data['futures'])} 筆")

    RATING_ORDER = {"strong-buy": 0, "buy": 1, "neutral": 2, "sell": 3, "strong-sell": 4}
    stocks_data = dict(sorted(stocks_data.items(), key=lambda kv: (RATING_ORDER.get(kv[1]["rating"]["rating_key"], 99), -kv[1]["rating"]["total"])))
    
    print("\n[3/4] 生成 HTML...")
    html = generate_html(stocks_data, market_data)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f: f.write(html)
    print(f"  ✓ {OUTPUT_FILE}")
    
    print("\n[4/4] 更新 GitHub Pages...")
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", OUTPUT_DIR], check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0: print("  無變動")
        else:
            subprocess.run(["git", "commit", "-m", f"Update {now_tw().strftime('%Y-%m-%d %H:%M')}"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("  ✓ 已更新")
    except Exception as e: print(f"  ⚠️ Git 失敗: {e}")
    
    tg_msg = f"📊 *股票監控* ({now_tw().strftime('%m-%d')})\n\n"
    for stock_id, data in stocks_data.items():
        tg_msg += f"*{stock_id}* ${data['latest']['close']:.2f} | 外資{data['institution']['foreign_today']:+.0f}張\n"
    tg_msg += "\n🌐 https://ewq4303-debug.github.io/stocknotice/"
    send_telegram(tg_msg)
    print("\n✅ 完成！")

if __name__ == "__main__":
    main()
