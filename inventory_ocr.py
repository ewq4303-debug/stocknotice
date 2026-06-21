#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股券商庫存截圖辨識 → docs/inventory.json
由 GitHub Action 在 inventory_uploads/ 有新截圖被推上來時自動執行。
辨識使用既有的 Claude / Gemini 視覺模型（AI_PROVIDER 控制）。
"""
import os
import sys
import json
import glob
import base64
import datetime

AI_PROVIDER       = os.getenv("AI_PROVIDER", "claude").lower()
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20240620")
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")

UPLOAD_DIR  = "inventory_uploads"
OUTPUT_FILE = "docs/inventory.json"

PROMPT = """你是台股券商庫存（持股）截圖的辨識助手。請從圖片中逐列讀出每一檔持有的股票，只輸出「純 JSON」，不要 markdown、不要任何說明文字。

輸出格式：
{"holdings":[{"stock_id":"股號","name":"股名","shares":股數,"avg_cost":成本均價,"price":現價或參考價,"market_value":市值,"pnl":未實現損益}]}

規則：
- 數字一律不要逗號、不要貨幣符號，可有小數。
- 股號只保留代號本身（例如 2330、00878）。
- 看不到或無法判讀的欄位請填 null，不要亂猜。
- 只輸出實際持股的資料列，略過表頭、合計、現金等非個股列。
- 若圖中沒有任何持股，回傳 {"holdings":[]}。"""


def now_tw():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def pick_image():
    files = [f for f in glob.glob(os.path.join(UPLOAD_DIR, "*"))
             if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def media_type(img_bytes, path):
    if img_bytes[:8].startswith(b"\x89PNG"):
        return "image/png"
    if img_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png" if path.lower().endswith(".png") else "image/jpeg"


def call_claude(img_bytes, mtype):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mtype,
                                          "data": base64.b64encode(img_bytes).decode()}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    return msg.content[0].text


def call_gemini(img_bytes, mtype):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY)
    res = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Part.from_bytes(data=img_bytes, mime_type=mtype), PROMPT],
        config=types.GenerateContentConfig(max_output_tokens=2500, temperature=0),
    )
    return res.text


def extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError("模型回應中找不到 JSON：" + text[:200])
    return json.loads(text[s:e + 1])


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def normalize(holdings):
    out = []
    for h in holdings:
        stock_id = str(h.get("stock_id") or "").strip()
        name = str(h.get("name") or "").strip()
        shares = num(h.get("shares"))
        avg_cost = num(h.get("avg_cost"))
        price = num(h.get("price"))
        mv = num(h.get("market_value"))
        pnl = num(h.get("pnl"))
        if not stock_id and not name:
            continue
        if mv is None and price is not None and shares is not None:
            mv = price * shares
        if pnl is None and avg_cost is not None and price is not None and shares is not None:
            pnl = (price - avg_cost) * shares
        cost = avg_cost * shares if (avg_cost is not None and shares is not None) else None
        pnl_pct = (pnl / cost * 100) if (pnl is not None and cost) else None
        out.append({
            "stock_id": stock_id,
            "name": name,
            "shares": shares,
            "avg_cost": avg_cost,
            "price": price,
            "market_value": round(mv, 0) if mv is not None else None,
            "pnl": round(pnl, 0) if pnl is not None else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        })
    return out


def main():
    img_path = pick_image()
    if not img_path:
        print("找不到任何待辨識的截圖，跳過。")
        return
    print(f"辨識截圖：{img_path}（provider={AI_PROVIDER}）")

    with open(img_path, "rb") as f:
        img_bytes = f.read()
    mtype = media_type(img_bytes, img_path)

    if AI_PROVIDER == "gemini":
        if not GEMINI_API_KEY:
            print("未設定 GEMINI_API_KEY"); sys.exit(1)
        raw = call_gemini(img_bytes, mtype)
    else:
        if not ANTHROPIC_API_KEY:
            print("未設定 ANTHROPIC_API_KEY"); sys.exit(1)
        raw = call_claude(img_bytes, mtype)

    parsed = extract_json(raw)
    holdings = normalize(parsed.get("holdings", []))

    total_mv = sum(h["market_value"] for h in holdings if h["market_value"] is not None)
    total_cost = sum(h["avg_cost"] * h["shares"] for h in holdings
                     if h["avg_cost"] is not None and h["shares"] is not None)
    total_pnl = sum(h["pnl"] for h in holdings if h["pnl"] is not None)

    for h in holdings:
        h["weight"] = round(h["market_value"] / total_mv * 100, 2) if (total_mv and h["market_value"]) else None

    result = {
        "updated_at": now_tw(),
        "currency": "TWD",
        "total_cost": round(total_cost, 0),
        "total_market_value": round(total_mv, 0),
        "total_pnl": round(total_pnl, 0),
        "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost else None,
        "holdings": holdings,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已寫入 {OUTPUT_FILE}，共辨識 {len(holdings)} 檔，總市值 {total_mv:,.0f}")


if __name__ == "__main__":
    main()
