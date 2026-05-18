import argparse
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

try:
    from py_vapid import Vapid
except Exception as e:
    print(f"py_vapid import failed: {e}")
    print("Install dependency first: pip install pywebpush")
    raise SystemExit(1)


def ensure_line(lines, key, value):
    prefix = f"{key}="
    replaced = False
    out = []
    for line in lines:
        if line.startswith(prefix):
            out.append(f'{prefix}{value}')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f'{prefix}{value}')
    return out


def main():
    parser = argparse.ArgumentParser(description="Generate VAPID key pair for Web Push")
    parser.add_argument("--write-env", action="store_true", help="write keys to .env")
    parser.add_argument("--subject", default="mailto:admin@example.com", help="VAPID_CLAIMS_SUB value")
    args = parser.parse_args()

    vapid = Vapid()
    vapid.generate_keys()
    private_key = vapid.private_pem().decode("utf-8")
    public_key = vapid.public_key

    print("VAPID_PUBLIC_KEY=" + public_key)
    print("VAPID_PRIVATE_KEY=" + private_key.replace("\n", "\\n"))
    print("VAPID_CLAIMS_SUB=" + args.subject)

    if args.write_env:
        env_path = os.path.join(ROOT_DIR, ".env")
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = [line.rstrip("\n") for line in f.readlines()]

        lines = ensure_line(lines, "VAPID_PUBLIC_KEY", public_key)
        lines = ensure_line(lines, "VAPID_PRIVATE_KEY", private_key.replace("\n", "\\n"))
        lines = ensure_line(lines, "VAPID_CLAIMS_SUB", args.subject)

        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        print(f"\nWritten to {env_path}")


if __name__ == "__main__":
    main()
