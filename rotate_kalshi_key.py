#!/usr/bin/env python3
"""
Rotate the Kalshi API key — local .env, VPS .env, and service restart.

The private key NEVER passes through stdout, a shell argument, or an assistant
transcript: it is read from the file Kalshi downloaded and written straight into
.env. (The previous key was exposed precisely because .env was cat'ed.)

Usage:
    python3 rotate_kalshi_key.py --key-id <NEW_KEY_ID> --key-file ~/Downloads/kalshi-key.txt
    python3 rotate_kalshi_key.py ... --verify-only     # test the new key, change nothing
    python3 rotate_kalshi_key.py ... --skip-deploy     # local .env only

Order matters: create the NEW key at https://kalshi.com/account/profile first and
leave the OLD one active. This script verifies the new key against the live API
before touching anything, so a bad key cannot take the scanner down. Delete the
old key in the Kalshi UI only after this reports success.
"""

import argparse
import base64
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ENV_PATH = Path(__file__).resolve().parent / ".env"
VPS = "ubuntu@98.82.172.44"
SSH_KEY = Path.home() / ".ssh" / "arb-key.pem"
REMOTE_ENV = "prediction-arb/.env"
KALSHI_BASE = "https://api.elections.kalshi.com"
TEST_PATH = "/trade-api/v2/portfolio/balance"


def load_and_validate(key_file: Path) -> str:
    """Read the PEM, confirm it is an RSA private key. Returns the PEM text."""
    pem = key_file.read_text().strip()
    if "BEGIN" not in pem or "PRIVATE KEY" not in pem:
        sys.exit(f"ERROR: {key_file} does not look like a PEM private key.")
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except Exception as exc:
        sys.exit(f"ERROR: could not parse private key: {exc}")
    if not isinstance(key, rsa.RSAPrivateKey):
        sys.exit("ERROR: Kalshi requires an RSA key; this is not one.")
    print(f"  key parsed OK — RSA {key.key_size} bits")
    return pem


def verify_against_kalshi(key_id: str, pem: str) -> bool:
    """Signed request against the live API. Proves the key is active server-side."""
    ts_ms = str(int(time.time() * 1000))
    message = f"{ts_ms}GET{TEST_PATH}".encode()
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    sig = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "Content-Type": "application/json",
    }
    resp = requests.get(KALSHI_BASE + TEST_PATH, headers=headers, timeout=15)
    if resp.status_code == 200:
        balance = resp.json().get("balance")
        print(f"  Kalshi accepted the key (HTTP 200, balance={balance})")
        return True
    print(f"  Kalshi REJECTED the key: HTTP {resp.status_code} {resp.text[:200]}")
    return False


def rewrite_env(key_id: str, pem: str) -> None:
    """Update the two Kalshi vars; drop the dead multi-line copy."""
    backup = ENV_PATH.parent / f".env.bak.{datetime.now():%Y%m%d%H%M%S}"
    shutil.copy2(ENV_PATH, backup)
    backup.chmod(0o600)
    print(f"  backed up current .env -> {backup.name}")

    escaped = pem.replace("\n", "\\n")
    out, skipping = [], False
    for line in ENV_PATH.read_text().split("\n"):
        # KALSHI_PRIVATE_KEY_NEWLINES is an unquoted multi-line PEM that no code
        # reads and that makes python-dotenv fail to parse the file. Drop it.
        if line.startswith("KALSHI_PRIVATE_KEY_NEWLINES="):
            skipping = True
            continue
        if skipping:
            if "=" in line and line.split("=")[0].isupper():
                skipping = False          # a new var begins; stop skipping
            else:
                continue
        if line.startswith("KALSHI_API_KEY_ID="):
            out.append(f"KALSHI_API_KEY_ID={key_id}")
        elif line.startswith("KALSHI_PRIVATE_KEY="):
            out.append(f"KALSHI_PRIVATE_KEY={escaped}")
        else:
            out.append(line)

    text = "\n".join(out).rstrip("\n") + "\n"
    ENV_PATH.write_text(text)
    ENV_PATH.chmod(0o600)
    print("  local .env updated (KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY; _NEWLINES removed)")


def deploy() -> None:
    subprocess.run(
        ["scp", "-q", "-i", str(SSH_KEY), str(ENV_PATH), f"{VPS}:{REMOTE_ENV}"], check=True
    )
    print("  .env copied to VPS")
    subprocess.run(
        ["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes", VPS,
         "chmod 600 ~/prediction-arb/.env && sudo systemctl restart arb-scanner"],
        check=True,
    )
    print("  arb-scanner restarted")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-id", required=True, help="new Kalshi Key ID (UUID)")
    ap.add_argument("--key-file", required=True, type=Path, help="downloaded private key file")
    ap.add_argument("--verify-only", action="store_true", help="test the key, change nothing")
    ap.add_argument("--skip-deploy", action="store_true", help="update local .env only")
    args = ap.parse_args()

    print("1. validating key file")
    pem = load_and_validate(args.key_file.expanduser())

    print("2. verifying against the live Kalshi API")
    if not verify_against_kalshi(args.key_id, pem):
        sys.exit("ABORTED — nothing was changed. Check the Key ID matches this key file.")

    if args.verify_only:
        print("\nverify-only: key is valid and active. Nothing changed.")
        return

    print("3. rewriting local .env")
    rewrite_env(args.key_id, pem)

    if args.skip_deploy:
        print("\nskip-deploy: local only. Run without --skip-deploy to update the VPS.")
        return

    print("4. deploying to VPS and restarting")
    deploy()
    print("\nDONE. Next: confirm the scanner reconnected, THEN delete the old key in the Kalshi UI.")


if __name__ == "__main__":
    main()
