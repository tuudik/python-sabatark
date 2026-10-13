"""Login token derivation matching the web UI's crypto.pbkdf2."""

import hashlib

ITERATIONS = 10000
KEY_LENGTH = 32
BLOCK_SIZE = 64


def normalize_email(email: str) -> str:
    """Return the e-mail as the web UI sends it: trimmed and lower-cased."""
    return email.strip().lower()


def derive_token(password: str, email: str) -> str:
    """Return the hex login token: PBKDF2-HMAC-SHA256 as the web UI's crypto.pbkdf2 computes it."""
    secret = password.encode()
    if len(password.encode("utf-16-le")) // 2 > BLOCK_SIZE:
        secret = hashlib.sha256(secret).digest()
    else:
        secret = secret[:BLOCK_SIZE]
    salt = normalize_email(email).encode()
    return hashlib.pbkdf2_hmac("sha256", secret, salt, ITERATIONS, KEY_LENGTH).hex()
