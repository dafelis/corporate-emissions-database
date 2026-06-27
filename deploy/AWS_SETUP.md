# AWS Setup Guide

## 1. Create EC2 Instance

1. Go to AWS Console → EC2 → Launch Instance
2. Settings:
   - **Name**: corporate-emissions-database
   - **AMI**: Ubuntu 22.04 LTS
   - **Instance type**: t3.small (2 vCPU, 2GB RAM)
   - **Key pair**: Create or select a .pem key
   - **Storage**: 20GB gp3
   - **Security group**: Allow SSH (port 22) and Custom TCP (port 8501) from your IP

## 2. Create S3 Bucket

1. Go to AWS Console → S3 → Create Bucket
2. **Name**: corporate-emissions-sources
3. **Region**: eu-north-1 (Stockholm)
4. Leave defaults (block public access = on)

## 3. Create IAM User for S3 Access

1. Go to IAM → Users → Create User
2. **Name**: emissions-s3-writer
3. Attach policy: AmazonS3FullAccess (or a scoped policy for your bucket only)
4. Create access key → save the Access Key ID and Secret

## 4. SSH In and Run Setup

```bash
ssh -i "your-key.pem" ubuntu@<your-ec2-ip>
sudo apt-get install -y git
git clone https://github.com/YOUR_USERNAME/corporate-emissions-database.git
cd corporate-emissions-database
sudo bash deploy/setup.sh
```

## 5. Configure .env

```bash
nano .env
```

Fill in:
- ANTHROPIC_API_KEY
- EXA_API_KEY
- LLAMA_CLOUD_API_KEY
- AWS_ACCESS_KEY_ID (from step 3)
- AWS_SECRET_ACCESS_KEY (from step 3)

## 6. Initialise and Test

```bash
source .venv/bin/activate

# Load FTSE 100 companies and look up LEIs
python run.py init

# Test with a single company
python run.py extract --id 1

# Check the result
python run.py status

# Open review UI in browser: http://<your-ec2-ip>:8501
```

## 7. Run Full Pipeline

```bash
# Extract all companies (takes several hours)
python run.py extract

# Run sanity checks
python run.py check

# Review flagged items in the UI
```

## Costs

| Component | Monthly cost |
|---|---|
| EC2 t3.small (if always on) | ~$16 |
| EC2 (run only during extraction) | ~$0.50 |
| RDS/PostgreSQL (local on EC2) | $0 |
| S3 (100 PDFs) | ~$0.05 |
| Claude API (100 companies) | ~$15-50 per run |
| Exa API (100 searches) | ~$1-5 per run |
| LlamaParse (100 PDFs) | ~$5-15 per run |

Tip: Stop the EC2 instance when not in use to save costs.
Only the S3 storage costs money when the instance is stopped.
