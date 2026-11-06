"""Read-only check that an access token can read one Instagram account.

Sends a single GET request and prints the account's id and username.
The token is typed in at runtime and is never stored, printed, or logged.
It travels in the Authorization header, so it never appears in the URL.
"""

import getpass
import json
import sys
import urllib.error
import urllib.request
import warnings

from instagram_config import ACCOUNT_ENV_VAR, configured_account_id

GRAPH_API_ROOT = "https://graph.facebook.com/v26.0"
TIMEOUT_SECONDS = 15


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, because urllib would resend the token to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_token():
    """Prompt for the token without echoing it. Returns None if that isn't possible."""
    # If the terminal can't hide input, getpass only warns and then shows what
    # is typed. Treat that warning as an error so the token is never displayed.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Access token (input stays hidden): ").strip()
        except getpass.GetPassWarning:
            print("Error: this terminal can't hide input, so no token was read.")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
    return None


def is_valid_format(token):
    """Reject empty or malformed values before they reach the HTTP library,
    whose error messages can quote the header value."""
    return bool(token) and all(33 <= ord(char) <= 126 for char in token)


def redact(text, token):
    """Remove the token from any text that is about to be printed."""
    return str(text).replace(token, "[REDACTED]")


def print_api_error(error, token):
    """Print the status and Graph API error details, never the raw response."""
    print(f"Request failed with HTTP status {error.code}.")
    try:
        details = json.loads(error.read().decode("utf-8")).get("error", {})
    except (ValueError, AttributeError, OSError):
        return
    for label, key in (("Type", "type"), ("Code", "code"), ("Message", "message")):
        if key in details:
            print(f"{label}: {redact(details[key], token)}")


def main():
    account_id = configured_account_id()
    if account_id is None:
        print(f"Set {ACCOUNT_ENV_VAR} to your own numeric Instagram professional account ID first.")
        return 1
    token = read_token()
    if token is None:
        return 1
    if not is_valid_format(token):
        print("Error: the token is empty or contains invalid characters.")
        return 1

    request = urllib.request.Request(
        f"{GRAPH_API_ROOT}/{account_id}?fields=id,username",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    opener = urllib.request.build_opener(NoRedirects)

    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print_api_error(error, token)
        return 1
    except urllib.error.URLError as error:
        print(f"Network error: {redact(error.reason, token)}")
        return 1
    except TimeoutError:
        print(f"Network error: no response within {TIMEOUT_SECONDS} seconds.")
        return 1
    except ValueError:
        print("Error: the response was not valid JSON.")
        return 1

    print("Success.")
    print(f"ID: {redact(data.get('id'), token)}")
    print(f"Username: {redact(data.get('username'), token)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
