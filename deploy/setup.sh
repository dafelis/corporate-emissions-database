#!/bin/bash
# Setup script for corporate-emissions-database on Ubuntu EC2
# Run as: sudo bash setup.sh

set -e

echo "=== Installing system dependencies ==="
apt-get update
apt-get install -y python3 python3-pip python3-venv git postgresql postgresql-contrib

echo "=== Setting up PostgreSQL ==="
sudo -u postgres psql -c "CREATE USER emissions WITH PASSWORD 'changeme';" 2>/dev/null || echo "User already exists"
sudo -u postgres psql -c "CREATE DATABASE emissions OWNER emissions;" 2>/dev/null || echo "Database already exists"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE emissions TO emissions;"

echo "=== Cloning repository ==="
cd /home/ubuntu
if [ -d "corporate-emissions-database" ]; then
    cd corporate-emissions-database
    git pull
else
    git clone https://github.com/YOUR_USERNAME/corporate-emissions-database.git
    cd corporate-emissions-database
fi

echo "=== Setting up Python environment ==="
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Creating .env file ==="
if [ ! -f .env ]; then
    cp .env.example .env
    # Update DATABASE_URL for local PostgreSQL
    sed -i 's|DATABASE_URL=.*|DATABASE_URL=postgresql://emissions:changeme@localhost:5432/emissions|' .env
    echo ""
    echo "IMPORTANT: Edit /home/ubuntu/corporate-emissions-database/.env"
    echo "and fill in your API keys before running the pipeline."
fi

echo "=== Setting up systemd service for review UI ==="
cat > /etc/systemd/system/emissions-review.service << 'EOF'
[Unit]
Description=Emissions Review UI (Streamlit)
After=network.target postgresql.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/corporate-emissions-database
Environment="PATH=/home/ubuntu/corporate-emissions-database/.venv/bin"
ExecStart=/home/ubuntu/corporate-emissions-database/.venv/bin/streamlit run review/app.py --server.port 8501 --server.address 0.0.0.0
Restart=always

[Install]
WantedBy=multi-user.target
EOF

echo "=== Setting up cron for pipeline ==="
# Create a wrapper script for the cron job
cat > /home/ubuntu/corporate-emissions-database/run_pipeline.sh << 'SCRIPT'
#!/bin/bash
cd /home/ubuntu/corporate-emissions-database
source .venv/bin/activate
python run.py extract >> /var/log/emissions-pipeline.log 2>&1
python run.py check >> /var/log/emissions-pipeline.log 2>&1
SCRIPT
chmod +x /home/ubuntu/corporate-emissions-database/run_pipeline.sh

# Read refresh cycle from .env (default 182 days)
REFRESH_DAYS=$(grep REFRESH_CYCLE_DAYS .env 2>/dev/null | cut -d= -f2)
REFRESH_DAYS=${REFRESH_DAYS:-182}

echo "Refresh cycle: every $REFRESH_DAYS days"
echo "To set up the cron job, run: crontab -e"
echo "And add: 0 2 */$REFRESH_DAYS * * /home/ubuntu/corporate-emissions-database/run_pipeline.sh"

echo "=== Enabling and starting services ==="
systemctl daemon-reload
systemctl enable emissions-review
systemctl start emissions-review

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "1. Edit .env with your API keys: nano /home/ubuntu/corporate-emissions-database/.env"
echo "2. Initialise the database: cd /home/ubuntu/corporate-emissions-database && source .venv/bin/activate && python run.py init"
echo "3. Test with one company: python run.py extract --id 1"
echo "4. Review UI available at: http://<your-ip>:8501"
echo "5. Run full extraction: python run.py extract"
echo "6. Set up cron for recurring runs (see above)"
