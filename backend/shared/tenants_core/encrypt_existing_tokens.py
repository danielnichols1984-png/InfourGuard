"""One-off script to encrypt already-stored plaintext OAuth secrets in
tenant_connections, in place. Run this against a database BEFORE
deploying code that expects EncryptedText columns (see crypto.py) — the
app can't decrypt a still-plaintext row, so running this after deploy
would break every existing connection between the deploy and this
script finishing.

Safe to run more than once: for each value, it tries to decrypt first —
if that succeeds, the value is already encrypted and is left alone.

    python -m shared.tenants_core.encrypt_existing_tokens
"""
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from shared.tenants_core.config import settings
from shared.tenants_core.db import engine

COLUMNS = ["access_token", "refresh_token", "oauth_state", "code_verifier"]


def main() -> None:
    fernet = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())

    with engine.begin() as conn:
        rows = conn.execute(text(f"SELECT id, {', '.join(COLUMNS)} FROM tenant_connections")).mappings().all()

        already_encrypted = 0
        newly_encrypted = 0
        for row in rows:
            updates = {}
            for col in COLUMNS:
                value = row[col]
                if value is None:
                    continue
                try:
                    fernet.decrypt(value.encode())
                    already_encrypted += 1
                    continue  # already ciphertext, leave it alone
                except InvalidToken:
                    pass
                updates[col] = fernet.encrypt(value.encode()).decode()
                newly_encrypted += 1

            if updates:
                set_clause = ", ".join(f"{col} = :{col}" for col in updates)
                conn.execute(text(f"UPDATE tenant_connections SET {set_clause} WHERE id = :id"), {**updates, "id": row["id"]})

        print(f"tenant_connections: {len(rows)} rows scanned, {newly_encrypted} values encrypted, {already_encrypted} already encrypted")


if __name__ == "__main__":
    main()
