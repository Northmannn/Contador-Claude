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
    match_genres: list[str] = field(default_factory=list)  # incluir só estes gêneros
    exclude_genres: list[str] = field(default_factory=list)  # nunca semear estes gêneros
    hits_only: bool = False  # só as faixas mais tocadas do artista (ideal p/ karaokê)


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
# Teto de resultados por busca após a migração da Web API (fev/2026): era 50.
SEARCH_MAX_LIMIT = 10


def _search_page(sp: Spotify, q: str, market: str, offset: int) -> list[Track]:
    """Uma página de busca (até 10 itens). Retorna [] em qualquer erro."""
    try:
        resp = sp.search(
            q=q, type="track", market=market, limit=SEARCH_MAX_LIMIT, offset=offset
        )
    except Exception:
        return []
    items = resp.get("tracks", {}).get("items", [])
    return [t for t in (_track_from_item(it) for it in items) if t]


def _search_tracks(sp: Spotify, query: str, market: str, want: int) -> list[Track]:
    """Busca até ``want`` faixas, paginando de 10 em 10.

    Desde a migração de fev/2026 a busca aceita no máximo ``limit=10``, então
    paginamos via ``offset`` pra juntar mais resultados. Filtros ``genre:`` /
    ``year:`` são instáveis na busca de faixas; se a query completa não trouxer
    nada, caímos pro texto puro — nunca deixamos uma query ruim zerar tudo.
    """
    base = query.split(" genre:")[0].split(" year:")[0].strip()
    candidates = [query] + ([base] if base and base != query else [])
    need = max(1, -(-want // SEARCH_MAX_LIMIT))  # nº de páginas pra encher

    for q in candidates:
        # Visita páginas aleatórias primeiro (variedade entre runs) e depois as
        # páginas iniciais (garante encher mesmo se as aleatórias caírem no fim
        # do catálogo, que retorna vazio).
        start = random.randint(0, 5)
        page_order = list(range(start, start + need + 1)) + list(range(need + 1))

        collected: list[Track] = []
        seen: set[str] = set()
        done_pages: set[int] = set()
        for page_idx in page_order:
            if len(collected) >= want:
                break
            if page_idx in done_pages:
                continue
            done_pages.add(page_idx)
            offset = page_idx * SEARCH_MAX_LIMIT
            if offset > 950:  # teto de offset da Web API
                continue
            for t in _search_page(sp, q, market, offset):
                if t.uri not in seen:
                    seen.add(t.uri)
                    collected.append(t)

        if collected:
            return collected[:want]
    return []


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


def _taste_seed_artists(
    sp: Spotify,
    match_genres: list[str],
    exclude_genres: list[str] | None = None,
    limit: int = 12,
) -> list[dict]:
    """Seus artistas mais ouvidos que batem com os gêneros desejados.

    ``match_genres`` vazio = aceita qualquer gênero. ``exclude_genres`` descarta
    artistas desses gêneros (ex: tirar funk/rap da playlist matinal).
    """
    exclude_genres = exclude_genres or []
    by_id: dict[str, dict] = {}
    for time_range in ("short_term", "medium_term", "long_term"):
        try:
            resp = sp.current_user_top_artists(limit=20, time_range=time_range)
        except Exception:
            continue
        for art in resp.get("items", []):
            genres = art.get("genres", [])
            if art["id"] in by_id:
                continue
            if not _matches_genres(genres, match_genres):
                continue
            if exclude_genres and _matches_genres(genres, exclude_genres):
                continue
            by_id[art["id"]] = art

    return list(by_id.values())[:limit]


def _artist_catalog(
    sp: Spotify, artist_id: str, market: str, hits_only: bool = False
) -> list[Track]:
    """Faixas do artista: top tracks + faixas de álbuns (cortes mais fundos).

    Com ``hits_only=True``, traz apenas as mais tocadas (ideal pra karaokê,
    onde você quer músicas conhecidas, não faixas obscuras).
    """
    tracks: list[Track] = []

    try:
        top = sp.artist_top_tracks(artist_id, country=market)
        tracks.extend(_track_from_item(it) for it in top.get("tracks", []))
    except Exception:
        pass

    if hits_only:
        return [t for t in tracks if t]

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
        seed_artists = _taste_seed_artists(sp, spec.match_genres, spec.exclude_genres)

    if seed_artists:
        for art in seed_artists:
            pool.extend(_artist_catalog(sp, art["id"], spec.market, spec.hits_only))
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
