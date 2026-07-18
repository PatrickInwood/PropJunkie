"""
End-to-end tests for the /signup route.

These drive the real Flask app through its test client (a fake browser), so
they cover the whole flow: form validation, account creation, password hashing,
auto-login, and the welcome-email hand-off. External email is monkeypatched,
and the app is pointed at an isolated SQLite database (see conftest.py).
"""

from datetime import date

import pytest

import propjunkie_server as srv
from models import db, User


VALID = {
    "email": "New.User@Example.com",
    "password": "supersecret1",
    "confirm_password": "supersecret1",
    "name": "New User",
    "date_of_birth": "1990-05-05",
    "favorite_sports": ["NBA", "NHL"],
    "referral_source": "friend",
}


@pytest.fixture
def client(monkeypatch):
    srv.app.config["TESTING"] = True
    srv.app.config["WTF_CSRF_ENABLED"] = False   # CSRF token isn't sent by the test client
    srv.app.config["RATELIMIT_ENABLED"] = False  # don't rate-limit rapid test requests

    # Record welcome-email attempts instead of sending real mail.
    sent = []
    monkeypatch.setattr(srv, "send_welcome_email", lambda user: sent.append(user.email))

    with srv.app.app_context():
        db.create_all()
        db.session.query(User).delete()
        db.session.commit()

    test_client = srv.app.test_client()
    test_client.sent = sent
    yield test_client

    with srv.app.app_context():
        db.session.query(User).delete()
        db.session.commit()


def _post(client, **overrides):
    data = dict(VALID)
    data.update(overrides)
    return client.post("/signup", data=data)


def test_get_signup_page_renders(client):
    r = client.get("/signup")
    assert r.status_code == 200
    assert b"Create your account" in r.data


def test_valid_signup_creates_user_hashes_password_and_logs_in(client):
    r = _post(client)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/app")

    with srv.app.app_context():
        user = db.session.query(User).filter_by(email="new.user@example.com").one()
        assert user.name == "New User"
        assert user.favorite_sports == "NBA,NHL"
        assert user.referral_source == "friend"
        assert user.check_password("supersecret1") is True
        assert "supersecret1" not in user.password_hash  # stored hashed, not raw

    # Welcome email was attempted (with the normalized address).
    assert client.sent == ["new.user@example.com"]

    # Auto-logged-in: visiting /signup again bounces a logged-in user to the app.
    assert client.get("/signup").status_code == 302


def test_underage_signup_rejected(client):
    dob = date(date.today().year - 18, 1, 1).isoformat()  # 18 years old, under the 21 gate
    r = _post(client, date_of_birth=dob)
    assert r.status_code == 200
    assert b"at least 21" in r.data
    with srv.app.app_context():
        assert db.session.query(User).count() == 0


def test_duplicate_email_rejected_case_insensitively(client):
    with srv.app.app_context():
        User.create_account(
            email="new.user@example.com",
            password="supersecret1",
            date_of_birth=date(1990, 1, 1),
        )
    # Same email, different capitalization — must still be treated as taken.
    r = _post(client, email="New.User@Example.com")
    assert r.status_code == 200
    assert b"already exists" in r.data
    with srv.app.app_context():
        assert db.session.query(User).filter_by(email="new.user@example.com").count() == 1


def test_password_mismatch_rejected(client):
    r = _post(client, confirm_password="different-password")
    assert r.status_code == 200
    assert b"match" in r.data
    with srv.app.app_context():
        assert db.session.query(User).count() == 0


def test_short_password_rejected(client):
    r = _post(client, password="short", confirm_password="short")
    assert r.status_code == 200
    assert b"between 8 and 128 characters" in r.data
    with srv.app.app_context():
        assert db.session.query(User).count() == 0


def test_blank_date_of_birth_rejected(client):
    r = _post(client, date_of_birth="")
    assert r.status_code == 200
    with srv.app.app_context():
        assert db.session.query(User).count() == 0


def test_invalid_favorite_sport_rejected(client):
    # WTForms' choice validation should reject a sport not in our list.
    r = _post(client, favorite_sports=["CRICKET"])
    assert r.status_code == 200
    with srv.app.app_context():
        assert db.session.query(User).count() == 0


def test_logout_ends_the_session(client):
    _post(client)                              # sign up → logged in
    assert client.get("/signup").status_code == 302   # confirm logged in (bounced)

    r = client.post("/logout")                 # POST logout (CSRF disabled in tests)
    assert r.status_code == 302

    # Logged out now: /signup renders the form again instead of redirecting.
    assert client.get("/signup").status_code == 200


def test_account_page_requires_login(client):
    # Not logged in → Flask-Login blocks access.
    assert client.get("/account").status_code == 401


def test_account_page_shows_email_when_logged_in(client):
    _post(client)  # sign up → logged in
    r = client.get("/account")
    assert r.status_code == 200
    assert b"new.user@example.com" in r.data
