"""Rapport de backtest HTML autonome (aucune dépendance externe, SVG inline)."""

from __future__ import annotations

from html import escape
from pathlib import Path

import pandas as pd

from bot.backtest.engine import BacktestResult
from bot.backtest.metrics import Criterion, Metrics, WalkForward

_CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--line:#2563eb;--neg:#c2410c;--pos:#15803d;--card:#fff;--bd:#e5e5e0}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececea;--muted:#9a9a94;--line:#60a5fa;--neg:#fb923c;
--pos:#4ade80;--card:#1d1d1d;--bd:#2c2c2c}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}
main{max-width:1000px;margin:auto}h1{font-size:22px}h2{font-size:17px;margin-top:32px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
td,th{padding:6px 8px;border-bottom:1px solid var(--bd);text-align:right}td:first-child,th:first-child{text-align:left}
.ok{color:var(--pos);font-weight:600}.ko{color:var(--neg);font-weight:600}.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi b{display:block;font-size:20px}svg{width:100%;height:auto}
"""


def _svg_line(series: pd.Series, *, color: str, height: int = 220, fill: bool = False) -> str:
    if len(series) < 2:
        return "<p class='muted'>Pas assez de points.</p>"
    w, h, pad = 960, height, 4
    values = [float(v) for v in series]
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    step = (w - 2 * pad) / (len(values) - 1)
    pts = [(pad + i * step, pad + (hi - v) / span * (h - 2 * pad)) for i, v in enumerate(values)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (
        f"<polygon points='{pad},{pad} {path} {w - pad},{pad}' fill='{color}' opacity='.15'/>"
        if fill
        else ""
    )
    first, last = series.index[0], series.index[-1]
    return (
        f"<svg viewBox='0 0 {w} {h + 20}' role='img'>{area}"
        f"<polyline points='{path}' fill='none' stroke='{color}' stroke-width='1.6'/>"
        f"<text x='{pad}' y='{h + 16}' font-size='12' fill='currentColor' opacity='.6'>{first:%Y-%m-%d}</text>"
        f"<text x='{w - pad}' y='{h + 16}' font-size='12' text-anchor='end' fill='currentColor' opacity='.6'>"
        f"{last:%Y-%m-%d}</text>"
        f"<text x='{pad}' y='14' font-size='12' fill='currentColor' opacity='.6'>{hi:,.2f}</text>"
        f"<text x='{pad}' y='{h - 4}' font-size='12' fill='currentColor' opacity='.6'>{lo:,.2f}</text></svg>"
    )


def _metrics_rows(m: Metrics) -> str:
    rows = [
        ("Trades", f"{m.trades}"),
        ("Taux de réussite", f"{m.win_rate:.1%}"),
        ("Profit factor", f"{m.profit_factor:.2f}"),
        ("R moyen", f"{m.avg_r:+.2f}"),
        ("P&L net", f"{m.net_pnl:+,.2f} USDT"),
        ("Rendement total", f"{m.total_return:+.1%}"),
        ("Drawdown max", f"{m.max_drawdown:.1%}"),
        ("Frais", f"{m.fees:,.2f} USDT"),
        ("Funding", f"{m.funding:+,.2f} USDT"),
        ("Période", f"{m.years:.1f} ans"),
    ]
    return "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)


def render_report(
    result: BacktestResult,
    full: Metrics,
    wf: WalkForward,
    mc: dict[str, float],
    criteria: list[Criterion],
    *,
    title: str = "Backtest — tendance 4 h",
    notes: str = "",
) -> str:
    eq = result.equity
    dd = (eq / eq.cummax() - 1) * 100 if len(eq) else eq
    verdict = all(c.passed for c in criteria)
    crit_rows = "".join(
        f"<tr><td>{escape(c.name)}</td><td>{escape(c.value)}</td>"
        f"<td class='{'ok' if c.passed else 'ko'}'>{'OK' if c.passed else 'NON'}</td></tr>"
        for c in criteria
    )
    trade_rows = "".join(
        f"<tr><td>{t.opened_at:%Y-%m-%d %H:%M}</td><td>{t.symbol}</td><td>{t.side.value}</td>"
        f"<td>{t.entry:,.2f}</td><td>{t.exit:,.2f}</td><td>{t.r_multiple:+.2f}</td>"
        f"<td>{t.net_pnl:+,.2f}</td><td>{t.fees:,.2f}</td><td>{t.funding:+,.2f}</td>"
        f"<td>{escape(t.exit_reason)}</td></tr>"
        for t in result.trades
    )
    veto_rows = "".join(
        f"<tr><td>{escape(k)}</td><td>{v}</td></tr>" for k, v in sorted(result.vetoes.items())
    )
    events = "".join(f"<li>{escape(e)}</li>" for e in result.events[-50:])
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Rapport de backtest</title>
<style>{_CSS}</style></head><body><main>
<h1>{escape(title)}</h1>
<p class="muted">{escape(notes)}</p>
<div class="card"><b>Verdict go/no-go : <span class="{"ok" if verdict else "ko"}">
{"PASSE — démo autorisée" if verdict else "ÉCHEC — ne pas passer en démo"}</span></b>
<table><tr><th>Critère</th><th>Valeur</th><th></th></tr>{crit_rows}</table></div>
<div class="grid">
<div class="card kpi">Profit factor<b>{full.profit_factor:.2f}</b></div>
<div class="card kpi">Drawdown max<b>{full.max_drawdown:.1%}</b></div>
<div class="card kpi">Trades<b>{full.trades}</b></div>
<div class="card kpi">Rendement<b>{full.total_return:+.1%}</b></div>
<div class="card kpi">DD Monte Carlo p95<b>{mc["p95"]:.1%}</b></div>
</div>
<h2>Courbe d'equity (USDT)</h2><div class="card">{_svg_line(eq, color="var(--line)")}</div>
<h2>Drawdown (%)</h2><div class="card">{_svg_line(dd, color="var(--neg)", height=140, fill=True)}</div>
<h2>Métriques</h2><div class="card"><table>{_metrics_rows(full)}</table></div>
<h2>Walk-forward (coupure {wf.split:%Y-%m-%d})</h2>
<div class="grid"><div class="card"><b>Échantillon (70 %)</b><table>{_metrics_rows(wf.in_sample)}</table></div>
<div class="card"><b>Hors échantillon (30 %)</b><table>{_metrics_rows(wf.out_of_sample)}</table></div></div>
<h2>Monte Carlo (ordre des trades, 2000 tirages)</h2>
<div class="card"><table><tr><td>Drawdown médian</td><td>{mc["p50"]:.1%}</td></tr>
<tr><td>Drawdown p95</td><td>{mc["p95"]:.1%}</td></tr><tr><td>Drawdown p99</td><td>{mc["p99"]:.1%}</td></tr>
</table></div>
<h2>Vetos du module de risque</h2><div class="card"><table>{veto_rows or "<tr><td>Aucun</td></tr>"}</table></div>
<h2>Événements de risque</h2><div class="card"><ul>{events or "<li>Aucun</li>"}</ul></div>
<h2>Trades</h2><div class="card"><table><tr><th>Ouverture</th><th>Paire</th><th>Sens</th><th>Entrée</th>
<th>Sortie</th><th>R</th><th>P&amp;L</th><th>Frais</th><th>Funding</th><th>Raison</th></tr>{trade_rows}</table></div>
</main></body></html>"""


def write_report(html: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
