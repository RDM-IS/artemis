"""Provision the OpenWeatherMap API key in AWS Secrets Manager.

Used by artemis/weather.py for the indoor/outdoor cardio decision.
Sign up for a free API key at https://openweathermap.org/api — you need
the One Call API 3.0 (free tier supports 1000 calls/day, plenty for
two prompts per day).

Usage:
    AWS_REGION=us-east-1 python scripts/provision_openweather_key.py KEY_VALUE
    AWS_REGION=us-east-1 python scripts/provision_openweather_key.py --rotate KEY_VALUE
    AWS_REGION=us-east-1 python scripts/provision_openweather_key.py --print

Unlike provision_health_api_key.py this does NOT generate a new key —
OpenWeatherMap keys come from their dashboard, not random bytes.
"""

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

SECRET_NAME = "rdmis/dev/openweather-api-key"


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


def write_secret(client, key: str, exists: bool) -> None:
    payload = json.dumps({"api_key": key})
    if exists:
        client.put_secret_value(SecretId=SECRET_NAME, SecretString=payload)
    else:
        client.create_secret(
            Name=SECRET_NAME,
            SecretString=payload,
            Description="OpenWeatherMap API key for artemis/weather.py.",
        )


def main():
    parser = argparse.ArgumentParser(description="Provision the OpenWeatherMap API key.")
    parser.add_argument("key_value", nargs="?", help="The API key from openweathermap.org dashboard.")
    parser.add_argument("--rotate", action="store_true", help="Replace existing secret value.")
    parser.add_argument("--print", action="store_true", help="Print the current key.")
    args = parser.parse_args()

    client = _client()
    existing = get_existing_key(client)

    if args.print:
        if existing:
            print(existing)
            return 0
        print(f"Secret '{SECRET_NAME}' not found.", file=sys.stderr)
        return 1

    if not args.key_value:
        print("ERROR: pass the API key as a positional arg, or use --print.", file=sys.stderr)
        parser.print_help()
        return 2

    if existing and not args.rotate:
        print(f"[OK] Secret '{SECRET_NAME}' already exists. Use --rotate to replace.")
        return 0

    write_secret(client, args.key_value, exists=bool(existing))
    verb = "ROTATED" if existing else "CREATED"
    print(f"[{verb}] '{SECRET_NAME}' written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
