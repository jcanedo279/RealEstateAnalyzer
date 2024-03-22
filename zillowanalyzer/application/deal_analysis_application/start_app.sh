#!/bin/bash

# Run 'chmod +x <path_to_start_app.sh>' to make this script executable.

# Find and kill the port in use
PID=$(lsof -ti:8000)
if [ ! -z "$PID" ]; then
  echo "Killing process on port 8000 with PID: $PID"
  kill -9 $PID
else
  echo "No process is using port 8000"
fi

# Directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Path to the virtual environment
VENV_PATH="${SCRIPT_DIR}/../../../venv/bin/activate"
source "$VENV_PATH"

# Navigate to your Flask application directory
cd "${SCRIPT_DIR}"

# Start Gunicorn with your application
gunicorn -w 4 -b 0.0.0.0:8000 backend:app
