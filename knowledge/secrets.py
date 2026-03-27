"""Centralized AWS Secrets Manager access — the ONLY module that talks to Secrets Manager.

Every other module imports from here. Secrets are cached per process via lru_cache
so each Lambda cold start (or local process) fetches once, not on every request.
"""

import json
import os
from functools import lru_cache

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")


@lru_cache(maxsize=None)
def get_secret(secret_name: str) -> dict:
    """Fetch and cache a secret from Secrets Manager.
    Returns parsed JSON dict. Raises on failure."""
    client = boto3.client("secretsmanager", region_name=REGION)
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------

def get_rds_credentials() -> dict:
    """Returns {username, password} from RDS secret.
    Secret name from env var RDS_SECRET_ARN."""
    arn = os.environ.get("RDS_SECRET_ARN")
    if not arn:
        raise RuntimeError("RDS_SECRET_ARN not set")
    return get_secret(arn)


def get_anthropic_key() -> str:
    """Returns Anthropic API key string.
    Secret name: rdmis/dev/anthropic-api-key"""
    secret = get_secret("rdmis/dev/anthropic-api-key")
    return secret["api_key"]


def get_mattermost_credentials() -> dict:
    """Returns {url, token, channel_id}.
    Secret name: rdmis/dev/mattermost"""
    return get_secret("rdmis/dev/mattermost")


def get_twilio_credentials() -> dict:
    """Returns {account_sid, auth_token, from_number}.
    Secret name: rdmis/dev/twilio"""
    return get_secret("rdmis/dev/twilio")


def get_gmail_credentials() -> dict:
    """Returns Gmail OAuth credentials dict.
    Secret name: rdmis/dev/gmail-oauth"""
    return get_secret("rdmis/dev/gmail-oauth")


def get_crm_api_key() -> str:
    """Returns CRM API key string.
    Secret name: rdmis/dev/crm-api-key"""
    secret = get_secret("rdmis/dev/crm-api-key")
    return secret["api_key"]


def get_zoho_webhook_secret() -> str:
    """Returns Zoho webhook secret string.
    Secret name: rdmis/dev/zoho-webhook-secret"""
    secret = get_secret("rdmis/dev/zoho-webhook-secret")
    return secret["webhook_secret"]
