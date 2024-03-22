#!/bin/bash

# Run 'chmod +x <path_to_start_app.sh>' to make this script executable.

# Activate virtual environment
source /venv/bin/activate

# Navigate to your Flask application directory
cd /Users/jorgecanedo/Desktop/zillowanalyzer/zillowanalyzer/application/deal_analysis_application/

# Start Gunicorn with your application
gunicorn -w 4 -b 0.0.0.0:8000 backend:app
