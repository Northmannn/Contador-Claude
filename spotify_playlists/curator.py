"""Curadoria de faixas.

Dois modos:

* ``search``   — monta a playlist por BUSCA (queries/gêneros/anos da config).
* ``discovery`` — lê o SEU gosto (artistas/músicas mais ouvidos), monta algo
  parecido no ritmo e **exclui o que você já escutou**, pra ser descoberta.

O endpoint de "Recommendations"/"Related Artists" da Spotify foi restringido
para apps novos em 2024. Então a descoberta aqui é feita assim: pegamos os
artistas que você mais ouve dentro dos gêneros desejados (ex: samba/pagode),
puxamos faixas do catálogo deles (inclusive de álbuns, não só os hits) + da
mesma cena via busca, e tiramos tudo que já está nas suas top tracks / curtidas.
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

    # --- modo descoberta ---
    mode: str = "search"  # "search" | "discovery"
    seed_from_taste: bool = False  # usar seus artistas mais ouvidos como semente
    exclude_heard: bool = False  # remover faixas que você já escutou
    match_genres: list[str] = field(default_factory=list)  # ex: ["samba", "pagode"]


def _track_from_item(item: dict) -> Track | None:
    if not item or not item.get("uri"):
        return None
    return Track(
        uri=item["uri"],
        name=item["name"],
        artists=", ".join(a["name"] for a in item.get("artists", [])),
    )


def _dedupe(tracks: list[Track]) -> list[Track]:
    seen: set[str] = set()
    return [t for t in tracks if t and not (t.uri in seen or seen.add(t.uri))]


# --------------------------------------------------------------------------- #
# Modo SEARCH (o de sempre)
# --------------------------------------------------------------------------- #
def _search_tracks(sp: Spotify, query: str, market: str, limit: int) -> list[Track]:
    """Busca faixas, variando o offset pra trazer resultados diferentes a cada run."""
    limit = max(1, min(limit, 50))  # teto da API
    offset = random.randint(0, 5) * limit
    try:
        resp = sp.search(q=query, type="track", market=market, limit=limit, offset=offset)
    except Exception:  # offset alto pode estourar; tenta sem offset
        resp = sp.search(q=query, type="track", market=market, limit=limit)
    items = resp.get("tracks", {}).get("items", [])
    return [t for t in (_track_from_item(it) for it in items) if t]


def _artist_top_tracks_by_name(sp: Spotify, artist_name: str, market: str) -> list[Track]:
    found = sp.search(q=artist_name, type="artist", limit=1)
    items = found.get("artists", {}).get("items", [])
    if not items:
        return []
    top = sp.artist_top_tracks(items[0]["id"], country=market)
    return [t for t in (_track_from_item(it) for it in top.get("tracks", [])) if t]


def _build_query(base: str, genres: list[str], year_range: str | None) -> str:
    parts = [base] if base else []
    for genre in genres:
        parts.append(f'genre:"{genre}"')
    if year_range:
        parts.append(f"year:{year_range}")
    return " ".join(parts).strip()


def _curate_search(sp: Spotify, spec: CurationSpec) -> list[Track]:
    pool: list[Track] = []
    per_query = max(10, spec.size)

    for base in spec.queries or [""]:
        query = _build_query(base, spec.genres, spec.year_range)
        if query:
            pool.extend(_search_tracks(sp, query, spec.market, per_query))

    for artist in spec.artist_seeds:
        pool.extend(_artist_top_tracks_by_name(sp, artist, spec.market))

    unique = _dedupe(pool)
    random.shuffle(unique)
    return unique[: spec.size]


# --------------------------------------------------------------------------- #
# Modo DISCOVERY (baseado no seu gosto, sem repetir o que você já ouviu)
# --------------------------------------------------------------------------- #
def _heard_uris(sp: Spotify) -> set[str]:
    """URIs de tudo que você já escutou bastante: top tracks + músicas curtidas."""
    uris: set[str] = set()

    for time_range in ("short_term", "medium_term", "long_term"):
        try:
            resp = sp.current_user_top_tracks(limit=50, time_range=time_range)
        except Exception:
            continue
        uris.update(it["uri"] for it in resp.get("items", []) if it.get("uri"))

    # Músicas curtidas (paginado).
    try:
        page = sp.current_user_saved_tracks(limit=50)
        while page:
            for item in page.get("items", []):
                track = item.get("track") or {}
                if track.get("uri"):
                    uris.add(track["uri"])
            page = sp.next(page) if page.get("next") else None
    except Exception:
        pass

    return uris


def _matches_genres(artist_genres: list[str], wanted: list[str]) -> bool:
    if not wanted:
        return True
    blob = " ".join(artist_genres).lower()
    return any(w.lower() in blob for w in wanted)


def _taste_seed_artists(sp: Spotify, match_genres: list[str], limit: int = 12) -> list[dict]:
    """Seus artistas mais ouvidos que batem com os gêneros desejados."""
    by_id: dict[str, dict] = {}
    for time_range in ("short_term", "medium_term", "long_term"):
        try:
            resp = sp.current_user_top_artists(limit=20, time_range=time_range)
        except Exception:
            continue
        for art in resp.get("items", []):
            if art["id"] not in by_id and _matches_genres(art.get("genres", []), match_genres):
                by_id[art["id"]] = art

    return list(by_id.values())[:limit]


def _artist_catalog(sp: Spotify, artist_id: str, market: str) -> list[Track]:
    """Faixas do artista: top tracks + faixas de alguns álbuns (cortes mais fundos)."""
    tracks: list[Track] = []

    try:
        top = sp.artist_top_tracks(artist_id, country=market)
        tracks.extend(_track_from_item(it) for it in top.get("tracks", []))
    except Exception:
        pass

    try:
        albums = sp.artist_albums(artist_id, album_type="album,single", limit=12)
        album_ids = [a["id"] for a in albums.get("items", [])]
        random.shuffle(album_ids)
        for album_id in album_ids[:5]:
            at = sp.album_tracks(album_id, limit=30)
            tracks.extend(_track_from_item(it) for it in at.get("items", []))
    except Exception:
        pass

    return [t for t in tracks if t]


def _curate_discovery(sp: Spotify, spec: CurationSpec) -> list[Track]:
    pool: list[Track] = []

    # 1) Sementes: seus artistas mais ouvidos do gênero (ou a lista da config).
    seed_artists: list[dict] = []
    if spec.seed_from_taste:
        seed_artists = _taste_seed_artists(sp, spec.match_genres)

    if seed_artists:
        for art in seed_artists:
            pool.extend(_artist_catalog(sp, art["id"], spec.market))
    # Sempre reforça com a lista manual de artistas (se houver).
    for name in spec.artist_seeds:
        pool.extend(_artist_top_tracks_by_name(sp, name, spec.market))

    # 2) Mesma cena via busca (pega artistas vizinhos que você talvez não ouça).
    per_query = max(20, spec.size)
    for base in spec.queries:
        query = _build_query(base, spec.genres, spec.year_range)
        if query:
            pool.extend(_search_tracks(sp, query, spec.market, per_query))

    unique = _dedupe(pool)

    # 3) Tira o que você já escutou — o coração da "descoberta".
    if spec.exclude_heard:
        heard = _heard_uris(sp)
        unique = [t for t in unique if t.uri not in heard]

    random.shuffle(unique)
    return unique[: spec.size]


# --------------------------------------------------------------------------- #
def curate(sp: Spotify, spec: CurationSpec) -> list[Track]:
    """Monta a lista final de faixas, escolhendo o modo conforme a config."""
    if spec.mode == "discovery":
        return _curate_discovery(sp, spec)
    return _curate_search(sp, spec)
