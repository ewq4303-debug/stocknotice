import pandas as pd

from stock_analyzer_v4 import generate_chart_scripts


def test_large_and_retail_ratios_use_separate_y_axes():
    index = pd.to_datetime(["2026-08-14"])
    stock_data = {
        "2330": {
            "df": pd.DataFrame(
                {
                    "Open": [100.0],
                    "Close": [101.0],
                    "Low": [99.0],
                    "High": [102.0],
                    "Volume": [1_000],
                    "SMA_20": [100.0],
                    "ST": [98.0],
                    "ST_DIR": [1],
                },
                index=index,
            ),
            "chart_data": {},
            "tdcc": {
                "history": [
                    {
                        "date": "20260814",
                        "large_ratio": 56.3,
                        "retail_ratio": 34.8,
                    }
                ]
            },
        }
    }

    script = generate_chart_scripts(stock_data, {})

    assert "name: '大戶持股比例', type: 'line', xAxisIndex: 3, yAxisIndex: 6" in script
    assert "name: '散戶持股比例', type: 'line', xAxisIndex: 3, yAxisIndex: 7" in script
    assert "position: 'right', name: '散戶'" in script
