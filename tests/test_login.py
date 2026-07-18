"""
End-to-end tests for the /login route.

Uses the shared `client` fixture from conftest.py (isolated database, CSRF and
rate limiting disabled for testing).
"""

from datetime import date

import propjunkie_server as srv
from models import db, User


def _make_user(email="returning@example.com", password="mypassword1"):
    with srv.app.app_context():
        User.create_account(
            email=email,
            password=password,
            name="Ret",
            date_of_birth=date(1990, 1, 1),
        )


def test_get_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"Welcome back" in r.data


def test_valid_login_logs_in(client):
    _make_user()
    r = client.post("/login", data={"email": "returning@example.com", "password": "mypassword1"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/app")
    assert client.get("/account").status_code == 200   # now logged in


def test_login_email_is_case_insensitive(client):
    _make_user(email="returning@example.com")
    r = client.post("/login", data={"email": "Returning@Example.COM", "password": "mypassword1"})
    assert r.status_code == 302
    assert client.get("/account").status_code == 200


def test_wrong_password_rejected(client):
    _make_user()
    r = client.post("/login", data={"email": "returning@example.com", "password": "wrongpass"})
    assert r.status_code == 200
    assert b"Incorrect email or password" in r.data
    # Not logged in → /account bounces to login.
    assert client.get("/account").status_code == 302


def test_unknown_email_gives_same_generic_error(client):
    # No account exists — the error must match the wrong-password error exactly,
    # so an attacker can't tell which emails are registered.
    r = client.post("/login", data={"email": "nobody@example.com", "password": "whatever1"})
    assert r.status_code == 200
    assert b"Incorrect email or password" in r.data
    with srv.app.app_context():
        assert db.session.query(User).count() == 0


def test_already_logged_in_redirects_to_app(client):
    _make_user()
    client.post("/login", data={"email": "returning@example.com", "password": "mypassword1"})
    assert client.get("/login").status_code == 302


def test_safe_next_redirect_is_honored(client):
    _make_user()
    r = client.post(
        "/login",
        data={"email": "returning@example.com", "password": "mypassword1", "next": "/lines"},
    )
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/lines")


def test_open_redirect_is_blocked(client):
    _make_user()
    r = client.post(
        "/login",
        data={"email": "returning@example.com", "password": "mypassword1", "next": "http://evil.com/x"},
    )
    assert r.status_code == 302
    assert "evil.com" not in r.headers["Location"]
    assert r.headers["Location"].endswith("/app")
