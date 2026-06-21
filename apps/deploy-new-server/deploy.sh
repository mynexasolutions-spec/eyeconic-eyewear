#!/bin/bash
# Deploy to new EC2 server
# Usage: bash deploy.sh

set -e

SERVER="ec2-user@ec2-13-206-123-17.ap-south-1.compute.amazonaws.com"
PEM="$(dirname "$0")/nexa-solutions.pem"
PROJECT="<project-name>"

echo "==> Pulling & restarting on new server..."
ssh -i "$PEM" "$SERVER" \
  "cd /home/ec2-user/$PROJECT && \
   git pull origin main && \
   source /home/ec2-user/${PROJECT}-venv/bin/activate && \
   pip install -r requirements.txt && \
   sudo systemctl restart $PROJECT"

echo "✅ Deployed!"
