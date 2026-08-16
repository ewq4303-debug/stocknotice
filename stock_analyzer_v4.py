"""
股票監控機器人 v4.3 - 深色終端版 (前端改版：Dark Terminal，資料/運算邏輯不變)
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

    def list_available_dates():
        """向 GitHub API 列出 tdcc_history/ 內實際存在的快照日期 (新到舊)。
        集保資料日期不一定落在週五 (例如遇假日會是週四)，
        故直接讀目錄而非用固定週幾推算，避免漏抓最新一筆。"""
        api_url = "https://api.github.com/repos/ewq4303-debug/stocknotice/contents/tdcc_history?ref=main"
        try:
            r = requests.get(api_url, timeout=15, headers={"Accept": "application/vnd.github+json"})
            if r.status_code != 200: return []
            dates = []
            for item in r.json():
                name = item.get("name", "")
                if name.endswith(".csv"):
                    stem = name[:-4]
                    if len(stem) == 8 and stem.isdigit():
                        dates.append(stem)
            return sorted(dates, reverse=True)
        except: return []

    results = []
    avail_dates = list_available_dates()
    if avail_dates:
        for date_str in avail_dates[:10]:
            data = fetch_csv(date_str)
            if data:
                results.append(data)
    else:
        # 後備：API 失敗時，以最近的週五為基準每週往回推。
        # 集保資料日期可能落在週五或週四(遇假日)，故每週依序試 週五→週四→週三，
        # 取第一個抓到的，避免像 20260618(週四) 這種非週五的快照被漏掉。
        friday = datetime.now()
        while friday.weekday() != 4: friday -= timedelta(days=1)
        for _ in range(12):
            if len(results) >= 10: break
            for offset in (0, 1, 2):  # 週五, 週四, 週三
                data = fetch_csv((friday - timedelta(days=offset)).strftime("%Y%m%d"))
                if data:
                    results.append(data)
                    break
            friday -= timedelta(days=7)

    if not results:
        return {"retail_ratio": 0, "retail_change": 0, "large_ratio": 0, "large_change": 0, "big_holder_change": 0, "total_holders": 0, "holders_change": 0, "avg_lots": 0, "avg_lots_change": 0, "retail_threshold_shares": retail_shares, "large_threshold_shares": large_shares, "history": []}

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
        buy = float(d.get("buy", 0))
        sell = float(d.get("sell", 0))
        diff = buy - sell
        if date not in inst_by_date:
            inst_by_date[date] = {
                "date": date, "Foreign_Investor_diff": 0, "Investment_Trust_diff": 0, "Dealer_diff": 0,
                "foreign_buy": 0, "foreign_sell": 0, "trust_buy": 0, "trust_sell": 0, "dealer_buy": 0, "dealer_sell": 0
            }
        name_low = name.lower()
        if "外" in name or "foreign" in name_low:
            inst_by_date[date]["Foreign_Investor_diff"] += diff
            inst_by_date[date]["foreign_buy"] += buy
            inst_by_date[date]["foreign_sell"] += sell
        elif "投信" in name or "trust" in name_low:
            inst_by_date[date]["Investment_Trust_diff"] += diff
            inst_by_date[date]["trust_buy"] += buy
            inst_by_date[date]["trust_sell"] += sell
        elif "自營" in name or "dealer" in name_low:
            inst_by_date[date]["Dealer_diff"] += diff
            inst_by_date[date]["dealer_buy"] += buy
            inst_by_date[date]["dealer_sell"] += sell

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

    # 台指期 / 選擇權 未平倉口數增減
    def _compute_oi_changes(raw_data):
        by_date = {}
        for r in raw_data:
            date = r.get("date", "")
            name = r.get("institutional_investors", r.get("name", ""))
            long_oi = float(r.get("long_open_interest_balance_volume", 0))
            short_oi = float(r.get("short_open_interest_balance_volume", 0))
            net = long_oi - short_oi
            if date not in by_date:
                by_date[date] = {"foreign": 0, "trust": 0}
            if "外資" in name:
                by_date[date]["foreign"] += net
            elif "投信" in name:
                by_date[date]["trust"] += net
        dates_sorted = sorted(by_date.keys())
        if len(dates_sorted) < 2:
            return {"foreign_change": 0, "trust_change": 0}
        latest, prev = by_date[dates_sorted[-1]], by_date[dates_sorted[-2]]
        return {"foreign_change": int(latest["foreign"] - prev["foreign"]), "trust_change": int(latest["trust"] - prev["trust"])}

    tx_oi = _compute_oi_changes(futures)
    txo_raw = fetch_finmind("TaiwanFuturesInstitutionalInvestors", data_id="TXO", start_date=start, end_date=end)
    txo_oi = _compute_oi_changes(txo_raw)

    margin_raw = fetch_finmind("TaiwanStockTotalMarginPurchaseShortSale", start_date=start, end_date=end)
    total_margin = []
    if margin_raw:
        margin_df = pd.DataFrame(margin_raw)
        margin_df = margin_df[margin_df['name'].astype(str).str.lower() == 'marginpurchasemoney']
        if not margin_df.empty:
            total_margin = [{"date": str(r["date"]), "TodayBalance": int(float(r["TodayBalance"]))} for _, r in margin_df.sort_values("date").tail(90).iterrows()]

    return {"taiex": taiex_data, "institution": institution, "futures": futures, "usd_twd": usd_twd, "retail": retail, "total_margin": total_margin, "tx_oi": tx_oi, "txo_oi": txo_oi}

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
    result = {"trailing_pe": None, "forward_pe": None, "peg": None, "eps_ttm": None, "eps_growth": None, "revenue_growth": None, "dividend_yield": None, "roe": None, "target_price": None, "eps": [], "gm": [], "rev": []}

    for suffix in (".TW", ".TWO"):
        try:
            info = yf.Ticker(f"{stock_id}{suffix}").info
            if info and len(info) > 5:
                result["trailing_pe"]    = info.get("trailingPE")
                result["forward_pe"]     = info.get("forwardPE")
                result["peg"]            = info.get("trailingPegRatio") or info.get("pegRatio")
                result["eps_ttm"]        = info.get("trailingEps") or info.get("epsTrailingTwelveMonths")
                result["eps_growth"]     = info.get("earningsGrowth")
                result["revenue_growth"] = info.get("revenueGrowth")
                div_y                    = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
                if div_y is not None: result["dividend_yield"] = div_y * 100 if div_y < 1 else div_y
                result["roe"]            = info.get("returnOnEquity")
                result["target_price"]   = info.get("targetMeanPrice") or info.get("targetMedianPrice")
                break
        except: pass

    try:
        today = datetime.now()
        url = "https://api.finmindtrade.com/api/v4/data"

        start_rev = (today - timedelta(days=730)).strftime("%Y-%m-%d")
        rev_res = requests.get(url, params={"dataset": "TaiwanStockMonthRevenue", "data_id": stock_id, "start_date": start_rev}, timeout=10).json()
        if rev_res.get("msg") == "success" and rev_res.get("data"):
            df_rev = pd.DataFrame(rev_res["data"])
            df_rev['date'] = pd.to_datetime(df_rev['date'])
            df_rev = df_rev.sort_values('date').reset_index(drop=True)
            df_rev['yoy'] = df_rev['revenue'].pct_change(periods=12) * 100

            last_6 = df_rev.tail(6).iloc[::-1]
            for _, r in last_6.iterrows():
                d_str = r['date'].strftime("%Y-%m")
                rev_val = f"{r['revenue'] / 1e8:.2f}億" if pd.notna(r['revenue']) else "-"
                result["rev"].append({"date": d_str, "rev": rev_val, "yoy": r['yoy'] if pd.notna(r['yoy']) else None})

        start_fin = (today - timedelta(days=1095)).strftime("%Y-%m-%d")
        fin_res = requests.get(url, params={"dataset": "TaiwanStockFinancialStatements", "data_id": stock_id, "start_date": start_fin}, timeout=10).json()
        if fin_res.get("msg") == "success" and fin_res.get("data"):
            df_fin = pd.DataFrame(fin_res["data"])
            df_fin['date'] = pd.to_datetime(df_fin['date'])
            df_pivot = df_fin.pivot_table(index='date', columns='type', values='value', aggfunc='first').sort_index()

            eps_col = [c for c in df_pivot.columns if 'EPS' in str(c).upper() or '每股盈餘' in str(c)]
            if eps_col:
                eps_series = df_pivot[eps_col[0]]
                eps_yoy = eps_series.pct_change(periods=4) * 100
                last_5_eps = eps_series.tail(5)[::-1]
                for date_idx, val in last_5_eps.items():
                    d_str = f"{date_idx.year}Q{((date_idx.month-1)//3)+1}"
                    y_val = eps_yoy.loc[date_idx]
                    result["eps"].append({"date": d_str, "eps": float(val) if pd.notna(val) else None, "yoy": float(y_val) if pd.notna(y_val) else None})

            rev_cols = [c for c in df_pivot.columns if '營業收入' in str(c)]
            gp_cols = [c for c in df_pivot.columns if '毛利' in str(c)]
            if rev_cols and gp_cols:
                gm_series = (df_pivot[gp_cols[0]] / df_pivot[rev_cols[0]]) * 100
                last_5_gm = gm_series.tail(5)[::-1]
                for date_idx, val in last_5_gm.items():
                    d_str = f"{date_idx.year}Q{((date_idx.month-1)//3)+1}"
                    result["gm"].append({"date": d_str, "gm": float(val) if pd.notna(val) else None})
    except Exception as e:
        print(f"    [Warning] FinMind 獲取失敗: {e}")

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
# 評等計算
# =========================================================

RATING_LEVELS = [("strong-buy","強力加碼"), ("buy","加碼"), ("neutral","中性觀望"), ("sell","減碼"), ("strong-sell","強力減碼")]
BASE_MAX = 19.0  # tech 10 + chip 9

def calculate_fundamental_score(fund: dict) -> dict:
    """基本面五項檢查:三態訊號 (+1/0/-1),惡化倒扣;資料不足標 N/A 不計分。分數獨立於日頻總分,僅以 Tilt 介入檔位。"""
    fund = fund or {}
    score, max_pts, n_available, details = 0.0, 0.0, 0, []

    def mean(xs): return sum(xs) / len(xs)

    def add(label, points, sig, na):
        nonlocal score, max_pts, n_available
        earned = 0.0
        if not na:
            earned = points * sig
            score += earned; max_pts += points; n_available += 1
        details.append({"bucket": "fund", "label": label, "points": points, "earned": earned, "passed": (not na) and sig > 0, "na": bool(na)})

    rev_yoys = [r["yoy"] for r in (fund.get("rev") or []) if r.get("yoy") is not None]
    if rev_yoys:
        y = rev_yoys[0]
        add("最新月營收YoY", 1.5, 1 if y > 0 else (-1 if y < -10 else 0), False)
    else: add("最新月營收YoY", 1.5, 0, True)

    if len(rev_yoys) >= 6:
        recent, prior = mean(rev_yoys[0:3]), mean(rev_yoys[3:6])
        add("營收YoY動能(3m vs 3m)", 1.0, 1 if recent > prior else (-1 if recent < 0 else 0), False)
    else: add("營收YoY動能(3m vs 3m)", 1.0, 0, True)

    eps_yoys = [e["yoy"] for e in (fund.get("eps") or []) if e.get("yoy") is not None]
    if eps_yoys:
        add("最新季EPS YoY", 1.5, 1 if eps_yoys[0] > 0 else -1, False)
    else: add("最新季EPS YoY", 1.5, 0, True)

    gms = [g["gm"] for g in (fund.get("gm") or []) if g.get("gm") is not None]
    if len(gms) >= 4:
        base = mean(gms[1:4])
        add("毛利率趨勢(vs前3季均)", 1.0, 1 if gms[0] > base + 0.5 else (-1 if gms[0] < base - 1.0 else 0), False)
    else: add("毛利率趨勢(vs前3季均)", 1.0, 0, True)

    tpe, fpe = fund.get("trailing_pe"), fund.get("forward_pe")
    if tpe is not None and fpe is not None and tpe > 0 and fpe > 0:
        add("Forward PE vs Trailing", 1.0, 1 if fpe < tpe * 0.95 else (-1 if fpe > tpe * 1.15 else 0), False)
    else: add("Forward PE vs Trailing", 1.0, 0, True)

    fund_ratio = (score / max_pts) if max_pts > 0 else 0.0
    tilt = 0
    if n_available >= 3 and max_pts > 0:
        if fund_ratio >= 0.60: tilt = 1
        elif fund_ratio <= -0.40: tilt = -1
    return {"score": round(score,1), "max_pts": round(max_pts,1), "n_available": n_available, "tilt": tilt, "details": details}

def calculate_stock_rating(data: dict) -> dict:
    ind, inst, margin, borrow, tdcc, latest, prev = data.get("indicators",{}), data.get("institution",{}), data.get("margin",{}), data.get("borrow",{}), data.get("tdcc",{}), data.get("latest",{}), data.get("prev",{})
    tech = 0.0
    chip = 0.0
    details = []

    def add(bucket, label, passed, points):
        nonlocal tech, chip
        earned = points if passed else 0
        if bucket == "tech": tech += earned
        else: chip += earned
        details.append({"bucket": bucket, "label": label, "points": points, "earned": earned, "passed": bool(passed)})

    close, prev_close, volume = latest.get("close",0), prev.get("close",0), latest.get("volume",0)
    ma20, ma60, ma5, ma10 = ind.get("ma20",0), ind.get("ma60",0), ind.get("ma5",0), ind.get("ma10",0)
    vol_ma5, vol_ma20 = ind.get("vol_ma5",0), ind.get("vol_ma20",0)
    add("tech", "站上月線", close > ma20 > 0, 1)
    add("tech", "月線高於季線", ma20 > ma60 > 0, 1)
    add("tech", "5日線高於10日線", ma5 > ma10 > 0, 1)
    add("tech", "接近20日高點", ind.get("high_20",0) > 0 and close >= ind.get("high_20",0)*0.97, 1.5)
    add("tech", "價漲且量增(量>5日均量)", prev_close > 0 and close > prev_close and volume > vol_ma5 > 0, 1.5)
    add("tech", "MACD紅柱", ind.get("macd_hist",0) > 0, 2)
    add("tech", "趨勢向上且量能擴張(5日均量>20日均量)", vol_ma5 > vol_ma20 > 0 and close > ma20 > 0, 2)

    last3 = inst.get("history", [])[-3:]
    add("chip", "外資3日買超", sum(h.get("foreign",0) for h in last3) > 0, 1.5)
    add("chip", "投信3日買超", sum(h.get("trust",0) for h in last3) > 0, 1.5)
    add("chip", "外資投信同買", inst.get("foreign_today",0) > 0 and inst.get("trust_today",0) > 0, 1)
    add("chip", "融資減少", margin.get("margin_change",0) < 0, 1.5)
    add("chip", "借券減少", borrow.get("borrow_change",0) < 0, 1.5)
    add("chip", "集保大戶增加", tdcc.get("large_change",0) > 0.1, 2)

    total = tech + chip
    ratio = total / BASE_MAX
    if   ratio >= 0.70: base_idx = 0
    elif ratio >= 0.50: base_idx = 1
    elif ratio >= 0.30: base_idx = 2
    elif ratio >= 0.15: base_idx = 3
    else:               base_idx = 4

    fund_result = calculate_fundamental_score(data.get("fundamentals") or {})
    final_idx = min(4, max(0, base_idx - fund_result["tilt"]))   # tilt 只有一檔力量
    rk, rating = RATING_LEVELS[final_idx]

    details.extend(fund_result["details"])
    return {"tech": round(tech,1), "chip": round(chip,1), "total": round(total,1), "ratio": round(total/BASE_MAX*100,1),
            "fund": fund_result["score"], "fund_max": fund_result["max_pts"], "fund_n": fund_result["n_available"], "fund_tilt": fund_result["tilt"],
            "base_idx": base_idx, "rating": rating, "rating_key": rk, "details": details, "previous_rating": data.get("previous_rating")}

# =========================================================
# HTML 生成 (深色終端版)
# =========================================================


def latest_date(rows, key="date"):
    vals = [str(r.get(key, "")) for r in rows or [] if r.get(key)]
    return max(vals) if vals else "無資料"

def generate_data_status_panel(stocks_data: dict, market_data: dict, update_time: str) -> str:
    stock_count = len(stocks_data)
    tdcc_dates = []
    inst_dates = []
    for data in stocks_data.values():
        tdcc_dates.extend([h.get("date", "") for h in data.get("tdcc", {}).get("history", []) if h.get("date")])
        inst_dates.extend([h.get("date", "") for h in data.get("institution", {}).get("history", []) if h.get("date")])
    items = [
        ("頁面產生", update_time, "Python batch"),
        ("追蹤個股", f"{stock_count} 檔", "stocks.txt / GAS"),
        ("大盤行情", latest_date(market_data.get("taiex", [])), "FinMind / yfinance"),
        ("三大法人", max(inst_dates) if inst_dates else latest_date(market_data.get("institution", [])), "FinMind"),
        ("集保快照", max(tdcc_dates) if tdcc_dates else "無資料", "tdcc_history"),
        ("庫存資料", "由 inventory.json 載入", "前端即時檢查"),
    ]
    cards = "".join([f'<div class="status-card"><div class="k">{label}</div><div class="v">{value}</div><div class="src">{src}</div></div>' for label, value, src in items])
    return f'''<div class="status-panel card">
      <div class="card-title"><span>資料新鮮度與來源狀態</span><span class="src-pill">避免不同來源更新時間混淆</span></div>
      <div class="status-grid">{cards}</div>
      <div class="status-note">提示：庫存頁會另外讀取 inventory.json；若該檔 updated_at 與頁面產生時間不同，請以各資料卡時間為準。</div>
    </div>'''

def generate_rating_table(stocks_data: dict) -> str:
    groups = {
        "strong-buy":  {"label": "強力加碼", "stocks": [], "k": "sb"},
        "buy":         {"label": "加碼",     "stocks": [], "k": "b"},
        "neutral":     {"label": "中性",     "stocks": [], "k": "n"},
        "sell":        {"label": "減碼",     "stocks": [], "k": "s"},
        "strong-sell": {"label": "強力減碼", "stocks": [], "k": "ss"},
    }
    for sid, data in stocks_data.items():
        r = data.get("rating", {})
        key = r.get("rating_key", "neutral")
        if key in groups:
            groups[key]["stocks"].append({"stock_id": sid, "name": data.get("name",""), "change": data.get("change_pct",0), "tech": r.get("tech",0), "chip": r.get("chip",0), "total": r.get("total",0), "fund": r.get("fund",0), "fund_max": r.get("fund_max",0), "fund_tilt": r.get("fund_tilt",0), "details": r.get("details", []), "previous_rating": r.get("previous_rating")})
    for g in groups.values():
        g["stocks"].sort(key=lambda s: -s["total"])

    cols_html = ""
    for key, g in groups.items():
        stocks = g["stocks"]
        chips_html = ""
        for s in stocks:
            cls = "up" if s["change"] >= 0 else "down"
            sign = "+" if s["change"] >= 0 else ""
            if s["fund_max"] > 0:
                fund_tag = f'<span class="tag f {"fp" if s["fund"] >= 0 else "fn"}">基 {s["fund"]:+g}</span>'
            else:
                fund_tag = '<span class="tag f fna">基 N/A</span>'
            tilt_tag = ""
            if s["fund_tilt"] > 0: tilt_tag = '<span class="tbadge tup">▲基本面</span>'
            elif s["fund_tilt"] < 0: tilt_tag = '<span class="tbadge tdn">▼基本面</span>'
            chips_html += f'<div class="schip"><div class="schip-top"><span class="schip-name">{s["stock_id"]} {s["name"]}</span><span class="schip-chg {cls}">{sign}{s["change"]:.2f}%</span></div><div class="schip-meta"><span class="tag t">技 {s["tech"]:g}</span><span class="tag c">籌 {s["chip"]:g}</span>{fund_tag}{tilt_tag}</div></div>'
        if not stocks: chips_html = '<div class="empty">無</div>'
        cols_html += f'<div class="rcol" data-k="{g["k"]}"><div class="rcol-head"><span class="rcol-label">{g["label"]}</span><span class="rcol-count">{len(stocks)}</span></div>{chips_html}</div>'

    return f"""<div class="rating-wrap">
  <div class="rating-top"><h3>個股操作建議 · 綜合評等</h3><span class="upd">更新於 {now_tw().strftime("%Y-%m-%d %H:%M")}</span></div>
  <div class="rating-grid">{cols_html}</div>
  <details class="legend"><summary>評分邏輯與門檻</summary><div class="legend-body">
    <div class="legend-row"><span class="lbadge t">技術 10</span><span>站月線 +1 · 月&gt;季 +1 · 5&gt;10 +1 · 近20日高 +1.5 · 價漲且量&gt;5均 +1.5 · MACD 紅 +2 · 趨勢向上且5日量&gt;20日量 +2（量能兩項均需價格方向配合）</span></div>
    <div class="legend-row"><span class="lbadge c">籌碼 9</span><span>外資3日 +1.5 · 投信3日 +1.5 · 同買 +1 · 融資減 +1.5 · 借券減 +1.5 · 大戶增&gt;0.1pp +2</span></div>
    <div class="legend-row"><span class="lbadge tt">日頻檔位</span><span>日頻分數 =（技+籌）/ 19 比例制：≥70% 強力加碼 · ≥50% 加碼 · ≥30% 中性觀望 · ≥15% 減碼 · &lt;15% 強力減碼</span></div>
    <div class="legend-row"><span class="lbadge f">基本面 Tilt</span><span>五項檢查（月營收YoY · 營收動能 · 季EPS YoY · 毛利率趨勢 · Forward PE）三態計分（+1/0/−1，惡化倒扣、缺值 N/A 不計）；可用項 ≥3 且比率 ≥+60% 升一檔 ▲、≤−40% 降一檔 ▼。基本面分數獨立顯示，永不加進日頻總分。</span></div>
    <div class="legend-row"><span class="lbadge tt">最終評等</span><span>日頻檔位 ± 基本面 Tilt（最多一檔，不超出五檔範圍）</span></div>
  </div></details>
</div>"""

def format_optional_number(value, format_spec: str = ".2f", fallback: str = "-") -> str:
    """Format a scalar from an external data source without crashing on missing data."""
    try:
        if value is None or pd.isna(value):
            return fallback
        return format(value, format_spec)
    except (TypeError, ValueError):
        return fallback


def optional_number_class(value) -> str:
    """Return the dashboard colour class for a possibly missing numeric scalar."""
    try:
        if value is None or pd.isna(value):
            return ""
        return "up" if value > 0 else ("down" if value < 0 else "")
    except (TypeError, ValueError):
        return ""


def generate_stock_card(stock_id: str, data: dict, is_first: bool = False) -> str:
    latest, ind, inst, margin, borrow, tdcc, news, r = data["latest"], data["indicators"], data["institution"], data["margin"], data.get("borrow", {}), data.get("tdcc", {}), data.get("news", []), data["rating"]
    fund = data.get("fundamentals", {})
    rk_map = {"strong-buy": "sb", "buy": "b", "neutral": "n", "sell": "s", "strong-sell": "ss"}
    rk = rk_map.get(r.get("rating_key", "neutral"), "n")

    c_cls = "up" if data.get("change_pct",0) >= 0 else "down"
    c_sign = "+" if data.get("change_pct",0) >= 0 else ""
    change_str = f"{c_sign}{data.get('change_pct',0):.2f}%"
    close_price = latest.get('close',0)
    tt_today = inst.get("foreign_today",0)+inst.get("trust_today",0)+inst.get("dealer_today",0)
    tt_5d    = inst.get("foreign_5d",0)+inst.get("trust_5d",0)+inst.get("dealer_5d",0)
    tt_20d   = inst.get("foreign_20d",0)+inst.get("trust_20d",0)+inst.get("dealer_20d",0)

    news_html = ""
    for n in news[:5]:
        news_html += f'<div class="news-item"><div class="d">{n.get("date","")}</div><a href="{n.get("url", n.get("link", "#"))}" target="_blank">{n.get("title","")}</a></div>'
    if not news: news_html = "<div class='news-item' style='color:var(--ink-3);'>近期無相關新聞</div>"

    inst_history = inst.get("history", [])
    inst_dict = {}
    for d in inst_history:
        date_key = d.get("date", "").replace("-", "")
        net_lots = (d.get("foreign", 0) + d.get("trust", 0) + d.get("dealer", 0)) / 1000
        inst_dict[date_key] = net_lots

    tdcc_hist = tdcc.get("history", [])
    tdcc_table_rows = ""
    def get_diff_html(diff, kind):
        if diff == 0: return ""
        cls = "up" if diff > 0 else "down"
        if kind == 'pct': return f'<span class="diff {cls}">({diff:+.2f}%)</span>'
        elif kind == 'int': return f'<span class="diff {cls}">({diff:+,})</span>'
        elif kind == 'float': return f'<span class="diff {cls}">({diff:+.1f})</span>'
        return ""

    for i in range(len(tdcc_hist)):
        curr = tdcc_hist[i]
        prev_r = tdcc_hist[i+1] if i+1 < len(tdcc_hist) else curr
        l_diff = curr['large_ratio'] - prev_r['large_ratio']
        r_diff = curr['retail_ratio'] - prev_r['retail_ratio']
        h_diff = curr['total_holders'] - prev_r['total_holders']
        a_diff = curr.get('avg_lots',0) - prev_r.get('avg_lots',0)

        date_str = curr.get('date', '')
        thu_fri_net = 0
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                fri_str = dt.strftime("%Y%m%d")
                thu_str = (dt - timedelta(days=1)).strftime("%Y%m%d")
                thu_fri_net = inst_dict.get(fri_str, 0) + inst_dict.get(thu_str, 0)
            except: pass

        tdcc_table_rows += f'''<tr><td>{curr['date']}</td><td>{curr['large_ratio']:.2f}% {get_diff_html(l_diff, 'pct')}</td><td class="{'up' if thu_fri_net >= 0 else 'down'}">{thu_fri_net:+,.0f}</td><td>{curr['retail_ratio']:.2f}% {get_diff_html(r_diff, 'pct')}</td><td>{curr['total_holders']:,} {get_diff_html(h_diff, 'int')}</td><td>{curr.get('avg_lots',0):.1f} {get_diff_html(a_diff, 'float')}</td></tr>'''

    if not tdcc_table_rows: tdcc_table_rows = "<tr><td colspan='6' style='text-align:center;'>無集保資料</td></tr>"

    r_shares = tdcc.get("retail_threshold_shares", 0) / 1000
    l_shares = tdcc.get("large_threshold_shares", 0) / 1000
    threshold_info = f"大戶 &gt;5千萬（{l_shares:,.0f}張）· 散戶 &lt;5百萬（{r_shares:,.0f}張）"

    # --- 基本面 UI 格式化 ---
    def fmt_pct(v): return f"{v*100:.2f}%" if pd.notna(v) and v is not None else "-"
    def fmt_f(v): return format_optional_number(v)

    target_val = fund.get('target_price')
    target_price = f"${target_val:,.2f}" if target_val else "-"
    div_val = fund.get('dividend_yield')
    div_str = f"{div_val:.2f}%" if pd.notna(div_val) and div_val is not None else "-"

    fund_strip = f"""<div class="fund-strip">
      <div class="fund-cell"><div class="k">本益比 TTM</div><div class="v">{fmt_f(fund.get('trailing_pe'))}</div></div>
      <div class="fund-cell"><div class="k">預估本益比</div><div class="v">{fmt_f(fund.get('forward_pe'))}</div></div>
      <div class="fund-cell"><div class="k">PEG</div><div class="v">{fmt_f(fund.get('peg'))}</div></div>
      <div class="fund-cell"><div class="k">EPS TTM</div><div class="v">{fmt_f(fund.get('eps_ttm'))}</div></div>
      <div class="fund-cell"><div class="k">ROE</div><div class="v">{fmt_pct(fund.get('roe'))}</div></div>
      <div class="fund-cell"><div class="k">殖利率</div><div class="v">{div_str}</div></div>
    </div>"""

    eps_data, rev_data = fund.get("eps", []), fund.get("rev", [])
    eps_rows, rev_rows = "", ""

    for i in range(5):
        if i < len(eps_data):
            d = eps_data[i]
            eps_str = format_optional_number(d.get('eps'))
            yoy_value = d.get('yoy')
            yoy_number = format_optional_number(yoy_value)
            yoy_str = f"{yoy_number}%" if yoy_number != "-" else "-"
            yoy_cls = optional_number_class(yoy_value)
            eps_rows += f"<tr><td>{d.get('date', '-')}</td><td>{eps_str}</td><td class='{yoy_cls}'>{yoy_str}</td></tr>"
        else: eps_rows += "<tr><td style='color:var(--ink-3);'>-</td><td style='color:var(--ink-3);'>-</td><td style='color:var(--ink-3);'>-</td></tr>"

    for i in range(6):
        if i < len(rev_data):
            d = rev_data[i]
            yoy_str = f"{d['yoy']:.2f}%" if d.get('yoy') is not None else "-"
            yoy_cls = 'up' if d.get('yoy') and d['yoy']>0 else ('down' if d.get('yoy') and d['yoy']<0 else '')
            rev_rows += f"<tr><td>{d['date']}</td><td>{d['rev']}</td><td class='{yoy_cls}'>{yoy_str}</td></tr>"
        else: rev_rows += "<tr><td style='color:var(--ink-3);'>-</td><td style='color:var(--ink-3);'>-</td><td style='color:var(--ink-3);'>-</td></tr>"

    fund_tables = f"""<div class="panel">
      <div class="panel-head">基本面追蹤 <span class="src-pill">資料來源 · FinMind</span></div>
      <div class="fund-tables">
        <div class="mini-table"><div class="cap">近 5 季 EPS 與 YoY</div><table class="dtable"><tr><th>季度</th><th>EPS</th><th>YoY</th></tr>{eps_rows}</table></div>
        <div class="mini-table"><div class="cap">近 6 個月營收與 YoY</div><table class="dtable"><tr><th>月份</th><th>營收</th><th>YoY</th></tr>{rev_rows}</table></div>
      </div>
    </div>"""

    def inst_cell(v): return f'<td class="{"up" if v>=0 else "down"}">{v:+,.0f}</td>'
    def marg_cell(v): return f'<td class="{"up" if v>=0 else "down"}">{v:+,}</td>'

    detail_rows = ""
    bucket_names = {"tech": "技術面", "chip": "籌碼面", "fund": "基本面"}
    bucket_short = {"tech": "技術", "chip": "籌碼", "fund": "基本"}
    last_bucket = None
    for d in r.get("details", []):
        b = d.get("bucket")
        if b != last_bucket:
            detail_rows += f"<tr class='bucket-head'><td colspan='4'>{bucket_names.get(b, b)}</td></tr>"
            last_bucket = b
        bucket = bucket_short.get(b, b)
        earned = d.get('earned', 0)
        if d.get("na"):
            detail_rows += f"<tr class='na-row'><td>{bucket}</td><td>{d.get('label','')}</td><td>{d.get('points',0):g}</td><td>資料不足</td></tr>"
        elif earned < 0:
            detail_rows += f"<tr><td>{bucket}</td><td>{d.get('label','')}</td><td>{d.get('points',0):g}</td><td class='fneg'>−{abs(earned):g}</td></tr>"
        else:
            status = "✓" if earned > 0 else "✗"
            status_cls = "up" if earned > 0 else ""
            detail_rows += f"<tr><td>{bucket}</td><td>{d.get('label','')}</td><td>{d.get('points',0):g}</td><td class='{status_cls}'>{status} {earned:g}</td></tr>"
    previous_rating = r.get("previous_rating") or "尚無前次評等"

    tilt, base_idx = r.get("fund_tilt", 0), r.get("base_idx", 0)
    tilt_mark = "▲" if tilt > 0 else ("▼" if tilt < 0 else "—")
    tilt_badge = ""
    if tilt != 0:
        saturated = (base_idx == 0 and tilt > 0) or (base_idx == 4 and tilt < 0)
        note = "（已達檔位上限）" if saturated else ""
        tilt_badge = f'<span class="tbadge {"tup" if tilt > 0 else "tdn"}">{"▲" if tilt > 0 else "▼"}基本面{note}</span>'
    fund_str = f"{r.get('fund',0):+g} / {r.get('fund_max',0):g}（{r.get('fund_n',0)}項）" if r.get('fund_max',0) > 0 else "N/A"

    return f"""
    <div class="stock-card {'active' if is_first else ''}" id="card_{stock_id}">

      <div class="sc-body">
        <div class="sc-header" onclick="toggleCard('{stock_id}')">
          <span class="chevron mobile-only" id="chev_{stock_id}">▶</span>
          <div><div class="sc-id">{stock_id}</div>
            <div class="sc-name">{data.get('name','')} <span class="sc-price {c_cls}">${close_price:,.2f} <span style="font-size:13px">({change_str})</span></span></div></div>
          <div class="sc-meta">
            <div class="block"><div class="k">法人目標價</div><div class="v target">{target_price}</div></div>
            <div class="block"><div class="k">綜合評等</div><div class="v"><span class="rbadge {rk}">★ {r.get('rating','')}</span>{tilt_badge}</div></div>
            <div class="block"><div class="k">技 / 籌</div><div class="v num">{r.get('tech',0):g} / {r.get('chip',0):g}</div></div>
          </div>
        </div>

        <div class="sc-detail">
          {fund_strip}

          <details class="fold rating-detail">
            <summary>評分明細與變化</summary>
            <div>
              <div class="rating-summary"><span>本次：<b>{r.get('rating','')}</b></span><span>前次：{previous_rating}</span><span>總分：{r.get('total',0):g}</span><span>基本面：{fund_str} · Tilt {tilt_mark}</span></div>
              <table class="dtable"><tr><th>分類</th><th>條件</th><th>配分</th><th>得分</th></tr>{detail_rows}</table>
            </div>
          </details>

          <div id="kline_{stock_id}" class="chart-box" style="height:710px;"></div>

          <details class="fold" open>
            <summary>三大法人 / 融資融券</summary>
            <div class="grid-2">
              <div>
                <div class="subhead">三大法人買賣超（張）</div>
                <table class="dtable">
                  <tr><th>法人</th><th>今日</th><th>5日</th><th>20日</th></tr>
                  <tr><td>外資</td>{inst_cell(inst.get('foreign_today',0))}{inst_cell(inst.get('foreign_5d',0))}{inst_cell(inst.get('foreign_20d',0))}</tr>
                  <tr><td>投信</td>{inst_cell(inst.get('trust_today',0))}{inst_cell(inst.get('trust_5d',0))}{inst_cell(inst.get('trust_20d',0))}</tr>
                  <tr><td>自營</td>{inst_cell(inst.get('dealer_today',0))}{inst_cell(inst.get('dealer_5d',0))}{inst_cell(inst.get('dealer_20d',0))}</tr>
                  <tr class="row-total"><td>合計</td>{inst_cell(tt_today)}{inst_cell(tt_5d)}{inst_cell(tt_20d)}</tr>
                </table>
              </div>
              <div>
                <div class="subhead">融資 / 融券 / 借券（張）</div>
                <table class="dtable">
                  <tr><th>項目</th><th>餘額</th><th>今日</th><th>5日</th><th>20日</th></tr>
                  <tr><td>融資</td><td>{margin.get('margin_balance',0):,}</td>{marg_cell(margin.get('margin_change',0))}{marg_cell(margin.get('margin_5d',0))}{marg_cell(margin.get('margin_20d',0))}</tr>
                  <tr><td>融券</td><td>{margin.get('short_balance',0):,}</td>{marg_cell(margin.get('short_change',0))}{marg_cell(margin.get('short_5d',0))}{marg_cell(margin.get('short_20d',0))}</tr>
                  <tr><td>借券</td><td>{borrow.get('borrow_balance',0):,}</td>{marg_cell(borrow.get('borrow_change',0))}{marg_cell(borrow.get('borrow_5d',0))}{marg_cell(borrow.get('borrow_20d',0))}</tr>
                </table>
              </div>
            </div>
          </details>

          <details class="fold" open>
            <summary>大戶與散戶持股變動</summary>
            <div class="tdcc-scroll">
              <table class="dtable" style="min-width:620px">
                <tr><th>日期</th><th>大戶比例</th><th>四五法人（張）</th><th>散戶比例</th><th>總股東人數</th><th>平均每戶（張）</th></tr>
                {tdcc_table_rows}
              </table>
            </div>
            <div style="font-size:10px;color:var(--ink-3);margin-top:6px">{threshold_info}</div>
          </details>

          <details class="fold" open>
            <summary>基本面追蹤</summary>
            <div class="fund-tables">
              <div class="mini-table"><div class="cap">近 5 季 EPS 與 YoY</div><table class="dtable"><tr><th>季度</th><th>EPS</th><th>YoY</th></tr>{eps_rows}</table></div>
              <div class="mini-table"><div class="cap">近 6 個月營收與 YoY</div><table class="dtable"><tr><th>月份</th><th>營收</th><th>YoY</th></tr>{rev_rows}</table></div>
            </div>
          </details>

          <details class="fold" open>
            <summary>AI 分析與建議</summary>
            <div class="ai-grid">
              <div class="ai-box">
                <div class="ai-head"><span class="ai-mono">AI</span>技術與籌碼分析</div>
                <div class="ai-text">{data.get('ai_tech','').replace(chr(10), '<br>')}</div>
                <div class="ai-text ai-divider">{data.get('ai_chip','').replace(chr(10), '<br>')}</div>
              </div>
              <div class="ai-box oper">
                <div class="ai-head"><span class="ai-mono">AI</span>操作建議</div>
                <div class="ai-text">{data.get('ai_oper','').replace(chr(10), '<br>')}</div>
              </div>
            </div>
          </details>

          <details class="news">
            <summary><span>近期相關新聞（{len(news[:5])}）</span><span style="font-size:11.5px;color:var(--ink-3);font-weight:500">▾</span></summary>
            <div class="news-list">{news_html}</div>
          </details>
        </div>
      </div>
    </div>"""

def generate_market_section(market_data: dict):
    taiex = market_data["taiex"]
    if not taiex: return "<p>大盤資料載入中...</p>"
    latest, prev = taiex[-1], taiex[-2] if len(taiex)>1 else taiex[-1]
    close, change = latest["close"], latest["close"] - prev["close"]
    change_pct = (change / prev["close"] * 100) if prev["close"] else 0
    change_class, arrow = ("up" if change > 0 else "down"), ("▲" if change > 0 else "▼")
    money_b, money_prev = latest.get("money",0) / 100_000_000, prev.get("money",0) / 100_000_000
    money_diff = money_b - money_prev
    money_pct = ((money_b - money_prev) / money_prev * 100) if money_prev else 0

    inst_latest = market_data["institution"][-1] if market_data["institution"] else {}
    foreign = _get(inst_latest, "Foreign_Investor_diff", "foreign_investor_diff") / 100_000_000
    trust   = _get(inst_latest, "Investment_Trust_diff", "investment_trust_diff") / 100_000_000

    tx_oi, txo_oi = market_data.get("tx_oi", {}), market_data.get("txo_oi", {})
    tx_f, tx_t = tx_oi.get("foreign_change", 0), tx_oi.get("trust_change", 0)
    txo_f, txo_t = txo_oi.get("foreign_change", 0), txo_oi.get("trust_change", 0)

    def oi_cls(v): return "up" if v >= 0 else "down"

    return f"""<div class="section-head"><span class="eyebrow">Market</span><h2>大盤總覽</h2></div>
    <div class="metrics">
      <div class="metric accent"><div class="label">加權指數</div><div class="value num {change_class}">{close:,.2f}</div>
        <div class="change {change_class}"><span class="chip chip-{change_class}">{arrow} {change:+.2f}</span><span class="num">{change_pct:+.2f}%</span></div></div>
      <div class="metric"><div class="label">成交金額（億）</div><div class="value num">{money_b:,.0f}</div>
        <div class="change {'up' if money_diff>=0 else 'down'}"><span class="chip chip-{'up' if money_diff>=0 else 'down'}">{'▲' if money_diff>=0 else '▼'} {money_diff:+,.0f}</span><span class="num">{money_pct:+.2f}%</span></div></div>
      <div class="metric {'up' if foreign>0 else 'down'}"><div class="label">外資（億）</div><div class="value num {'up' if foreign>0 else 'down'}">{foreign:+.1f}</div>
        <div class="oi-sub"><span>台指期 <span class="num {oi_cls(tx_f)}">{tx_f:+,}</span> 口</span><span>選擇權 <span class="num {oi_cls(txo_f)}">{txo_f:+,}</span> 口</span></div></div>
      <div class="metric {'up' if trust>0 else 'down'}"><div class="label">投信（億）</div><div class="value num {'up' if trust>0 else 'down'}">{trust:+.1f}</div>
        <div class="oi-sub"><span>台指期 <span class="num {oi_cls(tx_t)}">{tx_t:+,}</span> 口</span><span>選擇權 <span class="num {oi_cls(txo_t)}">{txo_t:+,}</span> 口</span></div></div>
    </div>
    <div class="card">
      <div class="card-title"><span>加權指數與市場指標綜合研判 · 近 90 個交易日</span></div>
      <div id="taiex_kline" class="chart-box" style="height:640px;"></div>
      <div class="toggles" id="taiex_toggles" style="margin-top:10px;"><span class="lab">顯示指標</span>
          <label><input type="checkbox" checked value="2">散戶多空</label>
          <label><input type="checkbox" value="3">USD/TWD</label>
          <label><input type="checkbox" value="4">融資市值比</label>
          <label><input type="checkbox" checked value="5">三大法人</label>
          <label><input type="checkbox" checked value="6">法人成交比重</label>
        </div>
    </div>"""

def generate_timeseries_section(market_data: dict):
    return ""

# ECharts 深色配色常數
THEME = {
    "up": "#ff525b", "down": "#1ed992", "ma": "#e0a83c",
    "axis_label": "#8b95a5", "axis_line": "#2a323e", "split_line": "#171e29",
    "title": "#8b95a5", "legend": "#8b95a5",
    "tooltip_bg": "rgba(10,14,21,.95)", "tooltip_border": "#1e2632", "tooltip_text": "#dde3ec",
    "dz_border": "#1e2632", "dz_filler": "rgba(77,127,255,.18)", "dz_handle": "#4d7fff",
    "dz_text": "#8b95a5", "dz_bg_line": "#2a323e", "dz_bg_area": "#151b24",
    "blue": "#4d7fff", "green": "#1ed992", "orange": "#e0a83c",
    "pink": "#ff5cae", "purple": "#b07bff", "rose": "#d98ab5",
}

def generate_chart_scripts(stocks_data: dict, market_data: dict):
    scripts = []
    T = THEME

    for stock_id, data in stocks_data.items():
        df_tail = data["df"].tail(90)
        ind_dates = [d.strftime("%m-%d") for d in df_tail.index]
        ind_ohlc  = [[float(r["Open"]), float(r["Close"]), float(r["Low"]), float(r["High"])] for _, r in df_tail.iterrows()]
        ind_vol   = [int(r["Volume"]/1000) if r["Volume"] else 0 for _, r in df_tail.iterrows()]
        ind_ma20  = [round(v, 2) if v==v else None for v in df_tail["SMA_20"].tolist()]
        st_up   = [int(round(v)) if (d==1 and pd.notna(v)) else None for v, d in zip(df_tail["ST"].tolist(), df_tail["ST_DIR"].tolist())]
        st_down = [int(round(v)) if (d==-1 and pd.notna(v)) else None for v, d in zip(df_tail["ST"].tolist(), df_tail["ST_DIR"].tolist())]
        ind_vol_color = [T["up"] if r["Close"] >= r["Open"] else T["down"] for _, r in df_tail.iterrows()]

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
    {{ text: '日K線與成交量(張)', left: '6%', top: '1%', textStyle: {{fontSize: 12, color: '{T["title"]}'}} }},
    {{ text: '三大法人買賣超(張)與累計', left: '6%', top: '46%', textStyle: {{fontSize: 11, color: '{T["title"]}'}} }},
    {{ text: '融資/券/借券 增減(張)與餘額', left: '6%', top: '63%', textStyle: {{fontSize: 11, color: '{T["title"]}'}} }},
    {{ text: '法人成交比重(%)', left: '6%', top: '80%', textStyle: {{fontSize: 11, color: '{T["title"]}'}} }}
  ],
  tooltip: {{
    trigger: 'axis',
    axisPointer: {{ type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}} }},
    backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1,
    textStyle: {{color: '{T["tooltip_text"]}', fontSize: 12, fontFamily: 'IBM Plex Mono'}}, padding: [8,12],
    formatter: function(params) {{
      var h = chartK_{stock_id}.getHeight();
      var yPct = (currentMouseY_{stock_id} / h) * 100;
      var date = params[0].axisValue;
      var html = '<div style="font-size:12px; line-height:1.5;"><b>' + date + '</b><br/>';

      var hoveredGrid = 0;
      if (yPct >= 46 && yPct < 63) hoveredGrid = 1;
      else if (yPct >= 63 && yPct < 80) hoveredGrid = 2;
      else if (yPct >= 80) hoveredGrid = 3;

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
          if (v == null || isNaN(v) || v === '') return;
          v = (p.seriesName.indexOf('占比') !== -1) ? v.toFixed(2) + '%' : Math.round(v).toLocaleString();
          html += p.marker + ' ' + p.seriesName + ': <b>' + v + '</b><br/>';
        }}
      }});
      html += '</div>';
      return html;
    }}
  }},
  legend: [
    {{ data: ['MA20', 'ST指標'], top: '1%', right: '8%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth:10, itemHeight:10 }},
    {{ data: ['外資', '投信', '自營', '法人累計(右)'], top: '48%', left: '6%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth:10, itemHeight:10 }},
    {{ data: ['融資增減', '融券增減', '借券增減', '融資餘額(右)', '融券餘額(右)', '借券餘額(右)'], top: '65%', left: '6%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth:10, itemHeight:10 }},
    {{ data: ['外資占比', '投信占比', '自營占比'], top: '82%', left: '6%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth:10, itemHeight:10 }}
  ],
  axisPointer: {{ link: {{xAxisIndex: 'all'}} }},
  grid: [
    {{ left: '6%', right: '8%', top: '4%', height: '36%' }},
    {{ left: '6%', right: '8%', top: '50%', height: '11%' }},
    {{ left: '6%', right: '8%', top: '67%', height: '11%' }},
    {{ left: '6%', right: '8%', top: '84%', height: '9%' }}
  ],
  xAxis: [
    {{ type: 'category', gridIndex: 0, data: dates_{stock_id}, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '{T["axis_label"]}', formatter: dateFmt_{stock_id}, showMinLabel: true, showMaxLabel: true}}, axisLine: {{onZero: false, lineStyle: {{color: '{T["axis_line"]}'}}}}, axisTick: {{show: true}} }},
    {{ type: 'category', gridIndex: 1, data: dates_{stock_id}, boundaryGap: true, axisLabel: {{show: false}}, axisLine: {{onZero: false, lineStyle: {{color: '{T["axis_line"]}'}}}}, axisTick: {{show: false}} }},
    {{ type: 'category', gridIndex: 2, data: dates_{stock_id}, boundaryGap: true, axisLabel: {{show: false}}, axisLine: {{onZero: false, lineStyle: {{color: '{T["axis_line"]}'}}}}, axisTick: {{show: false}} }},
    {{ type: 'category', gridIndex: 3, data: dates_{stock_id}, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '{T["axis_label"]}', formatter: dateFmt_{stock_id}, showMinLabel: true, showMaxLabel: true}}, axisLine: {{onZero: false, lineStyle: {{color: '{T["axis_line"]}'}}}}, axisTick: {{show: true}} }}
  ],
  yAxis: [
    {{ scale: true, gridIndex: 0, splitNumber: 5, splitArea: {{show: false}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}}, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }}}} }},
    {{ scale: true, gridIndex: 0, show: false, max: function(v) {{ return Math.max(v.max * 6, 100); }} }},
    {{ type: 'value', gridIndex: 1, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }}}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
    {{ type: 'value', gridIndex: 1, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }}}}, splitLine: {{show: false}}, position: 'right' }},
    {{ type: 'value', gridIndex: 2, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }}}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
    {{ type: 'value', gridIndex: 2, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }}}}, splitLine: {{show: false}}, scale: true, position: 'right' }},
    {{ type: 'value', gridIndex: 3, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v) + '%'; }}}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }}
  ],
  dataZoom: [
    {{ type: 'inside', xAxisIndex: [0, 1, 2, 3], start: 0, end: 100 }},
    {{ show: true, xAxisIndex: [0, 1, 2, 3], type: 'slider', bottom: 12, start: 0, end: 100, height: 15, borderColor: '{T["dz_border"]}', fillerColor: '{T["dz_filler"]}', handleStyle: {{color: '{T["dz_handle"]}'}}, textStyle: {{color: '{T["dz_text"]}'}}, dataBackground: {{lineStyle: {{color: '{T["dz_bg_line"]}'}}, areaStyle: {{color: '{T["dz_bg_area"]}'}}}} }}
  ],
  series: [
    {{ name: 'K線', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(ind_ohlc)}, itemStyle: {{color: '{T["up"]}', color0: '{T["down"]}', borderColor: '{T["up"]}', borderColor0: '{T["down"]}'}} }},
    {{ name: 'MA20', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(ind_ma20)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma"]}'}} }},
    {{ name: 'ST指標', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(st_up)}, smooth: false, showSymbol: false, lineStyle: {{width: 1.5, color: '{T["up"]}', type: 'dashed'}} }},
    {{ name: 'ST指標', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(st_down)}, smooth: false, showSymbol: false, lineStyle: {{width: 1.5, color: '{T["down"]}', type: 'dashed'}} }},

    {{ name: '成交量(張)', type: 'bar', xAxisIndex: 0, yAxisIndex: 1, data: {json.dumps(ind_vol)}, itemStyle: {{color: function(params) {{ return {json.dumps(ind_vol_color)}[params.dataIndex]; }} }} }},

    {{ name: '外資', type: 'bar', stack: 'inst', xAxisIndex: 1, yAxisIndex: 2, data: {json.dumps(cd.get('inst_foreign', []))}, itemStyle: {{color: '{T["blue"]}'}} }},
    {{ name: '投信', type: 'bar', stack: 'inst', xAxisIndex: 1, yAxisIndex: 2, data: {json.dumps(cd.get('inst_trust', []))}, itemStyle: {{color: '{T["green"]}'}} }},
    {{ name: '自營', type: 'bar', stack: 'inst', xAxisIndex: 1, yAxisIndex: 2, data: {json.dumps(cd.get('inst_dealer', []))}, itemStyle: {{color: '{T["orange"]}'}} }},
    {{ name: '法人累計(右)', type: 'line', xAxisIndex: 1, yAxisIndex: 3, data: {json.dumps(cd.get('inst_cum', []))}, itemStyle: {{color: '{T["pink"]}'}}, smooth: true, showSymbol: false }},

    {{ name: '融資增減', type: 'bar', xAxisIndex: 2, yAxisIndex: 4, data: {json.dumps(cd.get('margin_diff', []))}, itemStyle: {{color: '{T["up"]}'}} }},
    {{ name: '融券增減', type: 'bar', xAxisIndex: 2, yAxisIndex: 4, data: {json.dumps(cd.get('short_diff', []))}, itemStyle: {{color: '{T["green"]}'}} }},
    {{ name: '借券增減', type: 'bar', xAxisIndex: 2, yAxisIndex: 4, data: {json.dumps(cd.get('borrow_diff', []))}, itemStyle: {{color: '{T["purple"]}'}} }},
    {{ name: '融資餘額(右)', type: 'line', xAxisIndex: 2, yAxisIndex: 5, data: {json.dumps(cd.get('margin_bal', []))}, itemStyle: {{color: '{T["up"]}'}}, smooth: true, showSymbol: false }},
    {{ name: '融券餘額(右)', type: 'line', xAxisIndex: 2, yAxisIndex: 5, data: {json.dumps(cd.get('short_bal', []))}, itemStyle: {{color: '{T["green"]}'}}, smooth: true, showSymbol: false }},
    {{ name: '借券餘額(右)', type: 'line', xAxisIndex: 2, yAxisIndex: 5, data: {json.dumps(cd.get('borrow_bal', []))}, itemStyle: {{color: '{T["purple"]}'}}, smooth: true, showSymbol: false }},

    {{ name: '外資占比', type: 'bar', stack: 'ratio', xAxisIndex: 3, yAxisIndex: 6, data: {json.dumps(cd.get('inst_f_ratio', []))}, itemStyle: {{color: '{T["blue"]}'}} }},
    {{ name: '投信占比', type: 'bar', stack: 'ratio', xAxisIndex: 3, yAxisIndex: 6, data: {json.dumps(cd.get('inst_t_ratio', []))}, itemStyle: {{color: '{T["green"]}'}} }},
    {{ name: '自營占比', type: 'bar', stack: 'ratio', xAxisIndex: 3, yAxisIndex: 6, data: {json.dumps(cd.get('inst_d_ratio', []))}, itemStyle: {{color: '{T["orange"]}'}} }}
  ]
}});
window.addEventListener('resize', function() {{ chartK_{stock_id}.resize(); }});
""")

    taiex = market_data.get("taiex", [])
    if taiex and len(taiex) > 0:
        taiex_dates = [d["date"][-5:] for d in taiex]
        taiex_ohlc  = [[d["open"], d["close"], d["low"], d["high"]] for d in taiex]
        taiex_vol = [round(d.get("money", 0) / 1e8, 1) for d in taiex]
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
                    margin_now = d.get("MarginPurchaseTodayBalance", d.get("TodayBalance", d.get("MarginPurchaseMoney", 0)))
                    taiex_close = taiex_by_date[date]["close"]
                    if margin_now > 0 and taiex_close > 0:
                        margin_dict[date[-5:]] = round(margin_now / (taiex_close * 25 * 1e8) * 100, 3)
        margin_aligned = [margin_dict.get(date, None) for date in taiex_dates]

        inst = market_data.get("institution", [])
        inst_f_dict, inst_t_dict, inst_d_dict, fut_dict = {}, {}, {}, {}
        for d in inst:
            date_short = d.get("date", "")[-5:]
            inst_f_dict[date_short] = d.get("Foreign_Investor_diff", 0) / 1e8
            inst_t_dict[date_short] = d.get("Investment_Trust_diff", 0) / 1e8
            inst_d_dict[date_short] = d.get("Dealer_diff", 0) / 1e8

        futures = market_data.get("futures", [])
        for f in futures:
            if "外資" in f.get("institutional_investors", ""):
                fut_dict[f.get("date", "")[-5:]] = float(f.get("long_open_interest_balance_volume", 0)) - float(f.get("short_open_interest_balance_volume", 0))

        inst_f_aligned = [inst_f_dict.get(date, 0) for date in taiex_dates]
        inst_t_aligned = [inst_t_dict.get(date, 0) for date in taiex_dates]
        inst_d_aligned = [inst_d_dict.get(date, 0) for date in taiex_dates]
        fut_aligned = [fut_dict.get(date, None) for date in taiex_dates]

        f_ratio_aligned, t_ratio_aligned, d_ratio_aligned = [], [], []
        inst_full_dict = {d["date"]: d for d in inst}
        for d in taiex:
            date_str = d["date"]
            money = d.get("money", 0)
            i_data = inst_full_dict.get(date_str, {})
            fb, fs = i_data.get("foreign_buy", 0), i_data.get("foreign_sell", 0)
            tb, ts = i_data.get("trust_buy", 0), i_data.get("trust_sell", 0)
            db, ds = i_data.get("dealer_buy", 0), i_data.get("dealer_sell", 0)
            f_ratio_aligned.append(round((((fb+fs)/2)/money)*100, 2) if money>0 else 0)
            t_ratio_aligned.append(round((((tb+ts)/2)/money)*100, 2) if money>0 else 0)
            d_ratio_aligned.append(round((((db+ds)/2)/money)*100, 2) if money>0 else 0)

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
    {{ show: true, xAxisIndex: [0], type: 'slider', bottom: 5, start: 0, end: 100, height: 15, borderColor: '{T["dz_border"]}', fillerColor: '{T["dz_filler"]}', handleStyle: {{color: '{T["dz_handle"]}'}}, textStyle: {{color: '{T["dz_text"]}'}}, dataBackground: {{lineStyle: {{color: '{T["dz_bg_line"]}'}}, areaStyle: {{color: '{T["dz_bg_area"]}'}}}} }}
  ];

  var kHeight = activeIdxs.length > 0 ? 40 : 85;
  grids.push({{ left: '8%', right: '5%', top: '5%', height: kHeight + '%' }});
  titles.push({{ text: '加權指數 (含 MA20, ST指標, 成交金額)', left: '8%', top: '1%', textStyle: {{ fontSize: 13, color: '{T["title"]}' }} }});

  xAxes.push({{ type: 'category', gridIndex: 0, data: taiexDates, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '{T["axis_label"]}', formatter: taiexDateFmt, showMinLabel: true, showMaxLabel: true}}, axisLine: {{onZero: false, lineStyle: {{color: '{T["axis_line"]}'}}}}, axisTick: {{show: true}} }});

  yAxes.push({{ scale: true, gridIndex: 0, splitNumber: 5, splitArea: {{show: false}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}}, axisLabel: {{fontSize: 10, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }}}} }});
  yAxes.push({{ scale: true, gridIndex: 0, show: false, max: function(v) {{ return Math.max(v.max * 6, 100); }} }});

  series.push({{ name: '加權指數', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(taiex_ohlc)}, itemStyle: {{color: '{T["up"]}', color0: '{T["down"]}', borderColor: '{T["up"]}', borderColor0: '{T["down"]}'}} }});
  series.push({{ name: 'MA20', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(taiex_ma20)}, smooth: true, showSymbol: false, lineStyle: {{width: 1.5, color: '{T["ma"]}'}} }});
  series.push({{ name: 'ST指標', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(taiex_st_up)}, smooth: false, showSymbol: false, lineStyle: {{width: 1.5, color: '{T["up"]}', type: 'dashed'}} }});
  series.push({{ name: 'ST指標', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(taiex_st_down)}, smooth: false, showSymbol: false, lineStyle: {{width: 1.5, color: '{T["down"]}', type: 'dashed'}} }});
  series.push({{ name: '成交金額(億)', type: 'bar', xAxisIndex: 0, yAxisIndex: 1, data: {json.dumps(taiex_vol)}, itemStyle: {{color: function(params){{ return ({json.dumps(taiex_ohlc)}[params.dataIndex][1] >= {json.dumps(taiex_ohlc)}[params.dataIndex][0]) ? '{T["up"]}' : '{T["down"]}'; }} }} }});

  var startTop = 5 + kHeight + 8;
  var totalAvailable = 92 - startTop;

  if(activeIdxs.length > 0) {{
    var eachBlock = totalAvailable / activeIdxs.length;

    activeIdxs.forEach(function(val, idx) {{
      var blockTop = startTop + idx * eachBlock;
      var gridIdx = idx + 1;

      var titleText = '';
      if(val===2) titleText = '散戶多空比(%)';
      if(val===3) titleText = 'USD/TWD';
      if(val===4) titleText = '融資市值比(%)';
      if(val===5) titleText = '三大法人現貨(億) 與 外資期貨淨單(右軸)';
      if(val===6) titleText = '法人成交比重(%)';

      titles.push({{ text: titleText, left: '8%', top: blockTop + '%', textStyle: {{ fontSize: 11, color: '{T["title"]}' }} }});

      var gridTop = blockTop + 3.5;
      var gridHeight = eachBlock - 4.5;
      grids.push({{ left: '8%', right: '5%', top: gridTop + '%', height: gridHeight + '%' }});

      var isLast = (idx === activeIdxs.length - 1);
      xAxes.push({{ type: 'category', gridIndex: gridIdx, data: taiexDates, boundaryGap: true, axisLabel: {{show: isLast, fontSize: 10, color: '{T["axis_label"]}', formatter: taiexDateFmt, showMinLabel: true, showMaxLabel: true}}, axisLine: {{onZero: false, lineStyle: {{color: '{T["axis_line"]}'}}}}, axisTick: {{show: isLast}} }});
      dataZooms[0].xAxisIndex.push(gridIdx);
      dataZooms[1].xAxisIndex.push(gridIdx);

      if(val === 2) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v) + '%'; }} }}, splitLine: {{show: false}} }});
        series.push({{ name: '散戶多空比(%)', type: 'line', xAxisIndex: gridIdx, yAxisIndex: yAxes.length - 1, data: {json.dumps(retail_aligned)}, itemStyle: {{color: '{T["up"]}'}}, areaStyle: {{color: 'rgba(255,82,91,0.10)'}}, smooth: true, showSymbol: false }});
      }} else if(val === 3) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }} }}, splitLine: {{show: false}} }});
        series.push({{ name: 'USD/TWD', type: 'line', xAxisIndex: gridIdx, yAxisIndex: yAxes.length - 1, data: {json.dumps(fx_aligned)}, itemStyle: {{color: '{T["blue"]}'}}, areaStyle: {{color: 'rgba(77,127,255,0.10)'}}, smooth: true, showSymbol: false }});
      }} else if(val === 4) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return parseFloat(v).toFixed(2) + '%'; }} }}, splitLine: {{show: false}} }});
        series.push({{ name: '融資市值比(%)', type: 'line', xAxisIndex: gridIdx, yAxisIndex: yAxes.length - 1, data: {json.dumps(margin_aligned)}, itemStyle: {{color: '{T["rose"]}'}}, areaStyle: {{color: 'rgba(217,138,181,0.10)'}}, smooth: true, showSymbol: false }});
      }} else if(val === 5) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }} }}, splitLine: {{show: false}} }});
        yAxes.push({{ scale: true, gridIndex: gridIdx, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v).toLocaleString(); }} }}, splitLine: {{show: false}}, position: 'right' }});
        var rightY = yAxes.length - 1;
        var leftY = yAxes.length - 2;
        series.push({{ name: '外資現貨', type: 'bar', stack: 'inst', xAxisIndex: gridIdx, yAxisIndex: leftY, data: {json.dumps(inst_f_aligned)}, itemStyle: {{color: '{T["blue"]}'}} }});
        series.push({{ name: '投信現貨', type: 'bar', stack: 'inst', xAxisIndex: gridIdx, yAxisIndex: leftY, data: {json.dumps(inst_t_aligned)}, itemStyle: {{color: '{T["green"]}'}} }});
        series.push({{ name: '自營現貨', type: 'bar', stack: 'inst', xAxisIndex: gridIdx, yAxisIndex: leftY, data: {json.dumps(inst_d_aligned)}, itemStyle: {{color: '{T["orange"]}'}} }});
        series.push({{ name: '外資期貨', type: 'line', xAxisIndex: gridIdx, yAxisIndex: rightY, data: {json.dumps(fut_aligned)}, itemStyle: {{color: '{T["up"]}'}}, smooth: true, showSymbol: false }});
      }} else if(val === 6) {{
        yAxes.push({{ scale: true, gridIndex: gridIdx, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{ return Math.round(v) + '%'; }} }}, splitLine: {{show: false}} }});
        var rightY = yAxes.length - 1;
        series.push({{ name: '外資占比', type: 'bar', stack: 'ratio', xAxisIndex: gridIdx, yAxisIndex: rightY, data: {json.dumps(f_ratio_aligned)}, itemStyle: {{color: '{T["blue"]}'}} }});
        series.push({{ name: '投信占比', type: 'bar', stack: 'ratio', xAxisIndex: gridIdx, yAxisIndex: rightY, data: {json.dumps(t_ratio_aligned)}, itemStyle: {{color: '{T["green"]}'}} }});
        series.push({{ name: '自營占比', type: 'bar', stack: 'ratio', xAxisIndex: gridIdx, yAxisIndex: rightY, data: {json.dumps(d_ratio_aligned)}, itemStyle: {{color: '{T["orange"]}'}} }});
      }}
    }});
  }}

  chartTaiex.setOption({{
    title: titles,
    tooltip: {{
      trigger: 'axis',
      axisPointer: {{ type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}} }},
      backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1,
      textStyle: {{color: '{T["tooltip_text"]}', fontSize: 12, fontFamily: 'IBM Plex Mono'}}, padding: [8,12],
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
                if (yPct >= (blockTop - 2)) hoveredGrid = gridIdx;
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
                if (v == null || isNaN(v) || v === '') return;
                else {{
                    if (p.seriesName.indexOf('融資市值比') !== -1 || p.seriesName.indexOf('占比') !== -1) v = parseFloat(v).toFixed(2) + '%';
                    else v = Math.round(v).toLocaleString();
                }}
                html += p.marker + ' ' + p.seriesName + ': <b>' + v + '</b><br/>';
            }}
        }});
        html += '</div>';
        return html;
      }}
    }},
    legend: {{ data: ['MA20', 'ST指標'], top: '1%', right: '5%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth: 10, itemHeight: 10 }},
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
:root{
  --bg:#0a0e15; --surface:#121823; --surface-2:#0f141d; --surface-3:#1a2230;
  --ink:#dde3ec; --ink-2:#8b95a5; --ink-3:#5d6675; --line:#1e2632; --line-2:#171e29;
  --accent:#4d7fff; --accent-2:#6f9bff; --accent-dim:#2a3a64;
  --up:#ff525b; --up-soft:rgba(255,82,91,.12); --down:#1ed992; --down-soft:rgba(30,217,146,.12);
  --gold:#e0a83c; --radius:13px; --shadow:0 2px 8px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.35);
  --mono:'IBM Plex Mono',ui-monospace,monospace;
  --sans:'Manrope','Noto Sans TC',-apple-system,BlinkMacSystemFont,sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body{font-family:var(--sans);color:var(--ink);line-height:1.5;padding:20px;min-height:100vh;scroll-behavior:smooth;
  background:linear-gradient(rgba(77,127,255,.025) 1px,transparent 1px),
    radial-gradient(900px 500px at 85% -8%,rgba(77,127,255,.10),transparent 60%),var(--bg);
  background-size:100% 28px,100% 100%,100% 100%;}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.up,.positive{color:var(--up)} .down,.negative{color:var(--down)}
.container{max-width:1480px;margin:0 auto}

/* HEADER */
.header{background:linear-gradient(120deg,#101622 0%,#141d2e 55%,#16203a 100%);border:1px solid var(--line);
  border-radius:var(--radius);padding:18px 24px;display:flex;justify-content:space-between;align-items:center;
  position:relative;overflow:hidden;box-shadow:var(--shadow);margin-bottom:0}
.header::before{content:"";position:absolute;left:0;top:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent,var(--accent),#5fd3ff,transparent);opacity:.7}
.header h1{font-size:18px;font-weight:800;color:#fff;letter-spacing:-.01em;position:relative;z-index:1}
.update-time{font-size:11.5px;color:#7e8aa0;margin-top:3px;display:flex;align-items:center;gap:8px;position:relative;z-index:1}
.update-time::before{content:"";width:6px;height:6px;border-radius:50%;background:#1ed992;box-shadow:0 0 8px rgba(30,217,146,.9)}
.btn-run{font-family:var(--sans);font-size:12.5px;font-weight:600;color:#cdd6e6;background:rgba(77,127,255,.1);
  border:1px solid rgba(77,127,255,.32);padding:9px 15px;border-radius:9px;cursor:pointer;transition:.18s;
  position:relative;z-index:1;text-decoration:none;display:inline-flex;align-items:center;gap:7px}
.btn-run:hover{background:rgba(77,127,255,.2);border-color:var(--accent);transform:translateY(-1px);box-shadow:0 0 16px rgba(77,127,255,.35)}

/* TABS */
.tabs-container{display:flex;gap:3px;margin:18px 0;background:var(--surface);padding:4px;border-radius:11px;
  border:1px solid var(--line);width:fit-content;overflow-x:auto;scrollbar-width:none}
.tabs-container::-webkit-scrollbar{display:none}
.tab-btn{font-family:var(--sans);font-size:13px;font-weight:600;color:var(--ink-2);padding:8px 16px;border:none;
  background:transparent;border-radius:8px;cursor:pointer;transition:.18s;white-space:nowrap}
.tab-btn:hover{color:var(--ink);background:var(--surface-3)}
.tab-btn.active{background:linear-gradient(135deg,#3a63d8,#4d7fff);color:#fff;box-shadow:0 0 16px rgba(77,127,255,.4)}
.tab-content{display:none;animation:fade .35s ease}
.tab-content.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* SECTION / CARD */
.section-head{display:flex;align-items:baseline;gap:11px;margin:2px 0 14px}
.section-head h2{font-size:14px;font-weight:700;letter-spacing:.02em}
.section-head .eyebrow{font-size:10px;font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:.16em}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px;margin-bottom:16px}
.card-title{font-size:13.5px;font-weight:700;margin-bottom:13px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
.toggles{font-size:11.5px;font-weight:500;color:var(--ink-2);display:flex;gap:13px;align-items:center;flex-wrap:wrap;
  background:var(--surface-2);padding:6px 11px;border-radius:8px;border:1px solid var(--line)}
.toggles .lab{color:var(--ink-3);font-size:10.5px}
.toggles label{cursor:pointer;display:inline-flex;align-items:center;gap:5px;user-select:none}
.toggles input{accent-color:var(--accent)}
.chart-box{width:100%;border:1px solid var(--line);border-radius:11px;background:var(--surface-2);overflow:hidden;margin-bottom:6px}

/* METRICS */
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.metric{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px 13px;
  position:relative;overflow:hidden;transition:.2s}
.metric:hover{border-color:#2b3645;transform:translateY(-2px)}
.metric::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ink-3);opacity:.6}
.metric.accent::before{background:linear-gradient(var(--accent),#5fd3ff);opacity:1;box-shadow:0 0 12px rgba(77,127,255,.6)}
.metric.up::before{background:var(--up);box-shadow:0 0 12px rgba(255,82,91,.5)}
.metric.down::before{background:var(--down);box-shadow:0 0 12px rgba(30,217,146,.5)}
.metric .label{font-size:10px;font-weight:600;color:var(--ink-3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px}
.metric .value{font-size:21px;font-weight:600;letter-spacing:-.02em;line-height:1}
.metric .change{font-size:11.5px;font-weight:600;margin-top:7px;display:inline-flex;align-items:center;gap:5px}
.metric .chip{display:inline-flex;align-items:center;gap:3px;padding:2px 6px;border-radius:5px;font-size:10.5px}
.oi-sub{display:flex;gap:12px;margin-top:6px;font-size:11px;color:var(--ink-2)}
.chip-up{background:var(--up-soft);color:var(--up)} .chip-down{background:var(--down-soft);color:var(--down)}

/* STATUS */
.status-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.status-card{background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.status-card .k{font-size:10px;color:var(--ink-3);font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.status-card .v{font-size:14px;font-weight:700;margin-top:5px;font-family:var(--mono)}
.status-card .src{font-size:10.5px;color:var(--ink-3);margin-top:5px}
.status-note{font-size:11px;color:var(--ink-3);margin-top:10px;line-height:1.5}

/* RATING */
.rating-wrap{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:20px}
.rating-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding-bottom:13px;border-bottom:1px solid var(--line)}
.rating-top h3{font-size:14px;font-weight:700}
.rating-top .upd{font-size:11px;color:var(--ink-3);font-family:var(--mono)}
.rating-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:11px}
.rcol{background:var(--surface-2);border:1px solid var(--line);border-radius:11px;padding:11px;border-top:2px solid}
.rcol[data-k=sb]{border-top-color:#ff525b} .rcol[data-k=b]{border-top-color:#ff8a6b}
.rcol[data-k=n]{border-top-color:#6b7686} .rcol[data-k=s]{border-top-color:#3ddca0} .rcol[data-k=ss]{border-top-color:#1ed992}
.rcol-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.rcol-label{font-size:12px;font-weight:700}
.rcol[data-k=sb] .rcol-label{color:#ff525b} .rcol[data-k=b] .rcol-label{color:#ff8a6b}
.rcol[data-k=n] .rcol-label{color:#8b95a5} .rcol[data-k=s] .rcol-label{color:#3ddca0} .rcol[data-k=ss] .rcol-label{color:#1ed992}
.rcol-count{font-size:10.5px;font-weight:600;color:var(--ink-2);background:var(--surface-3);border:1px solid var(--line);border-radius:20px;padding:1px 8px}
.schip{background:var(--surface-3);border:1px solid var(--line);border-radius:8px;padding:7px 9px;margin-bottom:6px;transition:.16s}
.schip:hover{border-color:var(--accent-dim);transform:translateX(2px)}
.schip-top{display:flex;justify-content:space-between;align-items:baseline}
.schip-name{font-size:12px;font-weight:600}
.schip-chg{font-size:11px;font-weight:600;font-family:var(--mono)}
.schip-meta{display:flex;gap:6px;margin-top:5px;flex-wrap:wrap}
.rating-chip summary{list-style:none;cursor:pointer}
.rating-chip summary::-webkit-details-marker{display:none}
.rating-reason{font-size:10.5px;color:var(--ink-2);line-height:1.55;margin-top:7px;border-top:1px dashed var(--line);padding-top:7px}
.rating-summary{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--ink-2);margin-bottom:10px}
.tag{font-size:9.5px;font-weight:600;padding:1px 6px;border-radius:5px;background:var(--surface);color:var(--ink-2)}
.tag.t{background:rgba(77,127,255,.14);color:#7f9fff} .tag.c{background:var(--down-soft);color:var(--down)}
.empty{font-size:11px;color:var(--ink-3);text-align:center;padding:14px 0}
.legend{margin-top:15px;border-top:1px dashed var(--line);padding-top:13px}
.legend summary{cursor:pointer;font-size:12px;color:var(--ink-2);font-weight:600;list-style:none;display:inline-flex;gap:6px;align-items:center}
.legend summary::-webkit-details-marker{display:none}
.legend summary:hover{color:var(--accent-2)}
.legend-body{margin-top:11px;display:flex;flex-direction:column;gap:8px;font-size:11.5px;color:var(--ink-2);
  background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:13px}
.legend-row{display:flex;gap:10px;align-items:flex-start;line-height:1.5}
.lbadge{font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:5px;white-space:nowrap}
.lbadge.t{background:rgba(77,127,255,.14);color:#7f9fff} .lbadge.c{background:var(--down-soft);color:var(--down)} .lbadge.tt{background:rgba(224,168,60,.15);color:var(--gold)}
.lbadge.f{background:rgba(176,123,255,.15);color:#b07bff}

/* v2: 基本面分數與 Tilt */
.tag.f.fp{background:var(--up-soft);color:var(--up)} .tag.f.fn{background:var(--down-soft);color:var(--down)}
.tag.f.fna{background:var(--surface);color:var(--ink-3)}
.tbadge{display:inline-flex;align-items:center;gap:2px;font-size:9px;font-weight:700;padding:1px 5px;border-radius:5px;margin-left:4px;white-space:nowrap;vertical-align:middle}
.tbadge.tup{background:var(--up-soft);color:var(--up)} .tbadge.tdn{background:var(--down-soft);color:var(--down)}
.dtable tr.bucket-head td{background:var(--surface-2);color:var(--ink-2);font-size:10.5px;font-weight:700;font-family:var(--sans);text-align:left}
.dtable td.fneg{color:var(--down)}
.dtable tr.na-row td{color:var(--ink-3)}

/* DATA TABLE */
.dtable{width:100%;font-size:12px;border-collapse:collapse}
.dtable th{text-align:right;padding:7px 10px;background:var(--surface-2);color:var(--ink-2);font-weight:600;font-size:10.5px;
  border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.03em}
.dtable th:first-child{text-align:left}
.dtable td{padding:7px 10px;border-bottom:1px solid var(--line-2);text-align:right;font-family:var(--mono);font-weight:500}
.dtable td:first-child{text-align:left;font-family:var(--sans);font-weight:600;color:var(--ink)}
.dtable tr:last-child td{border-bottom:none}
.dtable .row-total td{background:var(--surface-2);font-weight:700}
.diff{font-size:10px;margin-left:3px}

/* AI BOX */
.ai-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.ai-box{border:1px solid var(--line);border-radius:11px;padding:16px;position:relative;overflow:hidden;
  background:linear-gradient(180deg,var(--surface),var(--surface-2))}
.ai-box::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(var(--accent),#5fd3ff);box-shadow:0 0 10px rgba(77,127,255,.5)}
.ai-box.oper::before{background:linear-gradient(var(--gold),#f0c060);box-shadow:0 0 10px rgba(224,168,60,.5)}
.ai-head{display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:13px;font-weight:700;color:var(--accent-2)}
.ai-box.oper .ai-head{color:var(--gold)}
.ai-mono{width:21px;height:21px;border-radius:6px;background:linear-gradient(135deg,#3a63d8,#4d7fff);color:#fff;font-size:9.5px;font-weight:700;display:grid;place-items:center;font-family:var(--mono)}
.ai-box.oper .ai-mono{background:linear-gradient(135deg,var(--gold),#f0c060)}
.ai-text{font-size:12.5px;color:var(--ink);line-height:1.65}
.ai-divider{margin:11px 0;border-top:1px dashed var(--line);padding-top:11px}

/* PANEL / FUND */
.panel{background:var(--surface-2);border:1px solid var(--line);border-radius:11px;padding:15px;margin-bottom:16px}
.panel-head{font-size:12.5px;font-weight:700;color:var(--accent-2);display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.src-pill{font-size:9.5px;font-weight:600;color:var(--ink-3);background:var(--surface-3);border:1px solid var(--line);padding:2px 8px;border-radius:20px;letter-spacing:.02em}
.tdcc-scroll{overflow-x:auto}
.fund-tables{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:13px}
.mini-table{background:var(--surface);border:1px solid var(--line);border-radius:9px;overflow:hidden}
.mini-table .cap{font-size:11px;font-weight:700;text-align:center;padding:7px;background:var(--surface-3);color:var(--ink-2)}
.fund-strip{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:11px;overflow:hidden;margin-bottom:18px}
.fund-cell{background:var(--surface-2);padding:12px 14px}
.fund-cell .k{font-size:10px;color:var(--ink-3);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.fund-cell .v{font-size:16px;font-weight:600;margin-top:5px;font-family:var(--mono);color:var(--ink)}

/* NEWS */
.news{margin-top:4px;border:1px solid var(--line);border-radius:11px;overflow:hidden}
.news summary{padding:12px 15px;background:var(--surface-2);cursor:pointer;font-size:12.5px;font-weight:700;color:var(--ink-2);
  list-style:none;display:flex;justify-content:space-between;align-items:center}
.news summary::-webkit-details-marker{display:none}
.news-list{padding:4px 15px 8px}
.news-item{padding:10px 0;border-bottom:1px dashed var(--line-2)}
.news-item:last-child{border-bottom:none}
.news-item .d{font-size:10.5px;color:var(--ink-3);font-family:var(--mono);margin-bottom:3px}
.news-item a{font-size:12.5px;font-weight:500;color:var(--ink);text-decoration:none}
.news-item a:hover{color:var(--accent-2)}

/* SIDEBAR + STOCK CARD */
.app-layout{display:flex;gap:16px;align-items:flex-start}
.sidebar{width:280px;flex-shrink:0;position:sticky;top:20px;display:flex;flex-direction:column;gap:8px;
  max-height:calc(100vh - 40px);overflow-y:auto;overscroll-behavior:contain;padding-right:6px;scrollbar-width:thin;
  scrollbar-color:var(--accent-dim) transparent}
.sidebar.desktop-only{display:flex}
.sidebar::-webkit-scrollbar{width:7px}
.sidebar::-webkit-scrollbar-track{background:transparent}
.sidebar::-webkit-scrollbar-thumb{background:var(--accent-dim);border-radius:999px}
.sidebar::-webkit-scrollbar-thumb:hover{background:var(--accent)}
.sidebar-title{font-size:12px;font-weight:700;color:var(--ink-2);text-transform:uppercase;letter-spacing:.08em;
  padding:2px 2px 8px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:10;
  background:linear-gradient(180deg,var(--bg) 0%,rgba(10,14,21,.96) 72%,rgba(10,14,21,0) 100%)}
.side-add{display:flex;gap:5px}
.side-add input{width:62px;padding:6px 8px;border:1px solid var(--line);border-radius:7px;font-size:12px;font-family:var(--mono);
  outline:none;background:var(--surface-2);color:var(--ink);transition:.16s}
.side-add input::placeholder{color:var(--ink-3)}
.side-add input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(77,127,255,.15)}
.side-add button{background:linear-gradient(135deg,#3a63d8,#4d7fff);color:#fff;border:none;padding:6px 11px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;transition:.16s}
.side-add button:hover{box-shadow:0 0 14px rgba(77,127,255,.45)}
.sidebar-item{background:var(--surface);border:1px solid var(--line);border-radius:11px;padding:11px 13px;cursor:pointer;
  transition:.16s;position:relative}
.sidebar-item::before{content:"";position:absolute;left:0;top:13px;bottom:13px;width:3px;border-radius:3px;background:transparent;transition:.16s}
.sidebar-item:hover{border-color:#2b3645}
.sidebar-item.active{border-color:var(--accent-dim);background:linear-gradient(var(--surface),var(--surface-3))}
.sidebar-item.active::before{background:linear-gradient(var(--accent),#5fd3ff);box-shadow:0 0 10px rgba(77,127,255,.6)}
.nav-top{display:flex;justify-content:space-between;align-items:baseline;font-size:13.5px;font-weight:700}
.nav-top .pct{font-family:var(--mono);font-size:12.5px}
.nav-bottom{display:flex;justify-content:space-between;font-size:11px;color:var(--ink-2);margin-top:5px}
.nav-bottom .score{font-family:var(--mono)}
.side-del{position:absolute;top:8px;right:8px;width:20px;height:20px;text-align:center;line-height:20px;border-radius:4px;
  color:var(--ink-3);font-size:12px;font-weight:bold;cursor:pointer;transition:.16s;z-index:5}
.side-del:hover{color:var(--ink);background:var(--surface-3)}
.rbadge{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:6px}
.rbadge.sb{background:var(--up-soft);color:var(--up)} .rbadge.b{background:rgba(255,138,107,.14);color:#ff8a6b}
.rbadge.n{background:var(--surface-3);color:var(--ink-2)} .rbadge.s{background:var(--down-soft);color:var(--down)} .rbadge.ss{background:var(--down-soft);color:var(--down)}
.main-content{flex:1;min-width:0}
.stock-card{display:none;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
.stock-card.active{display:block}
.sc-body{padding:22px}
.sc-header{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:14px;padding-bottom:16px;border-bottom:1px solid var(--line);margin-bottom:16px}
.sc-id{font-size:11px;font-weight:600;color:var(--accent);font-family:var(--mono);letter-spacing:.05em}
.sc-name{font-size:22px;font-weight:800;letter-spacing:-.01em;margin-top:2px;display:flex;align-items:baseline;gap:12px}
.sc-price{font-family:var(--mono);font-size:19px;font-weight:600}
.sc-meta{display:flex;gap:24px;align-items:center}
.sc-meta .block{text-align:right}
.sc-meta .k{font-size:10px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.sc-meta .v{font-size:15px;font-weight:700}
.sc-meta .v.target{font-family:var(--mono);color:var(--accent-2)}
.subhead{font-size:13px;font-weight:700;margin-bottom:10px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}

/* MOBILE */
.mobile-only{display:none} .desktop-only{display:block}
.mh-del{position:absolute;top:12px;right:15px;color:var(--ink-3);font-size:15px;font-weight:bold;padding:0 8px;z-index:10;cursor:pointer}
.mh-top{display:flex;align-items:center;font-size:15px;font-weight:700;padding-right:30px}
.mh-icon{margin-right:8px;color:var(--accent);font-size:11px}
.mh-name{font-weight:700}
.mh-price{margin-left:auto;font-family:var(--mono)}
.mh-bottom{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--ink-2);padding-left:20px}
.mh-score{font-family:var(--mono)}

/* FOLD (collapsible sections) */
.fold{border:1px solid var(--line);border-radius:11px;margin-bottom:12px;overflow:hidden}
.fold>summary{padding:11px 14px;background:var(--surface-2);cursor:pointer;font-size:12.5px;font-weight:700;color:var(--ink-2);
  list-style:none;display:flex;justify-content:space-between;align-items:center;user-select:none}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary::after{content:"▾";font-size:11px;color:var(--ink-3);transition:.2s}
.fold:not([open])>summary::after{transform:rotate(-90deg)}
.fold>summary:hover{color:var(--accent-2)}
.fold>div,.fold>.grid-2,.fold>.fund-tables,.fold>.ai-grid,.fold>.tdcc-scroll{padding:14px}

/* CHEVRON for mobile accordion */
.chevron{font-size:10px;color:var(--accent);margin-right:8px;transition:.2s;flex-shrink:0}
.stock-card.expanded .chevron{transform:rotate(90deg)}

@media(max-width:980px){
  .metrics{grid-template-columns:repeat(2,1fr)}
  .status-grid{grid-template-columns:repeat(2,1fr)}
  .fund-strip{grid-template-columns:repeat(3,1fr)}
  .grid-2,.ai-grid{grid-template-columns:1fr}
  .rating-grid{grid-template-columns:repeat(2,1fr)}
  .sidebar{display:none}
  .main-content{width:100%}
  .desktop-only{display:none !important}
  .mobile-only{display:flex}
  .stock-card{display:block;margin-bottom:8px;border-radius:11px}
  .sc-body{padding:0}
  .sc-header{padding:14px;cursor:pointer;border-radius:11px}
  .sc-detail{display:none;padding:14px;padding-top:0;border-top:1px solid var(--line);background:var(--surface-2)}
  .stock-card.expanded .sc-detail{display:block}
  .sc-meta{gap:14px;flex-wrap:wrap}
  .sc-name{font-size:17px}
  .fold>summary{padding:9px 12px;font-size:11.5px}
  .fold>div,.fold>.grid-2,.fold>.fund-tables,.fold>.ai-grid,.fold>.tdcc-scroll{padding:10px}
}
"""

def generate_html(stocks_data: dict, market_data: dict) -> str:
    update_time = now_tw().strftime("%Y-%m-%d %H:%M")
    market_section = generate_market_section(market_data)
    status_panel = generate_data_status_panel(stocks_data, market_data, update_time)
    rating_table = generate_rating_table(stocks_data)

    sidebar_items = ""
    stock_cards = ""
    is_first = True
    rk_map = {"strong-buy": "sb", "buy": "b", "neutral": "n", "sell": "s", "strong-sell": "ss"}
    for stock_id, data in stocks_data.items():
        stock_cards += generate_stock_card(stock_id, data, is_first)
        r = data["rating"]
        rk = rk_map.get(r.get("rating_key", "neutral"), "n")
        cls = "up" if data.get("change_pct",0) >= 0 else "down"
        sign = "+" if data.get("change_pct",0) >= 0 else ""
        sidebar_items += f'''
        <div class="sidebar-item {"active" if is_first else ""}" id="nav_{stock_id}" onclick="showStock('{stock_id}')">
            <div class="side-del" onclick="confirmDelete(event, '{stock_id}', this)" title="刪除 {stock_id}">✖</div>
            <div class="nav-top"><span>{stock_id} {data.get("name","")}</span><span class="pct {cls}">{sign}{data.get("change_pct",0):.2f}%</span></div>
            <div class="nav-bottom"><span class="rbadge {rk}">★ {r.get("rating","")}</span><span class="score">技{r.get("tech",0):g} / 籌{r.get("chip",0):g}</span></div>
        </div>'''
        is_first = False

    chart_scripts = generate_chart_scripts(stocks_data, market_data)

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股票監控儀表板</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=Noto+Sans+TC:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="inventory.css?v=20260622">
<link rel="stylesheet" href="risk.css?v=20260630">
<style>
{get_css()}
</style>
</head>
<body>
<div class="container">
  <div class="header" id="top">
    <div><h1>📊 股票監控儀表板</h1><div class="update-time">最後更新 {update_time} · 即時連線</div></div>
    <button id="runBtn" class="btn-run" onclick="triggerAction(this)">⟳ 重新抓取最新資料</button>
  </div>

  <div class="tabs-container">
      <button class="tab-btn active" onclick="switchTab('tab-market', this)">大盤總覽</button>
      <button class="tab-btn" onclick="switchTab('tab-rating', this)">綜合評等</button>
      <button class="tab-btn" onclick="switchTab('tab-stocks', this)">追蹤個股分析</button>
      <button class="tab-btn" id="tabbtn-inventory" onclick="switchTab('tab-inventory', this)">庫存與持股比例</button>
      <button class="tab-btn" onclick="switchTab('tab-risk', this)">期貨風險曝險</button>
  </div>

  <div id="tab-inventory" class="tab-content">
    <div class="section-head"><span class="eyebrow">Portfolio</span><h2>庫存與持股比例</h2><span class="inv-updated" id="invUpdated"></span></div>
    <div id="invSummary" class="inv-summary"></div>
    <div class="inv-grid">
      <div class="card"><div class="card-title"><span>持股比例</span></div><div id="invPie" class="inv-pie"></div></div>
      <div class="card inv-table-card"><div class="card-title"><span>庫存明細</span></div><div id="invTableWrap"></div></div>
    </div>
    <div id="invFuturesCard" class="card" style="display:none">
      <div class="card-title"><span>期貨留倉 · 風險控管</span><span class="inv-updated" id="invFutPnl"></span></div>
      <div id="invFutRisk" class="inv-risk"></div>
      <div class="inv-table-card" id="invFuturesWrap"></div>
    </div>
  </div>

  <div id="tab-risk" class="tab-content"><div id="riskRoot"></div></div>

  <div id="tab-market" class="tab-content active">{status_panel}{market_section}</div>
  <div id="tab-rating" class="tab-content">{rating_table}</div>

  <div id="tab-stocks" class="tab-content">
      <div class="app-layout">
        <div class="sidebar desktop-only">
            <div class="sidebar-title"><span>個股清單</span>
                <div class="side-add"><input type="text" id="stockInput" class="num" placeholder="股號"><button onclick="manageStock('add', null, 'stockInput', this)">新增</button></div>
            </div>
            {sidebar_items}
        </div>
        <div class="main-content">{stock_cards}</div>
      </div>
  </div>
</div>
<button id="backToTop" onclick="window.scrollTo({{top:0,behavior:'smooth'}});" style="display:none;position:fixed;bottom:30px;right:30px;background:linear-gradient(135deg,#3a63d8,#4d7fff);color:#fff;border:none;border-radius:50px;padding:10px 18px;cursor:pointer;box-shadow:0 0 18px rgba(77,127,255,.5);z-index:9999;font-weight:bold;font-size:13px">↑ 返回頂部</button>

<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script>
{chart_scripts}

window.DASHBOARD_CONFIG = {{
  gasUrl: 'https://script.google.com/macros/s/AKfycbyH5tWwcZoqHACX5yZx5xFBnPgiLFMUEvru4SL64IPyuPQckLl5N1yjUIJ3ADBm70VU/exec',
  triggerUrl: 'https://script.google.com/macros/s/AKfycbxnUDMfJgGIVxuKUz6DlqGcvOXAKHXP2GnBtNSEdRdslnd8sqPv9irKAlh8e3z1svNFnA/exec'
}};
</script>
<script src="app.js?v=20260816"></script>
<script src="inventory.js?v=20260622"></script>
<script src="risk.js?v=20260630"></script>
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

    fund        = get_fundamentals(stock_id)

    df = stock_data["df"]
    inst_hist = {d["date"]: d for d in institution.get("history", [])}
    marg_hist = {d["date"]: d for d in margin.get("history", [])}
    borr_hist = {d["date"]: d for d in borrow.get("history", [])}
    chart_data = {"dates": [], "inst_foreign": [], "inst_trust": [], "inst_dealer": [], "inst_cum": [], "margin_diff": [], "margin_bal": [], "short_diff": [], "short_bal": [], "borrow_diff": [], "borrow_bal": [], "inst_f_ratio": [], "inst_t_ratio": [], "inst_d_ratio": []}

    cum_inst = 0
    for d_ts, row in df.tail(90).iterrows():
        d_str, d_short = d_ts.strftime("%Y-%m-%d"), d_ts.strftime("%m-%d")
        price = float(row["Close"])
        vol = float(row["Volume"])
        chart_data["dates"].append(d_short)

        i_data = inst_hist.get(d_str, {"foreign":0, "trust":0, "dealer":0, "foreign_buy":0, "foreign_sell":0, "trust_buy":0, "trust_sell":0, "dealer_buy":0, "dealer_sell":0})

        f_amt = i_data.get("foreign", 0) / 1000
        t_amt = i_data.get("trust", 0) / 1000
        d_amt = i_data.get("dealer", 0) / 1000

        cum_inst += (f_amt + t_amt + d_amt)
        chart_data["inst_foreign"].append(round(f_amt, 2)); chart_data["inst_trust"].append(round(t_amt, 2)); chart_data["inst_dealer"].append(round(d_amt, 2)); chart_data["inst_cum"].append(round(cum_inst, 2))

        f_b, f_s = i_data.get("foreign_buy", 0), i_data.get("foreign_sell", 0)
        t_b, t_s = i_data.get("trust_buy", 0), i_data.get("trust_sell", 0)
        d_b, d_s = i_data.get("dealer_buy", 0), i_data.get("dealer_sell", 0)

        chart_data["inst_f_ratio"].append(round((((f_b + f_s) / 2) / vol * 100), 2) if vol > 0 else 0)
        chart_data["inst_t_ratio"].append(round((((t_b + t_s) / 2) / vol * 100), 2) if vol > 0 else 0)
        chart_data["inst_d_ratio"].append(round((((d_b + d_s) / 2) / vol * 100), 2) if vol > 0 else 0)

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
        "change_pct": change_pct, "ai_tech": ai_tech, "ai_chip": ai_chip, "ai_oper": ai_oper, "name": name, "chart_data": chart_data,
        "fundamentals": fund
    }
    record["rating"] = calculate_stock_rating(record)
    rt = record["rating"]
    tilt_str = f"{rt['fund_tilt']:+d}" if rt['fund_tilt'] else "0"
    print(f"    ✓ {stock_id} 評等: {rt['rating']} (技{rt['tech']:g}/籌{rt['chip']:g}/基{rt['fund']:+g} tilt {tilt_str})")
    return stock_id, record

def main():
    print(f'=== 股票監控機器人 v4.3 深色版 ({now_tw().strftime("%Y-%m-%d %H:%M")}) ===\n')
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
