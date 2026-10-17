"""Point d'entrée du service ``bot``.

Étape 1 : charge et valide la configuration, puis s'arrête. La boucle
données → stratégie → risque → exécution arrive aux étapes suivantes.
"""

from __future__ import annotations

import logging
import sys

from pydantic import ValidationError

from bot.config import Settings
from bot.logs import setup_logging

log = logging.getLogger("bot")


def main() -> int:
    try:
        settings = Settings()
    except (ValidationError, ValueError) as exc:
        # Les SecretStr restent masqués dans le message de validation.
        print(f"Configuration invalide, démarrage refusé : {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    log.info(
        "bot démarré",
        extra={"mode": str(settings.mode), "symbols": settings.symbols},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
