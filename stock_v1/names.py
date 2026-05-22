ALIASES = {
    "台灣積體電路製造股份有限公司": "台積電",
    "鴻海精密工業股份有限公司": "鴻海",
    "聯發科技股份有限公司": "聯發科",
    "中華電信股份有限公司": "中華電",
    "國泰金融控股股份有限公司": "國泰金",
    "富邦金融控股股份有限公司": "富邦金",
    "長榮海運股份有限公司": "長榮",
    "陽明海運股份有限公司": "陽明",
    "萬海航運股份有限公司": "萬海",
    "台塑石化股份有限公司": "台塑化",
    "大立光電股份有限公司": "大立光",
    "環球晶圓股份有限公司": "環球晶",
    "台達電子工業股份有限公司": "台達電",
}

SUFFIXES = [
    "(開曼)股份有限公司",
    "股份有限公司",
    "有限公司",
]


def short_name(name: str | None, max_len: int = 8) -> str:
    if not name:
        return ""
    text = str(name).strip()
    if text in ALIASES:
        return ALIASES[text][:max_len]
    for suffix in SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if text.endswith("(開曼)"):
        text = text[: -len("(開曼)")]
    return text[:max_len]
