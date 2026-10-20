import getpass
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

api_key = os.environ.get("JQUANTS_API_KEY") or getpass.getpass(
    "J-Quants APIキー（画面には表示されません）: "
)

response = requests.get(
    "https://api.jquants.com/v2/equities/bars/daily",
    headers={"x-api-key": api_key},
    params={
        "code": "72030",
        "date": "20261019",
    },
    timeout=30,
)

print("HTTPステータス:", response.status_code)
response.raise_for_status()

payload = response.json()
rows = payload.get("data", [])

print("取得件数:", len(rows))
if rows:
    print("先頭レコード:", rows[0])
else:
    print("契約の取得可能期間や指定日を確認してください。")