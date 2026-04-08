"""
Robust Clerk -> Supabase RLS bridge.

What this module provides:
1) Retrieves a Clerk session JWT from the incoming request Authorization header.
2) Verifies that JWT against Clerk JWKS.
3) Builds a Supabase client authenticated with that JWT so Row Level Security (RLS)
   policies are evaluated for the current user.
4) Exposes a helper to fetch rows from `user_preferences` for the current user.

This implementation is intentionally defensive and explicit:
- Strong configuration validation.
- Structured exceptions for auth vs config vs db failures.
- JWT signature, issuer, expiration, and optional audience validation.
- Retry wrapper for transient query failures.
- Optional fallback path to mint a session token through Clerk Backend API.

Environment variables:
  CLERK_JWT_ISSUER        Required (e.g. https://your-tenant.clerk.accounts.dev)
  CLERK_AUDIENCE          Optional (validate aud when provided)
  CLERK_SECRET_KEY        Optional (required only for Clerk token mint fallback)
  CLERK_API_BASE          Optional, defaults to https://api.clerk.com/v1
  CLERK_TOKEN_TEMPLATE    Optional, defaults to supabase

  SUPABASE_URL            Required
  SUPABASE_ANON_KEY       Required (do NOT use service-role key for user-scoped RLS)
  SUPABASE_PREFS_TABLE    Optional, defaults to user_preferences
  SUPABASE_USER_ID_COLUMN Optional, defaults to user_id

Usage (Flask example):
    from flask import request, jsonify
    from scripts.clerk_supabase_rls import fetch_current_user_preferences

    @app.get("/api/user/preferences")
    def get_preferences():
        rows = fetch_current_user_preferences(headers=request.headers)
        return jsonify(rows)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

import jwt
import requests
from jwt import PyJWKClient
from supabase import Client, create_client

try:
    # Available in supabase-py v2.
    from supabase.lib.client_options import ClientOptions
except Exception:  # pragma: no cover - fallback path for older versions
    ClientOptions = None


class ConfigError(RuntimeError):
    """Raised when required environment configuration is missing/invalid."""


class AuthError(RuntimeError):
    """Raised for Clerk token extraction, minting, or verification failures."""


class DatabaseError(RuntimeError):
    """Raised for Supabase client/query failures."""


@dataclass(frozen=True)
class Settings:
    clerk_jwt_issuer: str
    clerk_audience: str
    clerk_secret_key: str
    clerk_api_base: str
    clerk_token_template: str
    supabase_url: str
    supabase_anon_key: str
    prefs_table: str
    user_id_column: str

    @classmethod
    def from_env(cls) -> "Settings":
        issuer = (os.environ.get("CLERK_JWT_ISSUER") or "").strip().rstrip("/")
        audience = (os.environ.get("CLERK_AUDIENCE") or "").strip()
        secret = (os.environ.get("CLERK_SECRET_KEY") or "").strip()
        api_base = (os.environ.get("CLERK_API_BASE")
                    or "https://api.clerk.com/v1").strip().rstrip("/")
        template = (os.environ.get("CLERK_TOKEN_TEMPLATE")
                    or "supabase").strip()

        supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
        supabase_anon_key = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
        prefs_table = (os.environ.get("SUPABASE_PREFS_TABLE")
                       or "user_preferences").strip()
        user_id_column = (os.environ.get(
            "SUPABASE_USER_ID_COLUMN") or "user_id").strip()

        missing = []
        if not issuer:
            missing.append("CLERK_JWT_ISSUER")
        if not supabase_url:
            missing.append("SUPABASE_URL")
        if not supabase_anon_key:
            missing.append("SUPABASE_ANON_KEY")

        if missing:
            raise ConfigError(
                f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            clerk_jwt_issuer=issuer,
            clerk_audience=audience,
            clerk_secret_key=secret,
            clerk_api_base=api_base,
            clerk_token_template=template,
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            prefs_table=prefs_table,
            user_id_column=user_id_column,
        )


def _execute_with_retry(
    operation: Callable[[], Any],
    *,
    attempts: int = 3,
    initial_backoff_seconds: float = 0.25,
) -> Any:
    """Execute operation with exponential backoff for transient failures."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_error: Optional[Exception] = None
    backoff = initial_backoff_seconds

    for i in range(1, attempts + 1):
        try:
            return operation()
        # Broad catch by design for transport/db client errors.
        except Exception as exc:
            last_error = exc
            if i == attempts:
                break
            time.sleep(backoff)
            backoff *= 2

    raise DatabaseError(
        f"Operation failed after {attempts} attempts: {last_error}") from last_error


class ClerkAuthManager:
    """Handles Clerk session JWT retrieval and verification."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        jwks_url = f"{settings.clerk_jwt_issuer}/.well-known/jwks.json"
        self._jwks_client = PyJWKClient(jwks_url)
        self._http = requests.Session()

    @staticmethod
    def extract_bearer_token(headers: Mapping[str, str]) -> str:
        """Extract Bearer token from Authorization header."""
        auth_header = (headers.get("Authorization")
                       or headers.get("authorization") or "").strip()
        if not auth_header:
            raise AuthError("Missing Authorization header")

        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise AuthError(
                "Authorization header must be in format: Bearer <JWT>")

        return parts[1].strip()

    def mint_session_jwt(self, session_id: str, template: Optional[str] = None) -> str:
        """
        Optional fallback: mint a Clerk session token via Clerk Backend API.

        Requires:
          - CLERK_SECRET_KEY
          - valid session_id
        """
        if not self.settings.clerk_secret_key:
            raise AuthError(
                "CLERK_SECRET_KEY is required to mint session tokens")
        if not session_id:
            raise AuthError("session_id is required to mint a session token")

        url = f"{self.settings.clerk_api_base}/sessions/{session_id}/tokens"
        payload = {"template": template or self.settings.clerk_token_template}

        try:
            response = self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.settings.clerk_secret_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise AuthError(
                f"Failed to mint Clerk session token: {exc}") from exc

        token = (data.get("jwt") or "").strip()
        if not token:
            raise AuthError("Clerk token mint response did not include 'jwt'")
        return token

    def verify_jwt(self, token: str) -> Dict[str, Any]:
        """Verify Clerk JWT signature and claims; return decoded claims."""
        if not token:
            raise AuthError("JWT token is empty")

        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
        except Exception as exc:
            raise AuthError(
                f"Unable to resolve Clerk signing key: {exc}") from exc

        decode_kwargs: Dict[str, Any] = {
            "algorithms": ["RS256"],
            "issuer": self.settings.clerk_jwt_issuer,
        }
        if self.settings.clerk_audience:
            decode_kwargs["audience"] = self.settings.clerk_audience
        else:
            decode_kwargs["options"] = {"verify_aud": False}

        try:
            claims = jwt.decode(token, signing_key, **decode_kwargs)
        except jwt.PyJWTError as exc:
            raise AuthError(f"Invalid Clerk JWT: {exc}") from exc

        sub = (claims.get("sub") or "").strip()
        if not sub:
            raise AuthError("Verified JWT is missing required 'sub' claim")

        return claims

    def resolve_request_jwt(
        self,
        headers: Mapping[str, str],
        *,
        session_id: Optional[str] = None,
        allow_mint_fallback: bool = False,
    ) -> str:
        """
        Resolve current request JWT.

        Flow:
          1) Try Authorization: Bearer <token>
          2) Optional fallback: mint from Clerk with session_id
        """
        try:
            return self.extract_bearer_token(headers)
        except AuthError:
            if allow_mint_fallback and session_id:
                return self.mint_session_jwt(session_id=session_id)
            raise


class SupabaseRLSFactory:
    """Builds Supabase clients that send Clerk JWT in Authorization headers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_client_for_user_jwt(self, session_jwt: str) -> Client:
        if not session_jwt:
            raise DatabaseError(
                "Cannot create Supabase RLS client: session JWT is empty")

        headers = {
            "Authorization": f"Bearer {session_jwt}",
            "apikey": self.settings.supabase_anon_key,
        }

        try:
            if ClientOptions is not None:
                options = ClientOptions(headers=headers)
                return create_client(self.settings.supabase_url, self.settings.supabase_anon_key, options=options)

            # Backward-compatible fallback for older supabase-py versions.
            client = create_client(
                self.settings.supabase_url, self.settings.supabase_anon_key)
            if hasattr(client, "postgrest") and hasattr(client.postgrest, "session"):
                client.postgrest.session.headers.update(headers)
            return client
        except Exception as exc:
            raise DatabaseError(
                f"Failed to initialize Supabase client: {exc}") from exc


def fetch_current_user_preferences(
    headers: Mapping[str, str],
    *,
    settings: Optional[Settings] = None,
    select_columns: str = "*",
    session_id: Optional[str] = None,
    allow_mint_fallback: bool = False,
) -> Sequence[Dict[str, Any]]:
    """
    Fetch `user_preferences` rows for the authenticated Clerk user.

    Args:
        headers: Request headers (must include Authorization Bearer token unless fallback is used).
        settings: Optional injected Settings (uses env-backed Settings.from_env() when omitted).
        select_columns: PostgREST select expression.
        session_id: Optional Clerk session id (used only when allow_mint_fallback=True).
        allow_mint_fallback: If True, mint Clerk JWT via Backend API when Authorization header is missing.

    Returns:
        List of preference rows for the current user.
    """
    cfg = settings or Settings.from_env()
    auth = ClerkAuthManager(cfg)
    rls_factory = SupabaseRLSFactory(cfg)

    session_jwt = auth.resolve_request_jwt(
        headers,
        session_id=session_id,
        allow_mint_fallback=allow_mint_fallback,
    )
    claims = auth.verify_jwt(session_jwt)
    user_id = str(claims["sub"])

    supabase = rls_factory.create_client_for_user_jwt(session_jwt)

    def _query() -> Any:
        return (
            supabase
            .table(cfg.prefs_table)
            .select(select_columns)
            .eq(cfg.user_id_column, user_id)
            .execute()
        )

    response = _execute_with_retry(
        _query, attempts=3, initial_backoff_seconds=0.25)
    data = getattr(response, "data", None)

    if data is None:
        # Some client versions expose dict-like response.
        data = response.get("data") if isinstance(response, dict) else None

    if data is None:
        raise DatabaseError(
            "Supabase query succeeded but no data payload was returned")

    if not isinstance(data, list):
        raise DatabaseError(
            f"Expected list response from Supabase, got {type(data).__name__}")

    return data


__all__ = [
    "AuthError",
    "ConfigError",
    "DatabaseError",
    "Settings",
    "ClerkAuthManager",
    "SupabaseRLSFactory",
    "fetch_current_user_preferences",
]
