import pandas as pd

from stock_analyzer_v4 import generate_chart_scripts


def test_holder_ratios_get_independent_tight_axes():
    frame = pd.DataFrame(
        {
            "Open": [100.0], "Close": [101.0], "Low": [99.0], "High": [102.0],
            "Volume": [1_000], "SMA_20": [100.0], "ST": [98.0], "ST_DIR": [1],
        },
        index=pd.to_datetime(["2026-08-14"]),
    )
    stocks = {
        "2330": {
            "df": frame,
            "chart_data": {},
            "tdcc": {"history": [{"date": "20260814", "large_ratio": 56.3, "retail_ratio": 34.8}]},
        }
    }

    script = generate_chart_scripts(stocks, {})

    assert "yAxis[6] = holderRatioAxis_2330('大戶'" in script
    assert "yAxis.push(holderRatioAxis_2330('散戶'" in script
    assert "if (series.name === '散戶持股比例') series.yAxisIndex = 7" in script
    assert "(v.max - v.min) * 0.08, 0.1" in script
