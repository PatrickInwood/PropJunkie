"""
emails.py
=========
Transactional email for PropJunkie, sent via Resend (resend.com).

Design rules:
  - Sending must NEVER break the thing that triggered it. If the welcome
    email fails, the user is still signed up — we log the error and move on.
  - With no RESEND_API_KEY set (local dev, automated tests), we skip sending
    entirely and just return False, so nothing tries to hit the network.
"""

import os
import logging

import resend

logger = logging.getLogger("propjunkie.emails")


def _from_address() -> str:
    # Falls back to Resend's shared test sender, which only delivers to your
    # own Resend account email until you send from a verified domain.
    return os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")


def send_welcome_email(user) -> bool:
    """Send a welcome email to a newly registered user.

    Returns True if the email was handed off to Resend, False if it was
    skipped (no API key) or failed. Never raises.
    """
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        logger.info("RESEND_API_KEY not set — skipping welcome email for user %s", user.id)
        return False

    greeting = f"Hi {user.name}," if user.name else "Hi there,"
    html = f"""
    <div style="font-family: -apple-system, Segoe UI, sans-serif; color: #1a1a1a;">
      <h2>Welcome to PropJunkie 🏀</h2>
      <p>{greeting}</p>
      <p>Your account is ready. You can start analyzing player props right now —
      enter your projection, pick a game, and get instant probability analysis
      backed by live sportsbook lines and real player stats.</p>
      <p><a href="https://propjunkie.app/app"
            style="display:inline-block;padding:10px 20px;background:#c9a84c;
                   color:#0c0f0a;border-radius:8px;font-weight:600;">
        Open PropJunkie →</a></p>
      <p style="color:#666;font-size:13px;margin-top:24px;">
        You're receiving this because you created a PropJunkie account.</p>
    </div>
    """

    try:
        resend.api_key = api_key
        resend.Emails.send({
            "from": _from_address(),
            "to": [user.email],
            "subject": "Welcome to PropJunkie",
            "html": html,
        })
        logger.info("Welcome email sent for user %s", user.id)
        return True
    except Exception:
        # Log and swallow — a failed welcome email must not fail the signup.
        logger.exception("Failed to send welcome email for user %s", user.id)
        return False
