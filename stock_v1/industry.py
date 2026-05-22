"""User-facing industry taxonomy for Taiwan stocks.

The exchange universe feed often stores industry as a numeric or broad label.
This module keeps a richer display layer without changing the raw database
field that is still useful for peer grouping.
"""

from __future__ import annotations


MANUAL_INDUSTRY_CATEGORIES = {
    "2330": "電子上游-半導體-晶圓代工",
    "2317": "電子下游-EMS/組裝代工-製造",
    "2344": "電子上游-半導體-記憶體",
    "2367": "電子上游-PCB-製造",
    "2454": "電子上游-半導體-IC設計",
}

NAME_KEYWORD_CATEGORIES = [
    ("燿華", "電子上游-PCB-製造"),
    ("欣興", "電子上游-PCB-載板/製造"),
    ("華通", "電子上游-PCB-製造"),
    ("健鼎", "電子上游-PCB-製造"),
    ("金像電", "電子上游-PCB-製造"),
    ("南電", "電子上游-PCB-載板/製造"),
    ("景碩", "電子上游-PCB-載板/製造"),
    ("臻鼎", "電子上游-PCB-製造"),
    ("台光電", "電子上游-PCB-材料"),
    ("台燿", "電子上游-PCB-材料"),
    ("台積電", "電子上游-半導體-晶圓代工"),
    ("聯發科", "電子上游-半導體-IC設計"),
    ("聯電", "電子上游-半導體-晶圓代工"),
    ("日月光", "電子上游-半導體-封測"),
    ("矽品", "電子上游-半導體-封測"),
    ("鴻海", "電子下游-EMS/組裝代工-製造"),
]

RAW_INDUSTRY_FALLBACKS = {
    "24": "電子上游-半導體-綜合",
    "25": "電子中游-電腦及週邊-製造",
    "26": "電子下游-光電-製造",
    "27": "電子中游-通信網路-製造",
    "28": "電子上游-電子零組件-製造",
    "29": "電子下游-電子通路-服務",
    "30": "電子中游-資訊服務-軟體",
    "31": "電子中游-其他電子-製造",
    "32": "文創/數位-文化創意",
    "33": "生活消費-農業科技",
    "34": "數位經濟-電子商務",
}


def industry_profile(code: str | None, name: str | None, raw_industry: str | None) -> dict:
    code_text = str(code or "").strip()
    name_text = str(name or "").strip()
    raw_text = str(raw_industry or "").strip()

    category = MANUAL_INDUSTRY_CATEGORIES.get(code_text)
    source = "manual_code" if category else ""

    if not category:
        for keyword, mapped in NAME_KEYWORD_CATEGORIES:
            if keyword in name_text:
                category = mapped
                source = "keyword"
                break

    if not category:
        category = RAW_INDUSTRY_FALLBACKS.get(raw_text)
        source = "raw_code" if category else ""

    if not category:
        category = raw_text or "未分類"
        source = "raw"

    chain = [part for part in category.split("-") if part]
    return {
        "category": category,
        "chain": chain,
        "sector": chain[0] if chain else category,
        "segment": chain[1] if len(chain) > 1 else "",
        "activity": chain[2] if len(chain) > 2 else "",
        "raw": raw_text,
        "source": source,
    }
