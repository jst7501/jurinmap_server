"""
FCM 푸시 디버그 스크립트 — 서버 수정/재시작 없이 특정 토큰에만 테스트 푸시를 보내서
실제 에러 코드/메시지를 즉시 확인.

용도:
  python scripts/debug_fcm_push.py                    # 최근 갱신된 상위 5개 토큰에 각각 단독 푸시
  python scripts/debug_fcm_push.py --top 10           # 상위 10개
  python scripts/debug_fcm_push.py --token <FULL>     # 특정 토큰만
  python scripts/debug_fcm_push.py --all              # 202개 전부 (주의: 사용자 전체 대상)
  python scripts/debug_fcm_push.py --dry-run          # 실제 전송 안 하고 토큰 목록만 출력

주의:
  - 기본 모드(상위 5개)는 "최근에 재구독한 사용자 본인"에게만 가는 것을 전제로 함.
  - --all 은 202명 전체에게 테스트 메시지를 보냄. 사용자 경고 후 신중하게 사용.
"""

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# UTF-8 콘솔
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

import psycopg2
from psycopg2.extras import RealDictCursor
import firebase_admin
from firebase_admin import credentials as fb_credentials
from firebase_admin import messaging as fb_messaging


# ────────────────────────────────────────────────────────────
# 1. Firebase 초기화 (push_service.py의 _get_firebase_app 로직 복제)
# ────────────────────────────────────────────────────────────
def init_firebase():
    key_raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_KEY", "").strip()
    if not key_raw:
        print("❌ FIREBASE_SERVICE_ACCOUNT_KEY 환경변수가 없습니다.")
        sys.exit(1)

    if key_raw.startswith("'") and key_raw.endswith("'"):
        key_raw = key_raw[1:-1]
    info = json.loads(key_raw)
    if isinstance(info.get("private_key"), str):
        info["private_key"] = info["private_key"].replace("\\n", "\n")

    cred = fb_credentials.Certificate(info)
    try:
        app = firebase_admin.initialize_app(cred)
    except ValueError:
        # 이미 초기화된 경우
        app = firebase_admin.get_app()
    print(f"✅ Firebase 초기화 완료 (project={info.get('project_id', '?')})")
    return app


# ────────────────────────────────────────────────────────────
# 2. PostgreSQL에서 토큰 리스트 조회
# ────────────────────────────────────────────────────────────
def get_pg_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "postgres"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", "7410"),
        sslmode=os.getenv("PGSSLMODE", "disable"),
    )


def fetch_tokens(limit: int = 5, specific_token: str = ""):
    conn = get_pg_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        if specific_token:
            c.execute(
                "SELECT token, platform, user_agent, created_at, updated_at "
                "FROM fcm_tokens WHERE token = %s",
                (specific_token,),
            )
        else:
            c.execute(
                "SELECT token, platform, user_agent, created_at, updated_at "
                "FROM fcm_tokens ORDER BY updated_at DESC LIMIT %s",
                (limit,),
            )
        return c.fetchall()
    finally:
        conn.close()


def fetch_all_tokens():
    conn = get_pg_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute(
            "SELECT token, platform, user_agent, updated_at "
            "FROM fcm_tokens ORDER BY updated_at DESC"
        )
        return c.fetchall()
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────
# 3. 특정 토큰에 단독 푸시 전송 + 에러 상세 캡처
# ────────────────────────────────────────────────────────────
def send_test_push(token: str, app, title: str, body: str):
    """
    단일 토큰에 send() 호출. 성공 시 message_id 반환, 실패 시 예외 원문 반환.
    """
    data = {
        "title": title,
        "body": body,
        "url": "/#/news",
        "tag": "debug-fcm-test",
        "icon": "/favicon.svg",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    message = fb_messaging.Message(
        token=token,
        data={k: str(v) for k, v in data.items()},
        webpush=fb_messaging.WebpushConfig(
            notification=fb_messaging.WebpushNotification(
                title=title,
                body=body,
                icon="/favicon.svg",
                tag="debug-fcm-test",
            ),
        ),
    )
    try:
        msg_id = fb_messaging.send(message, app=app)
        return {"ok": True, "message_id": msg_id}
    except fb_messaging.UnregisteredError as e:
        return {"ok": False, "code": "UNREGISTERED", "error": str(e)}
    except fb_messaging.SenderIdMismatchError as e:
        return {"ok": False, "code": "SENDER_ID_MISMATCH", "error": str(e)}
    except fb_messaging.QuotaExceededError as e:
        return {"ok": False, "code": "QUOTA_EXCEEDED", "error": str(e)}
    except fb_messaging.ThirdPartyAuthError as e:
        return {"ok": False, "code": "THIRD_PARTY_AUTH", "error": str(e)}
    except Exception as e:
        code = getattr(e, "code", type(e).__name__)
        http_status = getattr(e, "http_response", None)
        status_text = ""
        if http_status is not None:
            try:
                status_text = f" http={http_status.status_code}"
            except Exception:
                pass
        return {
            "ok": False,
            "code": code,
            "error": f"{type(e).__name__}: {e}{status_text}",
        }


# ────────────────────────────────────────────────────────────
# 4. CLI
# ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="FCM 푸시 디버그")
    parser.add_argument("--top", type=int, default=5, help="최근 갱신 상위 N개 토큰 (기본 5)")
    parser.add_argument("--token", type=str, default="", help="특정 토큰 (전체 문자열)")
    parser.add_argument("--all", action="store_true", help="전체 202개 토큰에 각각 전송 (주의)")
    parser.add_argument("--dry-run", action="store_true", help="실제 전송 없이 대상만 출력")
    parser.add_argument("--title", type=str, default="🧪 테스트 알림")
    parser.add_argument(
        "--body",
        type=str,
        default="FCM 디버그 스크립트에서 보낸 테스트입니다. 무시하셔도 됩니다.",
    )
    args = parser.parse_args()

    # 토큰 수집
    if args.all:
        tokens = fetch_all_tokens()
        print(f"⚠️  --all 모드: 전체 {len(tokens)}개 토큰에 개별 전송합니다.")
    elif args.token:
        tokens = fetch_tokens(specific_token=args.token)
        if not tokens:
            print(f"❌ DB에서 토큰을 찾을 수 없습니다: {args.token[:20]}...")
            sys.exit(1)
    else:
        tokens = fetch_tokens(limit=args.top)
        print(f"🔍 최근 갱신 상위 {args.top}개 토큰:")

    # 출력
    for i, row in enumerate(tokens, 1):
        t = row["token"]
        tt = (t[:20] + "..." + t[-8:]) if len(t) > 40 else t
        ua = (row.get("user_agent") or "")[:70]
        updated = row.get("updated_at")
        print(f"  {i}. [{updated}] {row.get('platform')} | {tt}")
        print(f"     UA: {ua}")

    if args.dry_run:
        print("\n--dry-run: 전송 안 함")
        return

    # Firebase 초기화
    app = init_firebase()

    # 전송
    print("\n" + "=" * 60)
    print("푸시 전송 시작")
    print("=" * 60)

    success, fail = 0, 0
    errors_by_code: dict[str, int] = {}
    for i, row in enumerate(tokens, 1):
        token = row["token"]
        tt = (token[:16] + "..." + token[-6:]) if len(token) > 30 else token
        res = send_test_push(token, app, args.title, args.body)
        if res.get("ok"):
            success += 1
            print(f"  {i}. ✅ {tt} → msg_id={res.get('message_id')}")
        else:
            fail += 1
            code = res.get("code", "UNKNOWN")
            errors_by_code[code] = errors_by_code.get(code, 0) + 1
            print(f"  {i}. ❌ {tt}")
            print(f"     code={code}")
            print(f"     error={res.get('error', '')[:300]}")

    print("\n" + "=" * 60)
    print(f"결과: 성공 {success} / 실패 {fail}")
    if errors_by_code:
        print("실패 에러 코드 분포:")
        for code, count in sorted(errors_by_code.items(), key=lambda x: -x[1]):
            print(f"  - {code}: {count}")
    print("=" * 60)


if __name__ == "__main__":
    main()
