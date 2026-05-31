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
    "2408": "電子上游-半導體-記憶體",
    "3006": "電子上游-半導體-記憶體",
    "3260": "電子上游-半導體-記憶體",
    "6239": "電子上游-半導體-記憶體",
    "8271": "電子上游-半導體-記憶體",
    "2367": "電子上游-PCB-製造",
    "2454": "電子上游-半導體-IC設計",
}

THEME_CATEGORIES_BY_CODE = {
    "記憶體族群": [
        "2344",  # 華邦電
        "2408",  # 南亞科
        "2451",  # 創見
        "3006",  # 晶豪科
        "3260",  # 威剛
        "4967",  # 十銓
        "4973",  # 廣穎
        "6239",  # 力成
        "8271",  # 宇瞻
        "8299",  # 群聯
        "8088",  # 品安
        "5351",  # 鈺創
    ],
}

THEME_KEYWORD_CATEGORIES = [
    ("記憶體族群", ["華邦電", "南亞科", "創見", "晶豪科", "威剛", "十銓", "廣穎", "力成", "宇瞻", "群聯", "旺宏", "力積電", "商丞"]),
]

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
    ("華邦電", "電子上游-半導體-記憶體"),
    ("南亞科", "電子上游-半導體-記憶體"),
    ("創見", "電子上游-半導體-記憶體"),
    ("晶豪科", "電子上游-半導體-記憶體"),
    ("威剛", "電子上游-半導體-記憶體"),
    ("十銓", "電子上游-半導體-記憶體"),
    ("廣穎", "電子上游-半導體-記憶體"),
    ("力成", "電子上游-半導體-記憶體"),
    ("宇瞻", "電子上游-半導體-記憶體"),
    ("群聯", "電子上游-半導體-記憶體"),
    ("旺宏", "電子上游-半導體-記憶體"),
    ("力積電", "電子上游-半導體-記憶體"),
    ("商丞", "電子上游-半導體-記憶體"),
]

RAW_INDUSTRY_FALLBACKS = {
    "01": "傳統產業-水泥-製造",
    "02": "民生消費-食品-製造",
    "03": "傳統產業-塑膠-製造",
    "04": "傳統產業-紡織纖維-製造",
    "05": "傳統產業-電機機械-製造",
    "06": "傳統產業-電器電纜-製造",
    "07": "傳統產業-化學生技醫療-綜合",
    "08": "傳統產業-玻璃陶瓷-製造",
    "09": "傳統產業-造紙-製造",
    "10": "傳統產業-鋼鐵-製造",
    "11": "傳統產業-橡膠-製造",
    "12": "傳統產業-汽車-製造",
    "14": "傳統產業-建材營造-工程",
    "15": "傳統產業-航運-運輸",
    "16": "民生消費-觀光餐旅-服務",
    "17": "金融-金融保險-服務",
    "18": "民生消費-貿易百貨-服務",
    "20": "其他-綜合產業",
    "21": "傳統產業-化學工業-製造",
    "22": "生技醫療-醫療保健-研發製造",
    "23": "能源-油電燃氣-服務",
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
    "35": "綠能環保-綠能環保-服務",
    "36": "數位經濟-數位雲端-服務",
    "37": "生活消費-運動休閒-製造服務",
    "38": "生活消費-居家生活-製造服務",
    "80": "其他-管理股票",
    "91": "其他-特殊分類/外國企業",
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
    themes = stock_theme_categories(code_text, name_text)
    return {
        "category": category,
        "themes": themes,
        "chain": chain,
        "sector": chain[0] if chain else category,
        "segment": chain[1] if len(chain) > 1 else "",
        "activity": chain[2] if len(chain) > 2 else "",
        "raw": raw_text,
        "source": source,
    }


def stock_theme_categories(code: str | None, name: str | None) -> list[str]:
    code_text = str(code or "").strip()
    name_text = str(name or "").strip()
    themes = []
    for theme, codes in THEME_CATEGORIES_BY_CODE.items():
        if code_text in codes:
            themes.append(theme)
    for theme, keywords in THEME_KEYWORD_CATEGORIES:
        if any(keyword in name_text for keyword in keywords) and theme not in themes:
            themes.append(theme)
    return themes
