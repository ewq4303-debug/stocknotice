# -*- coding: utf-8 -*-
"""評等系統 v2 測試(TV-A ~ TV-K)。合成 fixture,不打外部 API。
執行:python tests/test_rating_v2.py(純 assert,亦相容 pytest)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stock_analyzer_v4 as sa

LEVEL_KEYS = [k for k, _ in sa.RATING_LEVELS]


def make_rev(yoys):
    return [{"date": f"2026-{6-i:02d}", "rev": "10.00億", "yoy": y} for i, y in enumerate(yoys)]

def make_eps(yoys):
    return [{"date": f"2026Q{i+1}", "eps": 1.0, "yoy": y} for i, y in enumerate(yoys)]

def make_gm(gms):
    return [{"date": f"2026Q{i+1}", "gm": g} for i, g in enumerate(gms)]

def fund_detail(res, label):
    return next(d for d in res["details"] if d["label"] == label)


# ---------- 基本面測試向量 ----------

def test_tv_a_all_positive():
    fund = {
        "rev": make_rev([42, 35, 28, 10, 5, 5.1]),      # F1 +42>0;F2 均35 vs 6.7 加速
        "eps": make_eps([120]),                           # F3 +120>0
        "gm": make_gm([38.2, 34.0, 34.5, 35.0]),          # F4 38.2 > 34.5+0.5
        "trailing_pe": 18, "forward_pe": 12,              # F5 12 < 18*0.95
    }
    res = sa.calculate_fundamental_score(fund)
    assert res["score"] == 6.0, res
    assert res["max_pts"] == 6.0, res
    assert res["n_available"] == 5, res
    assert res["tilt"] == 1, res

def test_tv_b_deterioration_with_na_renormalize():
    fund = {
        "rev": make_rev([-18, -12, -6, 10, 12, 14]),      # F1 -18<-10;F2 均-12 vs +12 減速且轉負
        "eps": make_eps([-40]),                            # F3 -40 ≤ 0
        "gm": make_gm([30.0, 29.0]),                       # F4 只有 2 筆 → N/A
        "trailing_pe": None, "forward_pe": 15,             # F5 tpe None → N/A
    }
    res = sa.calculate_fundamental_score(fund)
    assert res["score"] == -4.0, res
    assert res["max_pts"] == 4.0, res
    assert res["n_available"] == 3, res
    assert res["tilt"] == -1, res

def test_tv_c_deadzone_dilution():
    fund = {
        "rev": make_rev([-4, 2, 5, -10, -8, -6]),          # F1 -4 死區;F2 均1 vs -8 加速 → +1
        "eps": make_eps([8]),                               # F3 +8 → +1
        "gm": make_gm([30.2, 30.0, 30.0, 30.0]),            # F4 偏離 +0.2pp 死區 → 0
        "trailing_pe": 20, "forward_pe": 20,                # F5 區間內 → 0
    }
    res = sa.calculate_fundamental_score(fund)
    assert res["score"] == 2.5, res
    assert res["max_pts"] == 6.0, res
    assert res["tilt"] == 0, res                            # 0.42 < 0.6

def test_tv_d_no_data_no_activation():
    res = sa.calculate_fundamental_score({})
    assert res["n_available"] == 0, res
    assert res["tilt"] == 0, res
    assert res["max_pts"] == 0, res
    assert len(res["details"]) == 5 and all(d["na"] for d in res["details"]), res
    res_none = sa.calculate_fundamental_score(None)
    assert res_none["tilt"] == 0 and res_none["max_pts"] == 0

def test_tv_e_f2_high_base_protection():
    fund = {"rev": make_rev([40, 40, 40, 80, 80, 80])}      # 減速但近3月均仍為正 → 0
    res = sa.calculate_fundamental_score(fund)
    d = fund_detail(res, "營收YoY動能(3m vs 3m)")
    assert d["earned"] == 0 and not d["passed"] and not d["na"], d


# ---------- 主評等測試向量 ----------

def make_data(**over):
    data = {
        "latest": {"close": 100, "volume": 1000},
        "prev": {"close": 101},
        "indicators": {"ma5": 0, "ma10": 0, "ma20": 0, "ma60": 0, "high_20": 0,
                       "vol_ma5": 0, "vol_ma20": 0, "macd_hist": 0},
        "institution": {"history": [], "foreign_today": 0, "trust_today": 0},
        "margin": {"margin_change": 0},
        "borrow": {"borrow_change": 0},
        "tdcc": {"large_change": 0},
        "fundamentals": {},
        "previous_rating": None,
    }
    data.update(over)
    return data

TV_A_FUND = {
    "rev": make_rev([42, 35, 28, 10, 5, 5.1]), "eps": make_eps([120]),
    "gm": make_gm([38.2, 34.0, 34.5, 35.0]), "trailing_pe": 18, "forward_pe": 12,
}
TV_B_FUND = {
    "rev": make_rev([-18, -12, -6, 10, 12, 14]), "eps": make_eps([-40]),
    "gm": make_gm([30.0, 29.0]), "trailing_pe": None, "forward_pe": 15,
}

def rating_detail(r, label):
    return next(d for d in r["details"] if d["label"] == label)

def test_tv_f_down_with_volume_no_floor():
    # 帶量下跌:close<prev、volume>vol_ma5、vol_ma5>vol_ma20、close<ma20,其餘全不成立
    data = make_data(
        latest={"close": 95, "volume": 2000}, prev={"close": 100},
        indicators={"ma5": 0, "ma10": 0, "ma20": 100, "ma60": 0, "high_20": 120,
                    "vol_ma5": 1500, "vol_ma20": 1000, "macd_hist": -1},
    )
    r = sa.calculate_stock_rating(data)
    assert rating_detail(r, "價漲且量增(量>5日均量)")["earned"] == 0, r
    assert rating_detail(r, "趨勢向上且量能擴張(5日均量>20日均量)")["earned"] == 0, r
    assert r["total"] < 2.85, r
    assert r["rating_key"] == "strong-sell", r

TV_G_BASE = dict(
    latest={"close": 110, "volume": 500}, prev={"close": 100},
    indicators={"ma5": 108, "ma10": 106, "ma20": 105, "ma60": 90, "high_20": 110,
                "vol_ma5": 1000, "vol_ma20": 1200, "macd_hist": 1},
    institution={"history": [{"foreign": 100, "trust": 100}], "foreign_today": 1, "trust_today": 1},
    margin={"margin_change": 1}, borrow={"borrow_change": 1}, tdcc={"large_change": 0},
)  # tech 6.5 + chip 4 = 10.5 → ratio 55.3% → base=加碼

def test_tv_g_tilt_downgrade():
    data = make_data(**TV_G_BASE)
    data["fundamentals"] = TV_B_FUND
    r = sa.calculate_stock_rating(data)
    assert r["total"] == 10.5, r
    assert r["fund_tilt"] == -1, r
    assert r["rating_key"] == "neutral", r

def test_tv_h_tilt_saturation():
    data = make_data(
        latest={"close": 110, "volume": 2000}, prev={"close": 100},
        indicators={"ma5": 108, "ma10": 106, "ma20": 105, "ma60": 90, "high_20": 110,
                    "vol_ma5": 1500, "vol_ma20": 1000, "macd_hist": 1},
        institution={"history": [{"foreign": 100, "trust": 100}], "foreign_today": 1, "trust_today": 0},
        margin={"margin_change": -1}, borrow={"borrow_change": 1}, tdcc={"large_change": 0},
        fundamentals=TV_A_FUND,
    )  # tech 10 + chip 4.5 = 14.5 → ratio 76.3% → base=強力加碼
    r = sa.calculate_stock_rating(data)
    assert r["total"] == 14.5, r
    assert r["fund_tilt"] == 1, r
    assert r["rating_key"] == "strong-buy", r

def test_tv_i_bounded_tilt():
    fund_variants = [{}, TV_A_FUND, TV_B_FUND]
    data_variants = [make_data(), make_data(**TV_G_BASE),
                     make_data(latest={"close": 95, "volume": 2000}, prev={"close": 100},
                               indicators={"ma5": 0, "ma10": 0, "ma20": 100, "ma60": 0, "high_20": 120,
                                           "vol_ma5": 1500, "vol_ma20": 1000, "macd_hist": -1})]
    for dv in data_variants:
        for fv in fund_variants:
            d = dict(dv); d["fundamentals"] = fv
            r = sa.calculate_stock_rating(d)
            final_idx = LEVEL_KEYS.index(r["rating_key"])
            assert 0 <= final_idx <= 4, r
            assert 0 <= r["base_idx"] <= 4, r
            assert abs(final_idx - r["base_idx"]) <= 1, r

def test_tv_j_c6_threshold():
    r_low = sa.calculate_stock_rating(make_data(tdcc={"large_change": 0.05}))
    assert rating_detail(r_low, "集保大戶增加")["earned"] == 0, r_low
    r_high = sa.calculate_stock_rating(make_data(tdcc={"large_change": 0.15}))
    assert rating_detail(r_high, "集保大戶增加")["earned"] == 2, r_high


# ---------- 契約測試 ----------

def test_tv_k_output_contract():
    data = make_data(**TV_G_BASE)
    data["fundamentals"] = TV_A_FUND
    r = sa.calculate_stock_rating(data)
    for key in ("tech", "chip", "total", "ratio", "fund", "fund_max", "fund_tilt",
                "rating", "rating_key", "details", "previous_rating"):
        assert key in r, f"缺少鍵 {key}"
    assert r["total"] == r["tech"] + r["chip"], r          # 基本面分未混入
    buckets = {d["bucket"] for d in r["details"]}
    assert buckets == {"tech", "chip", "fund"}, buckets
    assert all("na" in d for d in r["details"] if d["bucket"] == "fund"), r["details"]
    assert isinstance(r["fund_tilt"], int) and r["fund_tilt"] in (-1, 0, 1), r
    assert r["previous_rating"] is None, r


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
