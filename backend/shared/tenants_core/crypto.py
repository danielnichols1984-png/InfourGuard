"""Transparent at-rest encryption for stored OAuth secrets. A SQLAlchemy
TypeDecorator, not a schema change — the column stays TEXT (ciphertext is
just a longer string), and every existing call site that reads/writes
.access_token etc. keeps working unchanged, since encryption happens at
the ORM/DB boundary, not in application code.

IMPORTANT: existing rows were stored in plaintext before this was added.
Run encrypt_existing_tokens.py against a database BEFORE deploying code
that uses this — decrypting an already-plaintext value raises, so a
still-plaintext row would break the very first time it's read otherwise.
"""
from cryptography.fernet import Fernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from shared.tenants_core.config import settings

_fernet = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return _fernet.encrypt(value.encode()).decode()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return _fernet.decrypt(value.encode()).decode()
