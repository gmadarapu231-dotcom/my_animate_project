"""Sign-in: identity, sessions, and the things that must not be possible.

Nothing here reaches Google or an SMTP server. The OAuth exchange is driven by
a fake JSON fetcher and the code email by a fake sender, so the suite tests our
half of each protocol without depending on anyone's uptime.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from careeros.auth import (
    AuthError,
    AuthMode,
    auth_mode,
    describe_auth,
    resolve_session,
    sign_in_with_code,
    sign_in_with_google,
    start_email_code,
)
from careeros.auth import email_code as codes
from careeros.auth import google
from careeros.auth.service import Identity, start_google, upsert_account
from careeros.auth.tokens import (
    TokenError,
    issue_token,
    looks_like_session_token,
    read_token,
)
from careeros.db.models import AuthChallenge, User


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_secret(tmp_path, monkeypatch):
    """Each test gets its own signing secret, generated on disk as in production."""
    monkeypatch.setenv("CAREEROS_HOME", str(tmp_path))
    monkeypatch.delenv("CAREEROS_SESSION_SECRET", raising=False)
    yield


@pytest.fixture
def google_configured(monkeypatch):
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("CAREEROS_GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/google/callback")


def google_fetcher(*, email="gopi@example.com", verified=True, scope="openid email profile", token_status=200):
    """A stand-in for Google's token and userinfo endpoints."""
    calls: list[tuple[str, str]] = []

    def fetch(method, url, body, headers):
        calls.append((method, url))
        if url == google.TOKEN_ENDPOINT:
            if token_status != 200:
                return token_status, {"error": "invalid_grant", "error_description": "code expired"}
            return 200, {
                "access_token": "ya29.access",
                "refresh_token": "1//refresh",
                "scope": scope,
                "expires_in": 3599,
                "token_type": "Bearer",
            }
        if url == google.USERINFO_ENDPOINT:
            assert headers.get("Authorization") == "Bearer ya29.access"
            return 200, {
                "sub": "109876543210",
                "email": email,
                "email_verified": verified,
                "name": "Gopi M",
                "picture": "https://lh3.googleusercontent.com/a/abc",
            }
        raise AssertionError(f"unexpected call to {url}")

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


# ---------------------------------------------------------------------------
# the promise: no passwords anywhere
# ---------------------------------------------------------------------------
def test_the_user_table_has_nowhere_to_put_a_password():
    """The rule is 'never request or store passwords'. This is how it is kept."""
    names = {column.name.lower() for column in User.__table__.columns}
    assert not {n for n in names if "password" in n or "passwd" in n or "secret" in n}


def test_no_sign_in_route_accepts_a_password():
    from careeros.api.routers import auth as auth_router

    for model in ("GoogleStart", "GoogleExchange", "EmailStart", "EmailVerify"):
        fields = getattr(auth_router, model).model_fields
        assert not any("password" in name.lower() for name in fields), model


def test_describe_auth_reports_methods_without_leaking_values(monkeypatch):
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_SECRET", "super-secret-client")
    described = describe_auth()
    assert "google" in described["methods"]
    assert described["passwords"] == "never stored or requested"
    assert "super-secret-client" not in repr(described)


# ---------------------------------------------------------------------------
# session tokens
# ---------------------------------------------------------------------------
def test_a_token_round_trips_and_names_its_method():
    token = issue_token(7, "Gopi@Example.COM ", method="google")
    assert looks_like_session_token(token)
    parsed = read_token(token)
    assert parsed.user_id == 7
    assert parsed.email == "gopi@example.com"   # normalised on the way in
    assert parsed.method == "google"
    assert parsed.seconds_remaining > 0


def test_a_tampered_payload_does_not_verify():
    token = issue_token(7, "a@b.co")
    prefix, body, signature = token.split(".")
    # Re-sign nothing: swap in another user's id and keep the old signature.
    forged_body = issue_token(9999, "a@b.co").split(".")[1]
    with pytest.raises(TokenError, match="signature"):
        read_token(f"{prefix}.{forged_body}.{signature}")


def test_an_expired_token_is_refused():
    with pytest.raises(TokenError, match="expired"):
        read_token(issue_token(1, "a@b.co", ttl_seconds=-1))


def test_rotating_the_secret_invalidates_every_session(monkeypatch):
    token = issue_token(1, "a@b.co")
    read_token(token)  # fine now
    monkeypatch.setenv("CAREEROS_SESSION_SECRET", "a-brand-new-secret")
    with pytest.raises(TokenError):
        read_token(token)


def test_the_generated_secret_is_not_world_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREEROS_HOME", str(tmp_path))
    issue_token(1, "a@b.co")
    path = tmp_path / "session_secret"
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_garbage_is_not_a_token():
    for value in ("", "Bearer", "cos1.x", "eyJhbGciOiJIUzI1NiJ9.e30.sig", "cos1.a.b.c"):
        with pytest.raises(TokenError):
            read_token(value)


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
def test_an_account_is_opened_on_first_sign_in_and_reused_after(db):
    first, created = upsert_account(db, Identity(email="New@Example.com", name="New Person"))
    assert created and first.email == "new@example.com"
    assert first.full_name == "New Person"
    assert first.email_verified_at is not None

    again, created_again = upsert_account(db, Identity(email="new@example.com"))
    assert not created_again and again.id == first.id


def test_a_name_is_derived_when_the_provider_gives_none(db):
    user, _ = upsert_account(db, Identity(email="gopi.m@example.com"))
    assert user.full_name == "gopi.m"


def test_a_malformed_address_is_refused(db):
    with pytest.raises(AuthError):
        upsert_account(db, Identity(email="not-an-address"))


# ---------------------------------------------------------------------------
# google
# ---------------------------------------------------------------------------
def test_the_consent_url_carries_pkce_and_only_identity_scopes(db, google_configured):
    started = start_google(db)
    import urllib.parse as up

    params = up.parse_qs(up.urlsplit(started["authorization_url"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [started["state"]]
    assert params["scope"] == ["openid email profile"]
    assert "gmail" not in params["scope"][0]


def test_asking_for_gmail_adds_those_scopes_to_the_same_consent(db, google_configured):
    started = start_google(db, include_gmail=True)
    assert "gmail.readonly" in started["authorization_url"]
    assert "gmail.compose" in started["authorization_url"]
    # Sending is never requested.
    assert "gmail.send" not in started["authorization_url"]


def test_google_sign_in_creates_the_account_and_issues_a_session(db, google_configured):
    started = start_google(db, include_gmail=True)
    fetch = google_fetcher(scope="openid email profile https://www.googleapis.com/auth/gmail.readonly")

    result = sign_in_with_google(db, code="4/auth-code", state=started["state"], fetch=fetch)

    assert result.created
    assert result.user.email == "gopi@example.com"
    assert result.user.google_subject == "109876543210"
    assert result.user.picture_url.startswith("https://lh3.googleusercontent.com/")
    assert result.granted_gmail
    assert result.refresh_token == "1//refresh"
    assert read_token(result.token).user_id == result.user.id


def test_an_unverified_google_address_is_not_an_identity(db, google_configured):
    started = start_google(db)
    with pytest.raises(AuthError, match="not verified"):
        sign_in_with_google(
            db, code="c", state=started["state"], fetch=google_fetcher(verified=False)
        )


def test_an_oauth_state_is_single_use(db, google_configured):
    started = start_google(db)
    sign_in_with_google(db, code="c", state=started["state"], fetch=google_fetcher())
    with pytest.raises(AuthError, match="already used"):
        sign_in_with_google(db, code="c", state=started["state"], fetch=google_fetcher())


def test_a_state_we_never_issued_is_refused(db, google_configured):
    with pytest.raises(AuthError, match="not one this server issued"):
        sign_in_with_google(db, code="c", state="attacker-chosen", fetch=google_fetcher())


def test_an_expired_state_is_refused(db, google_configured):
    from datetime import datetime, timedelta, timezone

    started = start_google(db)
    row = db.scalars(
        select(AuthChallenge).where(AuthChallenge.state == started["state"])
    ).first()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db.flush()
    with pytest.raises(AuthError, match="expired"):
        sign_in_with_google(db, code="c", state=started["state"], fetch=google_fetcher())


def test_googles_own_rejection_is_reported_not_swallowed(db, google_configured):
    started = start_google(db)
    with pytest.raises(AuthError, match="code expired"):
        sign_in_with_google(
            db, code="stale", state=started["state"], fetch=google_fetcher(token_status=400)
        )


def test_google_sign_in_needs_configuring_first(db, monkeypatch):
    monkeypatch.delenv("CAREEROS_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CAREEROS_GOOGLE_CLIENT_SECRET", raising=False)
    with pytest.raises(AuthError, match="CAREEROS_GOOGLE_CLIENT_ID"):
        start_google(db)


# ---------------------------------------------------------------------------
# emailed codes
# ---------------------------------------------------------------------------
class Outbox(list):
    def __call__(self, address: str, code: str) -> None:
        self.append((address, code))

    @property
    def last_code(self) -> str:
        return self[-1][1]


def test_an_emailed_code_signs_you_in(db):
    outbox = Outbox()
    started = start_email_code(db, "Gopi@Example.com ", sender=outbox)

    assert started["sent_to"] == "gopi@example.com"
    assert started["code_length"] == 6
    assert len(outbox.last_code) == 6 and outbox.last_code.isdigit()
    # The response must not carry the code; only the mailbox owner sees it.
    assert outbox.last_code not in repr(started)

    result = sign_in_with_code(db, "gopi@example.com", outbox.last_code)
    assert result.created and result.user.email == "gopi@example.com"
    assert read_token(result.token).method == "email_code"


def test_only_the_hash_of_a_code_is_stored(db):
    outbox = Outbox()
    start_email_code(db, "a@b.co", sender=outbox)
    row = db.scalars(select(AuthChallenge).where(AuthChallenge.email == "a@b.co")).first()
    assert outbox.last_code not in (row.code_hash or "")
    assert len(row.code_hash) == 64


def test_a_wrong_code_is_refused_and_attempts_are_capped(db):
    outbox = Outbox()
    start_email_code(db, "a@b.co", sender=outbox)
    for _ in range(codes.MAX_ATTEMPTS):
        with pytest.raises(AuthError):
            sign_in_with_code(db, "a@b.co", "000000")
    # Even the right code cannot rescue a burnt challenge.
    with pytest.raises(AuthError, match="Too many|not right"):
        sign_in_with_code(db, "a@b.co", outbox.last_code)


def test_a_code_cannot_be_used_twice(db):
    outbox = Outbox()
    start_email_code(db, "a@b.co", sender=outbox)
    sign_in_with_code(db, "a@b.co", outbox.last_code)
    with pytest.raises(AuthError, match="No sign-in code is outstanding"):
        sign_in_with_code(db, "a@b.co", outbox.last_code)


def test_requesting_a_second_code_supersedes_the_first(db):
    outbox = Outbox()
    start_email_code(db, "a@b.co", sender=outbox)
    first = outbox.last_code
    start_email_code(db, "a@b.co", sender=outbox)
    second = outbox.last_code
    assert first != second
    with pytest.raises(AuthError):
        sign_in_with_code(db, "a@b.co", first)
    assert sign_in_with_code(db, "a@b.co", second).user.email == "a@b.co"


def test_one_persons_code_does_not_sign_in_another(db):
    outbox = Outbox()
    start_email_code(db, "alice@example.com", sender=outbox)
    alice_code = outbox.last_code
    start_email_code(db, "bob@example.com", sender=outbox)

    with pytest.raises(AuthError):
        sign_in_with_code(db, "bob@example.com", alice_code)


def test_a_code_expires(db):
    from datetime import datetime, timedelta, timezone

    outbox = Outbox()
    start_email_code(db, "a@b.co", sender=outbox)
    row = db.scalars(select(AuthChallenge).where(AuthChallenge.email == "a@b.co")).first()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db.flush()
    with pytest.raises(AuthError, match="expired"):
        sign_in_with_code(db, "a@b.co", outbox.last_code)


def test_codes_need_smtp_before_they_can_be_offered(db, monkeypatch):
    for name in ("CAREEROS_SMTP_HOST", "CAREEROS_SMTP_FROM", "CAREEROS_SMTP_USER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AuthError, match="CAREEROS_SMTP_HOST"):
        start_email_code(db, "a@b.co")


# ---------------------------------------------------------------------------
# reading a session back
# ---------------------------------------------------------------------------
def test_a_session_resolves_to_its_account(db):
    user, _ = upsert_account(db, Identity(email="gopi@example.com"))
    token = issue_token(user.id, user.email)
    assert resolve_session(db, token).id == user.id


def test_a_session_for_a_deleted_account_is_refused(db):
    user, _ = upsert_account(db, Identity(email="gone@example.com"))
    token = issue_token(user.id, user.email)
    db.delete(user)
    db.flush()
    with pytest.raises(AuthError, match="no longer exists"):
        resolve_session(db, token)


def test_a_session_whose_row_was_repointed_is_refused(db):
    """The email is signed into the token, so the row cannot be swapped under it."""
    user, _ = upsert_account(db, Identity(email="first@example.com"))
    token = issue_token(user.id, user.email)
    user.email = "someone.else@example.com"
    db.flush()
    with pytest.raises(AuthError, match="no longer matches"):
        resolve_session(db, token)


# ---------------------------------------------------------------------------
# mode selection
# ---------------------------------------------------------------------------
def test_auth_is_open_when_no_sign_in_method_exists(monkeypatch):
    for name in (
        "CAREEROS_AUTH",
        "CAREEROS_GOOGLE_CLIENT_ID",
        "CAREEROS_GOOGLE_CLIENT_SECRET",
        "CAREEROS_SMTP_HOST",
        "CAREEROS_SMTP_FROM",
        "CAREEROS_SMTP_USER",
    ):
        monkeypatch.delenv(name, raising=False)
    assert auth_mode() is AuthMode.OPEN


def test_configuring_google_turns_sign_in_on_by_itself(monkeypatch):
    monkeypatch.delenv("CAREEROS_AUTH", raising=False)
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_SECRET", "csec")
    assert auth_mode() is AuthMode.REQUIRED


@pytest.mark.parametrize("value,expected", [
    ("required", AuthMode.REQUIRED),
    ("open", AuthMode.OPEN),
    ("1", AuthMode.REQUIRED),
    ("0", AuthMode.OPEN),
])
def test_the_mode_can_be_forced_either_way(monkeypatch, value, expected):
    monkeypatch.setenv("CAREEROS_AUTH", value)
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_SECRET", "csec")
    assert auth_mode() is expected
