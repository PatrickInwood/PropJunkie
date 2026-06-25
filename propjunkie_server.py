"""
propjunkie_server.py
====================
Flask web server that exposes the PropJunkie API.

Endpoints:
  GET  /health              — health check
  GET  /events/<sport>      — list today's games for a sport
  POST /analyze-prop        — analyze a player prop
  POST /create-checkout     — create a Stripe checkout session (Phase 4)

Run locally:
  python propjunkie_server.py

Run in production (Railway):
  gunicorn propjunkie_server:app --bind 0.0.0.0:$PORT --workers 4
"""

import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from prop_engine import analyze_prop, claude_explain, get_events, scan_props

# Load .env file when running locally
load_dotenv()

app = Flask(__name__)
CORS(app)  # allows your website's frontend to call this API

# ─────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'PropJunkie API'})


# ─────────────────────────────────────────
# EVENTS — list today's games
# ─────────────────────────────────────────

@app.route('/events/<sport>', methods=['GET'])
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
# ─────────────────────────────────────────

@app.route('/analyze-prop', methods=['POST'])
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
            result['claude_take'] = claude_explain(result, style=style)

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────
# BATCH SCAN — scan multiple props at once
# ─────────────────────────────────────────

@app.route('/scan-props', methods=['POST'])
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
# STRIPE CHECKOUT (Phase 4 — add your
# Stripe key and price IDs when ready)
# ─────────────────────────────────────────

@app.route('/create-checkout', methods=['POST'])
def create_checkout():
    """
    POST /create-checkout
    Body: { "email": "user@example.com", "tier": "plus" }
    Returns: { "checkout_url": "https://checkout.stripe.com/..." }

    Requires: pip install stripe
              STRIPE_SECRET_KEY in .env
              STRIPE_PLUS_PRICE_ID in .env
    """
    try:
        import stripe
        stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

        data  = request.json or {}
        email = data.get('email')
        tier  = data.get('tier', 'plus')

        price_id_map = {
            'plus':  os.getenv('STRIPE_PLUS_PRICE_ID'),
            'sharp': os.getenv('STRIPE_SHARP_PRICE_ID'),
        }
        price_id = price_id_map.get(tier)

        if not price_id:
            return jsonify({'error': f'Unknown tier: {tier}'}), 400

        session = stripe.checkout.Session.create(
            customer_email       = email,
            payment_method_types = ['card'],
            line_items           = [{'price': price_id, 'quantity': 1}],
            mode                 = 'subscription',
            success_url          = os.getenv('SUCCESS_URL', 'https://yoursite.com/success'),
            cancel_url           = os.getenv('CANCEL_URL',  'https://yoursite.com/pricing'),
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
