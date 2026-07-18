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


def _support_address() -> str:
    # The customer-support address shown in emails. Configurable via env var
    # so it can be changed without a code change / redeploy.
    return os.getenv("SUPPORT_EMAIL", "support@propjunkie.app")


def send_welcome_email(user) -> bool:
    """Send a welcome email to a newly registered user.

    Returns True if the email was handed off to Resend, False if it was
    skipped (no API key) or failed. Never raises.
    """
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        logger.info("RESEND_API_KEY not set — skipping welcome email for user %s", user.id)
        return False

    support = _support_address()
    name = (user.name or "").strip()
    hello = f"Welcome to PropJunkie, {name}" if name else "Welcome to PropJunkie"

    html = f"""\
<div style="background:#f4f1ea;padding:32px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #ececec;border-radius:14px;overflow:hidden;">
    <div style="background:#0c0f0a;padding:22px 32px;">
      <span style="font-size:22px;font-weight:800;color:#ffffff;">Prop<span style="color:#c9a84c;">Junkie</span></span>
    </div>
    <div style="padding:32px;color:#1a1a1a;line-height:1.6;">
      <h1 style="margin:0 0 14px;font-size:22px;">{hello} 🏀</h1>
      <p style="margin:0 0 16px;">Thanks for joining PropJunkie — we're glad to have you. You've got a genuine edge-finding engine in your corner now.</p>
      <p style="margin:0 0 8px;font-weight:600;">Here's what it does:</p>
      <ul style="margin:0 0 20px;padding-left:20px;color:#333;">
        <li style="margin-bottom:6px;">Turns your projection into a real hit probability — across NBA, NFL, MLB &amp; NHL.</li>
        <li style="margin-bottom:6px;">Compares your number to live sportsbook lines with the vig stripped out, so you see the <em>true</em> edge.</li>
        <li style="margin-bottom:6px;">Delivers instant AI breakdowns backed by real ESPN player stats.</li>
        <li style="margin-bottom:6px;">Stacks your picks into parlays with fair, no-vig odds.</li>
      </ul>
      <div style="text-align:center;margin:28px 0;">
        <a href="https://propjunkie.app/app" style="display:inline-block;padding:13px 28px;background:#c9a84c;color:#0c0f0a;border-radius:9px;font-weight:700;text-decoration:none;">Open PropJunkie →</a>
      </div>
      <p style="margin:0 0 4px;color:#333;">For questions, concerns, or issues with PropJunkie, please contact <a href="mailto:{support}" style="color:#a8893a;">{support}</a> for assistance. At PropJunkie, we listen to our customers' feedback and are constantly evolving.</p>
      <p style="margin:24px 0 0;font-size:12px;color:#888;border-top:1px solid #eee;padding-top:16px;">
        Bet with an edge — and always bet responsibly. Must be 21+. If gambling stops being fun, call 1-800-GAMBLER.<br>
        You're receiving this because you created a PropJunkie account.
      </p>
    </div>
  </div>
</div>"""

    text = f"""\
{hello}!

Thanks for joining PropJunkie — we're glad to have you.

Here's what it does:
- Turns your projection into a real hit probability across NBA, NFL, MLB & NHL
- Compares your number to live sportsbook lines with the vig stripped out, so you see the true edge
- Delivers instant AI breakdowns backed by real ESPN player stats
- Stacks your picks into parlays with fair, no-vig odds

Open PropJunkie: https://propjunkie.app/app

For questions, concerns, or issues with PropJunkie, please contact {support} for assistance. \
At PropJunkie, we listen to our customers' feedback and are constantly evolving.

Bet with an edge — and always bet responsibly. Must be 21+. If gambling stops being fun, call 1-800-GAMBLER.
You're receiving this because you created a PropJunkie account."""

    try:
        resend.api_key = api_key
        resend.Emails.send({
            "from": _from_address(),
            "to": [user.email],
            "subject": "Welcome to PropJunkie — you're in 🏀",
            "html": html,
            "text": text,
        })
        logger.info("Welcome email sent for user %s", user.id)
        return True
    except Exception:
        # Log and swallow — a failed welcome email must not fail the signup.
        logger.exception("Failed to send welcome email for user %s", user.id)
        return False


def send_password_reset_email(user, reset_url: str) -> bool:
    """Email a password-reset link. Never raises; returns False if skipped/failed."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        logger.info("RESEND_API_KEY not set — skipping reset email for user %s", user.id)
        return False

    html = f"""\
<div style="background:#f4f1ea;padding:32px 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #ececec;border-radius:14px;overflow:hidden;">
    <div style="background:#0c0f0a;padding:22px 32px;">
      <span style="font-size:22px;font-weight:800;color:#ffffff;">Prop<span style="color:#c9a84c;">Junkie</span></span>
    </div>
    <div style="padding:32px;color:#1a1a1a;line-height:1.6;">
      <h1 style="margin:0 0 14px;font-size:22px;">Reset your password</h1>
      <p style="margin:0 0 16px;">We received a request to reset the password for your PropJunkie account. Click the button below to choose a new one. This link expires in 1 hour.</p>
      <div style="text-align:center;margin:28px 0;">
        <a href="{reset_url}" style="display:inline-block;padding:13px 28px;background:#c9a84c;color:#0c0f0a;border-radius:9px;font-weight:700;text-decoration:none;">Reset password →</a>
      </div>
      <p style="margin:0;color:#666;font-size:13px;">If you didn't request this, you can safely ignore this email — your password won't change.</p>
    </div>
  </div>
</div>"""

    text = f"""\
Reset your PropJunkie password

We received a request to reset your password. Open this link to choose a new one (expires in 1 hour):

{reset_url}

If you didn't request this, ignore this email — your password won't change."""

    try:
        resend.api_key = api_key
        resend.Emails.send({
            "from": _from_address(),
            "to": [user.email],
            "subject": "Reset your PropJunkie password",
            "html": html,
            "text": text,
        })
        logger.info("Password reset email sent for user %s", user.id)
        return True
    except Exception:
        logger.exception("Failed to send password reset email for user %s", user.id)
        return False
