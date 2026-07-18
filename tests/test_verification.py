"""
Tests for email verification: the verify token, the /verify-email route, the
account-page status + resend button. Uses the shared `client` fixture.
"""

from datetime import date

import propjunkie_server as srv
from models import db, User
import tokens


def _make_user(email="verify@example.com", password="password123", verified=False):
    with srv.app.app_context():
        user = User.create_account(email=email, password=password, name="V", date_of_birth=date(1990, 1, 1))
        if verified:
            user.email_verified = True
            db.session.commit()


def _login(client):
    client.post("/login", data={"email": "verify@example.com", "password": "password123"})


def _verify_token():
    with srv.app.app_context():
        user = User.query.filter_by(email="verify@example.com").one()
        return tokens.generate_email_token(user)


def _reload():
    with srv.app.app_context():
        return User.query.filter_by(email="verify@example.com").one()


# ── Token ────────────────────────────────────────────────────────────

class TestVerifyToken:
    def test_round_trip(self, client):
        _make_user()
        token = _verify_token()
        with srv.app.app_context():
            user = User.query.filter_by(email="verify@example.com").one()
            assert tokens.verify_email_token(token).id == user.id

    def test_garbage_token_rejected(self, client):
        with srv.app.app_context():
            assert tokens.verify_email_token("garbage") is None

    def test_expired_token_rejected(self, client):
        _make_user()
        token = _verify_token()
        with srv.app.app_context():
            assert tokens.verify_email_token(token, max_age=-1) is None


# ── /verify-email/<token> ────────────────────────────────────────────

class TestVerifyEmailRoute:
    def test_new_user_starts_unverified(self, client):
        _make_user()
        assert _reload().email_verified is False

    def test_valid_link_verifies_the_email(self, client):
        _make_user()
        r = client.get(f"/verify-email/{_verify_token()}")
        assert r.status_code == 302
        assert _reload().email_verified is True

    def test_invalid_link_leaves_user_unverified(self, client):
        _make_user()
        client.get("/verify-email/bad-token")
        assert _reload().email_verified is False


# ── Account page status + resend ─────────────────────────────────────

class TestAccountVerificationUI:
    def test_unverified_shows_prompt(self, client):
        _make_user()
        _login(client)
        r = client.get("/account")
        assert b"Not verified yet" in r.data
        assert b"Resend verification email" in r.data

    def test_verified_hides_prompt(self, client):
        _make_user(verified=True)
        _login(client)
        r = client.get("/account")
        assert b"\xe2\x9c\x93 Verified" in r.data          # the "✓ Verified" badge
        assert b"Resend verification email" not in r.data


class TestResendVerification:
    def test_resend_sends_email(self, client, monkeypatch):
        _make_user()
        _login(client)
        sent = []
        monkeypatch.setattr(srv, "send_verification_email", lambda user, url: sent.append(url))
        r = client.post("/resend-verification")
        assert r.status_code == 302
        assert len(sent) == 1 and "/verify-email/" in sent[0]

    def test_resend_requires_login(self, client):
        assert client.post("/resend-verification").status_code == 302  # → login
