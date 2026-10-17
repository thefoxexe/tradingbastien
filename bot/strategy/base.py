"""Interface des stratégies (plugins).

Une stratégie reçoit des bougies CLOSES et renvoie un signal (paire, sens,
entrée, stop) ou rien. Elle propose aussi la gestion des positions ouvertes
(nouveau stop, sortie) ; le module de risque vérifie qu'un stop ne s'éloigne
jamais. Elle ne connaît ni l'equity, ni la taille, ni l'exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from bot.models import Position, Signal


@dataclass(frozen=True)
class Management:
    new_stop: float | None = None
    exit: bool = False
    reason: str = ""


class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def warmup(self) -> int:
        """Nombre de bougies closes nécessaires."""
        ...

    def signal(self, symbol: str, bars: pd.DataFrame) -> Signal | None: ...

    def manage(self, position: Position, bars: pd.DataFrame) -> Management: ...
