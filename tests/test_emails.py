"""
Unit tests for the welcome-email helper.

No real emails are sent — Resend's send call is monkeypatched.
"""

import emails


class DummyUser:
    id = 1
    email = "al@example.com"
    name = "Al"


def test_skips_when_no_api_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    # No key configured → returns False and never touches the network.
    assert emails.send_welcome_email(DummyUser()) is False


def test_sends_with_correct_fields(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "noreply@propjunkie.app")

    captured = {}

    def fake_send(payload):
        captured.update(payload)
        return {"id": "email_123"}

    monkeypatch.setattr(emails.resend.Emails, "send", staticmethod(fake_send))

    assert emails.send_welcome_email(DummyUser()) is True
    assert captured["to"] == ["al@example.com"]
    assert captured["from"] == "noreply@propjunkie.app"
    assert "Welcome" in captured["subject"]


def test_send_failure_never_raises(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")

    def boom(payload):
        raise RuntimeError("network down")

    monkeypatch.setattr(emails.resend.Emails, "send", staticmethod(boom))

    # A failed email must not raise — signup must never break on email.
    assert emails.send_welcome_email(DummyUser()) is False
