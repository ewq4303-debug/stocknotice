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
from datetime import datetime, timedelta, timezone
import yfinance as yf
import pandas as pd
import anthropic

# 台灣時區 (UTC+8)
TW_TZ = timezone(timedelta(hours=8))

def now_tw():
    """取得台灣時間 datetime"""
    return datetime.now(TW_TZ)

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


def calculate_atr(high, low, close, period=7):
    """計算 ATR (Average True Range)"""
    hl = high - low
    hc = (high - close.shift()).abs()
    lc = (low - close.shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calculate_supertrend(df, period=7, multiplier=3):
    """
    Supertrend 指標 (ATR period=7, multiplier=3)
    回傳: (supertrend_value, direction)
      direction: 1=上升趨勢(綠色，買進)，-1=下降趨勢(紅色，賣出)
    """
    high  = df['High'].astype(float)
    low   = df['Low'].astype(float)
    close = df['Close'].astype(float)
    
    atr = calculate_atr(high, low, close, period)
    hl2 = (high + low) / 2
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr
    
    # 預先建立陣列（避免 SettingWithCopyWarning）
    n = len(df)
    final_upper = [None] * n
    final_lower = [None] * n
    supertrend  = [None] * n
    direction   = [None] * n
    
    for i in range(n):
        bu = basic_upper.iloc[i]
        bl = basic_lower.iloc[i]
        c  = close.iloc[i]
        
        # ATR 尚未產生（前 period-1 筆）→ 跳過
        if pd.isna(bu) or pd.isna(bl):
            continue
        
        if i == 0 or final_upper[i-1] is None:
            final_upper[i] = bu
            final_lower[i] = bl
            supertrend[i]  = bu
            direction[i]   = -1
            continue
        
        # 更新 final upper
        if bu < final_upper[i-1] or close.iloc[i-1] > final_upper[i-1]:
            final_upper[i] = bu
        else:
            final_upper[i] = final_upper[i-1]
        
        # 更新 final lower
        if bl > final_lower[i-1] or close.iloc[i-1] < final_lower[i-1]:
            final_lower[i] = bl
        else:
            final_lower[i] = final_lower[i-1]
        
        # 決定 supertrend 與方向
        prev_st = supertrend[i-1]
        if prev_st == final_upper[i-1]:  # 前次下降
            if c <= final_upper[i]:
                supertrend[i] = final_upper[i]
                direction[i]  = -1
            else:
                supertrend[i] = final_lower[i]
                direction[i]  = 1
        else:  # 前次上升
            if c >= final_lower[i]:
                supertrend[i] = final_lower[i]
                direction[i]  = 1
            else:
                supertrend[i] = final_upper[i]
                direction[i]  = -1
    
    return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)


# =========================================================
# 資料抓取 - yfinance (股價、K線)
# =========================================================

def get_stock_data_yf(stock_id: str, days: int = 60):
    """使用 yfinance 取得台股股價資料；自動嘗試上市(.TW)與上櫃(.TWO)"""
    df = None
    suffix_used = None
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days+20)
    
    # 先試上市 (.TW)，失敗再試上櫃 (.TWO)
    for suffix in (".TW", ".TWO"):
        try:
            ticker = yf.Ticker(f"{stock_id}{suffix}")
            temp_df = ticker.history(start=start_date, end=end_date)
            if not temp_df.empty:
                df = temp_df
                suffix_used = suffix
                break
        except Exception as e:
            continue
    
    if df is None or df.empty:
        print(f"    ⚠️ {stock_id} yfinance 無資料 (.TW 與 .TWO 都試過)")
        return None
    
    if suffix_used == ".TWO":
        print(f"    ⓘ {stock_id} 為上櫃股票，使用 {stock_id}{suffix_used}")
    
    try:
        # 只保留最近 days 天
        df = df.tail(days)
        
        # 計算技術指標
        df['SMA_5']  = calculate_sma(df['Close'], 5)
        df['SMA_10'] = calculate_sma(df['Close'], 10)
        df['SMA_20'] = calculate_sma(df['Close'], 20)
        df['SMA_60'] = calculate_sma(df['Close'], 60)
        df['RSI_14'] = calculate_rsi(df['Close'], 14)
        df['MACD'], df['MACD_Signal'] = calculate_macd(df['Close'])
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']   # 柱狀體
        df['K'], df['D'] = calculate_stochastic(df['High'], df['Low'], df['Close'])
        
        # Supertrend (ATR=7, Multiplier=3)
        df['ST'], df['ST_DIR'] = calculate_supertrend(df, period=7, multiplier=3)
        
        # 20 日量價統計
        df['Vol_MA20']    = df['Volume'].rolling(20).mean()
        df['Vol_MA5']     = df['Volume'].rolling(5).mean()
        df['High_20']     = df['Close'].rolling(20).max()    # 近 20 日最高收盤
        
        latest = df.iloc[-1]
        prev   = df.iloc[-2] if len(df) > 1 else latest
        prev2  = df.iloc[-3] if len(df) > 2 else prev
        
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
        
    except Exception as e:
        print(f"    ⚠️ {stock_id} 指標計算錯誤: {e}")
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
    """取得個股三大法人買賣超
    FinMind 回傳格式可能為：
      {date, stock_id, name, buy, sell}  ← BuySell 系列
      或 {date, stock_id, Foreign_Investor_diff, ...} ← 舊格式
    """
    start = (datetime.now() - timedelta(days=days+10)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    
    # 嘗試多個 API 名稱
    raw = []
    api_used = None
    for api_name in ["TaiwanStockInstitutionalInvestorsBuySell",
                     "TaiwanStockInstitutionalInvestors"]:
        raw = fetch_finmind(api_name, data_id=stock_id,
                           start_date=start, end_date=end)
        if raw:
            api_used = api_name
            break
    
    if not raw:
        print(f"    ⚠️ {stock_id} 個股法人 API 全部失敗")
        return {"latest": {}, "foreign_today": 0, "foreign_5d": 0, "foreign_20d": 0,
                "trust_today": 0, "trust_5d": 0, "trust_20d": 0, "dealer_today": 0, "history": []}
    
    # Debug：只在第一支股票印
    if stock_id == STOCKS[0] if STOCKS else False:
        print(f"    [debug] {stock_id} API={api_used}, 首筆={raw[0]}, 欄位={list(raw[0].keys())}")
        unique_names = sorted(set(d.get("name", "") for d in raw))
        if unique_names and unique_names != [""]:
            print(f"    [debug] {stock_id} name 集合: {unique_names}")
    
    # Pivot：把每筆整理成每天一筆
    by_date = {}
    for d in raw:
        date = d.get("date", "")
        if not date:
            continue
        if date not in by_date:
            by_date[date] = {"date": date, "foreign": 0, "trust": 0, "dealer": 0}
        
        # 情境 A: 有 name/buy/sell 欄位 (新版 BuySell API)
        name = d.get("name", "")
        if name:
            buy  = float(d.get("buy", 0))
            sell = float(d.get("sell", 0))
            diff = buy - sell  # 股
            
            name_low = name.lower()
            if "外" in name or "foreign" in name_low:
                by_date[date]["foreign"] += diff
            elif "投信" in name or "trust" in name_low:
                by_date[date]["trust"] += diff
            elif "自營" in name or "dealer" in name_low:
                by_date[date]["dealer"] += diff
        else:
            # 情境 B: 直接有 *_diff 欄位（舊版）
            by_date[date]["foreign"] += float(d.get("Foreign_Investor_diff",
                                              d.get("foreign_investor_diff", 0)))
            by_date[date]["trust"]   += float(d.get("Investment_Trust_diff",
                                              d.get("investment_trust_diff", 0)))
            by_date[date]["dealer"]  += float(d.get("Dealer_diff",
                                              d.get("dealer_diff", 0)))
    
    history = sorted(by_date.values(), key=lambda x: x["date"])[-days:]
    
    if not history:
        return {"latest": {}, "foreign_today": 0, "foreign_5d": 0, "foreign_20d": 0,
                "trust_today": 0, "trust_5d": 0, "trust_20d": 0, "dealer_today": 0, "history": []}
    
    # 累計（除以 1000 → 張）
    foreign_5d  = sum(h["foreign"] for h in history[-5:])  / 1000
    foreign_20d = sum(h["foreign"] for h in history[-20:]) / 1000
    trust_5d    = sum(h["trust"]   for h in history[-5:])  / 1000
    trust_20d   = sum(h["trust"]   for h in history[-20:]) / 1000
    dealer_5d   = sum(h["dealer"]  for h in history[-5:])  / 1000
    dealer_20d  = sum(h["dealer"]  for h in history[-20:]) / 1000
    
    latest = history[-1]
    
    return {
        "latest": latest,
        "foreign_today": latest["foreign"] / 1000,
        "foreign_5d":    foreign_5d,
        "foreign_20d":   foreign_20d,
        "trust_today":   latest["trust"] / 1000,
        "trust_5d":      trust_5d,
        "trust_20d":     trust_20d,
        "dealer_today":  latest["dealer"] / 1000,
        "dealer_5d":     dealer_5d,
        "dealer_20d":    dealer_20d,
        "history": history,
    }


def get_margin_data(stock_id: str, days: int = 30):
    """取得融資、融券餘額（含 5日/20日增減）
    FinMind: MarginPurchaseTodayBalance (融資餘額), ShortSaleTodayBalance (融券餘額)
    """
    start = (datetime.now() - timedelta(days=days+25)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    data = fetch_finmind("TaiwanStockMarginPurchaseShortSale", 
                        data_id=stock_id, start_date=start, end_date=end)
    
    if not data:
        return {
            "margin_balance": 0, "margin_change": 0, "margin_5d": 0, "margin_20d": 0,
            "short_balance": 0, "short_change": 0, "short_5d": 0, "short_20d": 0,
            "history": [],
        }
    
    data = sorted(data, key=lambda x: x.get("date", ""))
    if stock_id == STOCKS[0] if STOCKS else False:
        print(f"    [debug] {stock_id} 融資融券欄位: {list(data[0].keys())}")
    
    # 嘗試多種欄位名稱（FinMind 版本差異）
    def get_balance(d, kind):
        if kind == "margin":
            return float(d.get("MarginPurchaseTodayBalance",
                          d.get("margin_purchase_today_balance",
                          d.get("MarginPurchaseBuy", 0))))
        else:
            return float(d.get("ShortSaleTodayBalance",
                          d.get("short_sale_today_balance",
                          d.get("ShortSaleBuy", 0))))
    
    latest = data[-1]
    margin_balance = int(get_balance(latest, "margin"))
    short_balance  = int(get_balance(latest, "short"))
    
    # 計算 N 日前的餘額，得到增減
    def diff_n_days_ago(n, kind):
        if len(data) <= n:
            return 0
        balance_now = get_balance(data[-1],  kind)
        balance_past = get_balance(data[-n-1], kind)
        return int(balance_now - balance_past)
    
    return {
        "margin_balance": margin_balance,
        "margin_change":  diff_n_days_ago(1,  "margin"),
        "margin_5d":      diff_n_days_ago(5,  "margin"),
        "margin_20d":     diff_n_days_ago(20, "margin"),
        "short_balance":  short_balance,
        "short_change":   diff_n_days_ago(1,  "short"),
        "short_5d":       diff_n_days_ago(5,  "short"),
        "short_20d":      diff_n_days_ago(20, "short"),
        "history": data[-days:],
    }


BALANCE_FIELD = "SBLShortSalesCurrentDayBalance"   # 已驗證的正確欄位


def _empty_borrow():
    return {
        "borrow_balance": 0,
        "borrow_change":  0,
        "borrow_5d":      0,
        "borrow_20d":     0,
        "history":        [],
    }


def get_borrowing_data(stock_id):
    """
    抓借券賣出餘額。
    資料來源：FinMind TaiwanDailyShortSaleBalances（同 TWSE TWT93U）
    """
    end = now_tw().date()
    start = end - timedelta(days=60)

    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset":    "TaiwanDailyShortSaleBalances",
        "data_id":    stock_id,
        "start_date": start.isoformat(),
        "end_date":   end.isoformat(),
    }
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        j = resp.json()
    except Exception as e:
        print(f"[warn] {stock_id} 借券 API 失敗: {e}")
        return _empty_borrow()

    rows = j.get("data") or []
    if not rows:
        # 假日 / 未更新 / API 配額耗盡都會走到這
        return _empty_borrow()

    # 防呆：若 FinMind 哪天改欄位名，至少噴一行讓我們知道
    if BALANCE_FIELD not in rows[0]:
        print(f"[warn] {stock_id} 借券 欄位 '{BALANCE_FIELD}' 不存在! 實際欄位: {list(rows[0].keys())}")
        return _empty_borrow()

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    df["balance"] = pd.to_numeric(df[BALANCE_FIELD], errors="coerce").fillna(0).astype(int)

    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else latest

    return {
        "borrow_balance": int(latest["balance"]),
        "borrow_change":  int(latest["balance"] - prev["balance"]),
        "borrow_5d":      int(df["balance"].tail(5).mean())  if len(df) >= 5  else int(latest["balance"]),
        "borrow_20d":     int(df["balance"].tail(20).mean()) if len(df) >= 20 else int(latest["balance"]),
        "history": [
            {"date": str(r["date"]), "balance": int(r["balance"])}
            for _, r in df.tail(30).iterrows()
        ],
    }
    
    def get_balance(d):
        """嘗試所有可能的借券餘額欄位"""
        for key in [
            "today_balance_volume",
            "TodayBalanceVolume",
            "SecuritiesLendingBalanceVolume",
            "securities_lending_balance_volume",
            "today_remaining_balance_volume",
            "OffsetLoanAndShort",
            "SecuritiesLendingBalance",
            "BalanceVolume",
            "balance_volume",
            "balance",
            "today_balance",
            "TodayBalance",
            "lending_balance",
            "LendingBalance",
        ]:
            if key in d and d[key] is not None:
                try:
                    return float(d[key])
                except (ValueError, TypeError):
                    continue
        return 0.0
    
    latest = data[-1]
    balance = int(get_balance(latest))
    
    def diff_n_days_ago(n):
        if len(data) <= n:
            return 0
        return int(get_balance(data[-1]) - get_balance(data[-n-1]))
    
    return {
        "borrow_balance": balance,
        "borrow_change":  diff_n_days_ago(1),
        "borrow_5d":      diff_n_days_ago(5),
        "borrow_20d":     diff_n_days_ago(20),
        "history": data[-days:],
    }


def get_tdcc_holding(stock_id: str):
    """
    從 GitHub repo 取得集保大戶持股比例變化
    URL: https://raw.githubusercontent.com/ewq4303-debug/stocknotice/main/tdcc_history/YYYYMMDD.csv
    
    CSV 格式: 資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%
    分級 12-15 = 400張以上大戶 (12: 400K-600K, 13: 600K-800K, 14: 800K-1M, 15: >1M 股)
    
    回傳本週 vs 上週的大戶持股比例變化
    """
    GITHUB_BASE = "https://raw.githubusercontent.com/ewq4303-debug/stocknotice/main/tdcc_history"
    BIG_LEVELS  = {"12", "13", "14", "15"}   # 400 張 (400,000股) 以上
    
    def fetch_csv(date_str):
        """下載並解析某天的 CSV，回傳該股票大戶持股比例%"""
        url = f"{GITHUB_BASE}/{date_str}.csv"
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200 or len(r.content) < 1000:
                return None
            
            # UTF-8 with BOM
            text = r.content.decode("utf-8-sig", errors="replace")
            big_ratio = 0.0
            found = False
            
            for line in text.splitlines()[1:]:   # 跳過標頭
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 6:
                    continue
                # 股票代號可能有空格補齊 (例 "2330  ")
                code  = parts[1].strip()
                if code != stock_id:
                    continue
                level = parts[2]
                if level not in BIG_LEVELS:
                    continue
                try:
                    ratio = float(parts[5])
                    big_ratio += ratio
                    found = True
                except ValueError:
                    continue
            
            return big_ratio if found else None
        except Exception as e:
            print(f"    ⚠️ TDCC {date_str} 下載失敗: {e}")
            return None
    
    # 從本週最近的週五往前找最近 2 個有資料的週五
    today = datetime.now()
    cursor = today
    # 先找到本週最近的週五（含今天）
    while cursor.weekday() != 4:
        cursor -= timedelta(days=1)
    
    results = []
    for _ in range(8):   # 最多往前找 8 個週五（約 2 個月）
        date_str = cursor.strftime("%Y%m%d")
        ratio = fetch_csv(date_str)
        if ratio is not None and ratio > 0:
            results.append({"date": cursor.strftime("%Y-%m-%d"), "big_ratio": ratio})
            if len(results) >= 2:
                break
        cursor -= timedelta(days=7)   # 回到上一個週五
    
    if not results:
        return {"big_holder_ratio": 0, "big_holder_change": 0, "trend": 0}
    
    latest_ratio = results[0]["big_ratio"]
    
    if len(results) >= 2:
        prev_ratio = results[1]["big_ratio"]
        change = round(latest_ratio - prev_ratio, 2)
    else:
        change = 0
    
    # Debug 第一支股票印出來
    if STOCKS and stock_id == STOCKS[0]:
        debug_info = ", ".join(f"{r['date']}={r['big_ratio']:.2f}%" for r in results)
        print(f"    [debug] {stock_id} 大戶資料: {debug_info}")
    
    return {
        "big_holder_ratio":  round(latest_ratio, 2),
        "big_holder_change": change,
        "trend":             1 if change > 0 else (-1 if change < 0 else 0),
    }


def get_market_overview():
    """取得大盤資料"""
    start = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    
    # 加權指數 OHLCV
    # 優先用 FinMind (更新及時 + 完整 OHLCV)，yfinance 為備援
    taiex_data = []
    
    # ── 方法 A: FinMind TAIEX ──
    fm_taiex = fetch_finmind("TaiwanStockPrice", data_id="TAIEX",
                             start_date=start, end_date=end)
    if fm_taiex:
        print(f"  [debug] FinMind TAIEX 欄位: {list(fm_taiex[0].keys())}")
        print(f"  [debug] FinMind TAIEX 範圍: {fm_taiex[0].get('date')} ~ {fm_taiex[-1].get('date')}")
        
        for r in sorted(fm_taiex, key=lambda x: x.get("date", ""))[-60:]:
            taiex_data.append({
                "date":  r.get("date", ""),
                "open":  float(r.get("open", r.get("Open", 0))),
                "high":  float(r.get("max",  r.get("High", r.get("high", 0)))),
                "low":   float(r.get("min",  r.get("Low",  r.get("low", 0)))),
                "close": float(r.get("close", r.get("Close", 0))),
                "volume": int(float(r.get("Trading_Volume", r.get("trading_volume", 0)) or 0)),
                "money":  float(r.get("Trading_money", r.get("trading_money", 0)) or 0),
            })
        print(f"  ✓ TAIEX (FinMind): {len(taiex_data)} 筆，最新={taiex_data[-1]['date']}")
        
        # 為大盤計算 Supertrend
        if len(taiex_data) >= 10:
            taiex_df = pd.DataFrame([
                {"Open": d["open"], "High": d["high"], "Low": d["low"], "Close": d["close"]}
                for d in taiex_data
            ])
            st_series, st_dir_series = calculate_supertrend(taiex_df, period=7, multiplier=3)
            for i, d in enumerate(taiex_data):
                v = st_series.iloc[i]
                dr = st_dir_series.iloc[i]
                d["supertrend"]     = float(v) if pd.notna(v) else None
                d["supertrend_dir"] = int(dr) if pd.notna(dr) else 0
    
    # ── 方法 B: 如果 FinMind 空或 OHLC 不完整，fallback 到 yfinance ──
    need_yf = (not taiex_data) or any(d["open"] == 0 for d in taiex_data[-5:])
    if need_yf:
        try:
            taiex_ticker = yf.Ticker("^TWII")
            taiex_df = taiex_ticker.history(period="3mo").tail(60).reset_index()
            print(f"  [debug] yfinance TAIEX 範圍: "
                  f"{taiex_df.iloc[0]['Date'].strftime('%Y-%m-%d')} ~ "
                  f"{taiex_df.iloc[-1]['Date'].strftime('%Y-%m-%d')}")
            
            if not taiex_data:
                # FinMind 完全沒抓到，用 yfinance
                # 仍嘗試從 FinMind 取得成交金額
                money_by_date = {r.get("date", ""): float(r.get("Trading_money", 0) or 0)
                                for r in (fm_taiex or [])}
                
                for _, row in taiex_df.iterrows():
                    date_str = row["Date"].strftime("%Y-%m-%d")
                    taiex_data.append({
                        "date":  date_str,
                        "open":  float(row["Open"]),
                        "high":  float(row["High"]),
                        "low":   float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": int(row["Volume"] or 0),
                        "money":  money_by_date.get(date_str, 0),
                    })
                print(f"  ✓ TAIEX (yfinance): {len(taiex_data)} 筆，最新={taiex_data[-1]['date']}")
        except Exception as e:
            print(f"  ⚠️ yfinance 備援失敗: {e}")
    
    # 大盤三大法人（FinMind 回 [{date,name,buy,sell}, ...]，需 pivot）
    inst_raw = fetch_finmind("TaiwanStockTotalInstitutionalInvestors",
                            start_date=start, end_date=end)
    
    # Debug: 看實際 name 值
    if inst_raw:
        unique_names = sorted(set(d.get("name", "") for d in inst_raw))
        print(f"  [debug] 大盤法人名稱: {unique_names}")
        print(f"  [debug] 大盤法人首筆: {inst_raw[0]}")
    
    # Pivot：每天一筆，名稱用「寬鬆」匹配
    inst_by_date = {}
    for d in inst_raw:
        date = d.get("date", "")
        name = d.get("name", "")
        diff = float(d.get("buy", 0)) - float(d.get("sell", 0))  # 元
        if date not in inst_by_date:
            inst_by_date[date] = {"date": date,
                                  "Foreign_Investor_diff": 0,
                                  "Investment_Trust_diff": 0,
                                  "Dealer_diff": 0}
        name_low = name.lower()
        # 寬鬆匹配：含「外」「foreign」歸外資
        if "外" in name or "foreign" in name_low:
            inst_by_date[date]["Foreign_Investor_diff"] += diff
        elif "投信" in name or "trust" in name_low:
            inst_by_date[date]["Investment_Trust_diff"] += diff
        elif "自營" in name or "dealer" in name_low:
            inst_by_date[date]["Dealer_diff"] += diff
    
    institution = sorted(inst_by_date.values(), key=lambda x: x["date"])[-30:]
    if institution:
        last = institution[-1]
        print(f"  ✓ 大盤三大法人 pivot 後: {len(institution)} 筆，"
              f"最新外資={last['Foreign_Investor_diff']/1e8:+.2f}億 "
              f"投信={last['Investment_Trust_diff']/1e8:+.2f}億")
    
    # 期貨留倉（TX 大台指）
    futures = fetch_finmind("TaiwanFuturesInstitutionalInvestors", 
                           data_id="TX", start_date=start, end_date=end)
    futures = sorted(futures, key=lambda x: x.get("date", ""))[-30:] if futures else []
    
    # 匯率 - 台北外匯發展基金會
    usd_twd = get_usd_twd_rate(days=35)
    usd_twd = usd_twd[-30:]
    
    # ── TMF 散戶多空比（自行計算）────────────────────────
    # 散戶淨未平倉 = -法人淨未平倉
    # 散戶多空比 % = (散戶淨未平倉 / 全市場未平倉) * 100
    
    # A. 抓 TMF 法人未平倉
    tmf_inst = fetch_finmind("TaiwanFuturesInstitutionalInvestors",
                             data_id="TMF", start_date=start, end_date=end)
    # B. 抓 TMF 全市場每日（含未平倉量）
    tmf_daily = fetch_finmind("TaiwanFuturesDaily",
                              data_id="TMF", start_date=start, end_date=end)
    
    if tmf_inst:
        print(f"  [debug] TMF 法人欄位: {list(tmf_inst[0].keys())}")
    if tmf_daily:
        print(f"  [debug] TMF 全市場欄位: {list(tmf_daily[0].keys())}")
    
    # 計算每日法人淨未平倉 (3 家法人加總)
    INST_KEYS = {"外資及陸資", "外資", "投信", "自營商"}
    inst_net_by_date = {}
    for r in tmf_inst:
        date = r.get("date", "")
        identity = r.get("institutional_investors", r.get("name", ""))
        if not any(k in identity for k in INST_KEYS):
            continue
        long_oi  = float(r.get("long_open_interest_balance_volume", 0))
        short_oi = float(r.get("short_open_interest_balance_volume", 0))
        net = long_oi - short_oi
        inst_net_by_date[date] = inst_net_by_date.get(date, 0) + net
    
    # 計算每日全市場未平倉 (所有契約月份加總)
    total_oi_by_date = {}
    for r in tmf_daily:
        date = r.get("date", "")
        # 全市場未平倉量欄位嘗試多種
        oi = float(r.get("open_interest",
              r.get("open_interest_balance",
              r.get("close_open_interest", 0))))
        total_oi_by_date[date] = total_oi_by_date.get(date, 0) + oi
    
    # 合併計算
    retail = []
    for date in sorted(total_oi_by_date.keys()):
        total_oi = total_oi_by_date[date]
        if total_oi == 0:
            continue
        inst_net = inst_net_by_date.get(date, 0)
        retail_net = -inst_net
        ratio = round((retail_net / total_oi) * 100, 2)
        retail.append({
            "date":          date,
            "retail_ratio":  ratio,
            "retail_net_oi": retail_net,
            "total_oi":      total_oi,
            "inst_net_oi":   inst_net,
        })
    retail = retail[-30:]
    print(f"  ✓ TMF 散戶多空比: {len(retail)} 筆"
          f"{'，最新=' + str(retail[-1]['retail_ratio']) + '%' if retail else ''}")
    
    # 台指期未平倉
    futures_oi = fetch_finmind("TaiwanFuturesDaily", 
                              data_id="TX", start_date=start, end_date=end)
    futures_oi = sorted(futures_oi, key=lambda x: x.get("date", ""))[-30:] if futures_oi else []
    
    # 大盤融資
    total_margin = fetch_finmind("TaiwanStockTotalMarginPurchaseShortSale", 
                                start_date=start, end_date=end)
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


def get_usd_twd_rate(days: int = 35):
    """從台北外匯發展基金會抓取 USD/TWD 匯率
    URL: https://www.tpefx.com.tw/uploads/service/tw/{YYYY}nt.csv
    """
    result = []
    
    # 計算需要的年份（30天前可能跨年）
    today = datetime.now()
    cutoff = today - timedelta(days=days)
    years_needed = list({cutoff.year, today.year})  # 去重後的年份列表
    
    for year in sorted(years_needed):
        url = f"https://www.tpefx.com.tw/uploads/service/tw/{year}nt.csv"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            
            # 解析 CSV（嘗試不同編碼）
            content = None
            for enc in ("utf-8", "big5", "cp950", "utf-8-sig"):
                try:
                    content = r.content.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            
            if not content:
                print(f"  ⚠️ 匯率 CSV 編碼解析失敗 ({year})")
                continue
            
            # 逐行解析
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if not lines:
                continue
            
            # 找標頭行（含 Close 或 close 或 CLOSE）
            header = None
            data_start = 0
            for i, line in enumerate(lines):
                cols = [c.strip().lower() for c in line.split(",")]
                if any("close" in c for c in cols):
                    header = [c.strip() for c in line.split(",")]
                    data_start = i + 1
                    break
            
            if not header:
                print(f"  ⚠️ 匯率 CSV 找不到 Close 欄位標頭 ({year}), 嘗試直接解析")
                # 嘗試直接用第一行當標頭
                header = [c.strip() for c in lines[0].split(",")]
                data_start = 1
            
            # 找 Close 欄位索引
            close_idx = None
            date_idx = 0  # 通常第一欄是日期
            for i, h in enumerate(header):
                if h.lower() in ("close", "收盤", "收盤價"):
                    close_idx = i
                if h.lower() in ("date", "日期", "交易日"):
                    date_idx = i
            
            if close_idx is None:
                print(f"  ⚠️ 找不到 Close 欄位，標頭: {header}")
                # 假設最後一欄是 Close
                close_idx = len(header) - 1
            
            # 解析資料行
            for line in lines[data_start:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) <= close_idx:
                    continue
                
                raw_date = parts[date_idx]
                raw_close = parts[close_idx]
                
                if not raw_close or raw_close in ("-", "N/A", ""):
                    continue
                
                # 統一日期格式 → YYYY-MM-DD
                try:
                    # 嘗試 YYYY/MM/DD 或 YYYY-MM-DD 或 MM/DD/YYYY 等格式
                    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                        try:
                            dt = datetime.strptime(raw_date, fmt)
                            date_str = dt.strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            continue
                    else:
                        continue  # 無法解析日期，跳過
                    
                    # 只保留最近 days 天
                    if dt < cutoff:
                        continue
                    
                    close_val = float(raw_close.replace(",", ""))
                    result.append({"date": date_str, "close": close_val})
                    
                except (ValueError, IndexError):
                    continue
            
            print(f"  ✓ 台北外匯 {year} 年: {len([d for d in result if d['date'].startswith(str(year))])} 筆")
            
        except Exception as e:
            print(f"  ⚠️ 台北外匯 {year} 年抓取失敗: {e}")
    
    # 排序、去重、取最近 days 天
    seen = set()
    unique = []
    for d in sorted(result, key=lambda x: x["date"]):
        if d["date"] not in seen:
            seen.add(d["date"])
            unique.append(d)
    
    return unique[-days:]


def get_tmf_retail_ratio(days: int = 30):
    """
    計算微台指 (TMF) 散戶多空比（近 N 個交易日）

    來源 A: futDataDown          → 全市場每日 Total_OI（批量）
    來源 B: futContractsDateDown → 三大法人 Inst_Net_OI（批量）

    公式:
      Retail_Net_OI  = -Inst_Net_OI
      Retail_Ratio % = (Retail_Net_OI / Total_OI) * 100
    """
    URL_A = "https://www.taifex.com.tw/cht/3/futDataDown"
    URL_B = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days + 15)   # 多抓幾天補假日
    start_str = start_dt.strftime("%Y/%m/%d")
    end_str   = end_dt.strftime("%Y/%m/%d")

    # ── 工具函式 ───────────────────────────────────────────
    def clean_num(s):
        """去千分位逗號，轉整數；失敗回傳 0"""
        try:
            return int(str(s).replace(",", "").replace(" ", "").strip())
        except (ValueError, TypeError):
            return 0

    def parse_big5(raw_bytes):
        """Big5 解碼，回傳非空 lines list"""
        for enc in ("big5", "cp950", "utf-8-sig", "utf-8"):
            try:
                text = raw_bytes.decode(enc)
                return [l.strip() for l in text.splitlines() if l.strip()]
            except UnicodeDecodeError:
                continue
        text = raw_bytes.decode("big5", errors="replace")
        return [l.strip() for l in text.splitlines() if l.strip()]

    def find_col(cols, keywords):
        """在 cols 中尋找包含任一 keyword 的欄位索引（回傳最後一個 match）"""
        found = [j for j, c in enumerate(cols) if any(k in c for k in keywords)]
        return found[-1] if found else None

    # ── 來源 A：全市場 TMF 每日 Total_OI ──────────────────
    print(f"  [TMF-A] 抓取 {start_str} ~ {end_str}...")
    try:
        resp_a = requests.post(
            URL_A,
            headers=HEADERS,
            data={
                "down_type":      "1",
                "commodity_id":   "TMF",       # 底線
                "queryStartDate": start_str,
                "queryEndDate":   end_str,
            },
            timeout=30,
        )
        resp_a.raise_for_status()
        lines_a = parse_big5(resp_a.content)
    except Exception as e:
        print(f"  ⚠️ [TMF-A] 請求失敗: {e}")
        return []

    print(f"  [TMF-A] 共 {len(lines_a)} 行")
    if len(lines_a) < 2:
        print("  ⚠️ [TMF-A] 無資料")
        return []

    # 找標頭行
    header_a = None
    date_col_a = oi_col_a = data_a_start = None
    for i, line in enumerate(lines_a):
        cols = [c.strip() for c in line.split(",")]
        oi_idx = find_col(cols, ["未平倉口數", "未平倉合約數"])
        dt_idx = find_col(cols, ["日期", "交易日期", "Date"])
        if oi_idx is not None:
            header_a    = cols
            oi_col_a    = oi_idx
            date_col_a  = dt_idx if dt_idx is not None else 0
            data_a_start = i + 1
            print(f"  [TMF-A] 標頭[{i}]: {cols[:6]}... date_col={date_col_a} oi_col={oi_col_a}")
            break

    if oi_col_a is None:
        print(f"  ⚠️ [TMF-A] 找不到 OI 欄位，前3行: {lines_a[:3]}")
        return []

    # 按日期累加 Total_OI（同一天可能有多個到期月份）
    total_oi_by_date = {}
    for line in lines_a[data_a_start:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) <= max(date_col_a, oi_col_a):
            continue
        raw_date = parts[date_col_a]
        # 統一日期格式
        for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                dt = datetime.strptime(raw_date, fmt)
                date_key = dt.strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        else:
            continue
        total_oi_by_date[date_key] = total_oi_by_date.get(date_key, 0) + clean_num(parts[oi_col_a])

    print(f"  [TMF-A] 解析出 {len(total_oi_by_date)} 個交易日")

    # ── 來源 B：三大法人 Inst_Net_OI ──────────────────────
    print(f"  [TMF-B] 抓取 {start_str} ~ {end_str}...")
    try:
        resp_b = requests.post(
            URL_B,
            headers=HEADERS,
            data={
                "queryStartDate": start_str,
                "queryEndDate":   end_str,
                "commodityId":    "TMF",       # 駝峰式
            },
            timeout=30,
        )
        resp_b.raise_for_status()
        lines_b = parse_big5(resp_b.content)
    except Exception as e:
        print(f"  ⚠️ [TMF-B] 請求失敗: {e}")
        return []

    print(f"  [TMF-B] 共 {len(lines_b)} 行")
    if len(lines_b) < 2:
        print("  ⚠️ [TMF-B] 無資料")
        return []

    # 找標頭行
    date_col_b = id_col_b = net_oi_col_b = data_b_start = None
    for i, line in enumerate(lines_b):
        cols = [c.strip() for c in line.split(",")]
        id_idx  = find_col(cols, ["身份別"])
        net_idx = find_col(cols, ["未平倉多空淨額口數", "淨額口數"])
        dt_idx  = find_col(cols, ["日期", "交易日期", "Date"])
        if id_idx is not None and net_idx is not None:
            date_col_b  = dt_idx if dt_idx is not None else 0
            id_col_b    = id_idx
            net_oi_col_b = net_idx
            data_b_start = i + 1
            print(f"  [TMF-B] 標頭[{i}]: {cols[:8]}... date={date_col_b} id={id_col_b} net={net_oi_col_b}")
            break

    if id_col_b is None:
        print(f"  ⚠️ [TMF-B] 找不到欄位，前3行: {lines_b[:3]}")
        return []

    # 按日期累加法人淨部位
    INST_NAMES = {"外資及陸資", "投信", "自營商"}
    inst_net_by_date = {}
    for line in lines_b[data_b_start:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) <= max(date_col_b, id_col_b, net_oi_col_b):
            continue
        raw_date = parts[date_col_b]
        for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                dt = datetime.strptime(raw_date, fmt)
                date_key = dt.strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        else:
            continue
        identity = parts[id_col_b]
        if identity in INST_NAMES:
            inst_net_by_date[date_key] = inst_net_by_date.get(date_key, 0) + clean_num(parts[net_oi_col_b])

    print(f"  [TMF-B] 解析出 {len(inst_net_by_date)} 個交易日的法人資料")

    # ── 合併計算 ──────────────────────────────────────────
    results = []
    for date_key in sorted(total_oi_by_date.keys()):
        total_oi    = total_oi_by_date[date_key]
        inst_net_oi = inst_net_by_date.get(date_key, 0)
        if total_oi == 0:
            continue
        retail_net_oi = -inst_net_oi
        retail_ratio  = round((retail_net_oi / total_oi) * 100, 2)
        results.append({
            "date":          date_key,
            "total_oi":      total_oi,
            "inst_net_oi":   inst_net_oi,
            "retail_net_oi": retail_net_oi,
            "retail_ratio":  retail_ratio,
        })

    # 只取最近 days 筆
    results = results[-days:]
    print(f"  ✓ TMF 散戶多空比: {len(results)} 筆，"
          f"最新={results[-1]['date'] if results else 'N/A'} "
          f"Ratio={results[-1]['retail_ratio'] if results else 'N/A'}%")
    return results


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


def get_fundamentals(stock_id: str):
    """取得個股基本面數據（yfinance）"""
    result = {
        "trailing_pe":   None,   # P/E (TTM)
        "forward_pe":    None,   # 預期 P/E
        "peg":           None,   # PEG
        "eps_ttm":       None,   # EPS (TTM)
        "eps_forward":   None,   # 預期 EPS
        "eps_growth":    None,   # 盈餘成長率
        "revenue_growth":None,   # 營收成長率
        "market_cap":    None,   # 市值
        "dividend_yield":None,   # 殖利率 %
        "roe":           None,   # 股東權益報酬率 %
    }
    
    # 嘗試 .TW 與 .TWO
    for suffix in (".TW", ".TWO"):
        try:
            ticker = yf.Ticker(f"{stock_id}{suffix}")
            info = ticker.info
            if not info or len(info) < 5:
                continue
            
            result["trailing_pe"]    = info.get("trailingPE")
            result["forward_pe"]     = info.get("forwardPE")
            result["peg"]            = info.get("trailingPegRatio") or info.get("pegRatio")
            result["eps_ttm"]        = info.get("trailingEps") or info.get("epsTrailingTwelveMonths")
            result["eps_forward"]    = info.get("forwardEps")
            result["eps_growth"]     = info.get("earningsGrowth")    # 比例 (0.15 = 15%)
            result["revenue_growth"] = info.get("revenueGrowth")
            result["market_cap"]     = info.get("marketCap")
            div_y                    = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
            if div_y is not None:
                # yfinance 有時回傳 0.025 (=2.5%)，有時回傳 2.5
                result["dividend_yield"] = div_y * 100 if div_y < 1 else div_y
            result["roe"]            = info.get("returnOnEquity")
            
            # Debug 第一支
            if STOCKS and stock_id == STOCKS[0]:
                print(f"    [debug] {stock_id} 基本面: PE={result['trailing_pe']}, "
                      f"FwdPE={result['forward_pe']}, PEG={result['peg']}, "
                      f"EPS={result['eps_ttm']}, RevGrowth={result['revenue_growth']}")
            return result
        except Exception as e:
            continue
    
    return result


# =========================================================
# AI 分析
# =========================================================

def generate_ai_analysis(stock_id: str, stock_name: str, data: dict,
                         institution: dict, margin: dict,
                         borrow: dict = None, tdcc: dict = None):
    """呼叫 Claude API 生成股票分析，回傳 (技術面, 籌碼面, 操作建議) 三段"""
    
    if not ANTHROPIC_API_KEY:
        msg = "AI 分析功能未啟用（請設定 ANTHROPIC_API_KEY）"
        return msg, msg, msg
    
    latest = data["latest"]
    prev = data["prev"]
    ind = data["indicators"]
    borrow = borrow or {}
    tdcc = tdcc or {}
    
    prompt = f"""請分析 {stock_id} {stock_name} 的當前狀況。

## 技術面 (T1~T4 模組)
- 收盤: {latest['close']:.2f} (前日: {prev['close']:.2f})
- 5/10/20/60MA: {ind.get('ma5',0):.2f} / {ind.get('ma10',0):.2f} / {ind.get('ma20',0):.2f} / {ind.get('ma60',0):.2f}
- Supertrend(7,3): {ind.get('supertrend',0):.2f} ({'↑上升' if ind.get('supertrend_dir')==1 else '↓下降'})
- MACD 柱體: {ind.get('macd_hist',0):+.3f} (昨日: {ind.get('macd_hist_prev',0):+.3f})
- 近20日最高收: {ind.get('high_20',0):.2f}
- 5日均量 vs 20日均量: {ind.get('vol_ma5',0):,.0f} vs {ind.get('vol_ma20',0):,.0f}
- KD: {ind.get('k',50):.1f}/{ind.get('d',50):.1f}, RSI: {ind.get('rsi',50):.1f}

## 籌碼面 (C1~C3 模組)
- 外資 今日/5日/20日: {institution['foreign_today']:+.0f} / {institution['foreign_5d']:+.0f} / {institution['foreign_20d']:+.0f} 張
- 投信 今日/5日/20日: {institution['trust_today']:+.0f} / {institution['trust_5d']:+.0f} / {institution['trust_20d']:+.0f} 張
- 融資 餘額/今日/5日: {margin.get('margin_balance',0):,} / {margin.get('margin_change',0):+,} / {margin.get('margin_5d',0):+,} 張
- 借券 餘額/5日: {borrow.get('borrow_balance',0):,} / {borrow.get('borrow_5d',0):+,} 張
- 集保 400張以上大戶比例: {tdcc.get('big_holder_ratio',0):.2f}% (週變化: {tdcc.get('big_holder_change',0):+.2f}%)

請用繁體中文，用 === 分隔三段：

=== 技術面 ===
（約 80 字，聚焦：均線排列、Supertrend 方向、MACD 動能、是否突破壓力、量價配合）

=== 籌碼面 ===
（約 80 字，聚焦：法人共識、散戶退場、大戶持股動向）

=== 操作建議 ===
（約 60 字，短線策略、進出場價位、停損點、風險提示）"""
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text
        
        # 解析三段
        sections = {"技術面": "", "籌碼面": "", "操作建議": ""}
        current = None
        for line in text.split("\n"):
            stripped = line.strip()
            if "===" in stripped:
                for key in sections:
                    if key in stripped:
                        current = key
                        break
            elif current and stripped:
                sections[current] += line + "\n"
        
        ai_tech = sections["技術面"].strip() or text
        ai_chip = sections["籌碼面"].strip() or ""
        ai_oper = sections["操作建議"].strip() or ""
        
        return ai_tech, ai_chip, ai_oper
        
    except Exception as e:
        print(f"  ⚠️ AI 分析失敗: {e}")
        err = f"AI 分析暫時無法使用: {e}"
        return err, err, err


# =========================================================
# HTML 生成（簡化版，包含所有修正）
# =========================================================

def calculate_stock_rating(data: dict) -> dict:
    """
    個股操作建議綜合評等 (技 10 + 籌 10 = 20)
    階梯式給分：符合多頭特徵就部分給分，符合強勢特徵再給滿分。
    
    === 技術面 (10 分) ===
    T1 均線趨勢 (+3):
       Close > 20MA          +1
       20MA  > 60MA          +1
       5MA   > 10MA          +1
    T2 強勢創高 (+3):
       Close >= 20日最高 x 0.97   +1.5
       今日量 > 5日均量            +1.5
    T3 MACD 動能 (+2):
       MACD 柱體 > 0           +2
    T4 量價配合 (+2):
       5日均量 > 20日均量       +2
    
    === 籌碼面 (10 分) ===
    C1 法人買盤 (+4):
       外資近 3 日合計 > 0    +1.5
       投信近 3 日合計 > 0    +1.5
       外資+投信今日同買      +1
    C2 散戶退場 (+3):
       今日融資 < 昨日        +1.5
       今日借券 < 昨日        +1.5
    C3 大戶持股 (+3):
       400 張以上 本週 > 上週  +3
    
    分級門檻:
      ≥ 14 強力加碼 / 10-13 加碼 / 6-9 中性觀望 / 3-5 減碼 / ≤ 2 強力減碼
    """
    ind     = data.get("indicators", {})
    inst    = data.get("institution", {})
    margin  = data.get("margin", {})
    borrow  = data.get("borrow", {})
    tdcc    = data.get("tdcc", {})
    latest  = data.get("latest", {})
    
    breakdown = {}
    
    # ── 技術面 ────────────────────────────────────
    tech = 0.0
    close = latest.get("close", 0)
    ma5   = ind.get("ma5",  0)
    ma10  = ind.get("ma10", 0)
    ma20  = ind.get("ma20", 0)
    ma60  = ind.get("ma60", 0)
    
    # T1: 均線趨勢
    t1a = close > ma20 > 0
    t1b = ma20  > ma60 > 0
    t1c = ma5   > ma10 > 0
    t1_score = (1 if t1a else 0) + (1 if t1b else 0) + (1 if t1c else 0)
    tech += t1_score
    breakdown["T1"] = (t1_score, 3, "均線趨勢",
                       f"站月線:{'✓' if t1a else '✗'} 月>季:{'✓' if t1b else '✗'} 5>10:{'✓' if t1c else '✗'}")
    
    # T2: 強勢創高
    volume   = latest.get("volume", 0)
    high_20  = ind.get("high_20",  0)
    vol_ma5  = ind.get("vol_ma5",  0)
    t2a = (high_20 > 0 and close >= high_20 * 0.97)
    t2b = (volume > vol_ma5 > 0)
    t2_score = (1.5 if t2a else 0) + (1.5 if t2b else 0)
    tech += t2_score
    breakdown["T2"] = (t2_score, 3, "強勢創高",
                       f"近高檔:{'✓' if t2a else '✗'} 量>5均:{'✓' if t2b else '✗'}")
    
    # T3: MACD 動能
    macd_hist = ind.get("macd_hist", 0)
    t3 = macd_hist > 0
    t3_score = 2 if t3 else 0
    tech += t3_score
    breakdown["T3"] = (t3_score, 2, "MACD紅柱", "✓" if t3 else "✗")
    
    # T4: 量價配合
    vol_ma20 = ind.get("vol_ma20", 0)
    t4 = vol_ma5 > vol_ma20 > 0
    t4_score = 2 if t4 else 0
    tech += t4_score
    breakdown["T4"] = (t4_score, 2, "5日量>20日量", "✓" if t4 else "✗")
    
    # ── 籌碼面 ────────────────────────────────────
    chip = 0.0
    
    # 取得最近 3 天法人資料
    inst_hist = inst.get("history", [])
    last3 = inst_hist[-3:] if len(inst_hist) >= 3 else []
    
    foreign_today = inst.get("foreign_today", 0)
    trust_today   = inst.get("trust_today", 0)
    
    # C1: 法人買盤
    foreign_3d_sum = sum(h.get("foreign", 0) for h in last3) if last3 else 0
    trust_3d_sum   = sum(h.get("trust",   0) for h in last3) if last3 else 0
    
    c1a = foreign_3d_sum > 0
    c1b = trust_3d_sum   > 0
    c1c = (foreign_today > 0 and trust_today > 0)
    c1_score = (1.5 if c1a else 0) + (1.5 if c1b else 0) + (1 if c1c else 0)
    chip += c1_score
    breakdown["C1"] = (c1_score, 4, "法人買盤",
                       f"外資3日:{'✓' if c1a else '✗'} 投信3日:{'✓' if c1b else '✗'} 同買:{'✓' if c1c else '✗'}")
    
    # C2: 散戶退場 (單日判定)
    margin_change = margin.get("margin_change", 0)
    borrow_change = borrow.get("borrow_change", 0)
    c2a = margin_change < 0
    c2b = borrow_change < 0
    c2_score = (1.5 if c2a else 0) + (1.5 if c2b else 0)
    chip += c2_score
    breakdown["C2"] = (c2_score, 3, "散戶退場",
                       f"融資減:{'✓' if c2a else '✗'} 借券減:{'✓' if c2b else '✗'}")
    
    # C3: 大戶持股增加
    big_change = tdcc.get("big_holder_change", 0)
    c3 = big_change > 0
    c3_score = 3 if c3 else 0
    chip += c3_score
    breakdown["C3"] = (c3_score, 3, "大戶增持", "✓" if c3 else "✗")
    
    total = tech + chip
    
    # 校準後分類門檻
    if total >= 14:
        rating, rating_key = "強力加碼", "strong-buy"
    elif total >= 10:
        rating, rating_key = "加碼", "buy"
    elif total >= 6:
        rating, rating_key = "中性觀望", "neutral"
    elif total >= 3:
        rating, rating_key = "減碼", "sell"
    else:
        rating, rating_key = "強力減碼", "strong-sell"
    
    return {
        "tech":       round(tech, 1),
        "chip":       round(chip, 1),
        "total":      round(total, 1),
        "rating":     rating,
        "rating_key": rating_key,
        "breakdown":  breakdown,
    }


def generate_rating_table(stocks_data: dict) -> str:
    """生成個股操作建議綜合評等表"""
    
    # 依分類分組
    groups = {
        "strong-buy":  {"label": "強力加碼", "stocks": [], "icon": "ti-arrow-big-up-filled"},
        "buy":         {"label": "加碼",     "stocks": [], "icon": "ti-arrow-up"},
        "neutral":     {"label": "中性",     "stocks": [], "icon": "ti-minus"},
        "sell":        {"label": "減碼",     "stocks": [], "icon": "ti-arrow-down"},
        "strong-sell": {"label": "強力減碼", "stocks": [], "icon": "ti-arrow-big-down-filled"},
    }
    
    for stock_id, data in stocks_data.items():
        rating = data.get("rating", {})
        key = rating.get("rating_key", "neutral")
        if key in groups:
            groups[key]["stocks"].append({
                "stock_id": stock_id,
                "name":     data.get("name", ""),
                "change":   data.get("change_pct", 0),
                "tech":     rating.get("tech", 0),
                "chip":     rating.get("chip", 0),
            })
    
    # 生成 HTML
    cols_html = ""
    for key, g in groups.items():
        stocks = g["stocks"]
        count = len(stocks)
        
        if stocks:
            chips_html = ""
            for s in stocks:
                change_class = "up" if s["change"] >= 0 else "down"
                change_sign  = "+" if s["change"] >= 0 else ""
                chips_html += f"""
        <div class="stock-chip">
          <div class="chip-top">
            <span class="chip-name">{s['stock_id']} {s['name']}</span>
            <span class="chip-change {change_class}">{change_sign}{s['change']:.2f}%</span>
          </div>
          <div class="chip-meta">
            <span class="chip-tag">技{s['tech']:g}</span>
            <span class="chip-tag">籌{s['chip']:g}</span>
          </div>
        </div>"""
        else:
            chips_html = '<div class="empty-hint">無</div>'
        
        cols_html += f"""
    <div class="rating-col rating-col-{key}">
      <div class="rating-col-header">
        <span class="rating-col-label rating-label-{key}">
          <i class="ti {g['icon']}" aria-hidden="true" style="font-size:11px;"></i>
          {g['label']}
        </span>
        <span class="rating-col-count">{count}</span>
      </div>
      {chips_html}
    </div>"""
    
    return f"""
<div class="rating-section">
  <div class="rating-header">
    <div class="rating-title">
      <i class="ti ti-target-arrow" aria-hidden="true"></i>
      個股操作建議綜合評等
    </div>
    <div class="rating-update">更新於 {now_tw().strftime("%Y-%m-%d %H:%M")}</div>
  </div>
  <div class="rating-grid">
    {cols_html}
  </div>
  <details style="background:var(--color-background-secondary);border-radius:8px;padding:8px 12px;margin-top:12px;">
    <summary style="cursor:pointer;font-size:12px;font-weight:500;list-style:none;">
      <i class="ti ti-info-circle" aria-hidden="true"></i> 評分邏輯說明（階梯式給分）
    </summary>
    <div class="scoring-key">
      <div class="key-block">
        <strong>技術面 (10分)</strong>
        T1 均線趨勢 (+3)：站月線+1 / 月>季+1 / 5>10+1<br>
        T2 強勢創高 (+3)：近20日高檔3%內+1.5 / 量>5均量+1.5<br>
        T3 MACD 紅柱 (+2)：柱體>0<br>
        T4 量價配合 (+2)：5日量>20日量
      </div>
      <div class="key-block">
        <strong>籌碼面 (10分)</strong>
        C1 法人買盤 (+4)：外資3日+1.5 / 投信3日+1.5 / 今日同買+1<br>
        C2 散戶退場 (+3)：融資減+1.5 / 借券減+1.5<br>
        C3 大戶持股 (+3)：400張大戶比例週週增
      </div>
      <div class="key-block">
        <strong>綜合評等 (技+籌)</strong>
        強力加碼 ≥14 / 加碼 10-13 / 中性觀望 6-9 / 減碼 3-5 / 強力減碼 ≤2
      </div>
    </div>
  </details>
</div>"""



def generate_fundamentals_block(fund: dict) -> str:
    """生成基本面區塊 HTML"""
    def fmt(v, suffix="", prec=2, percent=False):
        if v is None:
            return "—"
        try:
            v = float(v)
            if percent:
                # yfinance growth 是比例 0.15 = 15%
                v = v * 100
            if abs(v) < 0.01 and v != 0:
                return "—"
            return f"{v:,.{prec}f}{suffix}"
        except (ValueError, TypeError):
            return "—"
    
    def fmt_market_cap(v):
        if v is None:
            return "—"
        try:
            v = float(v)
            if v > 1e12:
                return f"{v/1e12:.2f} 兆"
            elif v > 1e8:
                return f"{v/1e8:.0f} 億"
            else:
                return f"{v:,.0f}"
        except (ValueError, TypeError):
            return "—"
    
    # 判斷估值
    pe  = fund.get("trailing_pe")
    fpe = fund.get("forward_pe")
    peg = fund.get("peg")
    
    pe_tag = ""
    if isinstance(pe, (int, float)) and pe > 0:
        if pe < 15:
            pe_tag = '<span class="label-tag tag-bull">低估</span>'
        elif pe > 30:
            pe_tag = '<span class="label-tag tag-bear">偏貴</span>'
    
    peg_tag = ""
    if isinstance(peg, (int, float)) and peg > 0:
        if peg < 1:
            peg_tag = '<span class="label-tag tag-bull">PEG&lt;1</span>'
        elif peg > 2:
            peg_tag = '<span class="label-tag tag-bear">PEG&gt;2</span>'
    
    return f"""
<div class="indicator-row" style="grid-template-columns:repeat(3,1fr);gap:8px;">
  <div class="indicator-cell">
    <div class="indicator-cell-label">本益比 P/E {pe_tag}</div>
    <div class="indicator-cell-value">{fmt(pe)}</div>
  </div>
  <div class="indicator-cell">
    <div class="indicator-cell-label">預期 Fwd P/E</div>
    <div class="indicator-cell-value">{fmt(fpe)}</div>
  </div>
  <div class="indicator-cell">
    <div class="indicator-cell-label">PEG {peg_tag}</div>
    <div class="indicator-cell-value">{fmt(peg)}</div>
  </div>
</div>
<div class="indicator-row" style="grid-template-columns:repeat(3,1fr);gap:8px;margin-top:6px;">
  <div class="indicator-cell">
    <div class="indicator-cell-label">EPS (TTM)</div>
    <div class="indicator-cell-value">{fmt(fund.get("eps_ttm"))}</div>
  </div>
  <div class="indicator-cell">
    <div class="indicator-cell-label">營收成長</div>
    <div class="indicator-cell-value {'positive' if (fund.get('revenue_growth') or 0) > 0 else 'negative' if (fund.get('revenue_growth') or 0) < 0 else ''}">{fmt(fund.get("revenue_growth"), "%", percent=True, prec=1)}</div>
  </div>
  <div class="indicator-cell">
    <div class="indicator-cell-label">EPS 成長</div>
    <div class="indicator-cell-value {'positive' if (fund.get('eps_growth') or 0) > 0 else 'negative' if (fund.get('eps_growth') or 0) < 0 else ''}">{fmt(fund.get("eps_growth"), "%", percent=True, prec=1)}</div>
  </div>
</div>
<div class="indicator-row" style="grid-template-columns:repeat(3,1fr);gap:8px;margin-top:6px;">
  <div class="indicator-cell">
    <div class="indicator-cell-label">市值</div>
    <div class="indicator-cell-value">{fmt_market_cap(fund.get("market_cap"))}</div>
  </div>
  <div class="indicator-cell">
    <div class="indicator-cell-label">殖利率</div>
    <div class="indicator-cell-value">{fmt(fund.get("dividend_yield"), "%", prec=2)}</div>
  </div>
  <div class="indicator-cell">
    <div class="indicator-cell-label">ROE</div>
    <div class="indicator-cell-value">{fmt(fund.get("roe"), "%", percent=True, prec=1)}</div>
  </div>
</div>"""


def generate_stock_card(stock_id: str, data: dict):
    """生成新版個股卡片：K 線 + 成交量、AI 分析摺疊、合計列、融資融券借券單一表格"""
    latest  = data["latest"]
    prev    = data["prev"]
    ind     = data["indicators"]
    inst    = data["institution"]
    margin  = data["margin"]
    borrow  = data.get("borrow", {})
    news    = data["news"]
    
    ai_tech  = data.get("ai_tech",  "")
    ai_chip  = data.get("ai_chip",  "")
    ai_oper  = data.get("ai_oper",  "")
    
    # 價格與漲跌
    close = latest["close"]
    prev_close = prev["close"]
    change = close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
    change_class = "positive" if change >= 0 else "negative"
    arrow = "↑" if change >= 0 else "↓"
    
    # 成交量（張）
    volume_today  = int(latest.get("volume", 0) / 1000)
    volume_prev   = data.get("volume_prev", 0)
    volume_diff   = volume_today - volume_prev
    volume_pct    = (volume_diff / volume_prev * 100) if volume_prev else 0
    vol_class     = "positive" if volume_diff >= 0 else "negative"
    vol_sign      = "+" if volume_diff >= 0 else ""
    
    # 籌碼合計（張） - 全部從 dict 直接取
    total_today = inst["foreign_today"] + inst["trust_today"]  + inst.get("dealer_today", 0)
    total_5d    = inst["foreign_5d"]    + inst["trust_5d"]     + inst.get("dealer_5d",    0)
    total_20d   = inst["foreign_20d"]   + inst["trust_20d"]    + inst.get("dealer_20d",   0)
    
    # MA60 條件顯示 (資料不足 60 天時 ma60 為 0)
    has_ma60 = ind.get("ma60", 0) > 0
    if has_ma60:
        ma_cells = f"""
    <div class="indicator-cell"><div class="indicator-cell-label">MA5</div><div class="indicator-cell-value">{ind['ma5']:.2f}</div></div>
    <div class="indicator-cell"><div class="indicator-cell-label">MA20</div><div class="indicator-cell-value">{ind['ma20']:.2f}</div></div>
    <div class="indicator-cell"><div class="indicator-cell-label">MA60</div><div class="indicator-cell-value">{ind['ma60']:.2f}</div></div>"""
        ma_row_cols = 5
    else:
        ma_cells = f"""
    <div class="indicator-cell"><div class="indicator-cell-label">MA5</div><div class="indicator-cell-value">{ind['ma5']:.2f}</div></div>
    <div class="indicator-cell"><div class="indicator-cell-label">MA20</div><div class="indicator-cell-value">{ind['ma20']:.2f}</div></div>"""
        ma_row_cols = 4
    
    # 新聞
    news_html = ""
    for n in news[:5]:
        news_html += f'<div class="news-item"><div class="news-time">{n.get("date","")}</div><div class="news-title">{n.get("title","")}</div></div>'
    
    # 評等徽章
    rating = data.get("rating", {})
    rating_label = rating.get("rating", "中性")
    rating_key = rating.get("rating_key", "neutral")
    
    return f"""
<div class="stock-card">
  
  <div class="card-header">
    <div>
      <h3>{stock_id} {data.get('name','')}</h3>
      <div class="card-header-sub">
        綜合評等 <span class="rating-badge rating-label-{rating_key}">{rating_label}</span>
        <span style="margin-left:8px;font-size:11px;color:#999;">
          技{rating.get('tech',0):g} / 籌{rating.get('chip',0):g} (共{rating.get('total',0):g})
        </span>
      </div>
    </div>
    <div style="text-align:right;min-width:160px;">
      <div class="card-price {change_class}">${close:,.2f}</div>
      <div class="card-change {change_class}">{arrow} {change:+.2f} ({change_pct:+.2f}%)</div>
      <div class="card-volume">
        <div class="card-volume-main">成交量 {volume_today:,} 張</div>
        <div class="{vol_class}" style="font-size:11px;">較昨日 {vol_sign}{volume_diff:,} 張 ({vol_sign}{volume_pct:.1f}%)</div>
      </div>
    </div>
  </div>
  
  <!-- K 線圖 + 成交量 -->
  <div class="section-title" style="font-size:13px;font-weight:500;margin:16px 0 8px 0;">
    📊 K 線圖 + 成交量 (近 60 日)
  </div>
  <div id="kline_{stock_id}" style="width:100%;height:280px;"></div>
  
  <!-- 基本面 -->
  <div class="section-title" style="font-size:13px;font-weight:500;margin:16px 0 8px 0;">
    📋 基本面
  </div>
  {generate_fundamentals_block(data.get("fundamentals", {}))}
  
  <!-- 技術面 -->
  <div class="section-title" style="font-size:13px;font-weight:500;margin:16px 0 8px 0;">
    📈 技術面
  </div>
  <div class="indicator-row" style="grid-template-columns:repeat({ma_row_cols},1fr);">{ma_cells}
    <div class="indicator-cell">
      <div class="indicator-cell-label">Supertrend</div>
      <div class="indicator-cell-value {'positive' if ind.get('supertrend_dir')==1 else 'negative'}">
        {('↑ ' if ind.get('supertrend_dir')==1 else '↓ ') + f"{ind.get('supertrend', 0):.2f}"}
      </div>
    </div>
    <div class="indicator-cell"><div class="indicator-cell-label">MACD柱</div><div class="indicator-cell-value {'positive' if ind.get('macd_hist',0)>=0 else 'negative'}">{ind.get('macd_hist',0):+.2f}</div></div>
  </div>
  
  <details class="collapsible">
    <summary>✨ AI 技術面分析</summary>
    <div class="collapsible-content">{ai_tech.replace(chr(10),'<br>') if ai_tech else 'AI 分析載入中...'}</div>
  </details>
  
  <!-- 三大法人 -->
  <div class="section-title" style="font-size:13px;font-weight:500;margin:12px 0 8px 0;">
    👥 籌碼面 - 三大法人買賣超 (張)
  </div>
  <table class="data-table">
    <tr><th>法人</th><th>今日</th><th>5日</th><th>20日</th></tr>
    <tr>
      <td>外資</td>
      <td class="{'positive' if inst['foreign_today']>=0 else 'negative'}">{inst['foreign_today']:+,.0f}</td>
      <td class="{'positive' if inst['foreign_5d']>=0 else 'negative'}">{inst['foreign_5d']:+,.0f}</td>
      <td class="{'positive' if inst['foreign_20d']>=0 else 'negative'}">{inst['foreign_20d']:+,.0f}</td>
    </tr>
    <tr>
      <td>投信</td>
      <td class="{'positive' if inst['trust_today']>=0 else 'negative'}">{inst['trust_today']:+,.0f}</td>
      <td class="{'positive' if inst['trust_5d']>=0 else 'negative'}">{inst['trust_5d']:+,.0f}</td>
      <td class="{'positive' if inst['trust_20d']>=0 else 'negative'}">{inst['trust_20d']:+,.0f}</td>
    </tr>
    <tr>
      <td>自營</td>
      <td class="{'positive' if inst['dealer_today']>=0 else 'negative'}">{inst['dealer_today']:+,.0f}</td>
      <td class="{'positive' if inst.get('dealer_5d',0)>=0 else 'negative'}">{inst.get('dealer_5d',0):+,.0f}</td>
      <td class="{'positive' if inst.get('dealer_20d',0)>=0 else 'negative'}">{inst.get('dealer_20d',0):+,.0f}</td>
    </tr>
    <tr class="row-total">
      <td>合計</td>
      <td class="{'positive' if total_today>=0 else 'negative'}">{total_today:+,.0f}</td>
      <td class="{'positive' if total_5d>=0 else 'negative'}">{total_5d:+,.0f}</td>
      <td class="{'positive' if total_20d>=0 else 'negative'}">{total_20d:+,.0f}</td>
    </tr>
  </table>
  
  <!-- 融資融券借券 -->
  <div class="section-title" style="font-size:13px;font-weight:500;margin:12px 0 8px 0;">
    💰 融資 / 融券 / 借券 (張)
  </div>
  <table class="data-table">
    <tr><th>項目</th><th>今日餘額</th><th>今日增減</th><th>5日增減</th><th>20日增減</th></tr>
    <tr>
      <td>融資</td>
      <td>{margin.get('margin_balance',0):,}</td>
      <td class="{'positive' if margin.get('margin_change',0)>=0 else 'negative'}">{margin.get('margin_change',0):+,}</td>
      <td class="{'positive' if margin.get('margin_5d',0)>=0 else 'negative'}">{margin.get('margin_5d',0):+,}</td>
      <td class="{'positive' if margin.get('margin_20d',0)>=0 else 'negative'}">{margin.get('margin_20d',0):+,}</td>
    </tr>
    <tr>
      <td>融券</td>
      <td>{margin.get('short_balance',0):,}</td>
      <td class="{'positive' if margin.get('short_change',0)>=0 else 'negative'}">{margin.get('short_change',0):+,}</td>
      <td class="{'positive' if margin.get('short_5d',0)>=0 else 'negative'}">{margin.get('short_5d',0):+,}</td>
      <td class="{'positive' if margin.get('short_20d',0)>=0 else 'negative'}">{margin.get('short_20d',0):+,}</td>
    </tr>
    <tr>
      <td>借券</td>
      <td>{borrow.get('borrow_balance',0):,}</td>
      <td class="{'positive' if borrow.get('borrow_change',0)>=0 else 'negative'}">{borrow.get('borrow_change',0):+,}</td>
      <td class="{'positive' if borrow.get('borrow_5d',0)>=0 else 'negative'}">{borrow.get('borrow_5d',0):+,}</td>
      <td class="{'positive' if borrow.get('borrow_20d',0)>=0 else 'negative'}">{borrow.get('borrow_20d',0):+,}</td>
    </tr>
  </table>
  
  <details class="collapsible">
    <summary>✨ AI 籌碼面分析</summary>
    <div class="collapsible-content">{ai_chip.replace(chr(10),'<br>') if ai_chip else 'AI 分析載入中...'}</div>
  </details>
  
  <details class="collapsible" style="background:rgba(55,138,221,0.05);border-color:#bbdefb;">
    <summary>🎯 AI 操作建議</summary>
    <div class="collapsible-content">{ai_oper.replace(chr(10),'<br>') if ai_oper else 'AI 分析載入中...'}</div>
  </details>
  
  <!-- 新聞（可摺疊） -->
  <details class="collapsible" style="margin-top:8px;">
    <summary>📰 相關新聞 ({len(news[:5])})</summary>
    <div class="collapsible-content" style="padding:0 12px 8px 12px;">
      <div class="news-list" style="max-height:none;">{news_html}</div>
    </div>
  </details>
  
</div>"""


def generate_html(stocks_data: dict, market_data: dict):
    """生成完整 HTML"""
    
    update_time = now_tw().strftime("%Y-%m-%d %H:%M")
    
    # 生成評等表
    rating_table = generate_rating_table(stocks_data)
    
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
  {rating_table}
  {timeseries_section}
  
  <div class="section-header">追蹤個股分析</div>
  <div class="stock-grid">{stock_cards}</div>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script>{chart_scripts}</script>
</body>
</html>"""
    
    return html


def _get(d, *keys, default=0.0):
    """安全取值：依序嘗試多個 key，找到非 None 即回傳"""
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                continue
    return float(default)


def generate_market_section(market_data: dict):
    """生成大盤區塊"""
    taiex = market_data["taiex"]
    if not taiex:
        return "<p>大盤資料載入中...</p>"

    latest    = taiex[-1]
    prev      = taiex[-2] if len(taiex) > 1 else latest
    close     = latest["close"]
    prev_close = prev["close"]
    change    = close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
    change_class = "positive" if change > 0 else "negative"
    arrow     = "↑" if change > 0 else "↓"

    # 成交金額：FinMind 回傳「元」→ 換算「億元」
    money_amount = latest.get("money", 0)
    money_b      = money_amount / 100_000_000

    inst     = market_data["institution"]
    inst_latest = inst[-1] if inst else {}

    if inst_latest:
        print(f"  [debug] 大盤法人欄位: {list(inst_latest.keys())}")

    # 嘗試多種欄位名稱（已 pivot，單位為「元」）
    foreign_raw = _get(inst_latest,
                       "Foreign_Investor_diff", "foreign_investor_diff")
    trust_raw   = _get(inst_latest,
                       "Investment_Trust_diff", "investment_trust_diff")
    
    # 單位是「元」，除以 1 億
    divisor = 100_000_000
    foreign = foreign_raw / divisor
    trust   = trust_raw   / divisor
    
    # ── 融資相對指標 ─────────────────────────────
    # 1. 融資餘額 / 大盤總市值 (用「融資餘額 / 加權指數成交金額」近似)
    # 2. 融資增幅% - 大盤漲幅% (反映散戶 vs 大盤相對強弱)
    total_margin = market_data.get("total_margin", [])
    margin_ratio = 0  # 融資 / 成交金額 %
    margin_vs_taiex = 0  # 融資增幅% - 大盤漲幅%
    margin_balance_b = 0  # 融資餘額(億)
    
    if total_margin:
        # 找最新一筆與 5 日前比較
        margin_sorted = sorted(total_margin, key=lambda x: x.get("date", ""))
        latest_m = margin_sorted[-1]
        
        # 嘗試多種欄位名 (可能是 TodayBalance、margin_purchase_today_balance...)
        def margin_value(d):
            for k in ["MarginPurchaseTodayBalance", "margin_purchase_today_balance",
                      "TodayBalance", "today_balance", "MarginPurchaseMoney",
                      "margin_purchase_money", "MarginPurchaseAmount"]:
                if k in d and d[k] is not None:
                    try:
                        return float(d[k])
                    except (ValueError, TypeError):
                        continue
            return 0
        
        margin_now = margin_value(latest_m)
        margin_balance_b = margin_now / 100_000_000  # 元 → 億
        
        # 融資 / 大盤成交金額（同樣是元 → 比例%）
        if money_amount > 0:
            margin_ratio = (margin_now / money_amount) * 100
        
        # 融資 5 日增幅% vs 大盤 5 日漲幅%
        if len(margin_sorted) >= 6:
            margin_5d_ago = margin_value(margin_sorted[-6])
            if margin_5d_ago > 0:
                margin_growth_pct = (margin_now - margin_5d_ago) / margin_5d_ago * 100
                
                # 大盤 5 日漲幅
                if len(taiex) >= 6:
                    taiex_5d_ago = taiex[-6]["close"]
                    taiex_growth_pct = (close - taiex_5d_ago) / taiex_5d_ago * 100
                    margin_vs_taiex = margin_growth_pct - taiex_growth_pct
    
    return f"""
<div class="section-header">大盤總覽</div>
<div class="metrics-grid-4">
  <div class="metric">
    <div class="metric-label">加權指數</div>
    <div class="metric-value {change_class}">{close:,.2f}</div>
    <div class="metric-change {change_class}">{arrow} {change:+.2f} ({change_pct:+.2f}%)</div>
  </div>
  <div class="metric">
    <div class="metric-label">成交金額(億)</div>
    <div class="metric-value">{money_b:,.0f}</div>
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
  <div class="card-title">加權指數 K 線圖 + 成交量 (近60日)</div>
  <div id="taiex_kline" style="width:100%;height:400px;"></div>
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
<div class="grid-2">
  <div class="card">
    <div class="card-title">融資市值比 (融資餘額 ÷ 台股總市值)</div>
    <div class="chart-container"><canvas id="margin_ratio"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">融資增幅 vs 大盤漲幅 (較前日)</div>
    <div class="chart-container"><canvas id="margin_vs_taiex"></canvas></div>
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
    
    # 個股 K 線 + Supertrend + 成交量 (ECharts)
    for stock_id, data in stocks_data.items():
        df = data["df"]
        df_tail = df.tail(60)
        ind_dates = [d.strftime("%m-%d") for d in df_tail.index]
        ind_ohlc  = [[float(row["Open"]), float(row["Close"]),
                      float(row["Low"]),  float(row["High"])]
                     for _, row in df_tail.iterrows()]
        ind_vol   = [int(row["Volume"] / 1000) if row["Volume"] else 0
                     for _, row in df_tail.iterrows()]
        ind_ma20  = [round(v, 2) if v == v else None for v in df_tail["SMA_20"].tolist()]
        ind_ma60  = [round(v, 2) if v == v else None for v in df_tail["SMA_60"].tolist()]
        
        # Supertrend：依方向拆兩條線（上升綠、下降紅）
        st_vals = df_tail["ST"].tolist()
        st_dirs = df_tail["ST_DIR"].tolist()
        st_up   = [v if (d == 1  and v == v) else None for v, d in zip(st_vals, st_dirs)]
        st_down = [v if (d == -1 and v == v) else None for v, d in zip(st_vals, st_dirs)]
        
        # 漲跌顏色
        ind_vol_color = []
        for _, row in df_tail.iterrows():
            ind_vol_color.append("#ef5350" if row["Close"] >= row["Open"] else "#26a69a")
        
        scripts.append(f"""
(function() {{
  var chart = echarts.init(document.getElementById('kline_{stock_id}'));
  chart.setOption({{
    animation: false,
    tooltip: {{
      trigger: 'axis',
      axisPointer: {{type: 'cross'}},
      backgroundColor: 'rgba(255,255,255,0.95)',
      borderColor: '#ccc',
      textStyle: {{color: '#333'}}
    }},
    axisPointer: {{link: [{{xAxisIndex: 'all'}}]}},
    legend: {{
      data: ['K線', 'MA20', 'MA60', 'Supertrend↑', 'Supertrend↓'],
      top: 0, textStyle: {{fontSize: 10}}
    }},
    grid: [
      {{left: '10%', right: '4%', top: '15%', height: '55%'}},
      {{left: '10%', right: '4%', top: '78%', height: '18%'}}
    ],
    xAxis: [
      {{type: 'category', data: {json.dumps(ind_dates)}, gridIndex: 0,
        axisLabel: {{show: false}}, axisLine: {{show: false}}}},
      {{type: 'category', data: {json.dumps(ind_dates)}, gridIndex: 1,
        axisLabel: {{fontSize: 9, color: '#666'}}}}
    ],
    yAxis: [
      {{scale: true, gridIndex: 0, splitNumber: 4,
        axisLabel: {{fontSize: 10, color: '#666'}},
        splitLine: {{lineStyle: {{color: '#eee'}}}}}},
      {{scale: true, gridIndex: 1, splitNumber: 2,
        axisLabel: {{fontSize: 9, color: '#666'}},
        splitLine: {{show: false}}}}
    ],
    series: [
      {{name: 'K線', type: 'candlestick',
        xAxisIndex: 0, yAxisIndex: 0,
        data: {json.dumps(ind_ohlc)},
        itemStyle: {{color: '#ef5350', color0: '#26a69a',
          borderColor: '#ef5350', borderColor0: '#26a69a'}}}},
      {{name: 'MA20', type: 'line',
        xAxisIndex: 0, yAxisIndex: 0,
        data: {json.dumps(ind_ma20)},
        smooth: true, showSymbol: false,
        lineStyle: {{color: '#fb8c00', width: 1}}}},
      {{name: 'MA60', type: 'line',
        xAxisIndex: 0, yAxisIndex: 0,
        data: {json.dumps(ind_ma60)},
        smooth: true, showSymbol: false,
        lineStyle: {{color: '#9c27b0', width: 1}}}},
      {{name: 'Supertrend↑', type: 'line',
        xAxisIndex: 0, yAxisIndex: 0,
        data: {json.dumps(st_up)},
        showSymbol: false, connectNulls: false,
        lineStyle: {{color: '#26a69a', width: 2}}}},
      {{name: 'Supertrend↓', type: 'line',
        xAxisIndex: 0, yAxisIndex: 0,
        data: {json.dumps(st_down)},
        showSymbol: false, connectNulls: false,
        lineStyle: {{color: '#ef5350', width: 2}}}},
      {{name: '成交量', type: 'bar',
        xAxisIndex: 1, yAxisIndex: 1,
        data: {json.dumps(ind_vol)}.map(function(v, i) {{
          return {{value: v, itemStyle: {{color: {json.dumps(ind_vol_color)}[i]}}}};
        }})}}
    ]
  }});
  window.addEventListener('resize', function() {{ chart.resize(); }});
}})();""")
    
    # 大盤 K 線 + 成交金額 (ECharts)
    taiex = market_data["taiex"]
    if taiex and len(taiex) > 0:
        taiex_dates  = [d["date"][-5:] for d in taiex]
        # ECharts candlestick 順序: [open, close, low, high]
        taiex_ohlc   = [[d["open"], d["close"], d["low"], d["high"]] for d in taiex]
        # 成交金額（億元）
        taiex_money  = [round(d.get("money", 0) / 1e8, 1) for d in taiex]
        # Supertrend 拆兩條
        taiex_st_up   = [d.get("supertrend") if d.get("supertrend_dir") == 1  else None for d in taiex]
        taiex_st_down = [d.get("supertrend") if d.get("supertrend_dir") == -1 else None for d in taiex]
        # 漲跌顏色（紅漲綠跌）
        taiex_vol_color = []
        for d in taiex:
            if d["close"] >= d["open"]:
                taiex_vol_color.append("#ef5350")
            else:
                taiex_vol_color.append("#26a69a")
        
        scripts.append(f"""
(function() {{
  var chart = echarts.init(document.getElementById('taiex_kline'));
  var dates = {json.dumps(taiex_dates)};
  var ohlc  = {json.dumps(taiex_ohlc)};
  var money = {json.dumps(taiex_money)};
  var stUp   = {json.dumps(taiex_st_up)};
  var stDown = {json.dumps(taiex_st_down)};
  var volColors = {json.dumps(taiex_vol_color)};
  
  chart.setOption({{
    animation: false,
    tooltip: {{
      trigger: 'axis',
      axisPointer: {{ type: 'cross' }},
      backgroundColor: 'rgba(255,255,255,0.95)',
      borderColor: '#ccc',
      textStyle: {{ color: '#333' }}
    }},
    axisPointer: {{ link: [{{xAxisIndex: 'all'}}] }},
    legend: {{
      data: ['加權指數', 'Supertrend↑', 'Supertrend↓', '成交金額(億)'],
      top: 0, textStyle: {{fontSize: 10}}
    }},
    grid: [
      {{ left: '8%', right: '4%', top: '8%',  height: '57%' }},
      {{ left: '8%', right: '4%', top: '72%', height: '20%' }}
    ],
    xAxis: [
      {{ type: 'category', data: dates, gridIndex: 0,
         axisLabel: {{show: false}}, axisLine: {{show: false}}, axisTick: {{show: false}} }},
      {{ type: 'category', data: dates, gridIndex: 1,
         axisLabel: {{fontSize: 10, color: '#666'}} }}
    ],
    yAxis: [
      {{ scale: true, gridIndex: 0, splitNumber: 5,
         axisLabel: {{color: '#666'}}, splitLine: {{lineStyle: {{color: '#eee'}}}} }},
      {{ scale: true, gridIndex: 1, splitNumber: 2, name: '成交金額(億)',
         nameTextStyle: {{color: '#999', fontSize: 10}},
         axisLabel: {{color: '#666'}}, splitLine: {{show: false}} }}
    ],
    dataZoom: [{{type: 'inside', xAxisIndex: [0, 1], start: 50, end: 100}}],
    series: [
      {{name: '加權指數', type: 'candlestick',
        xAxisIndex: 0, yAxisIndex: 0, data: ohlc,
        itemStyle: {{color: '#ef5350', color0: '#26a69a',
          borderColor: '#ef5350', borderColor0: '#26a69a'}}}},
      {{name: 'Supertrend↑', type: 'line',
        xAxisIndex: 0, yAxisIndex: 0, data: stUp,
        showSymbol: false, connectNulls: false,
        lineStyle: {{color: '#26a69a', width: 2}}}},
      {{name: 'Supertrend↓', type: 'line',
        xAxisIndex: 0, yAxisIndex: 0, data: stDown,
        showSymbol: false, connectNulls: false,
        lineStyle: {{color: '#ef5350', width: 2}}}},
      {{name: '成交金額(億)', type: 'bar',
        xAxisIndex: 1, yAxisIndex: 1,
        data: money.map(function(v, i) {{
          return {{value: v, itemStyle: {{color: volColors[i]}}}};
        }})}}
    ]
  }});
  
  window.addEventListener('resize', function() {{ chart.resize(); }});
}})();""")
    else:
        print("  ⚠️ TAIEX 無資料")
    
    # 散戶多空比
    retail = market_data["retail"]
    if retail and len(retail) > 0:
        retail_dates = [d["date"][-5:] for d in retail]
        retail_ratio = [d["retail_ratio"] for d in retail]

        scripts.append(f"""
new Chart(document.getElementById('retail'),{{
  type:'line',
  data:{{
    labels:{json.dumps(retail_dates)},
    datasets:[{{
      label:'散戶多單%',
      data:{json.dumps(retail_ratio)},
      borderColor:'#EF5350',
      backgroundColor:'rgba(239,83,80,0.1)',
      borderWidth:2,fill:true,tension:0.3,pointRadius:3,
      pointBackgroundColor:'#EF5350'
    }}]
  }},
  options:{{
    responsive:true,maintainAspectRatio:false,
    plugins:{{
      legend:{{display:false}},
      tooltip:{{callbacks:{{label:function(ctx){{return '散戶多單: '+ctx.parsed.y.toFixed(1)+'%'}}}}}}
    }},
    scales:{{
      y:{{
        ticks:{{callback:function(v){{return v.toFixed(0)+'%'}},font:{{size:11}}}},
        grid:{{color:'rgba(0,0,0,0.05)'}}
      }},
      x:{{ticks:{{font:{{size:10}},maxRotation:0,autoSkip:true,maxTicksLimit:8}},grid:{{display:false}}}}
    }}
  }}
}});""")
    else:
        print("  ⚠️ 散戶多空比無數據")
        scripts.append("""
document.getElementById('retail').parentElement.innerHTML =
  '<div style="padding:20px;text-align:center;color:#999;">散戶多空比暫無資料</div>';
""")
    
    # 匯率
    fx = market_data["usd_twd"]
    if fx and len(fx) > 0:
        # 調試：打印第一筆看欄位名稱
        print(f"  [debug] 匯率欄位: {list(fx[0].keys())}")
        
        fx_dates = [d.get("date", "")[-5:] for d in fx]
        # 嘗試不同欄位名稱
        fx_values = []
        for d in fx:
            val = d.get("close", d.get("Close", d.get("rate", d.get("exchange_rate", 0))))
            if val:
                fx_values.append(float(val))
            else:
                fx_values.append(None)
        
        # 移除 None 值
        valid_pairs = [(date, val) for date, val in zip(fx_dates, fx_values) if val]
        if valid_pairs:
            fx_dates_clean, fx_values_clean = zip(*valid_pairs)
            
            scripts.append(f"""
new Chart(document.getElementById('fx'),{{
  type:'line',
  data:{{
    labels:{json.dumps(list(fx_dates_clean))},
    datasets:[{{
      label:'USD/TWD',
      data:{json.dumps(list(fx_values_clean))},
      borderColor:'#378ADD',
      backgroundColor:'rgba(55,138,221,0.1)',
      borderWidth:2,tension:0.3,fill:true,pointRadius:2
    }}]
  }},
  options:{{
    responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}}}},
    scales:{{
      y:{{ticks:{{font:{{size:11}}}},grid:{{color:'rgba(0,0,0,0.05)'}}}},
      x:{{ticks:{{font:{{size:10}},maxRotation:0,autoSkip:true,maxTicksLimit:8}},grid:{{display:false}}}}
    }}
  }}
}});""")
        else:
            print("  ⚠️ 匯率數據值為空")
            scripts.append("""
document.getElementById('fx').parentElement.innerHTML = '<div style="padding:20px;text-align:center;color:#999;">匯率數據暫時無法取得</div>';
""")
    else:
        print("  ⚠️ 匯率無數據")
        scripts.append("""
document.getElementById('fx').parentElement.innerHTML = '<div style="padding:20px;text-align:center;color:#999;">匯率數據暫時無法取得</div>';
""")
    
    # ── 融資市值比 + 融資 vs 大盤 ────────────────────
    total_margin = market_data.get("total_margin", [])
    taiex = market_data.get("taiex", [])
    
    if total_margin and taiex and len(total_margin) >= 5 and len(taiex) >= 5:
        # 排序
        margin_sorted = sorted(total_margin, key=lambda x: x.get("date", ""))
        taiex_by_date = {d["date"]: d for d in taiex}
        
        # 嘗試多種融資餘額欄位
        def margin_value(d):
            for k in ["MarginPurchaseTodayBalance", "margin_purchase_today_balance",
                      "TodayBalance", "today_balance", "MarginPurchaseMoney",
                      "margin_purchase_money", "MarginPurchaseAmount"]:
                if k in d and d[k] is not None:
                    try:
                        return float(d[k])
                    except (ValueError, TypeError):
                        continue
            return 0
        
        if margin_sorted:
            print(f"  [debug] 大盤融資欄位: {list(margin_sorted[0].keys())}")
        
        # 計算近30日的兩個指標
        ratio_dates  = []
        ratio_values = []      # 融資市值比 (%)
        gap_dates    = []
        margin_growth = []     # 融資日增幅 %
        taiex_growth  = []     # 大盤日漲幅 %
        
        # 取最近30天 + 1天（多1天算增幅）
        recent = margin_sorted[-31:]
        for i in range(len(recent)):
            d = recent[i]
            date = d.get("date", "")
            if not date or date not in taiex_by_date:
                continue
            
            margin_now  = margin_value(d)
            taiex_close = taiex_by_date[date]["close"]
            
            # 總市值近似 = 加權指數 × 25 億元/點 (台股大約係數)
            # 換算成元：× 1e8
            market_cap_yuan = taiex_close * 25 * 1e8
            if market_cap_yuan > 0 and margin_now > 0:
                ratio = margin_now / market_cap_yuan * 100
                ratio_dates.append(date[-5:])
                ratio_values.append(round(ratio, 3))
            
            # 計算日增幅 (需要前一日)
            if i > 0:
                prev_d = recent[i-1]
                prev_date = prev_d.get("date", "")
                if prev_date in taiex_by_date:
                    prev_margin = margin_value(prev_d)
                    prev_taiex  = taiex_by_date[prev_date]["close"]
                    if prev_margin > 0 and prev_taiex > 0:
                        m_g = (margin_now - prev_margin) / prev_margin * 100
                        t_g = (taiex_close - prev_taiex) / prev_taiex * 100
                        gap_dates.append(date[-5:])
                        margin_growth.append(round(m_g, 2))
                        taiex_growth.append(round(t_g, 2))
        
        # 只保留最近30天
        ratio_dates  = ratio_dates[-30:]
        ratio_values = ratio_values[-30:]
        gap_dates    = gap_dates[-30:]
        margin_growth = margin_growth[-30:]
        taiex_growth  = taiex_growth[-30:]
        
        # 圖A：融資市值比
        if ratio_values:
            scripts.append(f"""
new Chart(document.getElementById('margin_ratio'),{{
  type:'line',
  data:{{
    labels:{json.dumps(ratio_dates)},
    datasets:[{{
      label:'融資市值比(%)',
      data:{json.dumps(ratio_values)},
      borderColor:'#D4537E',
      backgroundColor:'rgba(212,83,126,0.1)',
      borderWidth:2,fill:true,tension:0.3,pointRadius:2
    }}]
  }},
  options:{{
    responsive:true,maintainAspectRatio:false,
    plugins:{{
      legend:{{display:false}},
      tooltip:{{callbacks:{{label:function(ctx){{return ctx.parsed.y.toFixed(3)+'%'}}}}}}
    }},
    scales:{{
      y:{{ticks:{{callback:function(v){{return v.toFixed(2)+'%'}},font:{{size:11}}}},
          grid:{{color:'rgba(0,0,0,0.05)'}}}},
      x:{{ticks:{{font:{{size:10}},maxRotation:0,autoSkip:true,maxTicksLimit:8}},grid:{{display:false}}}}
    }}
  }}
}});""")
        
        # 圖B：融資增幅 vs 大盤漲幅
        if margin_growth and taiex_growth:
            scripts.append(f"""
new Chart(document.getElementById('margin_vs_taiex'),{{
  type:'line',
  data:{{
    labels:{json.dumps(gap_dates)},
    datasets:[
      {{label:'融資增幅(%)',data:{json.dumps(margin_growth)},
        borderColor:'#EF5350',backgroundColor:'rgba(239,83,80,0.1)',
        borderWidth:2,tension:0.3,pointRadius:2,fill:false}},
      {{label:'大盤漲幅(%)',data:{json.dumps(taiex_growth)},
        borderColor:'#378ADD',backgroundColor:'rgba(55,138,221,0.1)',
        borderWidth:2,tension:0.3,pointRadius:2,fill:false}}
    ]
  }},
  options:{{
    responsive:true,maintainAspectRatio:false,
    plugins:{{
      legend:{{display:true,position:'top',labels:{{font:{{size:11}},boxWidth:12}}}},
      tooltip:{{mode:'index',intersect:false,
        callbacks:{{label:function(ctx){{return ctx.dataset.label+': '+ctx.parsed.y.toFixed(2)+'%'}}}}}}
    }},
    scales:{{
      y:{{ticks:{{callback:function(v){{return v.toFixed(1)+'%'}},font:{{size:11}}}},
          grid:{{color:'rgba(0,0,0,0.05)'}}}},
      x:{{ticks:{{font:{{size:10}},maxRotation:0,autoSkip:true,maxTicksLimit:8}},grid:{{display:false}}}}
    }}
  }}
}});""")
    else:
        print("  ⚠️ 融資指標資料不足")
        scripts.append("""
document.getElementById('margin_ratio').parentElement.innerHTML = '<div style="padding:20px;text-align:center;color:#999;">融資市值比資料不足</div>';
document.getElementById('margin_vs_taiex').parentElement.innerHTML = '<div style="padding:20px;text-align:center;color:#999;">融資漲幅比較資料不足</div>';
""")
    
    # 三大法人
    inst = market_data["institution"]
    if inst and len(inst) > 0:
        print(f"  [debug] 法人欄位: {list(inst[0].keys())}")

        inst_dates = [d.get("date", "")[-5:] for d in inst]

        # pivot 後的欄位單位是「元」，除以 100,000,000 = 億
        divisor = 100_000_000

        foreign = [_get(d, "Foreign_Investor_diff") / divisor for d in inst]
        trust   = [_get(d, "Investment_Trust_diff") / divisor for d in inst]
        dealer  = [_get(d, "Dealer_diff") / divisor for d in inst]

        # 期貨留倉（右軸）：外資 TX 多空淨部位
        fut = market_data["futures"]
        if fut:
            print(f"  [debug] 期貨欄位: {list(fut[0].keys())}")
        
        # 按日期+法人聚合：只取外資及陸資
        fut_foreign_by_date = {}
        for f in fut:
            identity = f.get("institutional_investors", "")
            if "外資" not in identity:
                continue
            date_key = f.get("date", "")
            long_oi  = float(f.get("long_open_interest_balance_volume", 0))
            short_oi = float(f.get("short_open_interest_balance_volume", 0))
            fut_foreign_by_date[date_key] = long_oi - short_oi
        
        fut_foreign = [fut_foreign_by_date.get(d["date"], 0) for d in inst]

        scripts.append(f"""
new Chart(document.getElementById('inst'),{{
  type:'bar',
  data:{{
    labels:{json.dumps(inst_dates)},
    datasets:[
      {{label:'外資現貨(億)',data:{json.dumps(foreign)},
        backgroundColor:'rgba(55,138,221,0.7)',yAxisID:'y'}},
      {{label:'投信現貨(億)',data:{json.dumps(trust)},
        backgroundColor:'rgba(29,158,117,0.7)',yAxisID:'y'}},
      {{label:'自營現貨(億)',data:{json.dumps(dealer)},
        backgroundColor:'rgba(255,152,0,0.7)',yAxisID:'y'}},
      {{label:'外資期貨淨部位(口)',data:{json.dumps(fut_foreign)},
        type:'line',borderColor:'#EF5350',borderWidth:2,
        pointRadius:0,tension:0.3,fill:false,yAxisID:'y1'}}
    ]
  }},
  options:{{
    responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:true,position:'top',labels:{{font:{{size:11}},boxWidth:12}}}}}},
    scales:{{
      y:{{type:'linear',position:'left',
          title:{{display:true,text:'現貨買賣超(億)'}},
          ticks:{{font:{{size:11}}}},grid:{{color:'rgba(0,0,0,0.05)'}}}},
      y1:{{type:'linear',position:'right',
           title:{{display:true,text:'期貨口數'}},
           ticks:{{font:{{size:11}}}},grid:{{display:false}}}},
      x:{{ticks:{{font:{{size:10}},maxRotation:0,autoSkip:true,maxTicksLimit:10}},grid:{{display:false}}}}
    }}
  }}
}});""")
    else:
        print("  ⚠️ 三大法人無數據")
        scripts.append("""
document.getElementById('inst').parentElement.innerHTML =
  '<div style="padding:20px;text-align:center;color:#999;">法人數據暫時無法取得</div>';
""")
    
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

/* ── 新版個股卡片 ─────────────────── */
.stock-card{background:white;border-radius:12px;border:0.5px solid #e0e0e0;padding:1.25rem;margin-bottom:16px}
.stock-card .card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px;padding-bottom:12px;border-bottom:0.5px solid #e0e0e0}
.stock-card h3{font-size:18px;font-weight:500;margin:0}
.card-header-sub{font-size:12px;color:#666;margin-top:4px}
.card-price{font-size:22px;font-weight:500;line-height:1.1}
.card-change{font-size:12px;margin-top:2px}
.card-volume{font-size:12px;color:#666;margin-top:6px;padding-top:6px;border-top:0.5px dashed #e0e0e0}
.card-volume-main{font-size:13px;color:#333;font-weight:500}
.indicator-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:8px}
.indicator-cell{background:#f5f5f5;border-radius:8px;padding:8px;text-align:center}
.indicator-cell-label{font-size:10px;color:#666}
.indicator-cell-value{font-size:13px;font-weight:500;margin-top:2px}
.collapsible{background:#f5f5f5;border-radius:8px;margin-bottom:12px;overflow:hidden;border:0.5px solid #e0e0e0}
.collapsible summary{padding:10px 12px;cursor:pointer;font-size:13px;font-weight:500;display:flex;align-items:center;gap:8px;list-style:none;user-select:none}
.collapsible summary::-webkit-details-marker{display:none}
.collapsible summary::before{content:'▶';font-size:9px;transition:transform 0.2s;color:#666;margin-right:4px}
.collapsible[open] summary::before{transform:rotate(90deg)}
.collapsible-content{padding:0 12px 12px 28px;font-size:13px;color:#666;line-height:1.7}
.collapsible-content p{margin:6px 0}
.collapsible-icon{color:#378ADD;font-size:14px}
.data-table{width:100%;font-size:12px;border-collapse:collapse;margin-bottom:8px}
.data-table th{text-align:right;padding:6px 8px;background:#f5f5f5;color:#666;font-weight:500;font-size:11px}
.data-table th:first-child{text-align:left}
.data-table td{padding:6px 8px;border-bottom:0.5px solid #e0e0e0;text-align:right}
.data-table td:first-child{text-align:left}
.data-table .row-total{background:#f5f5f5;font-weight:500}
.data-table .row-total td{border-top:1px solid #ccc;border-bottom:none}

/* ── 個股評等表 ───────────────────── */
.rating-section{background:white;border-radius:12px;border:0.5px solid #e0e0e0;padding:1.25rem;margin-bottom:16px}
.rating-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:10px;border-bottom:0.5px solid #e0e0e0}
.rating-title{font-size:16px;font-weight:500;display:flex;align-items:center;gap:6px}
.rating-update{font-size:11px;color:#999}
.rating-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.rating-col{background:#f5f5f5;border-radius:8px;padding:10px 8px;border-top:3px solid}
.rating-col-strong-buy{border-top-color:#c62828}
.rating-col-buy{border-top-color:#ef5350}
.rating-col-neutral{border-top-color:#888}
.rating-col-sell{border-top-color:#66bb6a}
.rating-col-strong-sell{border-top-color:#2e7d32}
.rating-col-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;padding-bottom:6px;border-bottom:0.5px solid #e0e0e0}
.rating-col-label{font-size:12px;font-weight:500;display:flex;align-items:center;gap:4px}
.rating-label-strong-buy{color:#c62828}
.rating-label-buy{color:#ef5350}
.rating-label-neutral{color:#555}
.rating-label-sell{color:#66bb6a}
.rating-label-strong-sell{color:#2e7d32}
.rating-col-count{font-size:11px;color:#999;background:white;border-radius:8px;padding:1px 6px}
.rating-badge{display:inline-block;font-size:11px;font-weight:500;padding:2px 8px;border-radius:10px;background:#f5f5f5}
.stock-chip{background:white;border:0.5px solid #e0e0e0;border-radius:6px;padding:6px 8px;margin-bottom:5px;font-size:12px}
.chip-top{display:flex;justify-content:space-between;align-items:baseline}
.chip-name{font-weight:500;font-size:12px}
.chip-change{font-size:10px}
.chip-change.up{color:#d32f2f}
.chip-change.down{color:#388e3c}
.chip-meta{font-size:10px;color:#999;margin-top:2px;display:flex;gap:6px}
.chip-tag{display:inline-block;font-size:9px;padding:1px 4px;border-radius:4px;background:#f5f5f5;color:#666}
.empty-hint{font-size:11px;color:#999;text-align:center;padding:14px 0}
.scoring-key{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;padding:10px 12px;font-size:11px;color:#666}
.key-block{line-height:1.6}
.key-block strong{display:block;color:#333;margin-bottom:2px;font-weight:500}

@media (max-width:1024px){.grid-2{grid-template-columns:1fr}.metrics-grid-4{grid-template-columns:repeat(2,1fr)}.stock-grid{grid-template-columns:1fr}.rating-grid{grid-template-columns:repeat(2,1fr)}.indicator-row{grid-template-columns:repeat(3,1fr)}}
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
    print(f'=== 股票監控機器人 v4.2 ({now_tw().strftime("%Y-%m-%d %H:%M")}) ===\n')
    
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
        margin      = get_margin_data(stock_id)
        borrow      = get_borrowing_data(stock_id)
        tdcc        = get_tdcc_holding(stock_id)
        news        = get_news(stock_id)
        name        = get_stock_name(stock_id)
        fundamentals = get_fundamentals(stock_id)
        
        # 前一日成交量（用於顯示「較昨日 +N 張」）
        df = stock_data["df"]
        volume_prev = int(df["Volume"].iloc[-2] / 1000) if len(df) > 1 else 0
        
        # 漲跌幅
        close = stock_data["latest"]["close"]
        prev_close = stock_data["prev"]["close"]
        change_pct = ((close - prev_close) / prev_close * 100) if prev_close else 0
        
        print(f"    生成 AI 分析...")
        ai_tech, ai_chip, ai_oper = generate_ai_analysis(
            stock_id, name, stock_data, institution, margin, borrow, tdcc
        )
        
        record = {
            "latest":      stock_data["latest"],
            "prev":        stock_data["prev"],
            "df":          stock_data["df"],
            "indicators":  stock_data["indicators"],
            "institution": institution,
            "margin":      margin,
            "borrow":      borrow,
            "tdcc":        tdcc,
            "news":        news,
            "fundamentals": fundamentals,
            "volume_prev": volume_prev,
            "change_pct":  change_pct,
            "ai_tech":     ai_tech,
            "ai_chip":     ai_chip,
            "ai_oper":     ai_oper,
            "ai_analysis": ai_tech + "\n\n" + ai_chip + "\n\n" + ai_oper,
            "name":        name,
        }
        
        # 計算評等
        record["rating"] = calculate_stock_rating(record)
        r = record["rating"]
        print(f"    ✓ 評等: {r['rating']} (技{r['tech']:g}/籌{r['chip']:g} = {r['total']:g})")
        
        stocks_data[stock_id] = record
    
    # 2. 抓大盤
    print("\n[2/4] 抓取大盤資料...")
    market_data = get_market_overview()
    
    # 調試：打印市場數據狀態
    print(f"  ✓ 大盤指數: {len(market_data['taiex'])} 筆")
    print(f"  ✓ 三大法人: {len(market_data['institution'])} 筆")
    print(f"  ✓ 散戶多空: {len(market_data['retail'])} 筆")
    print(f"  ✓ 美元匯率: {len(market_data['usd_twd'])} 筆")
    print(f"  ✓ 期貨留倉: {len(market_data['futures'])} 筆")
    
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
            subprocess.run(["git", "commit", "-m", "Update " + now_tw().strftime("%Y-%m-%d %H:%M")], check=True)
            subprocess.run(["git", "push"], check=True)
            print("  ✓ 已更新")
    except Exception as e:
        print(f"  ⚠️ Git 失敗: {e}")
    
    # Telegram
    tg_msg = "📊 *股票監控* (" + now_tw().strftime("%m-%d") + ")\n\n"
    for stock_id, data in stocks_data.items():
        close = data["latest"]["close"]
        foreign = data["institution"]["foreign_today"]
        tg_msg += f"*{stock_id}* ${close:.2f} | 外資{foreign:+.0f}張\n"
    tg_msg += f"\n🌐 https://ewq4303-debug.github.io/stocknotice/"
    
    send_telegram(tg_msg)
    
    print("\n✅ 完成！")


if __name__ == "__main__":
    main()
