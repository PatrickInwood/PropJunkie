"""
tokens.py
=========
Secure, expiring links for password resets (and, next, email verification).

We use itsdangerous (ships with Flask) to create a signed, timestamped token.
"Signed" means the server can detect any tampering; "timestamped" lets us
expire it. Nothing is stored in the database — the token itself carries the
data, protected by the app's SECRET_KEY.

Single-use resets: we bake a fingerprint of the user's current password hash
into the reset token. The moment the password changes, that fingerprint no
longer matches, so the link (and any other outstanding reset links) stops
working — a used or stale reset link can't be replayed.
"""

import hashlib

from flask import current_app
from itsdangerous import URLSafeTimedSerializer, BadData

_RESET_SALT = "password-reset"
_VERIFY_SALT = "email-verify"


def _serializer() -> URLSafeTimedSerializer:
    # Called inside a request, so the app context (and SECRET_KEY) is available.
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def _password_fingerprint(user) -> str:
    # `or ""` guards against a passwordless account (e.g. a future OAuth login),
    # which would otherwise raise on .encode().
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def generate_reset_token(user) -> str:
    """Create a password-reset token for this user."""
    payload = {"uid": user.id, "pwh": _password_fingerprint(user)}
    return _serializer().dumps(payload, salt=_RESET_SALT)


def verify_reset_token(token: str, max_age: int = 3600):
    """Return the User for a valid, unexpired, unused reset token, else None.

    max_age is in seconds (default 1 hour).
    """
    from models import db, User

    try:
        data = _serializer().loads(token, salt=_RESET_SALT, max_age=max_age)
    except BadData:
        # Covers a bad signature, tampering, or an expired token.
        return None

    user = db.session.get(User, data.get("uid"))
    if user is not None and _password_fingerprint(user) == data.get("pwh"):
        return user
    return None


def generate_email_token(user) -> str:
    """Create an email-verification token for this user.

    Uses a different salt than reset tokens, so a token minted for one purpose
    can't be replayed for the other. No password fingerprint — verifying an
    already-verified email is harmless, so single-use isn't needed here.
    """
    return _serializer().dumps({"uid": user.id}, salt=_VERIFY_SALT)


def verify_email_token(token: str, max_age: int = 86400):
    """Return the User for a valid, unexpired verification token, else None.

    max_age defaults to 24 hours.
    """
    from models import db, User

    try:
        data = _serializer().loads(token, salt=_VERIFY_SALT, max_age=max_age)
    except BadData:
        return None
    return db.session.get(User, data.get("uid"))
