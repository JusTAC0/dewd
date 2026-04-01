#!/usr/bin/env bash
# DEWD Setup Script
# Run once: bash setup.sh
set -e

DEWD_DIR="$HOME/.local/share/dewd"
VOICES_DIR="$DEWD_DIR/voices"
PIPER_DIR="$DEWD_DIR/piper"
PIPER_VOICE="en_GB-alan-medium"
PIPER_VERSION="2023.11.14-2"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DEWD Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── System packages ──────────────────────────────────────────────────────────
echo "[1/4] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq portaudio19-dev libportaudio2 ffmpeg alsa-utils

# ── Python packages ──────────────────────────────────────────────────────────
echo "[2/4] Installing Python packages..."
pip3 install -q --break-system-packages -r "$(dirname "$0")/requirements.txt"

# ── Piper TTS binary (arm64) ─────────────────────────────────────────────────
echo "[3/4] Installing Piper TTS..."
mkdir -p "$PIPER_DIR" "$VOICES_DIR"

if [ ! -f "$PIPER_DIR/piper" ]; then
    TMP=$(mktemp -d)
    PIPER_URL="https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_aarch64.tar.gz"
    echo "  Downloading Piper binary..."
    wget -q --show-progress -O "$TMP/piper.tar.gz" "$PIPER_URL"
    tar -xzf "$TMP/piper.tar.gz" -C "$PIPER_DIR" --strip-components=1
    rm -rf "$TMP"
    chmod +x "$PIPER_DIR/piper"
    echo "  Piper installed at $PIPER_DIR/piper"
else
    echo "  Piper already installed — skipping."
fi

# ── Piper voice model ────────────────────────────────────────────────────────
echo "[4/4] Downloading voice model ($PIPER_VOICE)..."
HF_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium"

if [ ! -f "$VOICES_DIR/${PIPER_VOICE}.onnx" ]; then
    wget -q --show-progress -O "$VOICES_DIR/${PIPER_VOICE}.onnx" \
        "${HF_BASE}/${PIPER_VOICE}.onnx"
    wget -q --show-progress -O "$VOICES_DIR/${PIPER_VOICE}.onnx.json" \
        "${HF_BASE}/${PIPER_VOICE}.onnx.json"
    echo "  Voice model downloaded."
else
    echo "  Voice model already present — skipping."
fi

# ── Create data dir ──────────────────────────────────────────────────────────
mkdir -p "$(dirname "$0")/data"
echo '{"state":"standby","ts":""}' > "$(dirname "$0")/data/status.json"
echo '[]' > "$(dirname "$0")/data/conversation.json"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete."
echo "  Start DEWD:  python3 $(dirname "$0")/dewd_web.py"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
