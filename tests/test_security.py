"""
Tests for the site's security posture: response headers, cookie hardening, and
the cross-site guard on the JSON API endpoints. Uses the shared `client` fixture.
"""

import propjunkie_server as srv


class TestSecurityHeaders:
    def test_core_headers_present(self, client):
        r = client.get("/")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"          # no clickjacking
        assert "strict-origin" in r.headers["Referrer-Policy"]
        assert "Permissions-Policy" in r.headers

    def test_csp_locks_down_sources(self, client):
        csp = client.get("/").headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp
        assert "form-action 'self'" in csp
        # the resources the pages genuinely need must still be allowed
        assert "https://fonts.gstatic.com" in csp
        assert "https://a.espncdn.com" in csp
        assert "https://midfield.mlbstatic.com" in csp   # player headshots

    def test_headers_on_json_endpoints_too(self, client):
        r = client.get("/model-record")
        assert r.headers["X-Content-Type-Options"] == "nosniff"


class TestCrossSitePostGuard:
    def test_foreign_origin_is_blocked(self, client):
        r = client.post("/generate-projection",
                        json={"player": "X", "market": "player_batter_hits",
                              "sport": "baseball_mlb"},
                        headers={"Origin": "https://evil-example.com"})
        assert r.status_code == 403

    def test_same_origin_is_allowed(self, client, monkeypatch):
        monkeypatch.setattr(srv, "generate_projection", lambda *a, **k: {
            "projection": 1.0, "games_used": 9, "recent_values": [1],
            "low_confidence": False, "reason": None})
        r = client.post("/generate-projection",
                        json={"player": "X", "market": "player_batter_hits",
                              "sport": "baseball_mlb"},
                        headers={"Origin": "http://localhost"})
        assert r.status_code == 200

    def test_no_origin_still_works(self, client, monkeypatch):
        # curl / server-to-server callers send no Origin — must not be blocked.
        monkeypatch.setattr(srv, "generate_projection", lambda *a, **k: {
            "projection": 1.0, "games_used": 9, "recent_values": [1],
            "low_confidence": False, "reason": None})
        r = client.post("/generate-projection",
                        json={"player": "X", "market": "player_batter_hits",
                              "sport": "baseball_mlb"})
        assert r.status_code == 200


class TestCookieAndDebugPosture:
    def test_session_cookie_hardened(self, client):
        assert srv.app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert srv.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_debug_never_defaults_on(self, client):
        assert srv.app.debug is False
