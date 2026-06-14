#!/bin/bash
set -e

echo "=== Lumen Setup ==="

# Check for Homebrew
if ! command -v brew &>/dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Install Tesseract (for OCR)
if ! command -v tesseract &>/dev/null; then
    echo "Installing Tesseract OCR engine..."
    brew install tesseract
else
    echo "Tesseract already installed: $(tesseract --version | head -1)"
fi

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate and install dependencies
echo "Installing Python dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "=== Setup complete ==="
echo "To run the app: ./run.sh"
