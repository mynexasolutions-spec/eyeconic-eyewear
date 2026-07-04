#!/bin/bash
set -e

SERVER="ec2-user@ec2-65-2-56-239.ap-south-1.compute.amazonaws.com"
PEM="nexa.pem"
PROJECT="eyeconic-eyewear"

echo "==> Pulling latest code and restarting eyeconic-eyewear on EC2..."
ssh -o StrictHostKeyChecking=no -i "$PEM" "$SERVER" \
  "cd /home/ec2-user/$PROJECT && \
   git pull origin main && \
   source /home/ec2-user/venv/bin/activate && \
   pip install -r apps/requirements.txt && \
   sudo systemctl restart eyeconic-eyewear"

echo "✅ Deployed!"
