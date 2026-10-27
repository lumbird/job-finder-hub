"""WebAuthn (passkey) registration and sign-in around the `webauthn` library."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass

from fastapi import Request, Response
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.config import Settings
from app.models import PasskeyCredential, User

CHALLENGE_COOKIE = "hub_webauthn"
CHALLENGE_TTL_SECONDS = 300
RP_NAME = "Job Finder Hub"


@dataclass
class _Pending:
    challenge: bytes
    purpose: str  # "register" or "login"
    expires: float


class ChallengeStore:
    """In-memory, single-use challenges keyed by a random cookie. Fine for one process."""

    def __init__(self) -> None:
        self._items: dict[str, _Pending] = {}

    def put(self, response: Response, settings: Settings, challenge: bytes, purpose: str) -> None:
        self._sweep()
        key = secrets.token_urlsafe(24)
        self._items[key] = _Pending(challenge, purpose, time.monotonic() + CHALLENGE_TTL_SECONDS)
        response.set_cookie(
            CHALLENGE_COOKIE,
            key,
            max_age=CHALLENGE_TTL_SECONDS,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )

    def pop(self, request: Request, purpose: str) -> bytes | None:
        key = request.cookies.get(CHALLENGE_COOKIE)
        item = self._items.pop(key, None) if key else None
        if item is None or item.purpose != purpose or item.expires < time.monotonic():
            return None
        return item.challenge

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._items.items() if v.expires < now]:
            del self._items[key]


def registration_options(settings: Settings, user: User, existing: list[PasskeyCredential]):
    return generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=RP_NAME,
        user_id=str(user.id).encode(),
        user_name="me",
        user_display_name="Job Finder Hub",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.id)) for c in existing],
    )


def verify_registration(settings: Settings, credential: dict, challenge: bytes):
    """Returns (credential_id_b64url, public_key, sign_count). Raises on failure."""
    verified = verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=settings.rp_id,
        expected_origin=settings.public_origin,
        require_user_verification=True,
    )
    return bytes_to_base64url(verified.credential_id), verified.credential_public_key, verified.sign_count


def authentication_options(settings: Settings):
    # Empty allow-list: the phone offers its discoverable passkey for this site.
    return generate_authentication_options(
        rp_id=settings.rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
    )


def verify_authentication(settings: Settings, credential: dict, challenge: bytes, stored: PasskeyCredential) -> int:
    """Returns the new sign count. Raises on failure."""
    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=settings.rp_id,
        expected_origin=settings.public_origin,
        credential_public_key=stored.public_key,
        credential_current_sign_count=stored.sign_count,
        require_user_verification=True,
    )
    return verified.new_sign_count


def to_json(options) -> dict:
    return json.loads(options_to_json(options))

