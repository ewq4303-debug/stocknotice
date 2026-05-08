"""
每日台股分析機器人 v3.3 (Claude API)
v3.3 改動: 標題顯示「代號 名稱」(例: 2330 台積電), 內文 AI 用名稱敘述提升可讀性
保留: 個股籌碼 + 大盤動態 + 衍生訊號; 大盤摘要與個股訊息分開推播
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


# ===== 股票名稱查詢 =====
def get_stock_names(stock_ids: list) -> dict:
    """一次抓全市場股票對照, 過濾出追蹤清單. 失敗時用代號當名稱"""
    try:
        # TaiwanStockInfo 不依賴日期, 但 fetch_finmind 一律帶 start_date
        info = fetch_finmind("TaiwanStockInfo", "2024-01-01")
        all_names = {r["stock_id"]: r.get("stock_name", "") for r in info}
        result = {}
        for sid in stock_ids:
            name = all_names.get(sid, "")
            result[sid] = name if name else sid
        return result
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

def get_usdtwd_yahoo(days: int = 7) -> list:
    """從 Yahoo Finance 抓 USD/TWD 近 N 日匯率"""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/TWD=X"
        params = {"interval": "1d", "range": f"{days + 5}d"}
        headers = {"User-Agent": "Mozilla/5.0"}  # Yahoo 會擋沒 UA 的請求
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        history = []
        for ts, close in zip(timestamps, closes):
            if close is not None:
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                history.append({"date": date_str, "close": round(close, 4)})
        return history[-days:]
    except Exception as e:
        print(f"  ⚠️ Yahoo 匯率抓取失敗: {e}")
        return []


# ===== 大盤動態抓取 =====
def get_market_context() -> dict:
    start = (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%d")
    return {
        "taiex": fetch_finmind("TaiwanStockPrice", start, "TAIEX")[-5:],
        "total_institutional": fetch_finmind(
            "TaiwanStockTotalInstitutionalInvestors", start
        ),
        "fx_usdtwd": get_usdtwd_yahoo(7),
        "fut_TX": fetch_finmind("TaiwanFuturesInstitutionalInvestors", start, "TX"),
        "fut_MTX": fetch_finmind("TaiwanFuturesInstitutionalInvestors", start, "MTX"),
        "fut_TMF": fetch_finmind("TaiwanFuturesInstitutionalInvestors", start, "TMF"),
        "opt_TXO": fetch_finmind("TaiwanOptionInstitutionalInvestors", start, "TXO"),
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
            name = r.get("name", "")
            d = r.get("date", "")
            if "Foreign" in name:
                daily[d]["foreign"] += net
            elif "Trust" in name:
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

    sl = data.get("securities_lending", [])
    if sl:
        sig["short_sale_balance"] = (
            sl[-1].get("Volume")
            or sl[-1].get("today_balance")
            or sl[-1].get("balance")
            or 0
        )

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


def calc_market_signals(market: dict) -> dict:
    sig = {}

    taiex = market.get("taiex", [])
    if taiex and len(taiex) >= 2:
        try:
            latest, prev = taiex[-1], taiex[-2]
            sig["taiex_close"] = latest.get("close")
            chg = latest["close"] - prev["close"]
            sig["taiex_change"] = round(chg, 2)
            sig["taiex_change_pct"] = round(chg / prev["close"] * 100, 2)
        except (TypeError, KeyError):
            pass

    fx = market.get("fx_usdtwd", [])
    if fx:
        get_rate = lambda r: (
            r.get("cash_buy") or r.get("spot_buy") or
            r.get("close") or r.get("rate") or 0
        )
        latest_rate = get_rate(fx[-1])
        if latest_rate:
            sig["usdtwd"] = latest_rate
            if len(fx) >= 2:
                first_rate = get_rate(fx[0])
                if first_rate:
                    sig["usdtwd_change_5d"] = round(latest_rate - first_rate, 4)
                    if latest_rate < first_rate:
                        sig["usdtwd_trend"] = "台幣升值 (利多外資匯入)"
                    elif latest_rate > first_rate:
                        sig["usdtwd_trend"] = "台幣貶值 (留意外資匯出)"
                    else:
                        sig["usdtwd_trend"] = "持平"

    total_inst = market.get("total_institutional", [])
    if total_inst:
        latest_date = max(r["date"] for r in total_inst)
        today_rows = [r for r in total_inst if r["date"] == latest_date]
        sig["market_date"] = latest_date
        for r in today_rows:
            name = r.get("name", "")
            net_billion = round((r.get("buy", 0) - r.get("sell", 0)) / 1e8, 2)
            if "Foreign" in name:
                sig["market_foreign_net_billion"] = (
                    sig.get("market_foreign_net_billion", 0) + net_billion
                )
            elif "Trust" in name:
                sig["market_trust_net_billion"] = net_billion
            elif "Dealer" in name:
                sig["market_dealer_net_billion"] = (
                    sig.get("market_dealer_net_billion", 0) + net_billion
                )

    for code, key in [("TX", "fut_TX"), ("MTX", "fut_MTX"), ("TMF", "fut_TMF")]:
        rows = market.get(key, [])
        if not rows:
            continue
        dates = sorted(set(r["date"] for r in rows))
        if not dates:
            continue
        latest_date = dates[-1]
        prev_date = dates[max(0, len(dates) - 5)]

        def net_oi(target_date):
            net = 0
            for r in rows:
                if r["date"] != target_date:
                    continue
                long_oi = r.get("long_open_interest_balance_volume", 0) or 0
                short_oi = r.get("short_open_interest_balance_volume", 0) or 0
                net += long_oi - short_oi
            return net

        sig[f"{code}_inst_net_oi"] = net_oi(latest_date)
        sig[f"{code}_inst_net_oi_change_5d"] = (
            net_oi(latest_date) - net_oi(prev_date)
        )

    if "TMF_inst_net_oi" in sig:
        net = sig["TMF_inst_net_oi"]
        if net > 1000:
            sig["TMF_retail_sentiment"] = f"散戶偏空 (法人淨多 {net} 口)"
        elif net < -1000:
            sig["TMF_retail_sentiment"] = f"散戶偏多 (法人淨空 {abs(net)} 口)"
        else:
            sig["TMF_retail_sentiment"] = f"散戶中性 (法人淨 {net} 口)"

    opts = market.get("opt_TXO", [])
    if opts:
        latest_date = max(r["date"] for r in opts)
        today_opts = [r for r in opts if r["date"] == latest_date]
        call_net, put_net = 0, 0
        for r in today_opts:
            cp = str(r.get("call_put", ""))
            long_oi = r.get("long_open_interest_balance_volume", 0) or 0
            short_oi = r.get("short_open_interest_balance_volume", 0) or 0
            net = long_oi - short_oi
            if "Call" in cp or "call" in cp:
                call_net += net
            elif "Put" in cp or "put" in cp:
                put_net += net
        sig["txo_call_inst_net_oi"] = call_net
        sig["txo_put_inst_net_oi"] = put_net
        if call_net != 0:
            sig["txo_pc_ratio"] = round(put_net / call_net, 2)

    return sig


# ===== AI 分析 (Claude) =====
def analyze(
    stock_id: str,
    stock_name: str,
    data: dict,
    market: dict,
    market_sig: dict,
    news: list,
    signals: dict,
) -> str:
    """個股分析; 內文使用股票名稱敘述提升可讀性"""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # 在 prompt 用 display_name 統一指代; 名稱為空時退回代號
    display_name = stock_name if stock_name and stock_name != stock_id else stock_id

    prompt = f"""你是專業台股分析師。請依據以下資料給出「{display_name}」({stock_id}) 的客觀分析。

【重要寫作要求】
- 內文中提及這檔股票時, 請使用「{display_name}」這個名稱, 不要使用代號 {stock_id}
- 不要在輸出中重述大盤摘要也不要寫個股 vs 大盤段落 (大盤背景僅供你內部分析參考)

═══ 大盤背景 (僅供分析時參考, 不要在輸出中重述) ═══
加權指數 {market_sig.get('taiex_close')} ({market_sig.get('taiex_change_pct')}%)
USD/TWD {market_sig.get('usdtwd')} | {market_sig.get('usdtwd_trend', '')}
大盤外資 {market_sig.get('market_foreign_net_billion')} 億 / 投信 {market_sig.get('market_trust_net_billion')} 億
期貨法人淨 OI: TX {market_sig.get('TX_inst_net_oi')} / MTX {market_sig.get('MTX_inst_net_oi')} / TMF {market_sig.get('TMF_inst_net_oi')}
微台散戶推估: {market_sig.get('TMF_retail_sentiment')}
TXO Put/Call 比: {market_sig.get('txo_pc_ratio')}

═══ 「{display_name}」原始資料 ═══
近 7 日股價(OHLCV): {data.get('price')}
近期三大法人 (分外資/投信/自營商): {data.get('institutional')}
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
- 矛盾訊號: 若有 (例如外資買但融資也大增)

📰 *新聞重點* (挑 2 則最具影響力, 標註利多/利空)

🔍 *短期觀察*
- 中性陳述「{display_name}」近期動能, 不直接喊買賣

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
                "text": text[:4000],
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        if r.status_code == 400:
            r = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]},
                timeout=10,
            )
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ Telegram 推播失敗: {e}")


def send_market_summary(market_sig: dict):
    msg = f"""*🌐 大盤動態快訊* `{market_sig.get('market_date', '')}`

📈 *加權指數*
{market_sig.get('taiex_close')} ({market_sig.get('taiex_change_pct')}%)

💱 *匯率*
USD/TWD: {market_sig.get('usdtwd')} ({market_sig.get('usdtwd_trend', '')})

💼 *大盤三大法人 (億)*
外資 {market_sig.get('market_foreign_net_billion')} | 投信 {market_sig.get('market_trust_net_billion')} | 自營 {market_sig.get('market_dealer_net_billion')}

🎯 *期貨法人淨 OI (口)*
TX: {market_sig.get('TX_inst_net_oi')} (5日 {market_sig.get('TX_inst_net_oi_change_5d', 0):+d})
MTX: {market_sig.get('MTX_inst_net_oi')} (5日 {market_sig.get('MTX_inst_net_oi_change_5d', 0):+d})
TMF: {market_sig.get('TMF_inst_net_oi')} (5日 {market_sig.get('TMF_inst_net_oi_change_5d', 0):+d})

💡 *微台散戶情緒*
{market_sig.get('TMF_retail_sentiment', 'N/A')}
"""
    send_telegram(msg)


# ===== 主流程 =====
def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 每日股票分析 v3.3 Claude ({today}) ===")
    print(f"模型: {MODEL}")

    # 清理 STOCKS 字串
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
    for stock_id in stock_ids:
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


if __name__ == "__main__":
    main()
