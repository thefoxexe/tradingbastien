"""Service ``api`` : dashboard privé (Tailscale uniquement).

Étape 1 : seulement un endpoint de santé. Pages, pause et kill switch
arrivent à l'étape 7.
"""

from __future__ import annotations

from fastapi import FastAPI

from bot import __version__

app = FastAPI(title="tradingbastien", version=__version__, docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
