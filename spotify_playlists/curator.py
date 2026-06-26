"""Curadoria de faixas via Spotify Search.

O endpoint de "Recommendations" da Spotify foi restringido para apps novos
em 2024, então a curadoria aqui é feita com BUSCA: a gente pesquisa por
queries/gêneros/anos definidos na config, junta os resultados, remove
duplicatas e monta a playlist. As escolhas de "vibe" vêm das queries que
você (e eu, te ajudando) definimos no config/playlists.yaml.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from spotipy import Spotify


@dataclass
class Track:
    uri: str
    name: str
    artists: str

    def __str__(self) -> str:  # pragma: no cover - só exibição
        return f"{self.name} — {self.artists}"


@dataclass
class CurationSpec:
    """Como montar uma playlist. Espelha uma entrada do YAML."""

    queries: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    artist_seeds: list[str] = field(default_factory=list)
    year_range: str | None = None  # ex: "2010-2020"
    market: str = "BR"
    size: int = 30


def _track_from_item(item: dict) -> Track:
    return Track(
        uri=item["uri"],
        name=item["name"],
        artists=", ".join(a["name"] for a in item["artists"]),
    )


def _search_tracks(sp: Spotify, query: str, market: str, limit: int) -> list[Track]:
    """Busca faixas, variando o offset pra trazer resultados diferentes a cada run."""
    limit = max(1, min(limit, 50))  # teto da API
    offset = random.randint(0, 5) * limit
    try:
        resp = sp.search(q=query, type="track", market=market, limit=limit, offset=offset)
    except Exception:  # offset alto pode estourar; tenta sem offset
        resp = sp.search(q=query, type="track", market=market, limit=limit)
    return [_track_from_item(it) for it in resp.get("tracks", {}).get("items", [])]


def _artist_top_tracks(sp: Spotify, artist_name: str, market: str) -> list[Track]:
    found = sp.search(q=artist_name, type="artist", limit=1)
    items = found.get("artists", {}).get("items", [])
    if not items:
        return []
    top = sp.artist_top_tracks(items[0]["id"], country=market)
    return [_track_from_item(it) for it in top.get("tracks", [])]


def _build_query(base: str, genres: list[str], year_range: str | None) -> str:
    parts = [base] if base else []
    for genre in genres:
        parts.append(f'genre:"{genre}"')
    if year_range:
        parts.append(f"year:{year_range}")
    return " ".join(parts).strip()


def curate(sp: Spotify, spec: CurationSpec) -> list[Track]:
    """Monta a lista final de faixas para a playlist, sem duplicatas."""
    pool: list[Track] = []

    # Quanto buscar por query pra ter folga depois da dedupe.
    per_query = max(10, spec.size)

    queries = spec.queries or [""]
    for base in queries:
        query = _build_query(base, spec.genres, spec.year_range)
        if not query:
            continue
        pool.extend(_search_tracks(sp, query, spec.market, per_query))

    for artist in spec.artist_seeds:
        pool.extend(_artist_top_tracks(sp, artist, spec.market))

    # Remove duplicatas preservando a ordem.
    seen: set[str] = set()
    unique = [t for t in pool if not (t.uri in seen or seen.add(t.uri))]

    random.shuffle(unique)
    return unique[: spec.size]
