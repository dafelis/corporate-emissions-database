"""S3 storage for source documents and screenshots."""

import os

import boto3


def get_s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("AWS_REGION", "eu-north-1"),
    )


def upload_file(local_path: str, s3_key: str, bucket: str = None) -> str:
    """Upload a file to S3 and return the S3 key."""
    bucket = bucket or os.environ.get("AWS_S3_BUCKET", "corporate-emissions-sources")
    client = get_s3_client()
    client.upload_file(local_path, bucket, s3_key)
    return s3_key


def upload_pdf(local_path: str, company_name: str, year: str = "") -> str:
    """Upload a source PDF with a structured key."""
    safe_name = company_name.lower().replace(" ", "_").replace("&", "and")
    s3_key = f"sources/{safe_name}/{year}_report.pdf" if year else f"sources/{safe_name}/report.pdf"
    return upload_file(local_path, s3_key)


def upload_screenshot(local_path: str, company_name: str, page: int) -> str:
    """Upload a screenshot of a relevant page."""
    safe_name = company_name.lower().replace(" ", "_").replace("&", "and")
    s3_key = f"screenshots/{safe_name}/page_{page}.png"
    return upload_file(local_path, s3_key)


def download_file(s3_key: str, local_path: str, bucket: str = None) -> str:
    """Download a file from S3 to a local path."""
    bucket = bucket or os.environ.get("AWS_S3_BUCKET", "corporate-emissions-sources")
    client = get_s3_client()
    client.download_file(bucket, s3_key, local_path)
    return local_path
