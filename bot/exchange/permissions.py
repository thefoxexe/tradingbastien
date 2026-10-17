"""Contrôle des permissions de la clé API (``GET /v5/user/query-api``).

Liste blanche stricte : seules ``ContractTrade: Order + Position`` sont
admises. Toute autre permission (retrait, transferts, spot…) bloque le
démarrage (SPEC, règle 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

ALLOWED_PERMISSIONS: dict[str, frozenset[str]] = {
    "ContractTrade": frozenset({"Order", "Position"}),
}
EXPIRY_WARNING = timedelta(days=14)


class ForbiddenPermissionError(RuntimeError):
    """La clé a des droits au-delà du trading de contrats : démarrage refusé."""


@dataclass(frozen=True)
class KeyCheck:
    expires_at: datetime | None
    ip_bound: bool
    warnings: list[str] = field(default_factory=list)


def _parse_expiry(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def check_api_key(
    info: dict[str, Any], *, require_ip: bool, now: datetime | None = None
) -> KeyCheck:
    """Valide la réponse de ``query-api``. Lève ``ForbiddenPermissionError`` si refus."""
    now = now or datetime.now(UTC)
    forbidden: list[str] = []
    for group, perms in (info.get("permissions") or {}).items():
        extra = set(perms or []) - ALLOWED_PERMISSIONS.get(group, frozenset())
        forbidden.extend(f"{group}:{p}" for p in sorted(extra))
    if forbidden:
        raise ForbiddenPermissionError(f"Permissions interdites sur la clé : {forbidden}")
    if str(info.get("readOnly", "0")) == "1":
        raise ForbiddenPermissionError("Clé en lecture seule : impossible de poser des stops.")

    ips = [ip for ip in (info.get("ips") or []) if ip and ip != "*"]
    ip_bound = bool(ips)
    if require_ip and not ip_bound:
        raise ForbiddenPermissionError("Clé live non liée à l'IP du VPS.")

    warnings: list[str] = []
    expires_at = _parse_expiry(info.get("expiredAt"))
    if expires_at is not None and expires_at - now < EXPIRY_WARNING:
        warnings.append(f"La clé API expire le {expires_at:%Y-%m-%d}.")
    return KeyCheck(expires_at=expires_at, ip_bound=ip_bound, warnings=warnings)
