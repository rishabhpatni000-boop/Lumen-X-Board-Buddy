#!/bin/bash
# ============================================================
#  Lumen — Auto-start on Pi boot (optional)
#  Creates a systemd service so the app starts automatically.
#  Usage: bash setup_autostart.sh
# ============================================================

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
USER="$(whoami)"

echo "Setting up auto-start for user: $USER"
echo "App directory: $APP_DIR"

sudo tee /etc/systemd/system/lumen.service > /dev/null << EOF
[Unit]
Description=Lumen — Classroom Whiteboard Assistant
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/app_pi.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable lumen.service
sudo systemctl start  lumen.service

echo ""
echo "============================================================"
echo "  Auto-start configured!"
echo "============================================================"
echo ""
echo "  The app will now start automatically on every reboot."
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status  lumen   # check status"
echo "    sudo systemctl stop    lumen   # stop"
echo "    sudo systemctl start   lumen   # start"
echo "    sudo systemctl restart lumen   # restart"
echo "    sudo journalctl -u lumen -f    # view logs"
echo ""
