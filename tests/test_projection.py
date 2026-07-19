"""
Tests for the projection engine — generating PropJunkie's own projection from a
player's recent games. The ESPN network call is mocked; the math is tested directly.
"""

import prop_engine as pe


# ── weighted_projection (pure math) ──────────────────────────────────

class TestWeightedProjection:
    def test_empty_returns_none(self):
        assert pe.weighted_projection([]) is None

    def test_single_value(self):
        assert pe.weighted_projection([27.0]) == 27.0

    def test_constant_values(self):
        assert pe.weighted_projection([20, 20, 20, 20]) == 20

    def test_weights_recent_games_more(self):
        # A hot most-recent game pulls the projection above the simple mean (50).
        assert pe.weighted_projection([0, 100]) > 50

    def test_order_matters_recent_dominates(self):
        # Same numbers, opposite order → the one ending high projects higher.
        ending_high = pe.weighted_projection([0, 100])
        ending_low = pe.weighted_projection([100, 0])
        assert ending_high > ending_low


# ── generate_projection (fetch mocked) ───────────────────────────────

class TestGenerateProjection:
    def test_projects_with_enough_games(self, monkeypatch):
        monkeypatch.setattr(pe, "fetch_recent_stat_values", lambda *a, **k: [10, 12, 14, 16, 18])
        result = pe.generate_projection("Someone", "player_points", "basketball_nba")
        assert result["projection"] is not None
        assert result["games_used"] == 5
        assert result["low_confidence"] is False
        assert result["reason"] is None

    def test_thin_sample_is_low_confidence(self, monkeypatch):
        monkeypatch.setattr(pe, "fetch_recent_stat_values", lambda *a, **k: [10, 12, 14])
        result = pe.generate_projection("Someone", "player_points", "basketball_nba")
        assert result["projection"] is not None
        assert result["low_confidence"] is True   # < 5 games

    def test_not_enough_games_returns_no_projection(self, monkeypatch):
        monkeypatch.setattr(pe, "fetch_recent_stat_values", lambda *a, **k: [10, 12])
        result = pe.generate_projection("Someone", "player_points", "basketball_nba")
        assert result["projection"] is None
        assert result["games_used"] == 2
        assert result["reason"]

    def test_no_data_returns_no_projection(self, monkeypatch):
        monkeypatch.setattr(pe, "fetch_recent_stat_values", lambda *a, **k: [])
        result = pe.generate_projection("Nobody", "player_points", "basketball_nba")
        assert result["projection"] is None
        assert result["games_used"] == 0


# ── fetch_recent_stat_values (ESPN network mocked) ───────────────────

class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _fake_espn_get(url, **kwargs):
    if "/athletes/" in url and url.endswith("/gamelog"):
        return _FakeResp(200, {
            "splits": {
                "categories": [{"name": "scoring", "names": ["points"]}],
                "entries": [
                    {"stats": ["20"]}, {"stats": ["22"]}, {"stats": ["30"]},
                    {"stats": ["18"]}, {"stats": ["26"]},
                ],
            }
        })
    if url.endswith("/athletes"):
        return _FakeResp(200, {"items": [{"id": 123, "displayName": "Test Player"}]})
    return _FakeResp(404, {})


class TestFetchRecentStatValues:
    def test_parses_gamelog_values(self, monkeypatch):
        monkeypatch.setattr(pe.requests, "get", _fake_espn_get)
        values = pe.fetch_recent_stat_values("Test Player", "player_points", "basketball_nba")
        assert values == [20.0, 22.0, 30.0, 18.0, 26.0]

    def test_unknown_athlete_returns_empty(self, monkeypatch):
        monkeypatch.setattr(pe.requests, "get", lambda url, **k: _FakeResp(200, {"items": []}))
        assert pe.fetch_recent_stat_values("Nobody", "player_points", "basketball_nba") == []

    def test_network_error_returns_empty(self, monkeypatch):
        def _boom(url, **k):
            raise pe.requests.exceptions.RequestException("down")
        monkeypatch.setattr(pe.requests, "get", _boom)
        assert pe.fetch_recent_stat_values("X", "player_points", "basketball_nba") == []

    def test_unsupported_market_returns_empty(self, monkeypatch):
        # Should short-circuit before any network call.
        assert pe.fetch_recent_stat_values("X", "not_a_market", "basketball_nba") == []


# ── MLB path via the official Stats API (network mocked) ─────────────

def _fake_mlb_get(url, **kwargs):
    if url.endswith("/people/search"):
        return _FakeResp(200, {"people": [{"id": 592450, "fullName": "Aaron Judge"}]})
    if url.endswith("/stats"):
        return _FakeResp(200, {"stats": [{"splits": [
            {"stat": {"hits": 1}}, {"stat": {"hits": 2}}, {"stat": {"hits": 0}},
            {"stat": {"hits": 3}}, {"stat": {"hits": 1}},
        ]}]})
    return _FakeResp(404, {})


class TestFetchMLBValues:
    def test_parses_statsapi_gamelog(self, monkeypatch):
        monkeypatch.setattr(pe.requests, "get", _fake_mlb_get)
        vals = pe.fetch_recent_stat_values("Aaron Judge", "player_batter_hits", "baseball_mlb")
        assert vals == [1.0, 2.0, 0.0, 3.0, 1.0]

    def test_unknown_player_returns_empty(self, monkeypatch):
        monkeypatch.setattr(pe.requests, "get", lambda url, **k: _FakeResp(200, {"people": []}))
        assert pe.fetch_recent_stat_values("Nobody", "player_batter_hits", "baseball_mlb") == []

    def test_unsupported_mlb_market_returns_empty(self):
        # Not in MLB_STAT_MAP → short-circuits before any network call.
        assert pe.fetch_recent_stat_values("X", "player_batter_doubles", "baseball_mlb") == []


# ── /generate-projection route ───────────────────────────────────────

class TestGenerateProjectionRoute:
    def test_missing_fields_returns_400(self, client):
        assert client.post("/generate-projection", json={"player": "X"}).status_code == 400

    def test_returns_generated_projection(self, client, monkeypatch):
        import propjunkie_server as srv
        monkeypatch.setattr(srv, "generate_projection", lambda *a, **k: {
            "projection": 1.2, "games_used": 8, "recent_values": [1, 2, 1],
            "low_confidence": False, "reason": None,
        })
        r = client.post("/generate-projection", json={
            "player": "Aaron Judge", "market": "player_batter_hits", "sport": "baseball_mlb",
        })
        assert r.status_code == 200
        assert r.get_json()["projection"] == 1.2
