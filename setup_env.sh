#!/usr/bin/env bash
# Sets up the single Command virtual environment (.venv) and installs
# everything in requirements.txt.
#
# Run it with:   bash setup_env.sh
# Do NOT paste this file's contents into the terminal.
#
# No "set -e" on purpose: each step checks for errors itself, so a failure
# prints a message instead of closing your terminal.

VENV_DIR=".venv"

cd "$(dirname "$0")" || exit 1

echo "=== Command environment setup ==="
echo "Python found: $(python3 --version 2>&1)"

if [ ! -f requirements.txt ]; then
    echo "ERROR: requirements.txt not found. Run this script from the project folder."
    exit 1
fi

if [ -x "$VENV_DIR/bin/python" ] && [ -x "$VENV_DIR/bin/pip" ]; then
    echo "Reusing existing virtual environment in $VENV_DIR"
else
    rm -rf "$VENV_DIR"
    echo "Creating virtual environment in $VENV_DIR ..."
    if ! python3 -m venv "$VENV_DIR"; then
        rm -rf "$VENV_DIR"
        echo "ERROR: could not create the virtual environment."
        echo "Try: sudo apt-get update && sudo apt-get install -y python3-venv"
        echo "Then run: bash setup_env.sh"
        exit 1
    fi
fi

PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

echo "Upgrading pip ..."
"$PYTHON" -m pip install --upgrade pip

echo "Installing: pypsa networkx plotly scikit-learn pandas numpy streamlit scipy joblib"
if ! "$PIP" install -r requirements.txt; then
    echo "ERROR: pip install failed. Scroll up to see which package caused it."
    exit 1
fi

echo ""
echo "Running import test ..."
if "$PYTHON" test_environment.py; then
    echo ""
    echo "Setup complete. Activate the environment in your terminal with:"
    echo "    source .venv/bin/activate"
else
    echo "ERROR: import test failed. See the messages above."
    exit 1
fi