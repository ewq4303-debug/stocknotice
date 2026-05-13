"""
集保戶股權分散表週報機器人 v3.3
資料源策略:
  - 每週從集保 OpenData CSV 抓取最新資料
  - 直接存成 CSV 到 tdcc_history/YYYYMMDD.csv
  - 累積歷史：第 1 週只有本週，第 4 週有 4 週對比，第 12 週有 12 週對比
  - 優點: 保留原始資料，可用 Excel 直接查看

大戶/散戶三種判定法:
  1. 傳統法 (千張 / 400張以上 / 散戶<50張)
  2. 資金水位法 (依股價, 500萬/5000萬分界)
  3. 籌碼斷層法 (從 L5 起找人均比例 ≥ 前一階 3 倍的級距)
"""

import os
import csv
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

# 級距上限股數
LEVEL_UPPER_SHARES = {
    1: 999, 2: 5_000, 3: 10_000, 4: 15_000, 5: 20_000,
    6: 30_000, 7: 40_000, 8: 50_000, 9: 100_000, 10: 200_000,
    11: 400_000, 12: 600_000, 13: 800_000, 14: 1_000_000,
    15: None,
}

# 資金水位 (NTD)
RETAIL_CAP = 5_000_000
MID_CAP = 50_000_000

# 籌碼斷層
CONCENTRATION_MULTIPLIER = 3.0
CONCENTRATION_MIN_LEVEL = 5


# =========================================================
# 資料抓取 - 集保 OpenData CSV
# =========================================================
def fetch_tdcc_csv() -> tuple:
    """從集保 OpenData CSV 抓最新一週
    回傳: (date_str, csv_text)"""
    
    print("  從集保 OpenData CSV 抓取本週資料...")
    last_error = None
    
    for url in TDCC_URLS:
        try:
            print(f"  嘗試 URL: {url}")
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            print(f"    HTTP {r.status_code} | size={len(r.content):,} bytes")
            r.raise_for_status()
            
            csv_text = r.content.decode("utf-8-sig", errors="replace")
            
            if "證券代號" not in csv_text or "持股分級" not in csv_text:
                last_error = "CSV 格式不符"
                continue

            # 從 CSV 取得日期
            reader = csv.DictReader(StringIO(csv_text))
            first_row = next(reader, None)
            if not first_row:
                last_error = "CSV 無資料"
                continue
            
            data_date = (first_row.get("資料日期") or "").strip()
            if not data_date:
                last_error = "無資料日期"
                continue
            
            print(f"    ✓ 本週日期: {data_date}")
            return data_date, csv_text
                
        except requests.exceptions.RequestException as e:
            last_error = f"網路錯誤: {e}"
            continue
    
    print(f"  ⚠️ 所有 URL 都失敗: {last_error}")
    return None, None


# =========================================================
# 本地歷史 CSV 管理
# =========================================================
def parse_csv_to_levels(csv_text: str) -> dict:
    """解析 CSV 文字為分級資料
    回傳: {stock_id: {level: {people, shares, pct}}}"""
    
    reader = csv.DictReader(StringIO(csv_text))
    by_stock = defaultdict(dict)
    
    for row in reader:
        try:
            stock_id = (row.get("證券代號") or "").strip()
            level = int(row.get("持股分級") or 0)
            
            # 只保留 1-15 級 (跳過 16=差異, 17=合計)
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
        except (ValueError, TypeError):
            continue
    
    return dict(by_stock)


def load_all_local_csvs() -> dict:
    """讀取本地所有 CSV 快照
    回傳: {date_str: {stock_id: {level: {people, shares, pct}}}}"""
    
    if not HISTORY_DIR.exists():
        return {}
    
    snapshots = {}
    csv_files = sorted(HISTORY_DIR.glob("*.csv"))
    
    for csv_file in csv_files:
        try:
            date_str = csv_file.stem  # 檔名就是日期 YYYYMMDD
            with open(csv_file, encoding="utf-8-sig") as f:
                csv_text = f.read()
            levels = parse_csv_to_levels(csv_text)
            if levels:
                snapshots[date_str] = levels
        except Exception as e:
            print(f"  ⚠️ 讀取 {csv_file.name} 失敗: {e}")
    
    return snapshots


def find_historical_weeks(snapshots: dict, current_date: str, week_offsets: list) -> dict:
    """從快照中找出指定週數前的資料
    week_offsets: [-1, -2, -5, -13] 代表本週/上週/4週前/12週前
    回傳: {offset: (date_str, levels)}"""
    
    # 將日期字串轉成 datetime 並排序
    all_dates = sorted(snapshots.keys())
    if not all_dates:
        return {}
    
    date_objs = []
    for d in all_dates:
        try:
            date_objs.append(datetime.strptime(d, "%Y%m%d"))
        except:
            continue
    
    if not date_objs:
        return {}
    
    date_objs.sort()
    
    # 找出每個 offset 對應的快照
    result = {}
    for offset in week_offsets:
        idx = len(date_objs) + offset  # offset 是負數，-1=最後一個，-2=倒數第二個
        if 0 <= idx < len(date_objs):
            date_obj = date_objs[idx]
            date_str = date_obj.strftime("%Y%m%d")
            result[offset] = (date_str, snapshots[date_str])
    
    return result


def save_csv_snapshot(data_date: str, csv_text: str) -> Path:
    """儲存 CSV 快照到 tdcc_history/YYYYMMDD.csv"""
    
    HISTORY_DIR.mkdir(exist_ok=True)
    path = HISTORY_DIR / f"{data_date}.csv"
    
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(csv_text)
    
    return path


def git_commit_push(file_path: Path):
    """Git commit + push 快照檔案"""
    
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            ["git", "config", "user.email", 
             "41898282+github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "add", str(file_path)], check=True)
        
        # 檢查是否有變動
        result = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if result.returncode == 0:
            print("  ⓘ 無新變動, 不需 commit")
            return
        
        subprocess.run(
            ["git", "commit", "-m", f"📊 TDCC snapshot {file_path.stem}"], 
            check=True
        )
        subprocess.run(["git", "push"], check=True)
        print(f"  ✓ 已 commit + push: {file_path.name}")
    
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ Git 操作失敗: {e}")


# =========================================================
# 抓股價 + 股名
# =========================================================
def get_latest_prices(stock_ids: list) -> dict:
    """從 FinMind 抓股價"""
    
    if not FINMIND_TOKEN:
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
            print(f"  ⚠️ {sid} 股價失敗: {e}")
    
    return prices


def get_stock_names(stock_ids: list) -> dict:
    """從 FinMind 抓股票名稱"""
    
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
    except Exception:
        return {sid: sid for sid in stock_ids}


# =========================================================
# 衍生指標計算
# =========================================================
def classify_by_capital(levels: dict, stock_price: float) -> dict:
    """資金水位法: 依股價將級距分類"""
    
    result = {"retail_pct": 0.0, "mid_pct": 0.0, "big_pct": 0.0}
    
    if not stock_price or stock_price <= 0:
        return result
    
    for lvl in range(1, 16):
        d = levels.get(lvl, {})
        pct = d.get("pct", 0)
        upper = LEVEL_UPPER_SHARES[lvl]
        
        if upper is None:  # L15 無上限
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
    """籌碼斷層法: 從 L5 起找第一個人均比例 ≥ 3× 前一級的級距"""
    
    avg_pcts = {}
    for lvl in range(1, 16):
        d = levels.get(lvl, {})
        people = d.get("people", 0)
        pct = d.get("pct", 0)
        avg_pcts[lvl] = pct / people if people > 0 else 0
    
    # 從 L5 開始掃描 (跳過 L1→L2 結構性跳躍)
    for lvl in range(CONCENTRATION_MIN_LEVEL, 16):
        prev = avg_pcts.get(lvl - 1, 0)
        curr = avg_pcts.get(lvl, 0)
        if prev > 0 and curr >= prev * multiplier:
            return lvl
    
    return 12  # 預設 L12


def calc_metrics(levels: dict, stock_price: float = None) -> dict:
    """計算所有指標"""
    
    if not levels:
        return {}
    
    m = {}
    total_people = sum(d.get("people", 0) for d in levels.values())
    total_shares = sum(d.get("shares", 0) for d in levels.values())
    
    # 結構性指標
    m["total_holders"] = total_people
    m["avg_lots"] = round(total_shares / total_people / 1000, 2) if total_people else 0
    m["stock_price"] = stock_price or 0

    # 1. 傳統法
    m["trad_level15_pct"] = round(levels.get(15, {}).get("pct", 0), 3)
    m["trad_level15_people"] = levels.get(15, {}).get("people", 0)
    m["trad_big_400_pct"] = round(
        sum(levels.get(i, {}).get("pct", 0) for i in range(12, 16)), 3
    )
    m["trad_retail_50_pct"] = round(
        sum(levels.get(i, {}).get("pct", 0) for i in range(1, 9)), 3
    )

    # 2. 資金水位法
    if stock_price:
        cap = classify_by_capital(levels, stock_price)
        m["cap_retail_pct"] = cap["retail_pct"]
        m["cap_mid_pct"] = cap["mid_pct"]
        m["cap_big_pct"] = cap["big_pct"]

    # 3. 籌碼斷層法
    break_level = find_concentration_break(levels)
    m["break_level"] = break_level
    m["break_big_pct"] = round(
        sum(levels.get(i, {}).get("pct", 0) for i in range(break_level, 16)), 3
    )
    m["break_retail_pct"] = round(
        sum(levels.get(i, {}).get("pct", 0) for i in range(1, break_level)), 3
    )
    
    return m


# =========================================================
# Telegram 推播
# =========================================================
def send_telegram(text: str):
    """發送 Telegram 訊息"""
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID, 
                "text": text[:4000], 
                "parse_mode": "Markdown"
            },
            timeout=10,
        )
        if r.status_code == 400:  # Markdown 解析失敗，改用純文字
            r = requests.post(
                url, 
                data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}, 
                timeout=10
            )
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ Telegram 推播失敗: {e}")


def fmt_pct_diff(curr, prev, prefix="") -> str:
    """格式化百分比變化"""
    
    if prev is None or curr is None:
        return ""
    diff = curr - prev
    return f"{prefix}{diff:+.3f}"


def build_stock_message(sid, name, m, history_m: dict) -> str:
    """組裝單檔股票訊息"""
    
    display = f"{sid} {name}" if name and name != sid else sid
    p1 = history_m.get(-2, {})  # 上週
    p4 = history_m.get(-5, {})  # 4週前
    p12 = history_m.get(-13, {})  # 12週前

    lines = [f"📊 *{display}*"]

    # 結構指標
    price_str = f"股價 {m['stock_price']:.2f}" if m.get("stock_price") else "股價 N/A"
    holders_str = f"總股東 {m['total_holders']:,}"
    if "total_holders" in p1:
        holders_str += f" (1週 {m['total_holders'] - p1['total_holders']:+,})"
    avg_str = f"均持 {m['avg_lots']:.2f} 張"
    lines.append(f"{price_str} | {holders_str} | {avg_str}")

    # 傳統法 - 千張大戶帶多週趨勢
    l15 = m['trad_level15_pct']
    trend_parts = []
    if "trad_level15_pct" in p1:
        trend_parts.append(f"1週 {fmt_pct_diff(l15, p1['trad_level15_pct'])}")
    if "trad_level15_pct" in p4:
        trend_parts.append(f"4週 {fmt_pct_diff(l15, p4['trad_level15_pct'])}")
    if "trad_level15_pct" in p12:
        trend_parts.append(f"12週 {fmt_pct_diff(l15, p12['trad_level15_pct'])}")
    trend_str = f" ({' / '.join(trend_parts)})" if trend_parts else ""

    lines.append(f"[傳統法]")
    lines.append(f"  千張大戶: {l15}%{trend_str}")
    lines.append(
        f"  400張以上: {m['trad_big_400_pct']}%"
        f"{' (' + fmt_pct_diff(m['trad_big_400_pct'], p1.get('trad_big_400_pct'), '1週 ') + ')' if 'trad_big_400_pct' in p1 else ''}"
    )
    lines.append(
        f"  散戶<50張: {m['trad_retail_50_pct']}%"
        f"{' (' + fmt_pct_diff(m['trad_retail_50_pct'], p1.get('trad_retail_50_pct'), '1週 ') + ')' if 'trad_retail_50_pct' in p1 else ''}"
    )

    # 資金水位法
    if "cap_big_pct" in m:
        cap_str = (
            f"大戶 {m['cap_big_pct']}%"
            f"{' (' + fmt_pct_diff(m['cap_big_pct'], p1.get('cap_big_pct'), '1週 ') + ')' if 'cap_big_pct' in p1 else ''} | "
            f"中實 {m['cap_mid_pct']}% | 散戶 {m['cap_retail_pct']}%"
        )
        lines.append(f"[資金水位] {cap_str}")

    # 籌碼斷層法
    br_str = (
        f"L{m['break_level']}↑ 大戶 {m['break_big_pct']}%"
        f"{' (' + fmt_pct_diff(m['break_big_pct'], p1.get('break_big_pct'), '1週 ') + ')' if 'break_big_pct' in p1 else ''}"
    )
    lines.append(f"[斷層法] {br_str}")

    return "\n".join(lines)


# =========================================================
# 主流程
# =========================================================
def main():
    stock_ids = [s.strip() for s in STOCKS if s.strip()]
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 集保戶股權分散表週報 v3.3 ({today}) ===")

    # --- 1. 抓本週資料 ---
    print("\n[1/5] 抓取本週股權分散表...")
    this_week_date, csv_text = fetch_tdcc_csv()
    
    if not this_week_date or not csv_text:
        print("  ⚠️ 無法抓取本週資料, 結束")
        return
    
    print(f"  ✓ 本週日期: {this_week_date}")

    # --- 2. 讀取本地歷史 CSV ---
    print("\n[2/5] 讀取本地歷史 CSV...")
    all_snapshots = load_all_local_csvs()
    print(f"  找到 {len(all_snapshots)} 個歷史 CSV")
    
    # 加入本週資料
    this_week_levels = parse_csv_to_levels(csv_text)
    all_snapshots[this_week_date] = this_week_levels
    
    # 找出多週對比資料
    history = find_historical_weeks(
        all_snapshots, 
        this_week_date, 
        week_offsets=[-1, -2, -5, -13]  # 本週/上週/4週前/12週前
    )
    
    avail_weeks = sorted([w for w in history.keys() if w != -1])
    if avail_weeks:
        print(f"  可用的歷史週: {', '.join(str(w) for w in avail_weeks)}")
    else:
        print(f"  ⓘ 尚無歷史資料，本週是第一筆")

    # --- 3. 抓股價 + 股名 ---
    print(f"\n[3/5] 抓追蹤股票股價 + 股名...")
    prices = get_latest_prices(stock_ids)
    stock_names = get_stock_names(stock_ids)
    for sid in stock_ids:
        print(f"  {sid} ({stock_names.get(sid, '')}): 股價 {prices.get(sid, 'N/A')}")

    # --- 4. 計算各週指標並推播 ---
    print(f"\n[4/5] 計算各週指標並推播...")
    
    metrics_history = {}
    for offset, (date_str, levels_by_stock) in history.items():
        metrics_history[offset] = {}
        for sid in stock_ids:
            if sid in levels_by_stock:
                metrics_history[offset][sid] = calc_metrics(
                    levels_by_stock[sid], prices.get(sid)
                )

    current_metrics = metrics_history.get(-1, {})
    for sid in stock_ids:
        m = current_metrics.get(sid)
        if m:
            print(f"  {sid}: 千張 {m['trad_level15_pct']}% / 斷層 L{m['break_level']}")
        else:
            print(f"  ⚠️ {sid}: 資料缺失")

    # 組裝訊息
    header = f"*🏛 集保戶股權分散週報* `{this_week_date}`"
    
    compare_parts = []
    if -2 in history:
        compare_parts.append(f"1週前 `{history[-2][0]}`")
    if -5 in history:
        compare_parts.append(f"4週前 `{history[-5][0]}`")
    if -13 in history:
        compare_parts.append(f"12週前 `{history[-13][0]}`")
    
    if compare_parts:
        header += f"\n(對比: {' | '.join(compare_parts)})"
    else:
        header += "\n(本週是第一筆快照，尚無對比資料)"
    
    header += "\n_資金水位: <500萬=散戶 / 500萬~5000萬=中實 / >5000萬=大戶_"

    blocks = [header]
    history_for_msg = {
        o: metrics_history.get(o, {})
        for o in (-2, -5, -13) if o in metrics_history
    }
    
    for sid in stock_ids:
        m = current_metrics.get(sid)
        if not m:
            blocks.append(f"\n*{sid}* — 集保無資料")
            continue
        per_stock_history = {
            o: history_for_msg[o].get(sid, {}) 
            for o in history_for_msg
        }
        blocks.append("\n" + build_stock_message(
            sid, stock_names.get(sid, sid), m, per_stock_history
        ))

    send_telegram("\n".join(blocks))
    print("  ✓ Telegram 已送出")

    # --- 5. 儲存本週 CSV 快照並 push ---
    print("\n[5/5] 儲存本週 CSV 快照...")
    snapshot_path = save_csv_snapshot(this_week_date, csv_text)
    print(f"  存檔: {snapshot_path}")
    git_commit_push(snapshot_path)
    
    print("\n✅ 完成！下週累積後會有更多對比資料")


if __name__ == "__main__":
    main()
