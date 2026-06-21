#!/bin/bash
set -e

SERVER="ec2-user@13.206.123.17"
PEM="apps/deploy-new-server/nexa-solutions.pem"
PROJECT="eyeconic-eyewear"

echo "==> Pulling latest code and restarting eyeconic-eyewear on EC2..."
ssh -o StrictHostKeyChecking=no -i "$PEM" "$SERVER" \
  "cd /home/ec2-user/$PROJECT && \
   git pull origin main && \
   source /home/ec2-user/${PROJECT}-venv/bin/activate && \
   pip install -r apps/requirements.txt && \
   sudo systemctl restart eyeconic-eyewear"

echo "✅ Deployed!"
