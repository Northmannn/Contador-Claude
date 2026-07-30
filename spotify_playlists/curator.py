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
from dataclasses import dataclass, field, replace

from spotipy import Spotify
from spotipy.exceptions import SpotifyException

# Endpoints que o app descobre estarem bloqueados (403) NESTE processo.
# Depois do 1º 403, paramos de chamá-los — economiza um monte de requisição
# (e evita estourar o rate limit) já que em Development Mode eles sempre negam.
_BLOCKED: set[str] = set()


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
    include_top_tracks: bool = False  # incluir SUAS músicas mais ouvidas (acolhimento)
    fixed_tracks: list[str] = field(default_factory=list)  # trilha exata, em ordem

    # --- variedade e aprendizado ---
    exclude_artists: list[str] = field(default_factory=list)  # nunca incluir estes artistas
    max_per_artist: int = 0  # nº máx. de faixas do mesmo artista (0 = sem limite)
    new_tracks: int = 0  # nº de faixas NOVAS (desconhecidas) na mistura conhecida
    sing_along: bool = False  # modo "cantar junto": maioria conhecida + poucas novas
    learn_removals: bool = False  # aprende com o que você tira da playlist (vira "não gosto")


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


def _artist_key(track: Track) -> str:
    """Chave do artista principal (pra limitar repetição e contar dislikes)."""
    return track.artists.split(",")[0].strip().lower() if track.artists else ""


def _excluded_by_artist(track: Track, exclude_artists: list[str]) -> bool:
    if not exclude_artists:
        return False
    blob = track.artists.lower()
    return any(a.strip().lower() in blob for a in exclude_artists if a.strip())


def _cap_per_artist(tracks: list[Track], max_per_artist: int) -> list[Track]:
    """Mantém no máximo ``max_per_artist`` faixas de cada artista (0 = sem limite)."""
    if not max_per_artist or max_per_artist < 1:
        return tracks
    counts: dict[str, int] = {}
    out: list[Track] = []
    for t in tracks:
        key = _artist_key(t)
        if counts.get(key, 0) >= max_per_artist:
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append(t)
    return out


def _drop(tracks: list[Track], disliked_uris, exclude_artists: list[str]) -> list[Track]:
    """Remove faixas que você já marcou como 'não gosto' e os artistas vetados."""
    return [
        t
        for t in tracks
        if t.uri not in disliked_uris and not _excluded_by_artist(t, exclude_artists)
    ]


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


def _artist_top_tracks_by_name(
    sp: Spotify, artist_name: str, market: str, want: int = 12
) -> list[Track]:
    """Faixas conhecidas de um artista pelo nome.

    Tenta o endpoint oficial de top tracks (que dá 403 em apps no Development
    Mode pós-migração de fev/2026) e, se falhar/vier vazio, cai na BUSCA por
    nome — que continua funcionando — pra nunca quebrar nem voltar vazio.
    """
    if "artist_top_tracks" not in _BLOCKED:
        try:
            found = sp.search(q=artist_name, type="artist", limit=1)
            items = found.get("artists", {}).get("items", [])
            if items:
                top = sp.artist_top_tracks(items[0]["id"], country=market)
                tracks = [t for t in (_track_from_item(it) for it in top.get("tracks", [])) if t]
                if tracks:
                    return tracks
        except SpotifyException as exc:
            if exc.http_status == 403:
                _BLOCKED.add("artist_top_tracks")  # não tenta mais neste processo
        except Exception:
            pass

    # Fallback resiliente: busca de faixas pelo nome do artista.
    return _search_tracks(sp, artist_name, market, want)


def _build_query(base: str, genres: list[str], year_range: str | None) -> str:
    parts = [base] if base else []
    for genre in genres:
        parts.append(f'genre:"{genre}"')
    if year_range:
        parts.append(f"year:{year_range}")
    return " ".join(parts).strip()


def _curate_search(
    sp: Spotify, spec: CurationSpec, disliked_uris=frozenset()
) -> list[Track]:
    pool: list[Track] = []
    per_query = max(10, spec.size)

    for base in spec.queries or [""]:
        query = _build_query(base, spec.genres, spec.year_range)
        if query:
            pool.extend(_search_tracks(sp, query, spec.market, per_query))

    for artist in spec.artist_seeds:
        pool.extend(_artist_top_tracks_by_name(sp, artist, spec.market))

    unique = _drop(_dedupe(pool), disliked_uris, spec.exclude_artists)
    random.shuffle(unique)
    unique = _cap_per_artist(unique, spec.max_per_artist)
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

    # Músicas curtidas (paginado, com teto de páginas pra não estourar o
    # rate limit em quem tem milhares de curtidas — 6 x 50 = 300 mais recentes).
    try:
        page = sp.current_user_saved_tracks(limit=50)
        fetched = 0
        while page and fetched < 6:
            for item in page.get("items", []):
                track = item.get("track") or {}
                if track.get("uri"):
                    uris.add(track["uri"])
            fetched += 1
            page = sp.next(page) if page.get("next") else None
    except Exception:
        pass

    return uris


def _user_top_tracks(sp: Spotify) -> list[Track]:
    """Suas faixas mais ouvidas (vários anos + recentes) — o que você de fato curte."""
    tracks: list[Track] = []
    for time_range in ("long_term", "medium_term", "short_term"):
        try:
            resp = sp.current_user_top_tracks(limit=50, time_range=time_range)
        except Exception:
            continue
        tracks.extend(t for t in (_track_from_item(it) for it in resp.get("items", [])) if t)
    return tracks


def _saved_tracks(sp: Spotify, max_pages: int = 6) -> list[Track]:
    """Suas músicas curtidas (as ~300 mais recentes) — coisas que você conhece."""
    tracks: list[Track] = []
    try:
        page = sp.current_user_saved_tracks(limit=50)
        fetched = 0
        while page and fetched < max_pages:
            for item in page.get("items", []):
                t = _track_from_item(item.get("track") or {})
                if t:
                    tracks.append(t)
            fetched += 1
            page = sp.next(page) if page.get("next") else None
    except Exception:
        pass
    return tracks


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
    sp: Spotify,
    artist_id: str,
    artist_name: str,
    market: str,
    hits_only: bool = False,
    want: int = 12,
) -> list[Track]:
    """Faixas do artista, resiliente ao Development Mode.

    Tenta os endpoints oficiais (top tracks + álbuns), que em apps no
    Development Mode pós-migração de fev/2026 podem dar 403. Se vierem
    poucas/nenhuma faixa, completa com BUSCA pelo nome do artista (que
    funciona). Com ``hits_only=True`` (karaokê), pula os álbuns.
    """
    tracks: list[Track] = []

    if "artist_top_tracks" not in _BLOCKED:
        try:
            top = sp.artist_top_tracks(artist_id, country=market)
            tracks.extend(_track_from_item(it) for it in top.get("tracks", []))
        except SpotifyException as exc:
            if exc.http_status == 403:
                _BLOCKED.add("artist_top_tracks")
        except Exception:
            pass

    if not hits_only and "artist_albums" not in _BLOCKED:
        try:
            albums = sp.artist_albums(artist_id, album_type="album,single", limit=12)
            album_ids = [a["id"] for a in albums.get("items", [])]
            random.shuffle(album_ids)
            for album_id in album_ids[:5]:
                at = sp.album_tracks(album_id, limit=30)
                tracks.extend(_track_from_item(it) for it in at.get("items", []))
        except SpotifyException as exc:
            if exc.http_status == 403:
                _BLOCKED.add("artist_albums")
        except Exception:
            pass

    tracks = [t for t in tracks if t]

    # Fallback/reforço: se o catálogo oficial veio fraco (403 em dev mode),
    # busca as faixas do artista pelo nome.
    if len(tracks) < want and artist_name:
        tracks.extend(_search_tracks(sp, artist_name, market, want))

    return tracks


def _curate_discovery(
    sp: Spotify, spec: CurationSpec, disliked_uris=frozenset()
) -> list[Track]:
    pool: list[Track] = []

    # 0) Suas próprias músicas mais ouvidas (acolhimento: letras que você já ama).
    if spec.include_top_tracks:
        pool.extend(_user_top_tracks(sp))

    # 1) Sementes: seus artistas mais ouvidos do gênero (ou a lista da config).
    seed_artists: list[dict] = []
    if spec.seed_from_taste:
        seed_artists = _taste_seed_artists(sp, spec.match_genres, spec.exclude_genres)

    if seed_artists:
        for art in seed_artists:
            pool.extend(
                _artist_catalog(
                    sp, art["id"], art.get("name", ""), spec.market, spec.hits_only
                )
            )
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

    # 4) Tira "não gosto" (removidas por você) e artistas vetados; limita repetição.
    unique = _drop(unique, disliked_uris, spec.exclude_artists)
    random.shuffle(unique)
    unique = _cap_per_artist(unique, spec.max_per_artist)
    return unique[: spec.size]


def _curate_sing_along(
    sp: Spotify, spec: CurationSpec, disliked_uris=frozenset()
) -> list[Track]:
    """Modo "cantar junto": maioria de músicas que você CONHECE (top tracks +
    curtidas) + umas poucas NOVAS (``new_tracks``) pra você ir conhecendo aos
    poucos. Sem repetir artista e sem os que você já vetou/removeu.
    """
    # Conhecidas: o que você mais ouve + curtidas (dá pra cantar de cabeça).
    known = _drop(
        _dedupe(_user_top_tracks(sp) + _saved_tracks(sp)),
        disliked_uris,
        spec.exclude_artists,
    )
    random.shuffle(known)

    # Novas: descobertas no seu gosto (o cap por artista é aplicado no fim, aqui não).
    disc = replace(
        spec,
        exclude_heard=True,
        include_top_tracks=False,
        sing_along=False,
        max_per_artist=0,
        size=max(spec.size * 4, 40),
    )
    new_list = _curate_discovery(sp, disc, disliked_uris)

    chosen: list[Track] = []
    chosen_uris: set[str] = set()
    counts: dict[str, int] = {}

    def add(track: Track) -> bool:
        if len(chosen) >= spec.size or track.uri in chosen_uris:
            return False
        key = _artist_key(track)
        if spec.max_per_artist and counts.get(key, 0) >= spec.max_per_artist:
            return False
        chosen.append(track)
        chosen_uris.add(track.uri)
        counts[key] = counts.get(key, 0) + 1
        return True

    # 1) até ``new_tracks`` novas  2) completa com conhecidas  3) sobra? mais novas.
    added_new = 0
    for t in new_list:
        if added_new >= max(0, spec.new_tracks):
            break
        if add(t):
            added_new += 1
    for t in known:
        if len(chosen) >= spec.size:
            break
        add(t)
    for t in new_list:
        if len(chosen) >= spec.size:
            break
        add(t)

    random.shuffle(chosen)
    return chosen[: spec.size]


def _curate_fixed(sp: Spotify, spec: CurationSpec) -> list[Track]:
    """Playlist de trilha: resolve cada entrada de ``fixed_tracks`` NA ORDEM.

    Cada entrada é um texto de busca tipo "Título Artista". Pegamos o primeiro
    resultado; se não achar, a faixa é pulada (avisamos no console) — a ordem
    das demais é preservada, nunca embaralhada.
    """
    tracks: list[Track] = []
    seen: set[str] = set()
    for query in spec.fixed_tracks:
        found = _search_page(sp, query, spec.market, offset=0)
        if not found:
            print(f"   ⚠️ não achei: {query!r} — pulando")
            continue
        t = found[0]
        if t.uri not in seen:
            seen.add(t.uri)
            tracks.append(t)
    return tracks


# --------------------------------------------------------------------------- #
def curate(sp: Spotify, spec: CurationSpec, disliked_uris=frozenset()) -> list[Track]:
    """Monta a lista final de faixas, escolhendo o modo conforme a config.

    ``disliked_uris`` são faixas que você tirou de playlists antes (aprendidas
    pelo manager) — nunca voltam. Trilhas fixas (curadas a dedo) ignoram esse
    filtro, porque foram você/eu que escolhemos explicitamente.
    """
    if spec.fixed_tracks:
        return _curate_fixed(sp, spec)
    if spec.sing_along:
        return _curate_sing_along(sp, spec, disliked_uris)
    if spec.mode == "discovery":
        return _curate_discovery(sp, spec, disliked_uris)
    return _curate_search(sp, spec, disliked_uris)
