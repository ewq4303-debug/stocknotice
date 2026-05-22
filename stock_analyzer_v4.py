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
            "df": df,
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
    
    latest = history[-1]
    
    # Debug：印最新一筆的計算結果
    if stock_id == STOCKS[0] if STOCKS else False:
        print(f"    [debug] {stock_id} 最新外資={latest['foreign']/1000:+.0f}張, "
              f"投信={latest['trust']/1000:+.0f}張, 5日累計外資={foreign_5d:+.0f}張")
    
    return {
        "latest": latest,
        "foreign_today": latest["foreign"] / 1000,  # 張
        "foreign_5d":    foreign_5d,
        "foreign_20d":   foreign_20d,
        "trust_today":   latest["trust"] / 1000,
        "trust_5d":      trust_5d,
        "trust_20d":     trust_20d,
        "dealer_today":  latest["dealer"] / 1000,
        "history": history,
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
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
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
    
    # 大盤 K 線 + 成交金額 (ECharts)
    taiex = market_data["taiex"]
    if taiex and len(taiex) > 0:
        taiex_dates  = [d["date"][-5:] for d in taiex]
        # ECharts candlestick 順序: [open, close, low, high]
        taiex_ohlc   = [[d["open"], d["close"], d["low"], d["high"]] for d in taiex]
        # 成交金額（億元）
        taiex_money  = [round(d.get("money", 0) / 1e8, 1) for d in taiex]
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
    grid: [
      {{ left: '8%', right: '4%', top: '5%',  height: '60%' }},
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
      {{
        name: '加權指數', type: 'candlestick',
        xAxisIndex: 0, yAxisIndex: 0,
        data: ohlc,
        itemStyle: {{
          color: '#ef5350', color0: '#26a69a',
          borderColor: '#ef5350', borderColor0: '#26a69a'
        }}
      }},
      {{
        name: '成交金額(億)', type: 'bar',
        xAxisIndex: 1, yAxisIndex: 1,
        data: money.map(function(v, i) {{
          return {{value: v, itemStyle: {{color: volColors[i]}}}};
        }})
      }}
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
    print(f"=== 股票監控機器人 v4.2 ({datetime.now().strftime('%Y-%m-%d %H:%M')}) ===\n")
    
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
