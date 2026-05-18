"""Centralized runtime/env settings for server modules. Updated for Telegram chat ID change."""

import os
import sys
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)
load_dotenv(os.path.join(ROOT_DIR, ".env"))

DATA_DIR = os.path.join(ROOT_DIR, "data")
JSON_LATEST = os.path.join(DATA_DIR, "top100_full_latest.json")

PG_HOST = os.getenv("PGHOST", os.getenv("LOCAL_PG_HOST", "127.0.0.1")).strip() or "127.0.0.1"
PG_PORT = int((os.getenv("PGPORT", os.getenv("LOCAL_PG_PORT", "5432")).strip() or "5432"))
PG_DBNAME = os.getenv("PGDATABASE", os.getenv("LOCAL_PG_DB", "postgres")).strip() or "postgres"
PG_USER = os.getenv("PGUSER", os.getenv("LOCAL_PG_USER", "postgres")).strip() or "postgres"
PG_PASSWORD = os.getenv("PGPASSWORD", os.getenv("LOCAL_PG_PASSWORD", "")).strip()
PG_SSLMODE = os.getenv("PGSSLMODE", os.getenv("LOCAL_PG_SSLMODE", "disable")).strip() or "disable"

FIREBASE_SERVICE_ACCOUNT_KEY = os.getenv("FIREBASE_SERVICE_ACCOUNT_KEY", "").strip()
FIREBASE_VAPID_KEY = os.getenv("NEXT_PUBLIC_FIREBASE_VAPID_KEY", "").strip()
FIREBASE_WEB_CONFIG = {
    "apiKey": os.getenv("NEXT_PUBLIC_FIREBASE_API_KEY", "").strip(),
    "authDomain": os.getenv("NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN", "").strip(),
    "projectId": os.getenv("NEXT_PUBLIC_FIREBASE_PROJECT_ID", "").strip(),
    "storageBucket": os.getenv("NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET", "").strip(),
    "messagingSenderId": os.getenv("NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID", "").strip(),
    "appId": os.getenv("NEXT_PUBLIC_FIREBASE_APP_ID", "").strip(),
    "measurementId": os.getenv("NEXT_PUBLIC_FIREBASE_MEASUREMENT_ID", "").strip(),
}

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_CLAIMS_SUB = os.getenv("VAPID_CLAIMS_SUB", "mailto:admin@example.com").strip()
TELEMSG_PUSH_TOKEN = os.getenv("TELEMSG_PUSH_TOKEN", "").strip()
PUSH_DEV_ADMIN_TOKEN = os.getenv("PUSH_DEV_ADMIN_TOKEN", TELEMSG_PUSH_TOKEN).strip()

SHORT_RATIO_FILES = [
    os.path.join(DATA_DIR, "공매도.csv"),
    os.path.join(DATA_DIR, "코스피공매도.csv"),
    os.path.join(DATA_DIR, "코스닥공매도.csv"),
]

# Telegram Feedback Settings (Added)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
