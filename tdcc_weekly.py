"""
集保戶股權分散表週報機器人 v3.0
資料源策略:
  - 主要: FinLab (有 2016 起的歷史資料, 可看 1/4/12 週趨勢)
  - 備援: 集保 OpenData CSV + 本地快照 (FinLab 失效時自動退回)

大戶/散戶三種判定法:
  1. 傳統法 (千張 / 400張以上 / 散戶<50張)
  2. 資金水位法 (依股價, 500萬/5000萬分界)
  3. 籌碼斷層法 (從 L5 起找人均比例 ≥ 前一階 3 倍的級距)

結構性指標:
  - 總股東人數 (含 1 週變化)
  - 平均每戶持股張數
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
FINLAB_API_KEY = os.getenv("FINLAB_API_KEY", "")
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TDCC_URLS = [
    "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
    "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5",
]
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
HISTORY_DIR = Path("tdcc_history")

# 級距上限股數 (level 15 無上限)
LEVEL_UPPER_SHARES = {
    1: 999, 2: 5_000, 3: 10_000, 4: 15_000, 5: 20_000,
    6: 30_000, 7: 40_000, 8: 50_000, 9: 100_000, 10: 200_000,
    11: 400_000, 12: 600_000, 13: 800_000, 14: 1_000_000,
    15: None,
}

# 資金水位 (NTD)
RETAIL_CAP = 5_000_000     # 500 萬
MID_CAP = 50_000_000       # 5000 萬

# 籌碼斷層倍率與最低起算級距
CONCENTRATION_MULTIPLIER = 3.0
CONCENTRATION_MIN_LEVEL = 5  # 從 L5 開始, 跳過 L1→L2 結構性跳躍


# =========================================================
# 資料抓取 - FinLab (主要)
# =========================================================
def fetch_finlab_multiweek(stock_ids: list, week_offsets: list) -> dict:
    """從 FinLab 抓多週 TDCC 資料.
    week_offsets: list of int, e.g. [-1, -2, -5, -13] 代表本週/上週/4週前/12週前
    回傳: {offset: (date_str, {stock_id: {level: {people, shares, pct}}})}
    失敗回傳 {}"""
    import finlab
    from finlab import data

    finlab.login(api_token=FINLAB_API_KEY)

    print("  抓取 FinLab 三個資料集...")
    pct_df = data.get('集保戶股權分散表:佔集保庫存數比例(%)')
    people_df = data.get('集保戶股權分散表:人數')
    shares_df = data.get('集保戶股權分散表:持有股數')
    print(f"    資料 shape: {pct_df.shape} (rows × cols)")
    print(f"    最新一週: {pct_df.index[-1].strftime('%Y-%m-%d')}")

    all_sids = set(pct_df.columns.get_level_values('stock_id'))
    result = {}

    for offset in week_offsets:
        # 超出歷史範圍就跳過
        if abs(offset) > len(pct_df):
            print(f"    ⓘ offset={offset} 超出歷史範圍 (現有 {len(pct_df)} 週), 跳過")
            continue

        target_date = pct_df.index[offset]
        date_str = target_date.strftime("%Y%m%d")
        by_stock = {}

        for sid in stock_ids:
            if sid not in all_sids:
                continue
            try:
                pct_row = pct_df.xs(sid, axis=1, level='stock_id').loc[target_date]
                ppl_row = people_df.xs(sid, axis=1, level='stock_id').loc[target_date]
                shr_row = shares_df.xs(sid, axis=1, level='stock_id').loc[target_date]
            except KeyError:
                continue

            levels = {}
            for col in pct_row.index:
                try:
                    lvl = int(col)
                except (ValueError, TypeError):
                    continue
                if lvl < 1 or lvl > 15:
                    continue
                levels[lvl] = {
                    "pct": float(pct_row.get(col) or 0),
                    "people": int(ppl_row.get(col) or 0),
                    "shares": int(shr_row.get(col) or 0),
                }
            if levels:
                by_stock[sid] = levels

        result[offset] = (date_str, by_stock)

    return result


# =========================================================
# 資料抓取 - TDCC OpenData CSV (備援)
# =========================================================
def fetch_tdcc_csv() -> tuple:
    """備援: 從集保 OpenData CSV 抓最新一週"""
    last_error = None
    for url in TDCC_URLS:
        try:
            print(f"  嘗試 URL: {url}")
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            print(f"    HTTP {r.status_code} | size={len(r.content):,} bytes")
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", errors="replace")
            if "證券代號" not in text or "持股分級" not in text:
                last_error = "content mismatch"
                continue

            reader = csv.DictReader(StringIO(text))
            by_stock = defaultdict(dict)
            data_date = None
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
                except (ValueError, TypeError):
                    continue
            return data_date, dict(by_stock)
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            continue
    print(f"  ⚠️ TDCC CSV 也失敗: {last_error}")
    return None, {}


# =========================================================
# 抓股價 + 股名 (FinMind)
# =========================================================
def get_latest_prices(stock_ids: list) -> dict:
    if not FINMIND_TOKEN:
        return {}
    start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    prices = {}
    for sid in stock_ids:
        try:
            r = requests.get(
                FINMIND_URL,
                params={
                    "dataset": "TaiwanStockPrice", "data_id": sid,
                    "start_date": start, "token": FINMIND_TOKEN,
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
    if not FINMIND_TOKEN:
        return {sid: sid for sid in stock_ids}
    try:
        r = requests.get(
            FINMIND_URL,
            params={
                "dataset": "TaiwanStockInfo", "start_date": "2024-01-01",
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
    result = {"retail_pct": 0.0, "mid_pct": 0.0, "big_pct": 0.0}
    if not stock_price or stock_price <= 0:
        return result
    for lvl in range(1, 16):
        d = levels.get(lvl, {})
        pct = d.get("pct", 0)
        upper = LEVEL_UPPER_SHARES[lvl]
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
    """從 CONCENTRATION_MIN_LEVEL 起找第一個人均比例 ≥ multiplier 倍 前一級的級距"""
    avg_pcts = {}
    for lvl in range(1, 16):
        d = levels.get(lvl, {})
        people = d.get("people", 0)
        pct = d.get("pct", 0)
        avg_pcts[lvl] = pct / people if people > 0 else 0
    for lvl in range(CONCENTRATION_MIN_LEVEL, 16):
        prev = avg_pcts.get(lvl - 1, 0)
        curr = avg_pcts.get(lvl, 0)
        if prev > 0 and curr >= prev * multiplier:
            return lvl
    return 12


def calc_metrics(levels: dict, stock_price: float = None) -> dict:
    if not levels:
        return {}
    m = {}
    total_people = sum(d.get("people", 0) for d in levels.values())
    total_shares = sum(d.get("shares", 0) for d in levels.values())
    m["total_holders"] = total_people
    m["avg_lots"] = round(total_shares / total_people / 1000, 2) if total_people else 0
    m["stock_price"] = stock_price or 0

    # 傳統法
    m["trad_level15_pct"] = round(levels.get(15, {}).get("pct", 0), 3)
    m["trad_level15_people"] = levels.get(15, {}).get("people", 0)
    m["trad_big_400_pct"] = round(
        sum(levels.get(i, {}).get("pct", 0) for i in range(12, 16)), 3
    )
    m["trad_retail_50_pct"] = round(
        sum(levels.get(i, {}).get("pct", 0) for i in range(1, 9)), 3
    )

    # 資金水位法
    if stock_price:
        cap = classify_by_capital(levels, stock_price)
        m["cap_retail_pct"] = cap["retail_pct"]
        m["cap_mid_pct"] = cap["mid_pct"]
        m["cap_big_pct"] = cap["big_pct"]

    # 籌碼斷層法
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
# 快照 (本地備份)
# =========================================================
def load_latest_snapshot():
    if not HISTORY_DIR.exists():
        return None, None
    files = sorted(HISTORY_DIR.glob("*.json"))
    if not files:
        return None, None
    with open(files[-1], encoding="utf-8") as f:
        data = json.load(f)
    return data.get("date"), data.get("levels", {})


def save_snapshot(data_date: str, levels_by_stock: dict) -> Path:
    """快照存的是原始 levels (而非計算後的 metrics), 未來可重新計算任何指標"""
    HISTORY_DIR.mkdir(exist_ok=True)
    path = HISTORY_DIR / f"{data_date}.json"
    out = {"date": data_date, "levels": levels_by_stock}
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


# =========================================================
# Telegram
# =========================================================
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


def fmt_pct_diff(curr, prev, prefix="") -> str:
    """格式化變化幅度: '+0.234' 或 '-1.234'"""
    if prev is None or curr is None:
        return ""
    diff = curr - prev
    return f"{prefix}{diff:+.3f}"


def build_stock_message(sid, name, m, history_m: dict) -> str:
    """組裝單檔股票訊息.
    history_m: {offset: metrics_dict} 例如 {-2: prev_week, -5: 4_weeks_ago, -13: 12_weeks_ago}"""
    display = f"{sid} {name}" if name and name != sid else sid
    p1 = history_m.get(-2, {})  # 上週
    p4 = history_m.get(-5, {})  # 4 週前
    p12 = history_m.get(-13, {})  # 12 週前

    lines = [f"📊 *{display}*"]

    # 結構
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
    print(f"=== 集保戶股權分散表週報 v3 ({today}) ===")

    # --- 1. 抓資料 (FinLab 為主, TDCC CSV 備援) ---
    print("\n[1/5] 抓取股權分散表...")
    levels_history = {}  # {offset: (date_str, levels_by_stock)}

    if FINLAB_API_KEY:
        try:
            print("  使用 FinLab (主要)")
            levels_history = fetch_finlab_multiweek(
                stock_ids, week_offsets=[-1, -2, -5, -13]
            )
            print(f"  ✓ 抓到 {len(levels_history)} 週資料")
        except Exception as e:
            print(f"  ⚠️ FinLab 失敗: {e}")
            print("  退回 TDCC CSV + 本地快照")
    else:
        print("  ⓘ 未設定 FINLAB_API_KEY, 使用 TDCC CSV + 本地快照")

    # FinLab 失敗或沒設定 → 退回 TDCC CSV
    if not levels_history:
        date_str, levels = fetch_tdcc_csv()
        if not date_str:
            print("  ⚠️ TDCC CSV 也失敗, 結束")
            return
        levels_history[-1] = (date_str, levels)

        # 從本地快照載入上週
        prev_date, prev_levels = load_latest_snapshot()
        if prev_date and prev_date != date_str:
            levels_history[-2] = (prev_date, prev_levels)

    this_week_date, this_week_levels = levels_history.get(-1, (None, {}))
    if not this_week_date:
        print("  ⚠️ 本週資料缺失, 結束")
        return
    print(f"  本週日期: {this_week_date}")

    # --- 2. 抓股價 + 股名 ---
    print(f"\n[2/5] 抓追蹤股票股價 + 股名...")
    prices = get_latest_prices(stock_ids)
    stock_names = get_stock_names(stock_ids)
    for sid in stock_ids:
        print(f"  {sid} ({stock_names.get(sid, '')}): 股價 {prices.get(sid, 'N/A')}")

    # --- 3. 計算各週 metrics ---
    print(f"\n[3/5] 計算各週指標...")
    metrics_history = {}  # {offset: {stock_id: metrics}}
    for offset, (date_str, levels_by_stock) in levels_history.items():
        metrics_history[offset] = {}
        for sid in stock_ids:
            if sid in levels_by_stock:
                metrics_history[offset][sid] = calc_metrics(
                    levels_by_stock[sid], prices.get(sid)
                )

    # 本週的 metrics
    current_metrics = metrics_history.get(-1, {})
    for sid in stock_ids:
        m = current_metrics.get(sid)
        if m:
            print(f"  {sid}: 千張 {m['trad_level15_pct']}% / 斷層 L{m['break_level']}")
        else:
            print(f"  ⚠️ {sid}: 資料缺失")

    # --- 4. 組裝訊息 ---
    print("\n[4/5] 組裝訊息並推播...")
    header = f"*🏛 集保戶股權分散週報* `{this_week_date}`"
    avail_offsets = [o for o in (-2, -5, -13) if o in levels_history]
    if avail_offsets:
        compares = []
        for o in avail_offsets:
            d = levels_history[o][0]
            compares.append(f"{abs(o + 1) if o == -2 else abs(o + 1)}週前 `{d}`" if False else f"`{d}`")
        # 簡化顯示
        labels = {-2: "1週前", -5: "4週前", -13: "12週前"}
        compare_strs = [f"{labels[o]} `{levels_history[o][0]}`" for o in avail_offsets]
        header += f"\n(對比: {' | '.join(compare_strs)})"
    else:
        header += "\n(首次累積)"
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
        per_stock_history = {o: history_for_msg[o].get(sid, {}) for o in history_for_msg}
        blocks.append("\n" + build_stock_message(
            sid, stock_names.get(sid, sid), m, per_stock_history
        ))

    send_telegram("\n".join(blocks))
    print("  ✓ Telegram 已送出")

    # --- 5. 儲存本週快照 (備份) ---
    print("\n[5/5] 儲存本週快照...")
    snapshot_path = save_snapshot(this_week_date, this_week_levels)
    print(f"  存檔: {snapshot_path}")
    git_commit_push(snapshot_path)


if __name__ == "__main__":
    main()
