"""
propjunkie_server.py
====================
Flask web server that exposes the PropJunkie API.

Endpoints:
  GET  /                      — landing page
  GET  /app                   — prop analyzer UI
  GET  /lines                 — live lines browser (ML, spreads, totals)
  GET  /health                — health check
  GET  /events/<sport>        — list today's games for a sport
  GET  /game-lines/<sport>    — ML, spread, total odds for upcoming games
  POST /analyze-prop          — analyze a player prop
  POST /scan-props            — batch scan props
  POST /create-checkout       — create a Stripe checkout session (Phase 4)

Run locally:
  python propjunkie_server.py

Run in production (Railway):
  gunicorn propjunkie_server:app --bind 0.0.0.0:$PORT --workers 4
"""

import os
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from prop_engine import analyze_prop, claude_explain, get_events, scan_props, get_game_lines

# Load .env file when running locally
load_dotenv()

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────
# RATE LIMITING
# 60 requests/minute globally per IP
# Analyze-prop capped at 5/minute (free tier protection)
# ─────────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)


# ─────────────────────────────────────────
# PAGES
# ─────────────────────────────────────────

@app.route('/', methods=['GET'])
@limiter.exempt
def landing():
    return render_template('landing.html')


@app.route('/app', methods=['GET'])
@limiter.exempt
def app_page():
    return render_template('index.html')


@app.route('/lines', methods=['GET'])
@limiter.exempt
def lines_page():
    return render_template('lines.html')


# ─────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────

@app.route('/health', methods=['GET'])
@limiter.exempt
def health():
    return jsonify({'status': 'ok', 'service': 'PropJunkie API'})


# ─────────────────────────────────────────
# GAME LINES — ML, spreads, totals
# ─────────────────────────────────────────

@app.route('/game-lines/<sport>', methods=['GET'])
@limiter.limit("20 per minute")
def game_lines(sport):
    """
    GET /game-lines/basketball_nba
    Returns moneyline, spread, and totals for upcoming games.
    """
    try:
        data = get_game_lines(sport)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# EVENTS — list today's games
# ─────────────────────────────────────────

@app.route('/events/<sport>', methods=['GET'])
@limiter.limit("30 per minute")
def events(sport):
    """
    GET /events/basketball_nba
    GET /events/americanfootball_nfl
    GET /events/baseball_mlb
    GET /events/icehockey_nhl
    """
    try:
        data = get_events(sport)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# ANALYZE PROP — core endpoint
# Rate limited to 5/minute per IP (free tier)
# ─────────────────────────────────────────

@app.route('/analyze-prop', methods=['POST'])
@limiter.limit("5 per minute; 20 per hour")
def analyze():
    """
    POST /analyze-prop
    Body (JSON):
    {
        "player":      "LeBron James",
        "projection":  26.4,
        "market":      "player_points",
        "sport":       "basketball_nba",
        "event_id":    "abc123",
        "user_email":  "user@example.com",   (optional, for subscription check later)
        "style":       "sharp"               (optional: sharp / casual / detailed)
    }
    """
    data = request.json or {}

    player     = data.get('player')
    projection = data.get('projection')
    market     = data.get('market', 'player_points')
    sport      = data.get('sport', 'basketball_nba')
    event_id   = data.get('event_id')
    style      = data.get('style', 'sharp')
    home_team  = data.get('home_team')
    away_team  = data.get('away_team')

    # Validate required fields
    if not player or projection is None or not event_id:
        return jsonify({'error': 'Required fields: player, projection, event_id'}), 400

    try:
        projection = float(projection)
    except (TypeError, ValueError):
        return jsonify({'error': 'projection must be a number'}), 400

    try:
        result = analyze_prop(
            player_name = player,
            projection  = projection,
            market_key  = market,
            sport_key   = sport,
            event_id    = event_id,
        )

        if 'error' not in result:
            result['claude_take'] = claude_explain(
                result, style=style,
                home_team=home_team, away_team=away_team
            )

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# BATCH SCAN — scan multiple props at once
# ─────────────────────────────────────────

@app.route('/scan-props', methods=['POST'])
@limiter.limit("2 per minute; 10 per hour")
def scan():
    """
    POST /scan-props
    Body (JSON):
    {
        "sport":    "basketball_nba",
        "event_id": "abc123",
        "min_edge": 0.03,
        "props": [
            {"player": "LeBron James",  "projection": 26.4, "market": "player_points"},
            {"player": "Anthony Davis", "projection": 11.2, "market": "player_rebounds"}
        ]
    }
    """
    data     = request.json or {}
    sport    = data.get('sport', 'basketball_nba')
    event_id = data.get('event_id')
    min_edge = data.get('min_edge', 0.03)
    props    = data.get('props', [])

    if not event_id or not props:
        return jsonify({'error': 'Required: event_id and props array'}), 400

    try:
        results = scan_props(props, sport, event_id, min_edge=min_edge)
        return jsonify({'results': results, 'count': len(results)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# STRIPE CHECKOUT (Phase 4)
# ─────────────────────────────────────────

@app.route('/create-checkout', methods=['POST'])
@limiter.limit("10 per hour")
def create_checkout():
    """
    POST /create-checkout
    Body: { "email": "user@example.com", "tier": "pro" }
    Returns: { "checkout_url": "https://checkout.stripe.com/..." }

    Requires: pip install stripe
              STRIPE_SECRET_KEY in .env
              STRIPE_PRO_PRICE_ID in .env
    """
    try:
        import stripe
        stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

        data  = request.json or {}
        email = data.get('email')
        tier  = data.get('tier', 'pro')

        price_id_map = {
            'pro': os.getenv('STRIPE_PRO_PRICE_ID'),
        }
        price_id = price_id_map.get(tier)

        if not price_id:
            return jsonify({'error': f'Unknown tier: {tier}'}), 400

        session = stripe.checkout.Session.create(
            customer_email       = email,
            payment_method_types = ['card'],
            line_items           = [{'price': price_id, 'quantity': 1}],
            mode                 = 'subscription',
            success_url          = os.getenv('SUCCESS_URL', 'https://propjunkie-production.up.railway.app/app'),
            cancel_url           = os.getenv('CANCEL_URL',  'https://propjunkie-production.up.railway.app/#pricing'),
        )
        return jsonify({'checkout_url': session.url})

    except ImportError:
        return jsonify({'error': 'Stripe not installed. Run: pip install stripe'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# RUN
# ─────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"PropJunkie API running on http://localhost:{port}")
    app.run(port=port, debug=True)
