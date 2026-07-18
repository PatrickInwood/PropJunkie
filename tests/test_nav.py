"""
Tests that the auth links appear correctly in the main pages' navigation:
logged-out visitors see Log In / Sign Up; logged-in users see Account.
Uses the shared `client` fixture.
"""

from datetime import date

import propjunkie_server as srv
from models import User

PAGES = ["/", "/app", "/lines"]


def _login(client):
    with srv.app.app_context():
        User.create_account(email="nav@example.com", password="password123", name="N", date_of_birth=date(1990, 1, 1))
    client.post("/login", data={"email": "nav@example.com", "password": "password123"})


class TestLoggedOutNav:
    def test_pages_show_login_and_signup(self, client):
        for page in PAGES:
            r = client.get(page)
            assert r.status_code == 200, page
            assert b'href="/login"' in r.data, page
            assert b'href="/signup"' in r.data, page
            assert b'href="/account"' not in r.data, page


class TestLoggedInNav:
    def test_pages_show_account_not_login(self, client):
        _login(client)
        for page in PAGES:
            r = client.get(page)
            assert r.status_code == 200, page
            assert b'href="/account"' in r.data, page
            assert b'href="/login"' not in r.data, page
