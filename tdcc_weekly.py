"""
集保戶股權分散表週報機器人 v2.0
v2.0 新增:
  - 大戶/散戶三種判定法:
    1. 傳統法 (固定級距: 千張、400張以上、散戶<50張)
    2. 資金水位法 (依個股股價, 500萬/5000萬分界)
    3. 籌碼斷層法 (找人均佔比 ≥ 前一階 3 倍的級距)
  - 股東人數變化
  - 平均每戶持股張數
  - 自動抓 FinMind 個股最新股價 (供資金水位法計算)
"""

import os
import csv
import json
import subprocess
import requests
from collections import defaultdict
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

# ===== 設定 =====
STOCKS = os.getenv("STOCKS", "2330,2454,2317").split(",")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TDCC_URLS = [
    "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
    "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5",
]
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
HISTORY_DIR = Path("tdcc_history")

# 集保各級距「上限股數」(level 15 無上限)
LEVEL_UPPER_SHARES = {
    1: 999, 2: 5_000, 3: 10_000, 4: 15_000, 5: 20_000,
    6: 30_000, 7: 40_000, 8: 50_000, 9: 100_000, 10: 200_000,
    11: 400_000, 12: 600_000, 13: 800_000, 14: 1_000_000,
    15: None,  # > 1,000,000 股, 無上限
}

# 資金水位門檻 (NTD)
RETAIL_CAP = 5_000_000       # 散戶: 投入資金 ≤ 500 萬
MID_CAP = 50_000_000         # 中實戶: 500 萬 ~ 5000 萬, 超過為大戶

# 籌碼斷層法的倍率門檻
CONCENTRATION_MULTIPLIER = 3.0


# ===== 抓集保資料 =====
def fetch_tdcc() -> tuple:
    last_error = None
    for url in TDCC_URLS:
        try:
            print(f"  嘗試 URL: {url}")
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            print(f"    HTTP {r.status_code} | size={len(r.content):,} bytes")
            r.raise_for_status()

            text = r.content.decode("utf-8-sig", errors="replace")
            if "證券代號" not in text or "持股分級" not in text:
                preview = text[:300].replace("\n", " ")
                print(f"    ⓘ 內容不像 CSV (前 300 字): {preview}")
                last_error = "content mismatch"
                continue

            print("    ✓ 取得 CSV")
            reader = csv.DictReader(StringIO(text))
            print(f"    欄位: {reader.fieldnames}")

            by_stock = defaultdict(dict)
            data_date = None
            rows_parsed = 0

            for row in reader:
                try:
                    stock_id = (row.get("證券代號") or "").strip()
                    level = int(row.get("持股分級") or 0)
                    if not stock_id or level < 1 or level > 15:
                        continue
                    by_stock[stock_id][level] = {
                        "people": int(row.get("人數") or 0),
                        "shares": int(row.get("股數") or 0),
                        "pct": float(
                            row.get("占集保庫存數比例%")
                            or row.get("佔集保庫存數比例%") or 0
                        ),
                    }
                    if not data_date:
                        data_date = (row.get("資料日期") or "").strip()
                    rows_parsed += 1
                except (ValueError, TypeError):
                    continue

            print(f"    成功解析 {rows_parsed:,} 筆 / {len(by_stock):,} 檔股票")
            return data_date, dict(by_stock)

        except requests.exceptions.RequestException as e:
            print(f"    ⚠️ {e}")
            last_error = str(e)
            continue

    print(f"  ⚠️ 所有 URL 都失敗 (最後錯誤: {last_error})")
    return None, {}


# ===== 抓股價 + 股名 (FinMind) =====
def get_latest_prices(stock_ids: list) -> dict:
    """抓追蹤股票最新收盤價, 給資金水位法計算用"""
    if not FINMIND_TOKEN:
        print("  ⓘ 未設定 FINMIND_TOKEN, 跳過股價 (資金水位法不可用)")
        return {}

    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    prices = {}
    for sid in stock_ids:
        try:
            r = requests.get(
                FINMIND_URL,
                params={
                    "dataset": "TaiwanStockPrice",
                    "data_id": sid,
                    "start_date": start,
                    "token": FINMIND_TOKEN,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            if data:
                prices[sid] = float(data[-1].get("close") or 0)
        except Exception as e:
            print(f"  ⚠️ {sid} 股價抓取失敗: {e}")
    return prices


def get_stock_names(stock_ids: list) -> dict:
    if not FINMIND_TOKEN:
        return {sid: sid for sid in stock_ids}
    try:
        r = requests.get(
            FINMIND_URL,
            params={
                "dataset": "TaiwanStockInfo",
                "start_date": "2024-01-01",
                "token": FINMIND_TOKEN,
            },
            timeout=20,
        )
        r.raise_for_status()
        info = r.json().get("data", [])
        all_names = {x["stock_id"]: x.get("stock_name", "") for x in info}
        return {sid: (all_names.get(sid) or sid) for sid in stock_ids}
    except Exception as e:
        print(f"  ⚠️ 取得股票名稱失敗: {e}")
        return {sid: sid for sid in stock_ids}


# ===== 衍生指標 =====
def classify_by_capital(levels: dict, stock_price: float) -> dict:
    """資金水位法: 用各級距上限股數 × 股價判定分類"""
    result = {"retail_pct": 0.0, "mid_pct": 0.0, "big_pct": 0.0}
    if not stock_price or stock_price <= 0:
        return result

    for lvl in range(1, 16):
        d = levels.get(lvl, {})
        pct = d.get("pct", 0)
        upper = LEVEL_UPPER_SHARES[lvl]

        # level 15 無上限, 直接歸大戶
        if upper is None:
            result["big_pct"] += pct
            continue

        max_capital = upper * stock_price
        if max_capital <= RETAIL_CAP:
            result["retail_pct"] += pct
        elif max_capital <= MID_CAP:
            result["mid_pct"] += pct
        else:
            result["big_pct"] += pct

    return {k: round(v, 3) for k, v in result.items()}


def find_concentration_break(levels: dict, multiplier: float = CONCENTRATION_MULTIPLIER) -> int:
    """籌碼斷層法: 找出第一個「人均佔比 ≥ 前一階 multiplier 倍」的級距
    回傳該級距 (從此往上視為大戶); 找不到斷層時預設 12 (400 張以上)"""
    avg_pcts = {}
    for lvl in range(1, 16):
        d = levels.get(lvl, {})
        people = d.get("people", 0)
        pct = d.get("pct", 0)
        avg_pcts[lvl] = pct / people if people > 0 else 0

    # 由低往高找第一個明顯斷層
    for lvl in range(2, 16):
        prev = avg_pcts.get(lvl - 1, 0)
        curr = avg_pcts.get(lvl, 0)
        if prev > 0 and curr >= prev * multiplier:
            return lvl

    return 12  # 預設: 400 張以上算大戶


def classify_by_break(levels: dict, break_level: int) -> dict:
    """依斷層級距, 計算大戶/散戶占比"""
    big_pct = sum(levels.get(i, {}).get("pct", 0) for i in range(break_level, 16))
    retail_pct = sum(levels.get(i, {}).get("pct", 0) for i in range(1, break_level))
    return {
        "break_level": break_level,
        "big_pct": round(big_pct, 3),
        "retail_pct": round(retail_pct, 3),
    }


def calc_metrics(levels: dict, stock_price: float = None) -> dict:
    """彙整所有指標"""
    if not levels:
        return {}

    m = {}

    # ---- 結構性指標 ----
    total_people = sum(d.get("people", 0) for d in levels.values())
    total_shares = sum(d.get("shares", 0) for d in levels.values())
    m["total_holders"] = total_people
    m["avg_lots"] = round(total_shares / total_people / 1000, 2) if total_people else 0
    m["stock_price"] = stock_price or 0

    # ---- 傳統法 (固定級距) ----
    m["trad_level15_pct"] = round(levels.get(15, {}).get("pct", 0), 3)
    m["trad_level15_people"] = levels.get(15, {}).get("people", 0)
    m["trad_big_400_pct"] = round(
        sum(levels.get(i, {}).get("pct", 0) for i in range(12, 16)), 3
    )
    m["trad_retail_50_pct"] = round(
        sum(levels.get(i, {}).get("pct", 0) for i in range(1, 9)), 3
    )

    # ---- 資金水位法 ----
    if stock_price:
        cap_result = classify_by_capital(levels, stock_price)
        m["cap_retail_pct"] = cap_result["retail_pct"]
        m["cap_mid_pct"] = cap_result["mid_pct"]
        m["cap_big_pct"] = cap_result["big_pct"]

    # ---- 籌碼斷層法 ----
    break_level = find_concentration_break(levels)
    br_result = classify_by_break(levels, break_level)
    m["break_level"] = br_result["break_level"]
    m["break_big_pct"] = br_result["big_pct"]
    m["break_retail_pct"] = br_result["retail_pct"]

    return m


# ===== 快照管理 =====
def load_latest_snapshot():
    if not HISTORY_DIR.exists():
        return None, None
    files = sorted(HISTORY_DIR.glob("*.json"))
    if not files:
        return None, None
    with open(files[-1], encoding="utf-8") as f:
        data = json.load(f)
    return data.get("date"), data.get("metrics", {})


def save_snapshot(data_date: str, metrics: dict) -> Path:
    HISTORY_DIR.mkdir(exist_ok=True)
    path = HISTORY_DIR / f"{data_date}.json"
    out = {"date": data_date, "metrics": metrics}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return path


def git_commit_push(file_path: Path):
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            [
                "git", "config", "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ],
            check=True,
        )
        subprocess.run(["git", "add", str(file_path)], check=True)
        result = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if result.returncode == 0:
            print("  ⓘ 無新變動, 不需 commit")
            return
        subprocess.run(
            ["git", "commit", "-m", f"TDCC snapshot {file_path.stem}"], check=True
        )
        subprocess.run(["git", "push"], check=True)
        print(f"  ✓ 已 commit + push: {file_path.name}")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ Git 操作失敗: {e}")


# ===== Telegram =====
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


def fmt_diff(curr, prev, fmt="{:+.3f}", show_zero=True):
    """格式化變化值. curr/prev 為 None 時回傳空字串"""
    if prev is None:
        return ""
    diff = curr - prev
    if not show_zero and abs(diff) < 0.001:
        return ""
    return f" ({fmt.format(diff)})"


def fmt_int_diff(curr, prev):
    if prev is None:
        return ""
    diff = curr - prev
    return f" ({diff:+,})"


def build_stock_message(sid: str, name: str, m: dict, prev_m: dict = None) -> str:
    """組裝單檔股票的訊息區塊"""
    display = f"{sid} {name}" if name and name != sid else sid
    p = prev_m or {}

    lines = [f"📊 *{display}*"]

    # 結構行
    price_str = f"股價 {m['stock_price']:.2f}" if m.get("stock_price") else "股價 N/A"
    holders_str = f"總股東 {m['total_holders']:,}"
    if "total_holders" in p:
        holders_str += fmt_int_diff(m["total_holders"], p["total_holders"])
    avg_str = f"均持 {m['avg_lots']:.2f} 張"
    if "avg_lots" in p:
        avg_str += fmt_diff(m["avg_lots"], p["avg_lots"], "{:+.2f}")
    lines.append(f"{price_str} | {holders_str} | {avg_str}")

    # 傳統法
    big_400 = f"{m['trad_big_400_pct']}%" + fmt_diff(
        m['trad_big_400_pct'], p.get('trad_big_400_pct')
    )
    retail_50 = f"{m['trad_retail_50_pct']}%" + fmt_diff(
        m['trad_retail_50_pct'], p.get('trad_retail_50_pct')
    )
    l15 = f"{m['trad_level15_pct']}%" + fmt_diff(
        m['trad_level15_pct'], p.get('trad_level15_pct')
    )
    lines.append(f"[傳統] 千張 {l15} | 400張↑ {big_400} | 散戶<50張 {retail_50}")

    # 資金水位法
    if "cap_big_pct" in m:
        c_big = f"{m['cap_big_pct']}%" + fmt_diff(m['cap_big_pct'], p.get('cap_big_pct'))
        c_mid = f"{m['cap_mid_pct']}%" + fmt_diff(m['cap_mid_pct'], p.get('cap_mid_pct'))
        c_retail = f"{m['cap_retail_pct']}%" + fmt_diff(
            m['cap_retail_pct'], p.get('cap_retail_pct')
        )
        lines.append(f"[資金水位] 大戶 {c_big} | 中實 {c_mid} | 散戶 {c_retail}")

    # 籌碼斷層法
    br_big = f"{m['break_big_pct']}%" + fmt_diff(
        m['break_big_pct'], p.get('break_big_pct')
    )
    br_retail = f"{m['break_retail_pct']}%" + fmt_diff(
        m['break_retail_pct'], p.get('break_retail_pct')
    )
    lines.append(f"[斷層 L{m['break_level']}↑] 大戶 {br_big} | 散戶 {br_retail}")

    return "\n".join(lines)


# ===== 主流程 =====
def main():
    stock_ids = [s.strip() for s in STOCKS if s.strip()]
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 集保戶股權分散表週報 v2 ({today}) ===")

    print("\n[1/5] 抓取集保 OpenData CSV...")
    data_date, all_stocks = fetch_tdcc()
    if not data_date:
        print("  ⚠️ 抓取失敗, 結束")
        return
    print(f"  資料日期: {data_date} | 全市場 {len(all_stocks):,} 檔")

    print(f"\n[2/5] 抓追蹤股票最新股價 (給資金水位法用)...")
    prices = get_latest_prices(stock_ids)
    stock_names = get_stock_names(stock_ids)
    for sid in stock_ids:
        print(f"  {sid} ({stock_names.get(sid, '')}): 股價 {prices.get(sid, 'N/A')}")

    print(f"\n[3/5] 計算各檔指標...")
    current_metrics = {}
    for sid in stock_ids:
        levels = all_stocks.get(sid)
        if not levels:
            print(f"  ⚠️ {sid} 集保資料中找不到")
            continue
        current_metrics[sid] = calc_metrics(levels, prices.get(sid))
        m = current_metrics[sid]
        print(
            f"  {sid}: 千張 {m['trad_level15_pct']}% / 斷層 L{m['break_level']} / "
            f"均持 {m['avg_lots']} 張"
        )

    print("\n[4/5] 比對上週快照...")
    prev_date, prev_metrics = load_latest_snapshot()
    if prev_date and prev_date == data_date:
        print(f"  ⓘ 集保資料日期未變 ({data_date}), 不做變化對比")
        prev_date, prev_metrics = None, None
    elif prev_date:
        print(f"  上週快照: {prev_date}")
    else:
        print("  尚無歷史快照, 本次為首次")

    print("\n[5/5] 組裝訊息並推播...")
    header = f"*🏛 集保戶股權分散週報* `{data_date}`"
    if prev_date:
        header += f"\n(對比 `{prev_date}`)"
    else:
        header += "\n(首次累積, 下週起會有變化對比)"
    header += "\n_資金水位: <500萬=散戶 / 500萬~5000萬=中實 / >5000萬=大戶_"

    blocks = [header]
    for sid in stock_ids:
        m = current_metrics.get(sid)
        if not m:
            blocks.append(f"\n*{sid}* — 集保無資料")
            continue
        prev_m = (prev_metrics or {}).get(sid) if prev_metrics else None
        blocks.append("\n" + build_stock_message(sid, stock_names.get(sid, sid), m, prev_m))

    send_telegram("\n".join(blocks))
    print("  ✓ Telegram 已送出")

    print("\n[+] 儲存快照並 push 到 Repo...")
    snapshot_path = save_snapshot(data_date, current_metrics)
    print(f"  存檔: {snapshot_path}")
    git_commit_push(snapshot_path)


if __name__ == "__main__":
    main()
