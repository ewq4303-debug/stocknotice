import requests
import time
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# 【替換說明】
#   原本的 get_borrowing_data() 用 FinMind TaiwanStockSecuritiesLending，
#   但該 API 是「借券成交明細」（每筆交易事件），沒有「餘額」欄位，永遠回 0。
#
#   本版改用 TWSE TWT93U 端點（上市股）+ TPEX 端點（上櫃股），
#   回傳同樣的 dict 介面，上游程式不需要改。
#
#   資料來源：
#     上市: https://www.twse.com.tw/exchangeReport/TWT93U
#     上櫃: https://www.tpex.org.tw/web/stock/margin_short/sbl/sblStat_result.php
#
#   欄位對應（TWSE TWT93U，14 欄 0-indexed）：
#     [0]  日期（民國 "114/05/20"）
#     [1]  融券前日餘額
#     [2]  融券賣出
#     [3]  融券買進
#     [4]  融券現金償還
#     [5]  融券今日餘額
#     [6]  融券限額
#     [7]  借券前日餘額
#     [8]  借券賣出
#     [9]  借券還券
#     [10] 借券調整
#     [11] 借券今日餘額  ← 我們要的
#     [12] 借券限額
#     [13] 備注
#
# ⚠️ 注意事項：
#   1. TWSE 有 rate limit（每分鐘約 30 次），主程式跑多檔時需要在外層 sleep。
#      本函數已內建 0.5s sleep 於每次 HTTP 請求之後。
#   2. TPEX 欄位位置需在第一次執行後透過 debug log 驗證（見函數內說明）。
#   3. 上市/上櫃判斷：先試 TWSE，無資料再 fallback TPEX。
# ─────────────────────────────────────────────────────────────────────────────

_TWSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.twse.com.tw/zh/trading/short-sale/TWT93U.html",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

_TPEX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.tpex.org.tw/",
}


def _parse_twse_number(s: str) -> int:
    """把 '30,390,988' 之類的字串轉成 int，失敗回 0。"""
    try:
        return int(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0


def _roc_date_to_ad(roc_str: str) -> str:
    """
    把民國年 '114/05/20' → '2025-05-20'。
    TWSE 回傳的日期格式固定是 'YYY/MM/DD'（民國三位）。
    """
    parts = roc_str.strip().split("/")
    if len(parts) != 3:
        return ""
    return f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}"


def _fetch_twse_sbl(stock_id: str, query_date: str) -> list[dict]:
    """
    呼叫 TWSE TWT93U，回傳該月所有交易日的借券餘額。

    query_date: 'YYYYMMDD'（TWSE 接受同月內任何一天，會回整個月）
    回傳: [{"date": "2025-05-20", "balance": 30390988}, ...]
    """
    url = "https://www.twse.com.tw/exchangeReport/TWT93U"
    params = {"response": "json", "date": query_date, "stockNo": stock_id}
    rows = []
    try:
        r = requests.get(url, params=params, headers=_TWSE_HEADERS, timeout=15)
        time.sleep(0.5)  # 避免觸發 rate limit
        if r.status_code != 200:
            return rows
        data = r.json()
        if data.get("stat") != "OK":
            return rows
        for row in data.get("data", []):
            if len(row) < 12:
                continue
            date_ad = _roc_date_to_ad(row[0])
            if not date_ad:
                continue
            sbl_balance = _parse_twse_number(row[11])
            rows.append({"date": date_ad, "balance": sbl_balance})
    except Exception as e:
        print(f"[warn] TWSE TWT93U {stock_id} ({query_date}) 失敗: {e}")
    return rows


def _fetch_tpex_sbl(stock_id: str, check_date: datetime) -> dict | None:
    """
    呼叫 TPEX 借券餘額端點，回傳單日的 {"date": ..., "balance": ...}。

    ⚠️ TPEX 欄位位置尚未在真實環境驗證，第一次執行後請看 debug log 確認。
       函數已印出原始 row 供核對。

    TPEX 日期格式：民國 'YYY/MM/DD'（與 TWSE 相同）
    """
    year_roc = check_date.year - 1911
    date_str = f"{year_roc}/{check_date.month:02d}/{check_date.day:02d}"

    url = (
        "https://www.tpex.org.tw/web/stock/margin_short/sbl/"
        "sblStat_result.php"
    )
    params = {
        "l": "zh-tw",
        "o": "json",
        "d": date_str,
        "s": "0,asc,0",
    }
    try:
        r = requests.get(url, params=params, headers=_TPEX_HEADERS, timeout=15)
        time.sleep(0.5)
        if r.status_code != 200:
            return None
        data = r.json()
        aa_data = data.get("aaData", [])
        for row in aa_data:
            # 第 0 欄通常是股票代號
            if str(row[0]).strip() == stock_id:
                # ── debug 區塊（驗證後可刪除） ──────────────────────────────
                nonzero = {
                    i: v for i, v in enumerate(row)
                    if v not in ("", "0", 0, "--", None)
                }
                print(f"[debug] TPEX SBL {stock_id} {date_str} 非零欄位: {nonzero}")
                # ─────────────────────────────────────────────────────────────

                # 暫用 index 6 作為「借券賣出餘額」，
                # 待確認欄位順序後改成正確 index。
                # TPEX sblStat 典型欄位：
                #   [0]代號 [1]名稱 [2]前日餘額 [3]賣出 [4]還券 [5]調整 [6]今日餘額 [7]限額
                sbl_balance = _parse_twse_number(row[6])
                return {
                    "date": str(check_date.date()),
                    "balance": sbl_balance,
                }
    except Exception as e:
        print(f"[warn] TPEX SBL {stock_id} {date_str} 失敗: {e}")
    return None


def get_borrowing_data(stock_id: str) -> dict:
    """
    抓取個股借券賣出餘額（每日快照），回傳與原版相同介面：
    {
        "borrow_balance": int,   # 最新借券餘額（張）
        "borrow_change":  int,   # 較前一日增減（張）
        "borrow_5d":      int,   # 較 5 個交易日前增減（張）
        "borrow_20d":     int,   # 較 20 個交易日前增減（張）
        "history": [{"date": "YYYY-MM-DD", "balance": int}, ...]  # 最新 30 日，新→舊
    }
    """
    result = {
        "borrow_balance": 0,
        "borrow_change":  0,
        "borrow_5d":      0,
        "borrow_20d":     0,
        "history":        [],
    }

    try:
        today = now_tw()  # 使用專案現有的 now_tw()

        # ── Step 1：嘗試 TWSE（上市股）────────────────────────────────────
        # 抓本月 + 前兩個月，確保覆蓋 20 個交易日以上
        all_twse_rows: list[dict] = []
        for months_back in range(3):
            # 每次往前推一個月，用當月第一天作為 query date
            query_dt = (today.replace(day=1) - timedelta(days=months_back * 28)).replace(day=1)
            query_date_str = query_dt.strftime("%Y%m%d")
            rows = _fetch_twse_sbl(stock_id, query_date_str)
            all_twse_rows.extend(rows)
            if len(all_twse_rows) >= 25:  # 夠 20 個交易日就停止
                break

        # ── Step 2：若 TWSE 無資料，fallback 到 TPEX（上櫃股）──────────
        if not all_twse_rows:
            print(f"[debug] {stock_id} TWSE TWT93U 無資料，改試 TPEX SBL")
            tpex_rows: list[dict] = []
            days_checked = 0
            check_dt = today
            while len(tpex_rows) < 25 and days_checked < 60:
                check_dt -= timedelta(days=1)
                days_checked += 1
                if check_dt.weekday() >= 5:  # 跳過週末
                    continue
                row = _fetch_tpex_sbl(stock_id, check_dt)
                if row:
                    tpex_rows.append(row)
            all_twse_rows = tpex_rows

        if not all_twse_rows:
            print(f"[warn] {stock_id} 借券資料：TWSE 與 TPEX 均無資料")
            return result

        # ── Step 3：去重、排序（新→舊）───────────────────────────────────
        seen: dict[str, int] = {}
        for row in all_twse_rows:
            # 若同一天有多筆（不同月份請求可能重疊），取最後出現的
            seen[row["date"]] = row["balance"]
        sorted_rows = sorted(seen.items(), reverse=True)  # [("2025-05-20", 30390988), ...]
        history = [{"date": d, "balance": b} for d, b in sorted_rows]

        # ── Step 4：填入結果──────────────────────────────────────────────
        if history:
            result["borrow_balance"] = history[0]["balance"]
        if len(history) >= 2:
            result["borrow_change"] = history[0]["balance"] - history[1]["balance"]
        if len(history) >= 5:
            result["borrow_5d"] = history[0]["balance"] - history[4]["balance"]
        if len(history) >= 20:
            result["borrow_20d"] = history[0]["balance"] - history[19]["balance"]
        result["history"] = history[:30]

        print(
            f"[info] {stock_id} 借券餘額: {result['borrow_balance']:,} 張  "
            f"日增: {result['borrow_change']:+,}  "
            f"5d: {result['borrow_5d']:+,}  "
            f"20d: {result['borrow_20d']:+,}"
        )

    except Exception as e:
        print(f"[error] {stock_id} get_borrowing_data 意外失敗: {e}")

    return result
