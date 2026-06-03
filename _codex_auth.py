"""Shared Codex auth helpers — zero side effects, zero third-party deps.

Imported by both `server.py` and `lib_preflight.py` so that auth.json discovery
and JWT decoding live in exactly one place (not duplicated across the two).
"""
import base64
import json
import os
from pathlib import Path

# auth.json lookup order (first readable one wins).
AUTH_CANDIDATES = [
    os.environ.get("CODEX_IMAGE_AUTH_FILE"),
    (os.path.join(os.environ["CODEX_HOME"], "auth.json")
     if os.environ.get("CODEX_HOME") else None),
    str(Path.home() / ".codex" / "auth.json"),
]


def jwt_claims(token: str | None) -> dict | None:
    """Decode a JWT payload (no signature check) into its claims dict, or None."""
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else None
    except Exception:  # never bare except — would swallow sys.exit/KeyboardInterrupt
        return None


def jwt_exp(token: str | None) -> float | None:
    """Return a JWT's `exp` (epoch seconds), or None."""
    claims = jwt_claims(token)
    exp = claims.get("exp") if claims else None
    return float(exp) if isinstance(exp, (int, float)) else None
