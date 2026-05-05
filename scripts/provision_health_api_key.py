"""Provision the health API key in AWS Secrets Manager.

Creates secret `rdmis/dev/health-api-key` with a freshly generated 32-char
URL-safe random key. Idempotent: if the secret already exists, prints the
current key and exits without changes (use --rotate to force a new one).

Usage:
    AWS_REGION=us-east-1 python scripts/provision_health_api_key.py
    AWS_REGION=us-east-1 python scripts/provision_health_api_key.py --rotate
    AWS_REGION=us-east-1 python scripts/provision_health_api_key.py --print

The printed value is what gets configured as VITE_API_KEY on the
gym-display Cloudflare Pages deployment.
"""

import argparse
import json
import os
import secrets
import sys

import boto3
from botocore.exceptions import ClientError

SECRET_NAME = "rdmis/dev/health-api-key"


def _generate_key() -> str:
    return secrets.token_urlsafe(32)


def _client():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("secretsmanager", region_name=region)


def get_existing_key(client) -> str | None:
    try:
        resp = client.get_secret_value(SecretId=SECRET_NAME)
        secret = json.loads(resp["SecretString"])
        return secret.get("api_key")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def create_secret(client, key: str) -> None:
    client.create_secret(
        Name=SECRET_NAME,
        SecretString=json.dumps({"api_key": key}),
        Description="API key for /api/health/* endpoints (gym-display frontend).",
    )


def rotate_secret(client, key: str) -> None:
    client.put_secret_value(
        SecretId=SECRET_NAME,
        SecretString=json.dumps({"api_key": key}),
    )


def main():
    parser = argparse.ArgumentParser(description="Provision the health API key.")
    parser.add_argument(
        "--rotate", action="store_true",
        help="Generate a new key and overwrite the existing secret value.",
    )
    parser.add_argument(
        "--print", action="store_true",
        help="Print the current key value to stdout (for VITE_API_KEY config).",
    )
    args = parser.parse_args()

    client = _client()
    existing = get_existing_key(client)

    if existing and not args.rotate:
        if args.print:
            print(existing)
        else:
            print(f"[OK] Secret '{SECRET_NAME}' already exists.")
            print(f"     Use --print to display the key, or --rotate to replace it.")
        return 0

    new_key = _generate_key()
    if existing and args.rotate:
        rotate_secret(client, new_key)
        print(f"[ROTATED] '{SECRET_NAME}' updated with new key.")
    else:
        create_secret(client, new_key)
        print(f"[CREATED] '{SECRET_NAME}' provisioned.")

    print(f"\nSet on gym-display deployment as VITE_API_KEY:")
    print(f"  {new_key}")
    print(f"\nRemember: never commit this value. Distribute via Cloudflare Pages env only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
