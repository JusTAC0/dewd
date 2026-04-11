#!/usr/bin/env bash
# DEWD Setup Script
# Run once after cloning: bash setup.sh
set -e

DEWD_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DEWD Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Python packages ──────────────────────────────────────────────────────────
echo "[1/3] Installing Python packages..."
if [ -d "$DEWD_DIR/venv" ]; then
    source "$DEWD_DIR/venv/bin/activate"
    pip install -q -r "$DEWD_DIR/requirements.txt"
else
    python3 -m venv "$DEWD_DIR/venv"
    source "$DEWD_DIR/venv/bin/activate"
    pip install -q -r "$DEWD_DIR/requirements.txt"
fi

# ── Data directory ───────────────────────────────────────────────────────────
echo "[2/3] Creating data directory..."
mkdir -p "$DEWD_DIR/data/agents"
[ -f "$DEWD_DIR/data/status.json"       ] || echo '{"state":"standby","ts":""}' > "$DEWD_DIR/data/status.json"
[ -f "$DEWD_DIR/data/conversation.json" ] || echo '[]'                           > "$DEWD_DIR/data/conversation.json"
[ -f "$DEWD_DIR/data/calendar.json"     ] || echo '[]'                           > "$DEWD_DIR/data/calendar.json"

# ── Environment file ─────────────────────────────────────────────────────────
echo "[3/3] Checking environment..."
if [ ! -f "$DEWD_DIR/.env" ]; then
    cp "$DEWD_DIR/.env.example" "$DEWD_DIR/.env"
    chmod 600 "$DEWD_DIR/.env"
    echo "  Created .env from template — edit it and add your API keys before running."
else
    echo "  .env already exists — skipping."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete."
echo ""
echo "  Next steps:"
echo "  1. Edit .env and add your ANTHROPIC_API_KEY"
echo "  2. (Optional) Add Gmail, weather, and ntfy settings to .env"
echo "  3. (Optional) Copy known_services.example.txt to known_services.txt"
echo "     and document your system's expected services"
echo "  4. Run: source venv/bin/activate && python3 dewd_web.py"
echo ""
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
