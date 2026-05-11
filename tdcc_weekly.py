"""
集保戶股權分散表週報機器人 v1.1
v1.1 修正:
  - URL 改為 opendata.tdcc.com.tw (官方新位置)
  - 用 utf-8-sig 處理 CSV BOM (原本 r.text 解析失敗)
  - 兩個 URL 都試, fallback 機制
  - 加強除錯訊息, 失敗時印出實際內容協助診斷
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

# 集保 OpenData URLs (按優先序試)
TDCC_URLS = [
    "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
    "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5",
]
HISTORY_DIR = Path("tdcc_history")


# ===== 抓資料 =====
def fetch_tdcc() -> tuple:
    """抓集保 CSV. 嘗試多個 URL, 處理 BOM, 失敗時印出診斷資訊"""
    last_error = None

    for url in TDCC_URLS:
        try:
            print(f"  嘗試 URL: {url}")
            r = requests.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30
            )
            print(f"    HTTP {r.status_code} | size={len(r.content):,} bytes")
            r.raise_for_status()

            # 用 utf-8-sig 自動移除 BOM, errors='replace' 處理偶發異常字元
            text = r.content.decode("utf-8-sig", errors="replace")

            # 驗證確實是預期的 CSV
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
                            or row.get("佔集保庫存數比例%")
                            or 0
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


# ===== 衍生指標 =====
def calc_metrics(levels: dict) -> dict:
    if not levels:
        return {}
    return {
        "level15_pct": round(levels.get(15, {}).get("pct", 0), 3),
        "level15_people": levels.get(15, {}).get("people", 0),
        "big_holder_pct": round(
            sum(levels.get(i, {}).get("pct", 0) for i in range(12, 16)), 3
        ),
        "retail_pct": round(
            sum(levels.get(i, {}).get("pct", 0) for i in range(1, 9)), 3
        ),
        "total_holders": sum(
            levels.get(i, {}).get("people", 0) for i in range(1, 16)
        ),
    }


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


# ===== 主流程 =====
def main():
    stock_ids = [s.strip() for s in STOCKS if s.strip()]
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 集保戶股權分散表週報 ({today}) ===")

    print("\n[1/4] 抓取集保 OpenData CSV...")
    data_date, all_stocks = fetch_tdcc()
    if not data_date:
        print("  ⚠️ 抓取失敗, 結束")
        return
    print(f"  資料日期: {data_date}")
    print(f"  全市場股票數: {len(all_stocks):,}")

    print(f"\n[2/4] 計算 {len(stock_ids)} 檔追蹤股票指標...")
    current_metrics = {}
    for sid in stock_ids:
        levels = all_stocks.get(sid)
        if not levels:
            print(f"  ⚠️ {sid} 在集保資料中找不到")
            continue
        current_metrics[sid] = calc_metrics(levels)
        m = current_metrics[sid]
        print(
            f"  {sid}: 千張大戶 {m['level15_pct']}% / "
            f"400張以上 {m['big_holder_pct']}% / 散戶 {m['retail_pct']}%"
        )

    print("\n[3/4] 載入上週快照比對...")
    prev_date, prev_metrics = load_latest_snapshot()
    if prev_date and prev_date == data_date:
        print(f"  ⓘ 集保資料日期未變 ({data_date}), 視為無上週比較基準")
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
