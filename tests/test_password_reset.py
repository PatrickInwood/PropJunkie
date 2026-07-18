"""
Tests for password reset: the token machinery plus the /forgot-password and
/reset-password routes. Uses the shared `client` fixture (conftest.py).
"""

from datetime import date

import propjunkie_server as srv
from models import db, User
import tokens


def _make_user(email="reset@example.com", password="oldpassword1"):
    with srv.app.app_context():
        User.create_account(email=email, password=password, name="R", date_of_birth=date(1990, 1, 1))


def _token_for(email="reset@example.com"):
    with srv.app.app_context():
        user = User.query.filter_by(email=email).one()
        return tokens.generate_reset_token(user)


# ── Token machinery ──────────────────────────────────────────────────

class TestResetToken:
    def test_valid_token_round_trips(self, client):
        _make_user()
        with srv.app.app_context():
            user = User.query.filter_by(email="reset@example.com").one()
            token = tokens.generate_reset_token(user)
            assert tokens.verify_reset_token(token).id == user.id

    def test_tampered_token_rejected(self, client):
        with srv.app.app_context():
            assert tokens.verify_reset_token("not-a-real-token") is None

    def test_expired_token_rejected(self, client):
        _make_user()
        token = _token_for()
        with srv.app.app_context():
            assert tokens.verify_reset_token(token, max_age=-1) is None

    def test_token_is_single_use_after_password_change(self, client):
        _make_user()
        token = _token_for()
        with srv.app.app_context():
            user = User.query.filter_by(email="reset@example.com").one()
            user.set_password("somethingnew1")
            db.session.commit()
            # The fingerprint baked into the token no longer matches.
            assert tokens.verify_reset_token(token) is None


# ── /forgot-password ─────────────────────────────────────────────────

class TestForgotPassword:
    def test_get_renders(self, client):
        assert client.get("/forgot-password").status_code == 200

    def test_existing_email_sends_reset_link(self, client, monkeypatch):
        _make_user()
        sent = []
        monkeypatch.setattr(srv, "send_password_reset_email", lambda user, url: sent.append(url))
        r = client.post("/forgot-password", data={"email": "Reset@Example.com"})
        assert r.status_code == 200
        assert b"Check your email" in r.data
        assert len(sent) == 1 and "/reset-password/" in sent[0]

    def test_unknown_email_same_message_but_no_send(self, client, monkeypatch):
        sent = []
        monkeypatch.setattr(srv, "send_password_reset_email", lambda user, url: sent.append(url))
        r = client.post("/forgot-password", data={"email": "nobody@example.com"})
        assert r.status_code == 200
        assert b"Check your email" in r.data   # identical message → enumeration-safe
        assert sent == []                       # ...but nothing was actually sent


# ── /reset-password/<token> ──────────────────────────────────────────

class TestResetPassword:
    def test_valid_token_shows_form(self, client):
        _make_user()
        r = client.get(f"/reset-password/{_token_for()}")
        assert r.status_code == 200
        assert b"Set a new password" in r.data

    def test_invalid_token_shows_error(self, client):
        r = client.get("/reset-password/bogus-token")
        assert r.status_code == 200
        assert b"Link expired" in r.data

    def test_reset_updates_the_password(self, client):
        _make_user(password="oldpassword1")
        r = client.post(
            f"/reset-password/{_token_for()}",
            data={"password": "newpassword1", "confirm_password": "newpassword1"},
        )
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/login")
        with srv.app.app_context():
            user = User.query.filter_by(email="reset@example.com").one()
            assert user.check_password("newpassword1")
            assert not user.check_password("oldpassword1")

    def test_used_link_cannot_be_reused(self, client):
        _make_user(password="oldpassword1")
        token = _token_for()
        client.post(
            f"/reset-password/{token}",
            data={"password": "newpassword1", "confirm_password": "newpassword1"},
        )
        # Same link again → rejected (password hash has changed).
        assert b"Link expired" in client.get(f"/reset-password/{token}").data

    def test_mismatched_passwords_rejected(self, client):
        _make_user()
        r = client.post(
            f"/reset-password/{_token_for()}",
            data={"password": "newpassword1", "confirm_password": "different1"},
        )
        assert r.status_code == 200
        assert b"match" in r.data

    def test_post_with_invalid_token_cannot_reset(self, client):
        # POST is the path that actually changes a password — a bad/expired
        # token there must be refused, and the old password must still work.
        _make_user(password="oldpassword1")
        r = client.post(
            "/reset-password/bogus-token",
            data={"password": "newpassword1", "confirm_password": "newpassword1"},
        )
        assert b"Link expired" in r.data
        with srv.app.app_context():
            user = User.query.filter_by(email="reset@example.com").one()
            assert user.check_password("oldpassword1")   # unchanged

    def test_success_flash_shows_on_login(self, client):
        _make_user()
        client.post(
            f"/reset-password/{_token_for()}",
            data={"password": "newpassword1", "confirm_password": "newpassword1"},
        )
        assert b"password has been updated" in client.get("/login").data
