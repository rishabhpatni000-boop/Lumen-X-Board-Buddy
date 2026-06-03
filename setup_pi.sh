#!/bin/bash
# ============================================================
#  VisualAssistCam — Raspberry Pi 5 Setup Script
#  Run this ONCE after copying files to the Pi.
#  Usage: bash setup_pi.sh
# ============================================================
set -e

echo ""
echo "============================================================"
echo "  VisualAssistCam — Raspberry Pi 5 Setup"
echo "============================================================"

# ── 1. System packages ────────────────────────────────────────
echo ""
echo "[1/5] Installing system packages..."
sudo apt update -qq
sudo apt install -y \
    python3-venv \
    python3-pip \
    python3-dev \
    libatlas-base-dev \
    libopenblas-dev \
    libjpeg-dev \
    libpng-dev \
    libtiff-dev \
    libwebp-dev \
    tesseract-ocr \
    libssl-dev \
    libffi-dev \
    git

# ── 2. Python virtual environment ─────────────────────────────
echo ""
echo "[2/5] Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "  ✓ Virtual environment created."
else
    echo "  ✓ Virtual environment already exists."
fi

source venv/bin/activate

# ── 3. Python packages ─────────────────────────────────────────
echo ""
echo "[3/5] Installing Python packages (this takes a few minutes on Pi)..."
pip install --upgrade pip --quiet
pip install -r requirements_pi.txt

echo "  ✓ Python packages installed."

# ── 4. SSL certificate (self-signed, for HTTPS) ───────────────
echo ""
echo "[4/5] Generating self-signed SSL certificate for HTTPS..."
if [ ! -f "cert.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
        -days 3650 -nodes \
        -subj "/C=IN/ST=Maharashtra/L=Mumbai/O=VisualAssistCam/CN=raspberrypi.local" \
        2>/dev/null
    echo "  ✓ SSL certificate created (valid 10 years)."
else
    echo "  ✓ SSL certificate already exists."
fi

# ── 5. API key configuration ──────────────────────────────────
echo ""
echo "[5/5] Configuring API key..."
if [ ! -f ".env" ]; then
    echo "ANTHROPIC_API_KEY=paste-your-key-here" > .env
    echo ""
    echo "  ⚠  ACTION REQUIRED:"
    echo "     Edit the .env file and add your Anthropic API key:"
    echo "     nano .env"
    echo "     Replace 'paste-your-key-here' with your actual key (starts with sk-ant-)"
else
    echo "  ✓ .env file already exists."
fi

# ── Done ──────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo ""
echo "  NEXT STEPS:"
echo ""
echo "  1. Add your Anthropic API key:"
echo "     nano .env"
echo ""
echo "  2. Start the app:"
echo "     bash run_pi.sh"
echo ""
echo "  3. On first run, the app will print the URL."
echo "     Open it on any device on the same WiFi."
echo ""
echo "  OPTIONAL — Auto-start on boot:"
echo "     bash setup_autostart.sh"
echo ""
