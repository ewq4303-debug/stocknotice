"""
每日台股分析機器人 v3.9 (雙 AI 切換版)
v3.9 改動: 大盤動態快訊重新設計
  - 加權指數: 加上簡單技術面分析 (MA / 連續紅黑K)
  - USD/TWD: 只顯示收盤 + 1日/5日漲跌幅
  - 三大法人: 加 5日/30日 累積買賣超
  - 大台法人未平倉: 不變
  - 微台散戶多空比: 改顯示近 5 個交易日數字
"""

import os
import csv
import time
import requests
import feedparser
from collections import defaultdict
from datetime import datetime, timedelta
from io import StringIO

# ===== 設定區 =====
STOCKS = os.getenv("STOCKS", "2330,2454,2317").split(",")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

AI_PROVIDER = os.getenv("AI_PROVIDER", "claude").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_CALL_INTERVAL = float(os.getenv("GEMINI_CALL_INTERVAL", "4"))

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TPEFX_URL_TEMPLATE = "https://www.tpefx.com.tw/uploads/service/tw/{year}nt.csv"


# ===== AI 統一介面 =====
def validate_ai_config():
    if AI_PROVIDER not in ("claude", "gemini"):
        raise ValueError(f"AI_PROVIDER={AI_PROVIDER} 無效, 必須是 'claude' 或 'gemini'")
    if AI_PROVIDER == "claude" and not ANTHROPIC_API_KEY:
        raise ValueError("AI_PROVIDER=claude 但未設定 ANTHROPIC_API_KEY")
    if AI_PROVIDER == "gemini" and not GEMINI_API_KEY:
        raise ValueError("AI_PROVIDER=gemini 但未設定 GEMINI_API_KEY")


def call_ai(prompt: str) -> str:
    if AI_PROVIDER == "gemini":
        return _call_gemini(prompt)
    return _call_claude(prompt)


def _call_claude(prompt: str) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.replace("**", "*")


def _call_gemini(prompt: str, max_retries: int = 2) -> str:
    from google import genai
    client = genai.Client(api_key=GEMINI_API_KEY)
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt
            )
            return response.text.replace("**", "*")
        except Exception as e:
            err = str(e)
            if attempt < max_retries and (
                "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower()
            ):
                wait = 30 * (attempt + 1)
                print(f"  ⓘ Gemini rate limit, 等 {wait} 秒重試...")
                time.sleep(wait)
                continue
            raise


# ===== FinMind =====
def fetch_finmind(dataset: str, start_date: str, stock_id: str = "") -> list:
    params = {"dataset": dataset, "start_date": start_date, "token": FINMIND_TOKEN}
    if stock_id:
        params["data_id"] = stock_id
    try:
        r = requests.get(FINMIND_URL, params=params, timeout=20)
        if r.status_code == 400:
            print(f"  ⓘ {dataset} ({stock_id}): 可能需 Sponsor 等級, 跳過")
            return []
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"  ⚠️ FinMind {dataset} 失敗: {e}")
        return []


def classify_institution(name: str) -> str:
    if not name:
        return ""
    if "Foreign" in name or "外資" in name:
        return "foreign"
    if "Trust" in name or "投信" in name:
        return "trust"
    if "Dealer" in name or "自營" in name:
        return "dealer"
    return ""


# ===== TPEFX 匯率 =====
def _fetch_tpefx_year(year: int) -> list:
    url = TPEFX_URL_TEMPLATE.format(year=year)
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(text))
        rows = []
        for row in reader:
            date_str = (row.get("DATE") or "").strip()
            close_str = (row.get("CLOSE") or "").strip()
            if not date_str or not close_str:
                continue
            try:
                rows.append({"date": date_str, "close": float(close_str)})
            except ValueError:
                continue
        return rows
    except Exception as e:
        print(f"  ⚠️ TPEFX {year} 抓取失敗: {e}")
        return []


def get_tpefx_usdtwd(days: int = 10) -> list:
    """USD/TWD 銀行間即期收盤 (近 N 日, 由舊到新)"""
    year = datetime.now().year
    history = _fetch_tpefx_year(year)
    if len(history) < days:
        prev = _fetch_tpefx_year(year - 1)
        history = prev + history
    return history[-days:] if history else []


# ===== 加權指數技術面分析 =====
def analyze_taiex_ta(taiex_rows: list) -> str:
    """簡單技術面分析, 30 字內. 包含: MA5 位置 + 均線排列 + 連續紅黑K"""
    closes = [r.get("close") for r in taiex_rows if r.get("close") is not None]
    if len(closes) < 5:
        return "資料不足"

    latest = closes[-1]
    ma5 = sum(closes[-5:]) / 5

    parts = []
    parts.append("站上MA5" if latest > ma5 else "跌破MA5")

    # 均線排列
    if len(closes) >= 20:
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        if ma5 > ma10 > ma20:
            parts.append("多頭排列")
        elif ma5 < ma10 < ma20:
            parts.append("空頭排列")
        else:
            parts.append("均線糾結")
    elif len(closes) >= 10:
        ma10 = sum(closes[-10:]) / 10
        parts.append("短均偏多" if ma5 > ma10 else "短均偏空")

    # 連續紅黑 K
    if len(closes) >= 2:
        last_dir = "up" if closes[-1] > closes[-2] else (
            "down" if closes[-1] < closes[-2] else None
        )
        if last_dir:
            count = 1
            for i in range(len(closes) - 2, 0, -1):
                d = "up" if closes[i] > closes[i - 1] else (
                    "down" if closes[i] < closes[i - 1] else None
                )
                if d == last_dir:
                    count += 1
                else:
                    break
            if count >= 2:
                parts.append(f"連{count}{'紅' if last_dir == 'up' else '黑'}")

    return ", ".join(parts)[:30]


# ===== 股票名稱查詢 =====
def get_stock_names(stock_ids: list) -> dict:
    try:
        info = fetch_finmind("TaiwanStockInfo", "2024-01-01")
        all_names = {r["stock_id"]: r.get("stock_name", "") for r in info}
        return {sid: (all_names.get(sid) or sid) for sid in stock_ids}
    except Exception as e:
        print(f"  ⚠️ 取得股票名稱失敗: {e}")
        return {sid: sid for sid in stock_ids}


# ===== 個股資料抓取 =====
def get_stock_data(stock_id: str) -> dict:
    start = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
    return {
        "price": fetch_finmind("TaiwanStockPrice", start, stock_id)[-7:],
        "institutional": fetch_finmind(
            "TaiwanStockInstitutionalInvestorsBuySell", start, stock_id
        ),
        "margin": fetch_finmind(
            "TaiwanStockMarginPurchaseShortSale", start, stock_id
        )[-7:],
        "shareholding": fetch_finmind(
            "TaiwanStockShareholding", start, stock_id
        )[-7:],
        "securities_lending": fetch_finmind(
            "TaiwanDailyShortSaleBalances", start, stock_id
        )[-7:],
    }


def get_news(stock_id: str, max_items: int = 8) -> list:
    try:
        feed = feedparser.parse(f"https://tw.stock.yahoo.com/rss?s={stock_id}.TW")
        return [
            {
                "title": e.title,
                "summary": e.get("summary", "")[:300],
                "published": e.get("published", ""),
            }
            for e in feed.entries[:max_items]
        ]
    except Exception as e:
        print(f"  ⚠️ 新聞抓取失敗: {e}")
        return []


# ===== 大盤動態抓取 =====
def get_market_context() -> dict:
    """大盤背景; 加權指數和大盤三大法人需要長期資料 (TA + 30日累積)"""
    start_short = (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%d")
    start_long = (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")
    return {
        # 抓 50 天歷史用於 TA (MA20 + 連紅黑 K)
        "taiex": fetch_finmind("TaiwanStockPrice", start_long, "TAIEX"),
        # 抓 50 天用於 30 日累計
        "total_institutional": fetch_finmind(
            "TaiwanStockTotalInstitutionalInvestors", start_long
        ),
        "fx_usdtwd": get_tpefx_usdtwd(10),  # 抓 10 天確保有 5+ 日資料
        "fut_TX": fetch_finmind("TaiwanFuturesInstitutionalInvestors", start_short, "TX"),
        "fut_TMF_inst": fetch_finmind("TaiwanFuturesInstitutionalInvestors", start_short, "TMF"),
        "fut_TMF_daily": fetch_finmind("TaiwanFuturesDaily", start_short, "TMF"),
    }


# ===== 衍生訊號計算 =====
def streak(values: list) -> int:
    if not values:
        return 0
    last = values[-1]
    if last == 0:
        return 0
    direction = 1 if last > 0 else -1
    count = 0
    for v in reversed(values):
        if (v > 0 and direction > 0) or (v < 0 and direction < 0):
            count += 1
        else:
            break
    return count * direction


def calc_signals(data: dict) -> dict:
    sig = {}
    inst = data.get("institutional", [])
    if inst:
        daily = defaultdict(lambda: {"foreign": 0, "trust": 0, "dealer": 0})
        for r in inst:
            net = (r.get("buy", 0) - r.get("sell", 0)) // 1000
            cat = classify_institution(r.get("name", ""))
            d = r.get("date", "")
            if cat:
                daily[d][cat] += net
        days = sorted(daily.keys())
        if days:
            sig["foreign_5d_net"] = sum(daily[d]["foreign"] for d in days[-5:])
            sig["trust_5d_net"] = sum(daily[d]["trust"] for d in days[-5:])
            sig["dealer_5d_net"] = sum(daily[d]["dealer"] for d in days[-5:])
            sig["foreign_streak"] = streak([daily[d]["foreign"] for d in days])
            sig["trust_streak"] = streak([daily[d]["trust"] for d in days])

    margin = data.get("margin", [])
    if margin:
        latest = margin[-1]
        m_bal = latest.get("MarginPurchaseTodayBalance", 0)
        s_bal = latest.get("ShortSaleTodayBalance", 0)
        sig["margin_balance"] = m_bal
        sig["short_balance"] = s_bal
        sig["short_margin_ratio_pct"] = round(s_bal / m_bal * 100, 2) if m_bal else 0
        if len(margin) >= 5:
            sig["margin_change_5d"] = m_bal - margin[-5].get("MarginPurchaseTodayBalance", 0)
            sig["short_change_5d"] = s_bal - margin[-5].get("ShortSaleTodayBalance", 0)

    sl = data.get("securities_lending", [])
    if sl:
        sig["short_sale_balance"] = (
            sl[-1].get("Volume") or sl[-1].get("today_balance") or sl[-1].get("balance") or 0
        )

    sh = data.get("shareholding", [])
    if sh and len(sh) >= 2:
        get_ratio = lambda r: (
            r.get("ForeignInvestmentSharesRatio")
            or r.get("ForeignInvestmentRemainRatio") or 0
        )
        latest_ratio = get_ratio(sh[-1])
        old_ratio = get_ratio(sh[max(0, len(sh) - 5)])
        sig["foreign_holding_pct"] = latest_ratio
        sig["foreign_holding_change_5d_pct"] = round(latest_ratio - old_ratio, 3)

    return sig


def calc_market_signals(market: dict) -> dict:
    sig = {}

    # ---- 加權指數 (含技術面) ----
    taiex = market.get("taiex", [])
    if taiex and len(taiex) >= 2:
        try:
            latest, prev = taiex[-1], taiex[-2]
            sig["taiex_close"] = latest.get("close")
            chg = latest["close"] - prev["close"]
            sig["taiex_change"] = round(chg, 2)
            sig["taiex_change_pct"] = round(chg / prev["close"] * 100, 2)
            sig["taiex_ta"] = analyze_taiex_ta(taiex)
        except (TypeError, KeyError):
            pass

    # ---- USD/TWD (1日/5日漲跌幅) ----
    fx = market.get("fx_usdtwd", [])
    if fx and len(fx) >= 2:
        latest = fx[-1]
        sig["usdtwd"] = latest.get("close")
        sig["usdtwd_date"] = latest.get("date")
        try:
            close = latest["close"]
            close_1d_ago = fx[-2]["close"]
            sig["usdtwd_change_1d_pct"] = round((close - close_1d_ago) / close_1d_ago * 100, 2)
            if len(fx) >= 6:
                close_5d_ago = fx[-6]["close"]
                sig["usdtwd_change_5d_pct"] = round((close - close_5d_ago) / close_5d_ago * 100, 2)
        except (TypeError, KeyError, ZeroDivisionError):
            pass

    # ---- 三大法人 (今日 / 5日 / 30日) ----
    total_inst = market.get("total_institutional", [])
    if total_inst:
        daily_nets = defaultdict(lambda: {"foreign": 0, "trust": 0, "dealer": 0})
        for r in total_inst:
            cat = classify_institution(r.get("name", ""))
            if not cat:
                continue
            net_billion = (r.get("buy", 0) - r.get("sell", 0)) / 1e8
            daily_nets[r["date"]][cat] += net_billion

        dates_sorted = sorted(daily_nets.keys())
        if dates_sorted:
            today_date = dates_sorted[-1]
            sig["market_date"] = today_date
            last_5 = dates_sorted[-5:]
            last_30 = dates_sorted[-30:]
            for cat in ("foreign", "trust", "dealer"):
                sig[f"market_{cat}_today"] = round(daily_nets[today_date][cat], 2)
                sig[f"market_{cat}_5d"] = round(sum(daily_nets[d][cat] for d in last_5), 2)
                sig[f"market_{cat}_30d"] = round(sum(daily_nets[d][cat] for d in last_30), 2)

    # ---- TX 大台 三大法人 (不變) ----
    tx_rows = market.get("fut_TX", [])
    if tx_rows:
        dates = sorted(set(r["date"] for r in tx_rows))
        if dates:
            latest_date = dates[-1]
            tx_inst = {"foreign": 0, "trust": 0, "dealer": 0}
            for r in tx_rows:
                if r["date"] != latest_date:
                    continue
                name = (r.get("institutional_investors") or r.get("name")
                        or r.get("type") or "")
                cat = classify_institution(name)
                if not cat:
                    continue
                long_oi = r.get("long_open_interest_balance_volume", 0) or 0
                short_oi = r.get("short_open_interest_balance_volume", 0) or 0
                tx_inst[cat] += long_oi - short_oi
            sig["TX_foreign_net_oi"] = tx_inst["foreign"]
            sig["TX_trust_net_oi"] = tx_inst["trust"]
            sig["TX_dealer_net_oi"] = tx_inst["dealer"]
            sig["TX_total_net_oi"] = sum(tx_inst.values())

    # ---- TMF 微台 散戶多空比 (近 5 個交易日) ----
    tmf_inst = market.get("fut_TMF_inst", [])
    tmf_daily = market.get("fut_TMF_daily", [])
    if tmf_inst and tmf_daily:
        inst_dates = set(r["date"] for r in tmf_inst)
        daily_dates = set(r["date"] for r in tmf_daily)
        common = sorted(inst_dates & daily_dates)
        ratios = []
        for d in common[-5:]:
            il = sum((r.get("long_open_interest_balance_volume") or 0)
                     for r in tmf_inst if r["date"] == d)
            ish = sum((r.get("short_open_interest_balance_volume") or 0)
                      for r in tmf_inst if r["date"] == d)
            toi = sum((r.get("open_interest") or 0)
                      for r in tmf_daily if r["date"] == d)
            if toi > 0:
                ratio = (ish - il) / toi * 100  # = 散戶多空比
                ratios.append({"date": d, "ratio": round(ratio, 2)})
        sig["TMF_retail_ratios_5d"] = ratios

    return sig


# ===== AI 分析 =====
def analyze(stock_id, stock_name, data, market, market_sig, news, signals):
    display_name = stock_name if stock_name and stock_name != stock_id else stock_id

    # 取最近 1 筆 TMF 散戶多空比給 AI 參考
    tmf_ratios = market_sig.get("TMF_retail_ratios_5d") or []
    tmf_latest_ratio = tmf_ratios[-1]["ratio"] if tmf_ratios else None

    prompt = f"""你是專業台股分析師。請依據以下資料給出「{display_name}」({stock_id}) 的客觀分析。

【重要寫作要求】
- 內文中提及這檔股票時, 請使用「{display_name}」這個名稱, 不要使用代號 {stock_id}
- 不要在輸出中重述大盤摘要也不要寫個股 vs 大盤段落 (大盤背景僅供你內部分析參考)
- 重要詞彙用單個星號*粗體*標示, 不要用雙星號**

═══ 大盤背景 (僅供分析時參考, 不要在輸出中重述) ═══
加權指數 {market_sig.get('taiex_close')} ({market_sig.get('taiex_change_pct')}%) | 技術面: {market_sig.get('taiex_ta', '')}
USD/TWD {market_sig.get('usdtwd')} | 1日 {market_sig.get('usdtwd_change_1d_pct')}% | 5日 {market_sig.get('usdtwd_change_5d_pct')}%
大盤三大法人 (億): 外資今 {market_sig.get('market_foreign_today')} / 5日 {market_sig.get('market_foreign_5d')} / 30日 {market_sig.get('market_foreign_30d')}
                    投信今 {market_sig.get('market_trust_today')} / 5日 {market_sig.get('market_trust_5d')} / 30日 {market_sig.get('market_trust_30d')}
TX 大台法人淨 OI: 外資 {market_sig.get('TX_foreign_net_oi')} / 投信 {market_sig.get('TX_trust_net_oi')} / 自營商 {market_sig.get('TX_dealer_net_oi')}
TMF 微台散戶多空比 (最新): {tmf_latest_ratio}%

═══ 「{display_name}」原始資料 ═══
近 7 日股價(OHLCV): {data.get('price')}
近期三大法人: {data.get('institutional')}
近 7 日融資融券: {data.get('margin')}
近 7 日外資持股: {data.get('shareholding')}
近 7 日借券賣出餘額: {data.get('securities_lending')}

═══ 「{display_name}」籌碼訊號 ═══
[法人]
- 外資 5 日累計 (張): {signals.get('foreign_5d_net', 'N/A')} | 連續方向: {signals.get('foreign_streak', 'N/A')}
- 投信 5 日累計 (張): {signals.get('trust_5d_net', 'N/A')} | 連續方向: {signals.get('trust_streak', 'N/A')}
- 自營商 5 日累計 (張): {signals.get('dealer_5d_net', 'N/A')}
[融資融券]
- 融資餘額: {signals.get('margin_balance', 'N/A')} 張 | 5 日增減: {signals.get('margin_change_5d', 'N/A')}
- 融券餘額: {signals.get('short_balance', 'N/A')} 張 | 5 日增減: {signals.get('short_change_5d', 'N/A')}
- 券資比: {signals.get('short_margin_ratio_pct', 'N/A')}%
[借券 & 外資持股]
- 借券賣出餘額: {signals.get('short_sale_balance', 'N/A')}
- 外資持股: {signals.get('foreign_holding_pct', 'N/A')}% | 5 日變化: {signals.get('foreign_holding_change_5d_pct', 'N/A')}%

═══ 最新新聞 ═══
{news}

請嚴格依以下格式回覆 (限 300 字內, 條列為主, 內文使用「{display_name}」敘述, 不要新增其他段落):

📊 *籌碼面綜合* [偏多 / 中性偏多 / 中性 / 中性偏空 / 偏空]
- 法人動向: 外資+投信對「{display_name}」的合計訊號
- 主力動向: 融資融券+借券+外資持股的綜合解讀
- 矛盾訊號: 若有

📰 *新聞重點* (挑 2 則最具影響力, 標註利多/利空)

🔍 *短期觀察*
- 中性陳述「{display_name}」近期動能, 不直接喊買賣

⚠️ *風險提示*
- 1-2 點具體風險
"""
    return call_ai(prompt)


# ===== Telegram 推播 =====
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code == 400:
            r = requests.post(
                url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, timeout=10
            )
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ Telegram 推播失敗: {e}")


def fmt_oi(value):
    if value is None:
        return "N/A"
    return f"{int(value):+,}"


def fmt_pct(value):
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def fmt_billion(value):
    if value is None:
        return "N/A"
    return f"{value:+.2f}"


def send_market_summary(market_sig: dict):
    # TMF 5 日散戶多空比格式化
    tmf_ratios = market_sig.get("TMF_retail_ratios_5d") or []
    if tmf_ratios:
        tmf_lines = "\n".join(
            f"{r['date']}: {r['ratio']:+.2f}%" for r in tmf_ratios
        )
    else:
        tmf_lines = "資料不足"

    msg = f"""*🌐 大盤動態快訊* `{market_sig.get('market_date', '')}`

📈 *加權指數*
{market_sig.get('taiex_close')} ({market_sig.get('taiex_change_pct')}%)
技術面: {market_sig.get('taiex_ta', 'N/A')}

💱 *USD/TWD 銀行間即期 ({market_sig.get('usdtwd_date', '')})*
收盤 {market_sig.get('usdtwd')}
1日 {fmt_pct(market_sig.get('usdtwd_change_1d_pct'))} | 5日 {fmt_pct(market_sig.get('usdtwd_change_5d_pct'))}

💼 *三大法人買賣超 (億)*
外資   今 {fmt_billion(market_sig.get('market_foreign_today'))} / 5日 {fmt_billion(market_sig.get('market_foreign_5d'))} / 30日 {fmt_billion(market_sig.get('market_foreign_30d'))}
投信   今 {fmt_billion(market_sig.get('market_trust_today'))} / 5日 {fmt_billion(market_sig.get('market_trust_5d'))} / 30日 {fmt_billion(market_sig.get('market_trust_30d'))}
自營商 今 {fmt_billion(market_sig.get('market_dealer_today'))} / 5日 {fmt_billion(market_sig.get('market_dealer_5d'))} / 30日 {fmt_billion(market_sig.get('market_dealer_30d'))}

🎯 *TX 大台 法人未平倉淨口數*
外資: {fmt_oi(market_sig.get('TX_foreign_net_oi'))}
投信: {fmt_oi(market_sig.get('TX_trust_net_oi'))}
自營商: {fmt_oi(market_sig.get('TX_dealer_net_oi'))}
合計: {fmt_oi(market_sig.get('TX_total_net_oi'))}

📊 *TMF 微台 散戶多空比近5日*
{tmf_lines}
"""
    send_telegram(msg)


# ===== 主流程 =====
def main():
    today = datetime.now().strftime("%Y-%m-%d")
    validate_ai_config()
    current_model = CLAUDE_MODEL if AI_PROVIDER == "claude" else GEMINI_MODEL
    print(f"=== 每日股票分析 v3.9 ({today}) ===")
    print(f"AI 供應商: {AI_PROVIDER.upper()} | 模型: {current_model}")

    stock_ids = [s.strip() for s in STOCKS if s.strip()]

    print("\n[1/4] 取得股票名稱對照...")
    stock_names = get_stock_names(stock_ids)
    for sid in stock_ids:
        print(f"  {sid} = {stock_names.get(sid, sid)}")

    print("\n[2/4] 抓取大盤動態...")
    market = get_market_context()
    market_sig = calc_market_signals(market)
    print(f"  大盤訊號: {len(market_sig)} 筆")

    print("\n[3/4] 推播大盤摘要...")
    try:
        send_market_summary(market_sig)
        print("  ✓ 已送出")
    except Exception as e:
        print(f"  ⚠️ 失敗: {e}")

    print(f"\n[4/4] 處理 {len(stock_ids)} 檔股票...")
    for i, stock_id in enumerate(stock_ids):
        stock_name = stock_names.get(stock_id, stock_id)
        display = f"{stock_id} {stock_name}" if stock_name != stock_id else stock_id
        print(f"\n--- {display} ---")

        data = get_stock_data(stock_id)
        if not data["price"]:
            print(f"  ⏭ 跳過 ({stock_id} 無股價)")
            continue

        news = get_news(stock_id)
        signals = calc_signals(data)
        print(f"  訊號: {len(signals)} 筆 / 新聞: {len(news)} 則")

        try:
            analysis = analyze(
                stock_id, stock_name, data, market, market_sig, news, signals
            )
            message = (
                f"*📈 {display} 每日分析* `{today}`\n\n"
                f"{analysis}\n\n"
                f"_僅供參考, 投資請自行判斷_"
            )
            send_telegram(message)
            print(f"  ✓ 完成並已推播")
        except Exception as e:
            print(f"  ⚠️ 分析失敗: {e}")

        if AI_PROVIDER == "gemini" and i < len(stock_ids) - 1:
            time.sleep(GEMINI_CALL_INTERVAL)


if __name__ == "__main__":
    main()
