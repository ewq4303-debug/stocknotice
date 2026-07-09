# -*- coding: utf-8 -*-
"""評等系統 v2 測試（TV-A ~ TV-K）— 純 assert,合成 fixture,不打外部 API。
執行:python tests/test_rating_v2.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stock_analyzer_v4 import RATING_LEVELS, calculate_fundamental_score, calculate_stock_rating


def _detail(result, label):
    for d in result["details"]:
        if d["label"] == label: return d
    raise AssertionError(f"找不到明細項: {label}")


def _rev(yoys): return [{"date": f"2026-{6-i:02d}", "rev": "10.00億", "yoy": y} for i, y in enumerate(yoys)]
def _eps(yoys): return [{"date": f"2026Q{i+1}", "eps": 1.0, "yoy": y} for i, y in enumerate(yoys)]
def _gm(gms):   return [{"date": f"2026Q{i+1}", "gm": g} for i, g in enumerate(gms)]

FUND_TVA = {  # 全面向好
    "rev": _rev([42.0, 33.0, 30.0, 8.0, 6.0, 6.0]),   # F1 +1；F2 近3月均35 > 前3月均6.7 → +1
    "eps": _eps([120.0]),                              # F3 +1
    "gm": _gm([38.2, 34.0, 34.5, 35.0, 33.0]),         # F4 38.2 > 34.5+0.5 → +1
    "trailing_pe": 18.0, "forward_pe": 12.0,           # F5 12 < 18*0.95 → +1
}
FUND_TVB = {  # 惡化 + 缺值 renormalize
    "rev": _rev([-18.0, -10.0, -8.0, 10.0, 12.0, 14.0]),  # F1 -1；F2 -12 vs +12 減速且轉負 → -1
    "eps": _eps([-40.0]),                                  # F3 -1
    "gm": _gm([30.0, 31.0]),                               # F4 只有 2 筆 → N/A
    "trailing_pe": None, "forward_pe": 15.0,               # F5 → N/A
}


def test_tv_a():
    r = calculate_fundamental_score(FUND_TVA)
    assert r["score"] == 6.0, r
    assert r["max_pts"] == 6.0, r
    assert r["n_available"] == 5, r
    assert r["tilt"] == 1, r


def test_tv_b():
    r = calculate_fundamental_score(FUND_TVB)
    assert r["score"] == -4.0, r
    assert r["max_pts"] == 4.0, r
    assert r["n_available"] == 3, r
    assert r["tilt"] == -1, r
    assert _detail(r, "毛利率趨勢(vs前3季均)")["na"] is True
    assert _detail(r, "Forward PE vs Trailing")["na"] is True


def test_tv_c():
    fund = {
        "rev": _rev([-4.0, 10.0, 12.0, -20.0, -10.0, -5.0]),  # F1 死區 0；F2 加速 → +1
        "eps": _eps([8.0]),                                    # F3 +1
        "gm": _gm([35.2, 34.5, 35.5, 35.0, 34.0]),             # F4 偏離 +0.2pp → 死區 0
        "trailing_pe": 20.0, "forward_pe": 20.0,               # F5 區間內 → 0
    }
    r = calculate_fundamental_score(fund)
    assert r["score"] == 2.5, r
    assert r["max_pts"] == 6.0, r
    assert r["tilt"] == 0, r  # 2.5/6 ≈ 0.42 < 0.6


def test_tv_d():
    r = calculate_fundamental_score({})
    assert r["n_available"] == 0, r
    assert r["tilt"] == 0, r
    assert r["max_pts"] == 0, r
    assert r["score"] == 0, r
    assert len(r["details"]) == 5 and all(d["na"] for d in r["details"]), r


def test_tv_e():
    # F2 高基期保護:減速但近3月均仍為正 → 0,不倒扣
    fund = {"rev": _rev([40.0, 40.0, 40.0, 80.0, 80.0, 80.0])}
    r = calculate_fundamental_score(fund)
    d = _detail(r, "營收YoY動能(3m vs 3m)")
    assert d["na"] is False and d["earned"] == 0, d


def _base_data(**overrides):
    data = {
        "latest": {"close": 95.0, "volume": 50000.0},
        "prev": {"close": 100.0},
        "indicators": {"ma5": 96.0, "ma10": 97.0, "ma20": 100.0, "ma60": 101.0, "high_20": 110.0,
                       "vol_ma5": 30000.0, "vol_ma20": 20000.0, "macd_hist": -0.5},
        "institution": {"history": [{"date": "2026-07-07", "foreign": -100, "trust": -50},
                                    {"date": "2026-07-08", "foreign": -100, "trust": -50},
                                    {"date": "2026-07-09", "foreign": -100, "trust": -50}],
                        "foreign_today": -100.0, "trust_today": -50.0},
        "margin": {"margin_change": 100},
        "borrow": {"borrow_change": 50},
        "tdcc": {"large_change": 0.0},
        "fundamentals": {},
        "previous_rating": None,
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict): data[k].update(v)
        else: data[k] = v
    return data


def test_tv_f():
    # 帶量下跌無地板（方向性 bug fix 回歸測試）:
    # close < prev_close、volume > vol_ma5、vol_ma5 > vol_ma20、close < ma20,其餘全不成立
    r = calculate_stock_rating(_base_data())
    assert _detail(r, "價漲且量增(量>5日均量)")["earned"] == 0, r
    assert _detail(r, "趨勢向上且量能擴張(5日均量>20日均量)")["earned"] == 0, r
    assert r["total"] < 2.85, r
    assert r["rating_key"] == "strong-sell", r


def _bull_tech(**overrides):
    # 技術面 7 項全過 → tech = 10
    return _base_data(**{
        "latest": {"close": 110.0, "volume": 50000.0},
        "prev": {"close": 105.0},
        "indicators": {"ma5": 108.0, "ma10": 106.0, "ma20": 100.0, "ma60": 95.0, "high_20": 111.0,
                       "vol_ma5": 30000.0, "vol_ma20": 20000.0, "macd_hist": 0.5},
        **overrides})


def test_tv_g():
    # tech 10 + chip 1（僅同買）= 11 → ratio 57.9%（加碼);TV-B fund → tilt -1 → 中性觀望
    data = _bull_tech(institution={"foreign_today": 5.0, "trust_today": 5.0}, fundamentals=FUND_TVB)
    r = calculate_stock_rating(data)
    assert r["base_idx"] == 1, r
    assert r["fund_tilt"] == -1, r
    assert r["rating_key"] == "neutral", r


def test_tv_h():
    # tech 10 + chip 4（同買1 + 融資減1.5 + 借券減1.5）= 14 → ratio 73.7%（強力加碼);TV-A fund → tilt +1 飽和不變
    data = _bull_tech(institution={"foreign_today": 5.0, "trust_today": 5.0},
                      margin={"margin_change": -100}, borrow={"borrow_change": -50}, fundamentals=FUND_TVA)
    r = calculate_stock_rating(data)
    assert r["base_idx"] == 0, r
    assert r["fund_tilt"] == 1, r
    assert r["rating_key"] == "strong-buy", r


def test_tv_i():
    # 邊界不越級:任何輸入 final_idx ∈ {0..4} 且 |final_idx - base_idx| <= 1
    idx_of = {k: i for i, (k, _) in enumerate(RATING_LEVELS)}
    fund_cases = [{}, FUND_TVA, FUND_TVB]
    data_cases = [_base_data, _bull_tech]
    for make in data_cases:
        for fund in fund_cases:
            r = calculate_stock_rating(make(fundamentals=fund))
            final_idx = idx_of[r["rating_key"]]
            assert 0 <= final_idx <= 4, r
            assert abs(final_idx - r["base_idx"]) <= 1, r


def test_tv_j():
    r = calculate_stock_rating(_base_data(tdcc={"large_change": 0.05}))
    assert _detail(r, "集保大戶增加")["earned"] == 0, r
    r = calculate_stock_rating(_base_data(tdcc={"large_change": 0.15}))
    assert _detail(r, "集保大戶增加")["earned"] == 2, r


def test_tv_k():
    r = calculate_stock_rating(_bull_tech(fundamentals=FUND_TVA, previous_rating="加碼"))
    for key in ("tech", "chip", "total", "ratio", "fund", "fund_max", "fund_tilt", "rating", "rating_key", "details", "previous_rating"):
        assert key in r, f"缺少鍵: {key}"
    assert r["total"] == r["tech"] + r["chip"], r  # 基本面分未混入
    assert set(d["bucket"] for d in r["details"]) <= {"tech", "chip", "fund"}, r
    assert all("na" in d for d in r["details"] if d["bucket"] == "fund"), r
    assert r["previous_rating"] == "加碼", r


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
