"""
每日台股分析機器人 v2 (擴充版)
新增: 借券賣出、外資持股、大盤背景, 並預先計算籌碼衍生訊號
資料源: FinMind v4 API (免費等級可用)
"""

import os
import requests
import feedparser
from collections import defaultdict
from datetime import datetime, timedelta
from anthropic import Anthropic

# ===== 設定區 =====
STOCKS = os.getenv("STOCKS", "2330,2454,2317").split(",")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


# ===== FinMind 通用呼叫 =====
def fetch_finmind(dataset: str, start_date: str, stock_id: str = "") -> list:
    """通用 FinMind v4 API 呼叫; 遇到付費限制或錯誤都會回空 list 不中斷"""
    params = {"dataset": dataset, "start_date": start_date, "token": FINMIND_TOKEN}
    if stock_id:
        params["data_id"] = stock_id
    try:
        r = requests.get(FINMIND_URL, params=params, timeout=20)
        if r.status_code == 400:
            print(f"  ⓘ {dataset}: 可能需要 Sponsor 等級, 跳過")
            return []
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"  ⚠️ FinMind {dataset} 失敗: {e}")
        return []


# ===== 資料抓取 =====
def get_stock_data(stock_id: str) -> dict:
    """個股多面向籌碼資料"""
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


def get_market_context() -> dict:
    """大盤背景: 加權指數 + 大盤三大法人"""
    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    return {
        "taiex": fetch_finmind("TaiwanStockPrice", start, "TAIEX")[-5:],
        "total_institutional": fetch_finmind(
            "TaiwanStockTotalInstitutionalInvestors", start
        )[-5:],
    }


def get_news(stock_id: str, max_items: int = 8) -> list:
    """Yahoo 股市 RSS 新聞"""
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


# ===== 衍生訊號計算 =====
def streak(values: list) -> int:
    """連續同向天數; +N=連 N 天買超, -N=連 N 天賣超"""
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
    """從原始資料推導關鍵訊號 (轉成張數, 比率為 %)"""
    sig = {}

    # ---- 三大法人 (FinMind 每天每法人一筆, 需聚合) ----
    inst = data.get("institutional", [])
    if inst:
        daily = defaultdict(lambda: {"foreign": 0, "trust": 0, "dealer": 0})
        for r in inst:
            net = (r.get("buy", 0) - r.get("sell", 0)) // 1000  # 股 -> 張
            name = r.get("name", "")
            d = r.get("date", "")
            if "Foreign" in name:
                daily[d]["foreign"] += net
            elif "Investment_Trust" in name or "Trust" in name:
                daily[d]["trust"] += net
            elif "Dealer" in name:
                daily[d]["dealer"] += net

        days = sorted(daily.keys())
        if days:
            sig["foreign_5d_net"] = sum(daily[d]["foreign"] for d in days[-5:])
            sig["trust_5d_net"] = sum(daily[d]["trust"] for d in days[-5:])
            sig["dealer_5d_net"] = sum(daily[d]["dealer"] for d in days[-5:])
            sig["foreign_streak"] = streak([daily[d]["foreign"] for d in days])
            sig["trust_streak"] = streak([daily[d]["trust"] for d in days])

    # ---- 融資融券 ----
    margin = data.get("margin", [])
    if margin:
        latest = margin[-1]
        m_bal = latest.get("MarginPurchaseTodayBalance", 0)
        s_bal = latest.get("ShortSaleTodayBalance", 0)
        sig["margin_balance"] = m_bal
        sig["short_balance"] = s_bal
        sig["short_margin_ratio_pct"] = round(s_bal / m_bal * 100, 2) if m_bal else 0
        if len(margin) >= 5:
            sig["margin_change_5d"] = m_bal - margin[-5].get(
                "MarginPurchaseTodayBalance", 0
            )
            sig["short_change_5d"] = s_bal - margin[-5].get(
                "ShortSaleTodayBalance", 0
            )

    # ---- 借券賣出餘額 ----
    sl = data.get("securities_lending", [])
    if sl:
        latest_sl = sl[-1]
        # FinMind 欄位可能是 Volume / today_balance 等, 用多個 fallback
        sig["short_sale_balance"] = (
            latest_sl.get("Volume")
            or latest_sl.get("today_balance")
            or latest_sl.get("balance")
            or 0
        )

    # ---- 外資持股比例 ----
    sh = data.get("shareholding", [])
    if sh and len(sh) >= 2:
        get_ratio = lambda r: (
            r.get("ForeignInvestmentSharesRatio")
            or r.get("ForeignInvestmentRemainRatio")
            or 0
        )
        latest_ratio = get_ratio(sh[-1])
        old_ratio = get_ratio(sh[max(0, len(sh) - 5)])
        sig["foreign_holding_pct"] = latest_ratio
        sig["foreign_holding_change_5d_pct"] = round(latest_ratio - old_ratio, 3)

    return sig


# ===== AI 分析 =====
def analyze(
    stock_id: str, data: dict, market: dict, news: list, signals: dict
) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""你是專業台股分析師。請依據以下「{stock_id}」的近期資料給出客觀分析。

═══ 大盤背景 ═══
加權指數近 5 日: {market.get('taiex')}
大盤三大法人近 5 日: {market.get('total_institutional')}

═══ 個股原始資料 ═══
近 7 日股價(OHLCV): {data.get('price')}
近期三大法人 (分外資/投信/自營商): {data.get('institutional')}
近 7 日融資融券: {data.get('margin')}
近 7 日外資持股: {data.get('shareholding')}
近 7 日借券賣出餘額: {data.get('securities_lending')}

═══ 預先計算的籌碼訊號 ═══
[法人]
- 外資 5 日累計 (張): {signals.get('foreign_5d_net', 'N/A')}
- 投信 5 日累計 (張): {signals.get('trust_5d_net', 'N/A')}
- 自營商 5 日累計 (張): {signals.get('dealer_5d_net', 'N/A')}
- 外資連續方向: {signals.get('foreign_streak', 'N/A')} (+ 表連續買超天數, - 表賣超)
- 投信連續方向: {signals.get('trust_streak', 'N/A')}
[融資融券]
- 融資餘額 (張): {signals.get('margin_balance', 'N/A')}
- 融券餘額 (張): {signals.get('short_balance', 'N/A')}
- 券資比 (%): {signals.get('short_margin_ratio_pct', 'N/A')}
- 5 日融資增減 (張): {signals.get('margin_change_5d', 'N/A')}
- 5 日融券增減 (張): {signals.get('short_change_5d', 'N/A')}
[借券 & 外資持股]
- 借券賣出餘額: {signals.get('short_sale_balance', 'N/A')}
- 外資持股比例 (%): {signals.get('foreign_holding_pct', 'N/A')}
- 5 日外資持股變化 (%): {signals.get('foreign_holding_change_5d_pct', 'N/A')}

═══ 最新新聞 ═══
{news}

請依以下格式回覆 (限 400 字內, 條列為主):

📊 *籌碼面綜合* [偏多 / 中性偏多 / 中性 / 中性偏空 / 偏空]
- 法人動向: 外資+投信合計訊號
- 主力動向: 融資融券+借券+外資持股的綜合解讀
- 矛盾訊號: 如果有 (例如外資買但融資也大增可能是散戶追高)

📰 *新聞重點* (挑 2 則最有影響力)
- 利多/利空標註

🌐 *大盤連動*
- 個股相對大盤強弱 (跟得上漲, 抗跌, 弱於大盤等)

🔍 *短期觀察*
- 中性陳述近期動能, 不直接喊買賣

⚠️ *風險提示*
- 1-2 點具體風險
"""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ===== Telegram 推播 =====
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ Telegram 推播失敗: {e}")


# ===== 主流程 =====
def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 每日股票分析 v2 ({today}) ===")

    print("\n[1/2] 抓取大盤背景...")
    market = get_market_context()
    print(f"  加權指數筆數: {len(market.get('taiex', []))}")
    print(f"  大盤三大法人筆數: {len(market.get('total_institutional', []))}")

    print(f"\n[2/2] 處理 {len(STOCKS)} 檔股票...")
    for stock_id in STOCKS:
        stock_id = stock_id.strip()
        print(f"\n--- {stock_id} ---")

        data = get_stock_data(stock_id)
        if not data["price"]:
            print(f"  ⏭ 跳過 ({stock_id} 無股價資料)")
            continue

        news = get_news(stock_id)
        signals = calc_signals(data)
        print(f"  訊號數: {len(signals)} 筆 / 新聞數: {len(news)} 則")

        try:
            analysis = analyze(stock_id, data, market, news, signals)
            message = (
                f"*📈 {stock_id} 每日分析* `{today}`\n\n"
                f"{analysis}\n\n"
                f"_僅供參考, 投資請自行判斷_"
            )
            send_telegram(message)
            print(f"  ✓ 完成並已推播")
        except Exception as e:
            print(f"  ⚠️ 分析失敗: {e}")


if __name__ == "__main__":
    main()
