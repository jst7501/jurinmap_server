import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()
app_key = os.getenv('KIS_APP_KEY')
app_secret = os.getenv('KIS_APP_SECRET')

url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
payload = {
    "grant_type": "client_credentials",
    "appkey": app_key,
    "appsecret": app_secret
}
res = requests.post(url, json=payload)
data = res.json()
if "access_token" in data:
    token = data["access_token"]
    expiry = data.get("access_token_token_expired", "")
    with open("KIS_TOKEN", "w", encoding="utf-8") as f:
        f.write(f"token: {token}\nvalid-date: {expiry}\n")
    print("Token successfully created and saved to KIS_TOKEN!")
else:
    print("Error creating token:", data)
