"""
models.py
=========
Database models for PropJunkie.

A "model" is a Python class that maps to a table in the database. One instance
of the class = one row in the table. We use SQLAlchemy, which lets us work with
Python objects instead of writing raw SQL by hand (and it safely escapes values,
which prevents a class of security bugs called SQL injection).

`db` is created here with no app attached. The Flask app calls `db.init_app(app)`
in propjunkie_server.py — this "app factory"-friendly split keeps the model layer
importable on its own (e.g. from tests) without spinning up the whole server.
"""

from datetime import date, datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import validates
from werkzeug.security import generate_password_hash, check_password_hash


db = SQLAlchemy()


class User(db.Model):
    """A registered PropJunkie user."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    # Login identity
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    # We NEVER store the raw password — only a one-way hash of it (see set_password).
    # 512 chars leaves generous headroom above werkzeug's current ~162-char hash.
    password_hash = db.Column(db.String(512), nullable=False)

    # Profile / signup data
    name = db.Column(db.String(120))
    favorite_sports = db.Column(db.String(255))   # comma-separated, e.g. "NBA,NHL"
    referral_source = db.Column(db.String(255))   # "how did you hear about us"
    date_of_birth = db.Column(db.Date, nullable=False)   # for the age gate

    # State
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    # timezone=True so PostgreSQL keeps the UTC offset instead of dropping it
    # (a plain DateTime column silently stores a naive value on Postgres).
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ── Email normalization ──────────────────────────────────────────
    @staticmethod
    def normalize_email(raw: str) -> str:
        """Standardize an email: trim spaces and lowercase it.

        Email addresses are effectively case-insensitive, so we store and
        look them up in one canonical form to avoid duplicate accounts and
        capitalization-dependent login failures.
        """
        return raw.strip().lower() if raw else raw

    @validates("email")
    def _validate_email(self, key, value):
        # Runs automatically whenever `user.email = ...` is assigned, so the
        # stored value is always normalized.
        return self.normalize_email(value)

    # ── Password handling ────────────────────────────────────────────
    def set_password(self, raw_password: str) -> None:
        """Hash and store a password. The raw text is never saved."""
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        """Return True if raw_password matches the stored hash."""
        return check_password_hash(self.password_hash, raw_password)

    # ── Age gate ─────────────────────────────────────────────────────
    @property
    def age(self) -> int:
        """Current age in whole years, computed from date_of_birth."""
        today = date.today()
        had_birthday = (today.month, today.day) >= (
            self.date_of_birth.month,
            self.date_of_birth.day,
        )
        return today.year - self.date_of_birth.year - (0 if had_birthday else 1)

    def is_of_age(self, minimum: int) -> bool:
        """True if the user is at least `minimum` years old.

        Returns False (not old enough) if the birthday is missing, so a blank
        date-of-birth field can never accidentally pass the age gate — and
        never crashes the age calculation.
        """
        if self.date_of_birth is None:
            return False
        return self.age >= minimum

    def __repr__(self) -> str:
        return f"<User {self.email}>"
