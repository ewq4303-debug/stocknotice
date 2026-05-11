"""
集保戶股權分散表週報機器人
每週六下午抓集保 OpenData CSV, 比對上週快照, 推播追蹤股票大戶/散戶變化
資料來源: https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5
歷史累積方式: 每週快照存到 tdcc_history/{date}.json 並 commit 回 Repo
"""

import os
import csv
import json
import subprocess
import requests
from collections import defaultdict
from datetime import datetime
from io import StringIO
from pathlib import Path

# ===== 設定 =====
STOCKS = os.getenv("STOCKS", "2330,2454,2317").split(",")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TDCC_URL = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"
HISTORY_DIR = Path("tdcc_history")


# ===== 抓資料 =====
def fetch_tdcc() -> tuple:
    """抓集保 CSV 並依股票代號歸類.
    回傳 (data_date, {stock_id: {level: {people, shares, pct}}})"""
    r = requests.get(TDCC_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()

    reader = csv.DictReader(StringIO(r.text))
    by_stock = defaultdict(dict)
    data_date = None

    for row in reader:
        try:
            stock_id = (row.get("證券代號") or "").strip()
            level = int(row.get("持股分級") or 0)
            # 等級 1-15 才是真實資料 (16=不歸戶, 17=合計)
            if not stock_id or level < 1 or level > 15:
                continue

            by_stock[stock_id][level] = {
                "people": int(row.get("人數") or 0),
                "shares": int(row.get("股數") or 0),
                "pct": float(
                    row.get("占集保庫存數比例%")
                    or row.get("佔集保庫存數比例%")
                    or 0
                ),
            }
            if not data_date:
                data_date = (row.get("資料日期") or "").strip()
        except (ValueError, TypeError):
            continue

    return data_date, dict(by_stock)


# ===== 衍生指標 =====
def calc_metrics(levels: dict) -> dict:
    """從持股分級資料算出大戶/散戶占比"""
    if not levels:
        return {}
    return {
        # 千張大戶 (level 15: 1,000,001 股以上)
        "level15_pct": round(levels.get(15, {}).get("pct", 0), 3),
        "level15_people": levels.get(15, {}).get("people", 0),
        # 400 張以上 (level 12-15: 40 萬股以上)
        "big_holder_pct": round(
            sum(levels.get(i, {}).get("pct", 0) for i in range(12, 16)), 3
        ),
        # 散戶 (level 1-8: 5 萬股以下)
        "retail_pct": round(
            sum(levels.get(i, {}).get("pct", 0) for i in range(1, 9)), 3
        ),
        # 總股東人數
        "total_holders": sum(
            levels.get(i, {}).get("people", 0) for i in range(1, 16)
        ),
    }


# ===== 快照管理 =====
def load_latest_snapshot():
    """讀取 tdcc_history/ 裡最新的快照 (排除本次要存的)"""
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
    """commit + push 新快照. 失敗也不中斷流程"""
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "add", str(file_path)], check=True)
        # 若沒變動就 skip
        result = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if result.returncode == 0:
            print("  ⓘ 無新變動, 不需 commit")
            return
        subprocess.run(
            ["git", "commit", "-m", f"TDCC snapshot {file_path.stem}"],
            check=True,
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


# ===== 主流程 =====
def main():
    stock_ids = [s.strip() for s in STOCKS if s.strip()]
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 集保戶股權分散表週報 ({today}) ===")

    print("\n[1/4] 抓取集保 OpenData CSV...")
    data_date, all_stocks = fetch_tdcc()
    if not data_date:
        print("  ⚠️ 抓取失敗或資料格式有變, 結束")
        return
    print(f"  集保資料日期: {data_date}")
    print(f"  全市場股票數: {len(all_stocks)}")

    print(f"\n[2/4] 計算 {len(stock_ids)} 檔追蹤股票指標...")
    current_metrics = {}
    for sid in stock_ids:
        levels = all_stocks.get(sid)
        if not levels:
            print(f"  ⚠️ {sid} 在集保資料中找不到")
            continue
        current_metrics[sid] = calc_metrics(levels)
        m = current_metrics[sid]
        print(f"  {sid}: 千張大戶 {m['level15_pct']}% / 400張以上 {m['big_holder_pct']}% / 散戶 {m['retail_pct']}%")

    print("\n[3/4] 載入上週快照比對...")
    prev_date, prev_metrics = load_latest_snapshot()
    if prev_date and prev_date == data_date:
        print(f"  ⓘ 集保資料日期未變 ({data_date}), 上週快照才是本週資料")
        # 視為「沒有上週」, 不做差異
        prev_date, prev_metrics = None, None
    elif prev_date:
        print(f"  上週快照: {prev_date}")
    else:
        print("  尚無歷史快照, 本次為首次")

    print("\n[4/4] 組裝訊息並推播...")
    lines = [f"*🏛 集保戶股權分散週報* `{data_date}`"]
    if prev_date:
        lines.append(f"(對比 `{prev_date}`)")
    else:
        lines.append("(首次累積, 下週起會有變化對比)")

    for sid in stock_ids:
        m = current_metrics.get(sid)
        if not m:
            lines.append(f"\n*{sid}* — 集保無資料")
            continue

        lines.append(f"\n*{sid}*")
        prev_m = (prev_metrics or {}).get(sid) if prev_metrics else None

        if prev_m:
            l15_diff = m["level15_pct"] - prev_m["level15_pct"]
            ppl_diff = m["level15_people"] - prev_m["level15_people"]
            big_diff = m["big_holder_pct"] - prev_m["big_holder_pct"]
            retail_diff = m["retail_pct"] - prev_m["retail_pct"]

            lines.append(f"千張大戶占比: {m['level15_pct']}% ({l15_diff:+.3f})")
            lines.append(f"千張大戶人數: {m['level15_people']:,} ({ppl_diff:+,})")
            lines.append(f"400 張以上占比: {m['big_holder_pct']}% ({big_diff:+.3f})")
            lines.append(f"散戶 (<50 張) 占比: {m['retail_pct']}% ({retail_diff:+.3f})")
        else:
            lines.append(f"千張大戶占比: {m['level15_pct']}%")
            lines.append(f"千張大戶人數: {m['level15_people']:,}")
            lines.append(f"400 張以上占比: {m['big_holder_pct']}%")
            lines.append(f"散戶 (<50 張) 占比: {m['retail_pct']}%")

    send_telegram("\n".join(lines))
    print("  ✓ Telegram 已送出")

    print("\n[+] 儲存快照並 push 到 Repo...")
    snapshot_path = save_snapshot(data_date, current_metrics)
    print(f"  存檔: {snapshot_path}")
    git_commit_push(snapshot_path)


if __name__ == "__main__":
    main()
