# Deploy Flask App — New EC2 Server

> **Server**: `ec2-13-206-123-17.ap-south-1.compute.amazonaws.com`  
> **SSH User**: `ec2-user`  
> **PEM Key**: `nexa-solutions.pem` (included)

---

## Step 1 — Push Code to GitHub

```bash
cd your-project
git init && git add . && git commit -m "Initial"
git remote add origin git@github.com:your-org/your-repo.git
git push -u origin main
```

---

## Step 2 — One-Time Server Setup

```bash
ssh -i nexa-solutions.pem ec2-user@ec2-13-206-123-17.ap-south-1.compute.amazonaws.com
```

### 2a — Install basics

```bash
sudo yum install python3-pip nginx git -y
```

### 2b — Clone repo & create venv OUTSIDE the repo

```bash
cd /home/ec2-user
git clone git@github.com:your-org/your-repo.git <project-name>

# venv outside repo (as requested)
python3 -m venv <project-name>-venv
source <project-name>-venv/bin/activate
pip install --upgrade pip
pip install -r <project-name>/requirements.txt
```

### 2c — Add deploy key for GitHub

```bash
ssh-keygen -t ed25519 -f ~/.ssh/<project>-deploy -N ""
cat ~/.ssh/<project>-deploy.pub
```

Copy the output → Go to GitHub repo → **Settings** → **Deploy Keys** → **Add deploy key** → Title: `<project>-deploy` → Paste key → ✔ **Allow write access**

Then configure SSH:

```bash
cat >> ~/.ssh/config << 'EOF'
Host github.com-<project>
    HostName github.com
    IdentityFile ~/.ssh/<project>-deploy
    StrictHostKeyChecking no
EOF

cd /home/ec2-user/<project-name>
git remote set-url origin git@github.com-<project>:your-org/your-repo.git
```

### 2d — Create `.env`

```bash
nano /home/ec2-user/<project-name>/.env
```

Add secrets, then:

```bash
chmod 600 /home/ec2-user/<project-name>/.env
```

---

## Step 3 — Systemd Service

```bash
sudo nano /etc/systemd/system/<project-name>.service
```

Paste:

```ini
[Unit]
Description=New Store
After=network.target

[Service]
User=ec2-user
Group=ec2-user
WorkingDirectory=/home/ec2-user/<project-name>
Environment="PATH=/home/ec2-user/<project-name>-venv/bin"
EnvironmentFile=/home/ec2-user/<project-name>/.env
ExecStart=/home/ec2-user/<project-name>-venv/bin/gunicorn --workers 1 --bind 127.0.0.1:8005 wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable <project-name>
sudo systemctl start <project-name>
sudo systemctl status <project-name>
```

---

## Step 4 — Nginx

```bash
sudo nano /etc/nginx/conf.d/<project-name>.conf
```

Paste:

```nginx
server {
    server_name yourdomain.com www.yourdomain.com;

    client_max_body_size 20M;

    location /static/ {
        alias /home/ec2-user/<project-name>/static/;
        expires 30d;
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:8005;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;
    return 404;
}
```

Apply:

```bash
sudo nginx -t && sudo nginx -s reload
```

---

## Step 5 — SSL

```bash
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

---

## Step 6 — DNS

Point your domain's **A record** to the server IP.

---

## Step 7 — Daily Deploy (Run from Your Laptop)

Save this as `deploy-<project>.sh`:

```bash
#!/bin/bash
set -e

SERVER="ec2-user@ec2-13-206-123-17.ap-south-1.compute.amazonaws.com"
PEM="nexa-solutions.pem"
PROJECT="<project-name>"

ssh -i "$PEM" "$SERVER" \
  "cd /home/ec2-user/$PROJECT && \
   git pull origin main && \
   source /home/ec2-user/${PROJECT}-venv/bin/activate && \
   pip install -r requirements.txt && \
   sudo systemctl restart $PROJECT"

echo "✅ Deployed!"
```

```bash
chmod +x deploy-<project>.sh
# After git push, run:
./deploy-<project>.sh
```

---

## Useful Commands

```bash
# View logs
sudo journalctl -u <project-name> -f

# Restart
sudo systemctl restart <project-name>

# Check status
sudo systemctl status <project-name>

# Test Nginx
sudo nginx -t
sudo nginx -s reload
```
