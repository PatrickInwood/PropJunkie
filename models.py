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

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import validates
from werkzeug.security import generate_password_hash, check_password_hash


db = SQLAlchemy()


class User(UserMixin, db.Model):
    """A registered PropJunkie user.

    UserMixin adds the properties Flask-Login needs to track a logged-in
    session (is_authenticated, get_id, etc.) — no extra code required.
    """

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

    # ── Account creation ─────────────────────────────────────────────
    @classmethod
    def create_account(
        cls,
        *,
        email: str,
        password: str,
        date_of_birth,
        name: str = None,
        favorite_sports: str = None,
        referral_source: str = None,
    ) -> "User":
        """Create, hash-protect, and persist a new user, then return it.

        Email is normalized automatically (see the email validator). Assumes
        the caller has already validated the input (age, password rules, etc.).
        """
        user = cls(
            email=email,
            name=name,
            favorite_sports=favorite_sports,
            referral_source=referral_source,
            date_of_birth=date_of_birth,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user

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


class Pick(db.Model):
    """A model 'lean' PropJunkie published, recorded so we can grade our accuracy.

    One row per (game, market). We snapshot the lean when it's first flagged and
    freeze it, then fill in the result once the game finishes — that's how the
    Slate can honestly show a hit-rate instead of just claiming one.
    """

    __tablename__ = "picks"

    id = db.Column(db.Integer, primary_key=True)

    # What game / market this lean is on. game_id is the ESPN event id, which is
    # also what our lines use — so grading is an exact id lookup.
    game_id = db.Column(db.String(64), nullable=False, index=True)
    sport = db.Column(db.String(40), nullable=False, index=True)
    market = db.Column(db.String(16), nullable=False)   # 'h2h' | 'totals'
    commence_time = db.Column(db.DateTime(timezone=True))
    home_team = db.Column(db.String(80))
    away_team = db.Column(db.String(80))

    # The lean itself, frozen at first flag.
    pick = db.Column(db.String(80))          # human label, e.g. "Under 9.0"
    side = db.Column(db.String(8))           # 'over'|'under'|'home'|'away' (for grading)
    line = db.Column(db.Float)               # totals line (null for moneyline)
    model_value = db.Column(db.Float)        # projected total, or model win prob
    edge = db.Column(db.Float)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Result, filled in once the game is final.
    graded = db.Column(db.Boolean, default=False, nullable=False, index=True)
    result = db.Column(db.String(8))         # 'win' | 'loss' | 'push'
    home_score = db.Column(db.Integer)
    away_score = db.Column(db.Integer)
    graded_at = db.Column(db.DateTime(timezone=True))

    # One lean per game+market — snapshotting is idempotent.
    __table_args__ = (
        db.UniqueConstraint("game_id", "market", name="uix_pick_game_market"),
    )

    def grade(self, home_score: int, away_score: int) -> str:
        """Return 'win' / 'loss' / 'push' for this lean given the final score."""
        if self.market == "totals":
            total = home_score + away_score
            if self.line is not None and total == self.line:
                return "push"
            went_over = total > (self.line or 0)
            won = went_over if self.side == "over" else not went_over
        else:  # h2h — no ties in the sports we cover
            home_won = home_score > away_score
            won = home_won if self.side == "home" else not home_won
        return "win" if won else "loss"

    def __repr__(self) -> str:
        return f"<Pick {self.sport} {self.market} {self.pick} result={self.result}>"
