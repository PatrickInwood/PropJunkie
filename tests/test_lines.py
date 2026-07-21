"""
Tests for the game-data endpoints' server-side caching. These caches exist to
conserve the Odds API's limited free-tier quota: many viewers polling scores
must collapse to a single upstream API call per TTL window.
Uses the shared `client` fixture.
"""

import propjunkie_server as srv


class TestScoresCache:
    def test_repeat_requests_hit_api_once(self, client, monkeypatch):
        srv._scores_cache.clear()
        calls = {"n": 0}

        def fake(sport, days_from=1):
            calls["n"] += 1
            return [{"id": "g1", "completed": False,
                     "scores": [{"name": "A", "score": "3"}, {"name": "B", "score": "2"}]}]

        monkeypatch.setattr(srv, "get_game_scores", fake)
        r1 = client.get("/scores/baseball_mlb?daysFrom=1")
        r2 = client.get("/scores/baseball_mlb?daysFrom=1")
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.get_json() == r2.get_json()
        assert calls["n"] == 1   # second request served from cache

    def test_distinct_daysfrom_are_cached_separately(self, client, monkeypatch):
        srv._scores_cache.clear()
        calls = {"n": 0}

        def fake(sport, days_from=1):
            calls["n"] += 1
            return []

        monkeypatch.setattr(srv, "get_game_scores", fake)
        client.get("/scores/baseball_mlb?daysFrom=1")
        client.get("/scores/baseball_mlb?daysFrom=3")
        assert calls["n"] == 2   # different keys → not shared


class TestSlatePage:
    def test_slate_renders_with_market_tabs(self, client):
        r = client.get("/slate")
        assert r.status_code == 200
        # The three market tabs and the sport selector must be present.
        for needle in (b'data-market="h2h"', b'data-market="spreads"',
                       b'data-market="totals"', b'Daily Slate'):
            assert needle in r.data

    def test_slate_has_nav_links(self, client):
        r = client.get("/slate")
        assert b'href="/lines"' in r.data
        assert b'href="/app"' in r.data


class TestPropBoard:
    def test_props_page_renders(self, client):
        r = client.get("/props")
        assert r.status_code == 200
        assert b"AI Player Prop Predictions" in r.data
        assert b'data-sport="baseball_mlb"' in r.data
        assert b'href="/record"' in r.data

    def test_prop_predictions_cached(self, client, monkeypatch):
        import propjunkie_server as srv
        srv._prop_board_cache.clear()
        calls = {"n": 0}

        def fake(sport):
            calls["n"] += 1
            return [{"player": "Ace", "projection": 7.0, "market": "Strikeouts",
                     "game_id": "g", "matchup": "A vs B", "commence_time": "2026-07-20T23:00Z",
                     "recent": [7, 8], "l5_avg": 7.5, "l10_avg": 7.5, "games_used": 10,
                     "low_confidence": False, "role": "SP", "era": 3.0}]

        monkeypatch.setattr(srv, "generate_prop_board", fake)
        r1 = client.get("/prop-predictions/baseball_mlb")
        r2 = client.get("/prop-predictions/baseball_mlb")
        assert r1.status_code == 200 and r2.status_code == 200
        assert calls["n"] == 1   # second request served from cache


class TestLinesCache:
    def test_repeat_requests_hit_api_once(self, client, monkeypatch):
        srv._lines_cache.clear()
        calls = {"n": 0}

        def fake(sport):
            calls["n"] += 1
            return [{"id": "g1", "home_team": "A", "away_team": "B"}]

        monkeypatch.setattr(srv, "get_game_lines", fake)
        client.get("/game-lines/basketball_nba")
        client.get("/game-lines/basketball_nba")
        assert calls["n"] == 1
