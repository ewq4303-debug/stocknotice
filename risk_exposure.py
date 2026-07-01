"""Daily futures/portfolio risk exposure report.

Reads positions.json and subbasket.json, fetches recent prices, and writes docs/risk.json.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf

TW_TZ = timezone(timedelta(hours=8))
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
OUTPUT_DIR = Path("docs")
POSITIONS_FILE = Path(os.getenv("POSITIONS_FILE", "positions.json"))
SUBBASKET_FILE = Path(os.getenv("SUBBASKET_FILE", "subbasket.json"))
RISK_FILE = OUTPUT_DIR / "risk.json"

MULTIPLIERS = {
    "standard": 2000,
    "mini": 100,
    "micro_index": 10,
    "mini_index": 50,
    "index": 200,
}
INDEX_KINDS = {"micro_index", "mini_index", "index"}
SHOCKS = [-0.10, -0.20]


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def fetch_finmind_prices(symbol: str, start_date: str) -> pd.Series:
    if not FINMIND_TOKEN:
        return pd.Series(dtype="float64")
    params = {"dataset": "TaiwanStockPrice", "data_id": symbol, "start_date": start_date, "token": FINMIND_TOKEN}
    resp = requests.get(FINMIND_URL, params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    if not rows:
        return pd.Series(dtype="float64")
    df = pd.DataFrame(rows)
    close_col = "close" if "close" in df.columns else "Close"
    return pd.Series(pd.to_numeric(df[close_col], errors="coerce").values, index=pd.to_datetime(df["date"])).dropna().sort_index()


def fetch_yfinance_prices(symbol: str, days: int) -> pd.Series:
    end = datetime.now(UTC).replace(tzinfo=None)
    start = end - timedelta(days=days)
    tickers = ["^TWII"] if symbol.upper() == "TAIEX" else [f"{symbol}.TW", f"{symbol}.TWO"]
    for ticker in tickers:
        try:
            df = yf.Ticker(ticker).history(start=start, end=end)
            if not df.empty and "Close" in df:
                return pd.Series(df["Close"].values, index=pd.to_datetime(df.index).tz_localize(None)).dropna().sort_index()
        except Exception:
            continue
    return pd.Series(dtype="float64")


def fetch_price_history(symbol: str, days: int = 130) -> pd.Series:
    if symbol.upper() == "TXF":
        symbol = "TAIEX"
    start_date = (now_tw() - timedelta(days=days + 20)).strftime("%Y-%m-%d")
    series = fetch_finmind_prices(symbol, start_date) if symbol != "TAIEX" else pd.Series(dtype="float64")
    if series.empty:
        series = fetch_yfinance_prices(symbol, days + 20)
    return series.tail(days)


def latest_price(symbol: str, histories: dict[str, pd.Series]) -> float | None:
    series = histories.get(symbol)
    if series is None or series.empty:
        return None
    return float(series.iloc[-1])


def nlr_verdict(nlr_gross: float) -> str:
    if nlr_gross < 2.0:
        return "OK"
    if nlr_gross <= 3.5:
        return "WATCH"
    return "OVER_LEVERED"


def margin_verdict(futures_equity_after: float, initial_margin: float, maintenance_margin: float) -> str:
    if futures_equity_after >= initial_margin:
        return "SAFE"
    if futures_equity_after >= maintenance_margin:
        return "WATCH"
    if futures_equity_after > 0:
        return "MARGIN_CALL"
    return "LIQUIDATION"


def calc_beta(symbol: str, basket_returns: pd.Series, histories: dict[str, pd.Series], is_member: bool) -> tuple[float, str]:
    if is_member:
        return 1.0, "member_default"
    series = histories.get(symbol)
    if series is None or series.empty or basket_returns.empty:
        return 0.0, "missing"
    returns = series.pct_change().dropna()
    joined = pd.concat([returns.rename("asset"), basket_returns.rename("basket")], axis=1, join="inner").dropna().tail(60)
    if len(joined) < 20 or math.isclose(float(joined["basket"].var()), 0.0):
        return 0.0, "missing"
    beta = float(joined["asset"].cov(joined["basket"]) / joined["basket"].var())
    return round(beta, 4), "rolling_60d"


def build_basket_returns(members: list[str], weighting: str, histories: dict[str, pd.Series], notionals: dict[str, float]) -> pd.Series:
    returns = []
    weights = []
    for member in members:
        series = histories.get(member)
        if series is None or series.empty:
            continue
        returns.append(series.pct_change().rename(member))
        weights.append(abs(notionals.get(member, 0.0)) if weighting == "by_notional" else 1.0)
    if not returns:
        return pd.Series(dtype="float64")
    df = pd.concat(returns, axis=1, join="inner").dropna()
    if df.empty:
        return pd.Series(dtype="float64")
    weights_s = pd.Series(weights, index=df.columns, dtype="float64")
    if weights_s.sum() <= 0:
        weights_s[:] = 1.0
    return df.mul(weights_s / weights_s.sum(), axis=1).sum(axis=1)


def validate_multipliers(positions: dict[str, Any]) -> None:
    checks = positions.get("multiplier_checks", [])
    for check in checks:
        kind = check["kind"]
        expected = MULTIPLIERS[kind]
        inferred = abs(float(check["realized_pnl"]) / ((float(check["exit_price"]) - float(check["entry_price"])) * float(check.get("contracts", 1))))
        if not math.isclose(inferred, expected, rel_tol=0.02, abs_tol=1):
            raise ValueError(f"Multiplier check failed for {kind}: inferred {inferred:g}, expected {expected:g}")


def calculate_risk(positions: dict[str, Any], subbasket: dict[str, Any]) -> dict[str, Any]:
    validate_multipliers(positions)
    members = [str(x) for x in subbasket.get("members", [])]
    futures = positions.get("stock_futures", [])
    spot = positions.get("spot", [])
    symbols = {p["symbol"] for p in spot} | {p["symbol"] for p in futures if p.get("kind") not in INDEX_KINDS} | set(members)
    if any(p.get("kind") in INDEX_KINDS for p in futures):
        symbols.add("TAIEX")
    histories = {s: fetch_price_history(s) for s in sorted(symbols)}

    missing_prices: list[str] = []
    rows: list[dict[str, Any]] = []
    for p in spot:
        symbol = str(p["symbol"])
        price = latest_price(symbol, histories)
        if price is None:
            missing_prices.append(symbol)
            continue
        rows.append({"symbol": symbol, "name": p.get("name", symbol), "asset_type": "spot", "signed_notional": float(p.get("shares", 0)) * price, "price": price})

    for p in futures:
        kind = p.get("kind")
        if kind not in MULTIPLIERS:
            raise ValueError(f"Unknown futures kind: {kind}")
        price_symbol = "TAIEX" if kind in INDEX_KINDS else str(p["symbol"])
        price = latest_price(price_symbol, histories)
        if price is None:
            missing_prices.append(price_symbol)
            continue
        rows.append({"symbol": str(p["symbol"]), "name": p.get("name", p["symbol"]), "asset_type": "futures", "kind": kind, "signed_notional": float(p.get("contracts", 0)) * MULTIPLIERS[kind] * price, "price": price})

    notionals = {r["symbol"]: r["signed_notional"] for r in rows}
    basket_returns = build_basket_returns(members, subbasket.get("weighting", "equal"), histories, notionals)
    missing_beta: list[str] = []
    for r in rows:
        beta_symbol = "TAIEX" if r.get("kind") in INDEX_KINDS else r["symbol"]
        beta, source = calc_beta(beta_symbol, basket_returns, histories, r["symbol"] in members, )
        r["beta"] = beta
        r["beta_source"] = source
        if source == "missing":
            missing_beta.append(r["symbol"])

    acct = positions.get("account_equity", {})
    securities = float(acct.get("securities_nav") or 0)
    futures_equity = float(acct.get("futures_equity") or 0)
    equity = securities + futures_equity
    equity_partial = securities <= 0
    if equity <= 0:
        raise ValueError("Combined equity must be positive")
    gross = sum(abs(r["signed_notional"]) for r in rows)
    net = sum(r["signed_notional"] for r in rows)
    init_margin = float(acct.get("futures_initial_margin") or 0)
    maint_margin = float(acct.get("futures_maintenance_margin") or 0)

    stress = []
    for shock in SHOCKS:
        dpnl_total = sum(r["signed_notional"] * shock * r["beta"] for r in rows)
        dpnl_futures = sum(r["signed_notional"] * shock * r["beta"] for r in rows if r["asset_type"] == "futures")
        stress.append({"shock": shock, "dpnl_total": round(dpnl_total), "dpnl_pct_equity": round(dpnl_total / equity, 4), "futures_equity_after": round(futures_equity + dpnl_futures), "verdict": margin_verdict(futures_equity + dpnl_futures, init_margin, maint_margin)})

    for r in rows:
        r["dpnl_at_-20pct"] = round(r["signed_notional"] * -0.20 * r["beta"])
        r["signed_notional"] = round(r["signed_notional"])
    top = sorted(rows, key=lambda x: abs(x["dpnl_at_-20pct"]), reverse=True)[:10]

    return {
        "as_of": positions.get("as_of") or now_tw().strftime("%Y-%m-%d"),
        "equity": {"combined": round(equity), "securities": round(securities), "futures": round(futures_equity), "futures_maint_margin": round(maint_margin), "futures_init_margin": round(init_margin), "equity_partial": equity_partial},
        "leverage": {"gross_notional": round(gross), "net_notional": round(net), "nlr_gross": round(gross / equity, 4), "nlr_net": round(net / equity, 4), "verdict": nlr_verdict(gross / equity)},
        "stress": stress,
        "top_contributors": top,
        "data_quality": {"missing_prices": sorted(set(missing_prices)), "missing_beta": sorted(set(missing_beta)), "basket_members_loaded": sorted([m for m in members if m in histories and not histories[m].empty])},
    }


def main() -> None:
    positions = load_json(POSITIONS_FILE)
    subbasket = load_json(SUBBASKET_FILE)
    OUTPUT_DIR.mkdir(exist_ok=True)
    risk = calculate_risk(positions, subbasket)
    RISK_FILE.write_text(json.dumps(risk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {RISK_FILE}")

    if os.getenv("RISK_AUTO_COMMIT") == "1":
        subprocess.run(["git", "add", str(RISK_FILE)], check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", f"Update risk exposure {now_tw().strftime('%Y-%m-%d')}"], check=True)
            subprocess.run(["git", "push"], check=True)


if __name__ == "__main__":
    main()
