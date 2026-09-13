"""Who the user is.

Two ways in, and neither involves a password:

* **Sign in with Google** -- the OAuth code flow. The same consent that grants
  Gmail access identifies the account, so a user who connects their mail is
  already signed in.
* **Emailed sign-in code** -- a six-digit code sent to the address, for anyone
  not on Google.

There is deliberately no password column, no password hashing, and no endpoint
that accepts one. The project's rule is that credentials for a mail account are
never requested or stored, and the cheapest way to keep that promise is to have
nowhere to put one.
"""

from careeros.auth.service import (
    AuthError,
    AuthMode,
    Identity,
    SignInResult,
    auth_mode,
    describe_auth,
    resolve_session,
    sign_in_with_code,
    sign_in_with_google,
    start_email_code,
)
from careeros.auth.tokens import SessionToken, issue_token, read_token

__all__ = [
    "AuthError",
    "AuthMode",
    "Identity",
    "SessionToken",
    "SignInResult",
    "auth_mode",
    "describe_auth",
    "issue_token",
    "read_token",
    "resolve_session",
    "sign_in_with_code",
    "sign_in_with_google",
    "start_email_code",
]
