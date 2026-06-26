"""Detecção de estação do ano (com suporte a hemisfério sul — Brasil)."""

from __future__ import annotations

import datetime as _dt

# Ordem dos meses -> estação no HEMISFÉRIO NORTE.
# No sul, a estação é a oposta (somamos 2 estações / 6 meses).
_NORTH_BY_MONTH = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}

_OPPOSITE = {
    "winter": "summer",
    "summer": "winter",
    "spring": "autumn",
    "autumn": "spring",
}

# Tradução só pra exibição amigável.
PT = {
    "summer": "Verão",
    "autumn": "Outono",
    "winter": "Inverno",
    "spring": "Primavera",
}


def current_season(hemisphere: str = "southern", today: _dt.date | None = None) -> str:
    """Retorna a estação atual: 'summer' | 'autumn' | 'winter' | 'spring'.

    Brasil usa ``hemisphere='southern'`` (padrão).
    """
    today = today or _dt.date.today()
    north = _NORTH_BY_MONTH[today.month]
    if hemisphere.lower().startswith("s"):
        return _OPPOSITE[north]
    return north


def season_label(season: str) -> str:
    """Nome em português para exibição."""
    return PT.get(season, season)
