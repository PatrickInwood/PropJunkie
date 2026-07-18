# Presence of this file puts the repo root on sys.path so tests can import
# prop_engine / propjunkie_server / models / forms / emails directly.
#
# It also sets safe defaults for the env vars the server reads at import time,
# so importing propjunkie_server during tests doesn't crash on the SECRET_KEY
# guard and uses an isolated throwaway SQLite database — never a real one.

import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.gettempdir(), "propjunkie_test.db"),
)

import pytest


@pytest.fixture
def client(monkeypatch):
    """A Flask test client backed by an isolated, freshly-reset database.

    Shared by the signup and login test modules. Rate limiting and CSRF are
    disabled (the test client can't carry a CSRF token), and the welcome email
    is stubbed out so no real mail is sent.
    """
    import propjunkie_server as srv
    from models import db, User

    srv.app.config["TESTING"] = True
    srv.app.config["WTF_CSRF_ENABLED"] = False
    # flask-limiter caches its enabled flag at startup, so setting the config
    # here wouldn't take effect — disable the limiter object directly. Otherwise
    # POSTs accumulate against the real rate limits across the whole test run.
    srv.limiter.enabled = False

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
