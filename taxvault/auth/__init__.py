"""Sign-in, one-time codes, and identity verification."""

from taxvault.auth.codes import (
    DeliveryError,
    mask_email,
    mask_mobile,
    normalise_email,
    normalise_mobile,
)
from taxvault.auth.service import (
    AuthError,
    SignInResult,
    audit,
    complete_mobile_verification,
    complete_sign_in,
    describe_auth,
    development_mode,
    find_account,
    resolve_session,
    start_mobile_verification,
    start_sign_in,
    verify_identity,
)
from taxvault.auth.tokens import (
    SessionToken,
    TokenError,
    issue_token,
    looks_like_session_token,
    read_token,
)

__all__ = [
    "AuthError", "DeliveryError", "SessionToken", "SignInResult", "TokenError",
    "audit", "complete_mobile_verification", "complete_sign_in", "describe_auth",
    "development_mode", "find_account", "issue_token", "looks_like_session_token",
    "mask_email", "mask_mobile", "normalise_email", "normalise_mobile", "read_token",
    "resolve_session", "start_mobile_verification", "start_sign_in", "verify_identity",
]
