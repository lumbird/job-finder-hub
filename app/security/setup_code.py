"""One-time setup code, printed to the logs on first run and needed to create the login."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

log = logging.getLogger("hub.setup")

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


def _normalise(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _digest(code: str) -> str:
    return hashlib.sha256(_normalise(code).encode()).hexdigest()


class SetupCode:
    def __init__(self) -> None:
        self._digest: str | None = None

    @property
    def active(self) -> bool:
        return self._digest is not None

    def issue(self) -> str:
        code = "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))
        self._digest = _digest(code)
        log.warning(
            "\n\n  ┌──────────────────────────────────────────────┐\n"
            "  │  Job Finder Hub first-run setup code:        │\n"
            "  │                                              │\n"
            "  │      %s                          │\n"
            "  │                                              │\n"
            "  │  Open the hub and enter it to create your    │\n"
            "  │  login. It stops working once used.          │\n"
            "  └──────────────────────────────────────────────┘\n",
            code,
        )
        return code

    def check(self, code: str) -> bool:
        return self._digest is not None and hmac.compare_digest(self._digest, _digest(code))

    def consume(self) -> None:
        self._digest = None
