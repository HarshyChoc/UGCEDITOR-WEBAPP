#!/bin/bash
# Activate virtual environment and run the app
cd "$(dirname "$0")"
if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
else
  echo "No virtualenv found at .venv/ or venv/."
  exit 1
fi
python app.py
