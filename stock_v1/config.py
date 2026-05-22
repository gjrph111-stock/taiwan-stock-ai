from pathlib import Path
import os


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = Path(os.environ.get("STOCK_V1_DB_PATH", ROOT_DIR / "data" / "tw_stocks.sqlite"))

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json,text/csv,text/plain,*/*",
}

TWSE_COMPANY_URLS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv",
]

TPEX_COMPANY_URLS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap03_O",
    "https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv",
]
