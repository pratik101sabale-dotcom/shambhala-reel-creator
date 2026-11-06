"""Read the account chosen by this installation without storing credentials."""

import os

ACCOUNT_ENV_VAR = "IG_ACCOUNT_ID"


def configured_account_id():
    """Return a positive ASCII decimal Graph account ID or None for offline use."""
    account_id = os.environ.get(ACCOUNT_ENV_VAR, "").strip()
    if not account_id.isascii() or not account_id.isdecimal() or not account_id.strip("0"):
        return None
    return account_id
