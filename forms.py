"""
forms.py
========
Web forms for PropJunkie, built with Flask-WTF.

Flask-WTF gives us two things for free:
  1. Validation — each field lists rules (required, valid email, min length),
     and the form reports clear errors when they're not met.
  2. CSRF protection — a hidden token that stops another website from
     submitting this form on a logged-in user's behalf. (CSRF = Cross-Site
     Request Forgery.)
"""

import os
from datetime import date

from flask_wtf import FlaskForm
from wtforms import (
    StringField,
    PasswordField,
    DateField,
    SelectField,
    SelectMultipleField,
    BooleanField,
    SubmitField,
)
from wtforms.validators import DataRequired, Email, Length, EqualTo, ValidationError

from models import User


SPORT_CHOICES = [("NBA", "NBA"), ("NFL", "NFL"), ("MLB", "MLB"), ("NHL", "NHL")]

REFERRAL_CHOICES = [
    ("", "How did you hear about us?"),
    ("search", "Search engine"),
    ("social", "Social media"),
    ("friend", "Friend / word of mouth"),
    ("ad", "Advertisement"),
    ("other", "Other"),
]


def _min_age() -> int:
    """The legal minimum signup age, from the MIN_AGE env var (default 21)."""
    try:
        return int(os.getenv("MIN_AGE", "21"))
    except ValueError:
        return 21


class SignupForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(),
            # Cap the length too: hashing is deliberately slow, so a giant
            # password would waste server CPU.
            Length(min=8, max=128, message="Password must be between 8 and 128 characters."),
        ],
    )
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    name = StringField("Name")
    favorite_sports = SelectMultipleField("Favorite sports", choices=SPORT_CHOICES)
    referral_source = SelectField("How did you hear about us?", choices=REFERRAL_CHOICES)
    date_of_birth = DateField("Date of birth", validators=[DataRequired()])
    submit = SubmitField("Create account")

    # ── Custom validators ────────────────────────────────────────────
    # WTForms auto-runs any method named validate_<fieldname>.

    def validate_email(self, field):
        """Reject an email that already has an account (case-insensitive)."""
        normalized = User.normalize_email(field.data)
        if User.query.filter_by(email=normalized).first() is not None:
            raise ValidationError("An account with this email already exists.")

    def validate_date_of_birth(self, field):
        """Enforce the age gate."""
        if field.data is None:
            raise ValidationError("Please enter your date of birth.")
        if field.data > date.today():
            raise ValidationError("Date of birth can't be in the future.")
        # Reuse the model's age logic so there's one source of truth.
        if not User(date_of_birth=field.data).is_of_age(_min_age()):
            raise ValidationError(f"You must be at least {_min_age()} to sign up.")


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Log in")


class ForgotPasswordForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Send reset link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New password",
        validators=[
            DataRequired(),
            Length(min=8, max=128, message="Password must be between 8 and 128 characters."),
        ],
    )
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    submit = SubmitField("Set new password")


class LogoutForm(FlaskForm):
    """No fields — it exists only to carry a CSRF token for the logout button,
    so logging out can't be triggered by another site."""
    pass
