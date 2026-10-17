"""Commandes manuelles sur le VPS (jamais exposées dans le dashboard).

    python -m bot.admin status
    python -m bot.admin resume          # reprise après une pause manuelle
    python -m bot.admin clear-halt      # sortie du mode HALT, confirmation tapée à la main

Le bot doit être arrêté pendant ``clear-halt`` (``docker compose stop bot``) :
il relit l'état au redémarrage.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from bot.config import Settings
from bot.models import BotStatus
from bot.risk.manager import HALT_CONFIRMATION, RiskManager, RiskState
from bot.store import Store


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.admin")
    parser.add_argument("command", choices=["status", "resume", "clear-halt"])
    args = parser.parse_args(argv)
    settings = Settings()
    store = Store(settings.database_url.get_secret_value())
    risk = RiskManager(settings.risk_config(), RiskState.from_dict(store.get("risk_state") or {}))
    state = risk.state
    print(f"Statut : {state.status.value} — {state.status_reason or '-'}")

    if args.command == "resume":
        if state.status is not BotStatus.PAUSE:
            print("Rien à reprendre (le bot n'est pas en PAUSE).")
            return 1
        risk.resume()
    elif args.command == "clear-halt":
        if state.status is not BotStatus.HALT:
            print("Le bot n'est pas en HALT.")
            return 1
        print(f"Pour sortir du mode HALT, tapez exactement : {HALT_CONFIRMATION}")
        if not risk.clear_halt(ask("> ").strip()):
            print("Confirmation incorrecte, rien n'a changé.", file=sys.stderr)
            return 1
        print("HALT levé : le bot redémarrera en PAUSE, puis `resume` pour reprendre.")
    else:
        return 0
    store.put("risk_state", risk.state.to_dict())
    store.log_event("admin", f"{args.command} → {risk.state.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
