"""
Unit tests for the User model.

Fully offline: each test spins up a throwaway Flask app backed by a temporary
SQLite database file, so nothing touches production data or needs a real
database/API key.

Run:  pytest -q
"""

from datetime import date

import pytest
from flask import Flask
from sqlalchemy.exc import IntegrityError

import models
from models import db, User


@pytest.fixture
def app(tmp_path):
    """A minimal Flask app wired to a temporary SQLite database."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path}/test.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _dob_for_age(years: int) -> date:
    """Return a date of birth that makes someone exactly `years` old today."""
    today = date.today()
    return date(today.year - years, 1, 1)


def _make_user(email="patrick@example.com", password="hunter2-secret", age=30):
    user = User(
        email=email,
        name="Patrick",
        favorite_sports="NBA,NHL",
        referral_source="friend",
        date_of_birth=_dob_for_age(age),
    )
    user.set_password(password)
    return user


# ── Password hashing ─────────────────────────────────────────────────

class TestPasswordHashing:
    def test_password_is_not_stored_in_plain_text(self):
        user = _make_user(password="supersecret")
        assert user.password_hash is not None
        assert "supersecret" not in user.password_hash

    def test_check_password_accepts_correct_password(self):
        user = _make_user(password="supersecret")
        assert user.check_password("supersecret") is True

    def test_check_password_rejects_wrong_password(self):
        user = _make_user(password="supersecret")
        assert user.check_password("wrongpass") is False


# ── Age gate ─────────────────────────────────────────────────────────

class TestAgeGate:
    def test_age_is_computed_from_dob(self):
        assert _make_user(age=25).age == 25

    def test_of_age_user_passes_21_gate(self):
        assert _make_user(age=21).is_of_age(21) is True

    def test_underage_user_fails_21_gate(self):
        assert _make_user(age=20).is_of_age(21) is False

    def test_missing_birthday_fails_age_gate(self):
        # A user with no date_of_birth must never pass the gate (and must not
        # crash the age calculation).
        user = User(email="nobirthday@example.com")
        assert user.is_of_age(21) is False


@pytest.fixture
def fixed_today(monkeypatch):
    """Freeze date.today() inside models so age tests are deterministic."""
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 7, 1)

    monkeypatch.setattr(models, "date", _FixedDate)
    return date(2026, 7, 1)


class TestAgeAcrossBirthday:
    """Exercise the birthday-based +/- 1 correction in the age calculation."""

    def test_age_before_birthday_this_year(self, fixed_today):
        # Born Dec 25, 2001. As of Jul 1, 2026 the birthday hasn't happened yet
        # this year, so they are 24 (not 26 - 2001 = 25). This covers the
        # "hasn't had their birthday" branch the original tests never hit.
        user = User(date_of_birth=date(2001, 12, 25))
        assert user.age == 24

    def test_age_on_exact_birthday(self, fixed_today):
        # Birthday is exactly "today" — should count as having had it.
        user = User(date_of_birth=date(2001, 7, 1))
        assert user.age == 25

    def test_age_after_birthday_this_year(self, fixed_today):
        user = User(date_of_birth=date(2001, 1, 10))
        assert user.age == 25


class TestEmailNormalization:
    def test_email_is_lowercased_and_trimmed_on_assignment(self):
        user = User(email="  Patrick@Example.COM  ")
        assert user.email == "patrick@example.com"


# ── Persistence (saving / loading from the database) ─────────────────

class TestPersistence:
    def test_user_can_be_saved_and_loaded(self, app):
        user = _make_user(email="save@example.com")
        db.session.add(user)
        db.session.commit()

        loaded = db.session.query(User).filter_by(email="save@example.com").one()
        assert loaded.name == "Patrick"
        assert loaded.favorite_sports == "NBA,NHL"
        assert loaded.check_password("hunter2-secret") is True

    def test_email_verified_defaults_to_false(self, app):
        user = _make_user()
        db.session.add(user)
        db.session.commit()
        assert user.email_verified is False

    def test_duplicate_email_is_rejected(self, app):
        db.session.add(_make_user(email="dupe@example.com"))
        db.session.commit()

        db.session.add(_make_user(email="dupe@example.com"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
