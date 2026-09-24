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
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

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
    artist_ids: list[str] = field(default_factory=list)  # p/ consultar gêneros em lote

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
    portuguese_only: bool = False  # só português; inglês apenas se já estiver nas curtidas
    match_artists: list[str] = field(default_factory=list)  # LISTA DE PERMITIDOS (por nome)
    foreign_artists: list[str] = field(default_factory=list)  # estrangeiros: só do acervo conhecido
    exclude_keywords: list[str] = field(default_factory=list)  # veta por palavra no título/artista
    rotation_days: int = 0  # não repetir a mesma MÚSICA (qualquer versão) por N dias


def _track_from_item(item: dict) -> Track | None:
    if not item or not item.get("uri"):
        return None
    artists = item.get("artists", []) or []
    return Track(
        uri=item["uri"],
        name=item["name"],
        artists=", ".join(a["name"] for a in artists),
        artist_ids=[a["id"] for a in artists if a.get("id")],
    )


# --------------------------------------------------------------------------- #
# Idioma e gênero por artista (pra regra "só português" e "só melódico")
# --------------------------------------------------------------------------- #
_GENRE_CACHE: dict[str, list[str]] = {}  # artist_id -> genres (por processo)

_BR_GENRE_KEYS = (
    "brazil", "brasil", "sertanej", "pagode", "samba", "mpb", "axé", "axe",
    "forró", "forro", "arrocha", "piseiro", "bossa", "nacional", "carioca",
    "paulista", "baian", "gaúch", "gauch", "mineir", "nordestin", "lambada",
    "brega", "mandel", "mtg", "tropicalia", "tropicália",
)
_PT_MARKERS = ("ç", "ã", "õ", "á", "é", "í", "ó", "ú", "â", "ê", "ô")
_PT_WORDS = {
    "de", "do", "da", "que", "não", "nao", "você", "voce", "meu", "minha", "amor",
    "coração", "pra", "com", "eu", "sem", "tudo", "mais", "quero", "vem", "seu",
    "sua", "nós", "bem", "noite", "vida", "saudade", "fica", "deixa", "ainda",
}


def _remember_genres(artists: list[dict]) -> None:
    for a in artists or []:
        if a and a.get("id"):
            _GENRE_CACHE.setdefault(a["id"], a.get("genres", []) or [])


def _artist_genres_for(sp: Spotify, tracks: list[Track]) -> dict[str, list[str]]:
    """Gêneros do artista principal de cada faixa, em lotes de 50 (GET /artists).

    Cache por processo; se o endpoint der 403 (Development Mode), marca em
    ``_BLOCKED`` e segue só com o que já se sabe (top artists).
    """
    ids: list[str] = []
    for t in tracks:
        if t.artist_ids and t.artist_ids[0] not in _GENRE_CACHE:
            ids.append(t.artist_ids[0])
    ids = list(dict.fromkeys(ids))
    if ids and "artists" not in _BLOCKED:
        for i in range(0, len(ids), 50):
            try:
                resp = sp.artists(ids[i : i + 50])
                _remember_genres(resp.get("artists", []) or [])
            except SpotifyException as exc:
                if exc.http_status == 403:
                    _BLOCKED.add("artists")
                break
            except Exception:
                break
    return _GENRE_CACHE


def _genres_of(track: Track) -> list[str]:
    return _GENRE_CACHE.get(track.artist_ids[0], []) if track.artist_ids else []


def _is_brazilian(genres: list[str]) -> bool:
    blob = " ".join(genres).lower()
    return any(k in blob for k in _BR_GENRE_KEYS)


def _looks_portuguese(title: str, artists: str = "") -> bool:
    """Acento conta só no TÍTULO (senão 'Bublé' faz 'Feeling Good' virar PT);
    palavras comuns contam no título + artista ('Grupo Menos É Mais')."""
    low_title = (title or "").lower()
    if any(m in low_title for m in _PT_MARKERS):
        return True
    words = set(re.findall(r"[a-zà-ú]+", f"{low_title} {(artists or '').lower()}"))
    return bool(words & _PT_WORDS)


def _passes_language(
    track: Track, saved_uris: set[str], spec: CurationSpec, allow_by_list: bool = False
) -> bool:
    """Regra do idioma: português sempre; estrangeira só se já for curtida.

    - ``foreign_artists`` (ex.: Michael Bublé, Boyce Avenue): entram só no acervo
      conhecido (``allow_by_list=True``); como faixa NOVA, nunca.
    - artista brasileiro da lista de permitidos passa direto (foi curado por nome,
      não depende de acento no título).
    - resto: gênero (se houver), heurística de texto, ou já estar nas curtidas.
    """
    if not spec.portuguese_only:
        return True
    if spec.foreign_artists and _name_matches(track.artists, spec.foreign_artists):
        return allow_by_list
    if spec.match_artists and _name_matches(track.artists, spec.match_artists):
        return True
    genres = _genres_of(track)
    if genres:
        return _is_brazilian(genres) or track.uri in saved_uris
    return _looks_portuguese(track.name, track.artists) or track.uri in saved_uris


_FUNK_NAME_RE = re.compile(r"(^|[\s,/&(])(mc|mcs|dj)\b\.?", re.IGNORECASE)


def _looks_funk(track: Track) -> bool:
    """Heurística pelo nome: 'MC Fulano' / 'DJ Beltrano' = funk/eletrônico."""
    return bool(_FUNK_NAME_RE.search(track.artists or ""))


def _passes_genres(track: Track, spec: CurationSpec) -> bool:
    """Aplica exclude/match de gêneros a UMA faixa.

    Gênero desconhecido (a API devolve muitos artistas sem tag) não barra —
    exceto o que dá pra reconhecer pelo nome: se a config exclui funk e o
    artista é "MC …"/"DJ …", fica de fora.
    """
    genres = _genres_of(track)
    if not genres:
        excludes_funk = any("funk" in g.lower() for g in spec.exclude_genres)
        return not (excludes_funk and _looks_funk(track))
    if spec.exclude_genres and _matches_genres(genres, spec.exclude_genres):
        return False
    if spec.match_genres and not _matches_genres(genres, spec.match_genres):
        return False
    return True


def _dedupe(tracks: list[Track]) -> list[Track]:
    seen: set[str] = set()
    return [t for t in tracks if t and not (t.uri in seen or seen.add(t.uri))]


def _artist_key(track: Track) -> str:
    """Chave do artista principal (pra limitar repetição e contar dislikes)."""
    return track.artists.split(",")[0].strip().lower() if track.artists else ""


def _name_matches(name_blob: str, patterns: list[str]) -> bool:
    """Casa nome de artista por PALAVRA INTEIRA ('Belo' não casa 'Rabelo')."""
    blob = (name_blob or "").lower()
    for pat in patterns or []:
        pat = pat.strip().lower()
        if pat and re.search(r"(?<!\w)" + re.escape(pat) + r"(?!\w)", blob):
            return True
    return False


def _excluded_by_artist(track: Track, exclude_artists: list[str]) -> bool:
    return _name_matches(track.artists, exclude_artists)


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


def _has_keyword(track: Track, keywords: list[str]) -> bool:
    if not keywords:
        return False
    blob = f"{track.name} {track.artists}".lower()
    return any(k.strip().lower() in blob for k in keywords if k.strip())


def _artist_allowed(track: Track, match_artists: list[str]) -> bool:
    """Lista de permitidos por nome (vazia = todos permitidos)."""
    if not match_artists:
        return True
    return _name_matches(track.artists, match_artists)


def _drop(
    tracks: list[Track],
    disliked_uris,
    exclude_artists: list[str],
    exclude_keywords: list[str] | None = None,
) -> list[Track]:
    """Remove 'não gosto', artistas vetados e títulos com palavras vetadas."""
    return [
        t
        for t in tracks
        if t.uri not in disliked_uris
        and not _excluded_by_artist(t, exclude_artists)
        and not _has_keyword(t, exclude_keywords or [])
    ]


_MEDLEY_PREFIX = re.compile(r"^(pot[\s-]?pourri|medley)\s*:?\s*", re.IGNORECASE)


def song_keys(name: str) -> set[str]:
    """Identidade da MÚSICA, independente da versão.

    'Talvez - Ao Vivo', 'Talvez (feat. X)' → {'talvez'}; medleys viram uma chave
    por música: '(Medley) Para Tudo / Loucura do Seu Coração - Ao Vivo' →
    {'para tudo', 'loucura do seu coração'}. Assim a mesma canção em outra
    gravação/medley conta como repetição.
    """
    s = re.sub(r"[\(\[].*?[\)\]]", " ", name or "")
    s = re.split(r"\s[-–—]\s", s)[0]
    keys: set[str] = set()
    for part in re.split(r"\s*/\s*", s):
        part = _MEDLEY_PREFIX.sub("", part.strip())
        k = re.sub(r"[^\w\s]", "", part.lower())
        k = re.sub(r"\s+", " ", k).strip()
        if k:
            keys.add(k)
    return keys


def _norm_title(name: str) -> str:
    """Compat: chave única e estável da música (a menor chave do medley)."""
    keys = song_keys(name)
    return min(keys) if keys else ""


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

    unique = _drop(_dedupe(pool), disliked_uris, spec.exclude_artists, spec.exclude_keywords)
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


def _saved_tracks(sp: Spotify, max_pages: int = 12) -> list[Track]:
    """Suas músicas curtidas (as ~600 mais recentes) — coisas que você conhece."""
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
    exclude_artists: list[str] | None = None,
    match_artists: list[str] | None = None,
    limit: int = 12,
) -> list[dict]:
    """Seus artistas mais ouvidos que batem com os gêneros desejados.

    ``match_genres`` vazio = aceita qualquer gênero. ``exclude_genres`` descarta
    artistas desses gêneros (ex: tirar funk/rap da playlist matinal).

    Pós-migração (2026) a API devolve a maioria dos artistas SEM ``genres``.
    Artista sem tag não é barrado por gênero (senão nenhum dos seus artistas
    vira semente e as "novas" viram busca genérica = artistas aleatórios);
    pra ele valem só os vetos por NOME (``exclude_artists`` e "MC/DJ" = funk).
    """
    exclude_genres = exclude_genres or []
    exclude_artists = [a.strip().lower() for a in (exclude_artists or []) if a.strip()]
    match_artists = [a.strip().lower() for a in (match_artists or []) if a.strip()]
    excludes_funk = any("funk" in g.lower() for g in exclude_genres)
    by_id: dict[str, dict] = {}
    for time_range in ("short_term", "medium_term", "long_term"):
        try:
            resp = sp.current_user_top_artists(limit=20, time_range=time_range)
        except Exception:
            continue
        _remember_genres(resp.get("items", []))
        for art in resp.get("items", []):
            if art["id"] in by_id:
                continue
            name = art.get("name") or ""
            if exclude_artists and _name_matches(name, exclude_artists):
                continue
            if match_artists and not _name_matches(name, match_artists):
                continue
            genres = art.get("genres", []) or []
            if genres:
                if not _matches_genres(genres, match_genres):
                    continue
                if exclude_genres and _matches_genres(genres, exclude_genres):
                    continue
            elif excludes_funk and _FUNK_NAME_RE.search(art.get("name") or ""):
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
        seed_artists = _taste_seed_artists(
            sp, spec.match_genres, spec.exclude_genres, spec.exclude_artists, spec.match_artists
        )

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

    # 4) Tira "não gosto" (removidas por você), vetos por nome/palavra e, se
    #    houver lista de permitidos, só ela; limita repetição de artista.
    unique = _drop(unique, disliked_uris, spec.exclude_artists, spec.exclude_keywords)
    unique = [t for t in unique if _artist_allowed(t, spec.match_artists)]
    random.shuffle(unique)
    unique = _cap_per_artist(unique, spec.max_per_artist)
    return unique[: spec.size]


# --------------------------------------------------------------------------- #
# Catálogo dos artistas permitidos (cache em data/catalog.json, pelo manager)
# --------------------------------------------------------------------------- #
# Sem "top tracks"/"álbuns" (403 em Development Mode), o catálogo de cada artista
# vem da BUSCA com filtro artist:"Nome" (ordem ≈ popularidade = os hits dele).
# Cacheado com TTL e atualizado em lotes pequenos por execução, com pausa entre
# chamadas — nunca estourar o rate limit (ver AGENTS.md §6).
CATALOG_PAGES = 3          # 3 x 10 = até 30 músicas por artista
CATALOG_TTL_DAYS = 14
CATALOG_PAUSE_S = 0.35


class CatalogRateLimited(Exception):
    """429 durante a atualização do catálogo: paramos e seguimos com o cache."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_artist_catalog(sp: Spotify, name: str, market: str, pages: int) -> list[Track]:
    tracks: list[Track] = []
    seen: set[str] = set()
    for query in (f'artist:"{name}"', name):
        for page in range(pages):
            try:
                resp = sp.search(
                    q=query, type="track", market=market,
                    limit=SEARCH_MAX_LIMIT, offset=page * SEARCH_MAX_LIMIT,
                )
            except SpotifyException as exc:
                if exc.http_status == 429:
                    raise CatalogRateLimited() from exc
                break
            except Exception:
                break
            finally:
                time.sleep(CATALOG_PAUSE_S)
            items = resp.get("tracks", {}).get("items", [])
            if not items:
                break
            for t in (_track_from_item(it) for it in items):
                # só músicas DESTE artista (a busca por texto traz homônimos)
                if t and t.uri not in seen and _name_matches(t.artists, [name]):
                    seen.add(t.uri)
                    tracks.append(t)
        if tracks:
            break  # o filtro artist:"" funcionou; não precisa do texto puro
    return tracks


def refresh_catalog(
    sp: Spotify,
    cache: dict,
    artists: list[str],
    market: str,
    budget: int,
    ttl_days: int = CATALOG_TTL_DAYS,
    pages: int = CATALOG_PAGES,
) -> int:
    """Atualiza até ``budget`` artistas vencidos/ausentes (os mais velhos antes).

    ``cache`` = {"artists": {nome_minúsculo: {"name", "fetched", "tracks": [...]}}}.
    Retorna quantos artistas foram atualizados. Em 429, para e mantém o cache.
    """
    entries = cache.setdefault("artists", {})
    now = _utcnow()

    def age(name: str) -> float:
        e = entries.get(name.lower())
        if not e or not e.get("fetched"):
            return float("inf")
        try:
            return (now - datetime.fromisoformat(e["fetched"])).total_seconds()
        except ValueError:
            return float("inf")

    due = [a for a in artists if age(a) >= ttl_days * 86400]
    due.sort(key=age, reverse=True)
    done = 0
    for name in due[: max(0, budget)]:
        try:
            tracks = _fetch_artist_catalog(sp, name, market, pages)
        except CatalogRateLimited:
            print("   ⛔ rate limit ao atualizar o catálogo — sigo com o que já tenho.")
            break
        entries[name.lower()] = {
            "name": name,
            "fetched": now.isoformat(timespec="seconds"),
            "tracks": [
                {"uri": t.uri, "name": t.name, "artists": t.artists, "artist_ids": t.artist_ids}
                for t in tracks
            ],
        }
        done += 1
    return done


def catalog_tracks(cache: dict, artists: list[str]) -> list[Track]:
    """Faixas cacheadas dos artistas pedidos (vazio se ainda não baixou)."""
    out: list[Track] = []
    for name in artists:
        e = (cache.get("artists") or {}).get(name.lower())
        for d in (e or {}).get("tracks", []):
            out.append(
                Track(d["uri"], d["name"], d["artists"], list(d.get("artist_ids") or []))
            )
    return out


def _last_played(track: Track, recent: dict[str, str]) -> str | None:
    """Última vez (ISO) que QUALQUER versão desta música tocou; None = fresca."""
    dates = [recent[k] for k in song_keys(track.name) if k in recent]
    return max(dates) if dates else None


def _curate_sing_along(
    sp: Spotify,
    spec: CurationSpec,
    disliked_uris=frozenset(),
    recent: dict[str, str] | None = None,
    catalog: list[Track] | None = None,
) -> list[Track]:
    """Modo "cantar junto".

    - CONHECIDAS: suas mais ouvidas + curtidas (dá pra cantar de cabeça).
    - NOVAS (``new_tracks``): músicas dos seus artistas que o Spotify não te
      viu ouvir — vêm do catálogo cacheado dos artistas permitidos.
    - Rodízio: nada que tocou nos últimos ``rotation_days`` dias volta (qualquer
      versão/medley da mesma música). Quando o acervo conhecido acaba na semana,
      completa com hits dos SEUS artistas (catálogo); só em último caso reusa,
      e aí a que tocou há mais tempo.
    - 1 por artista, lista de permitidos, idioma, vetos e "não gosto".
    """
    recent = recent or {}

    # Conhecidas: o que você mais ouve + curtidas.
    saved = _saved_tracks(sp)
    saved_uris = {t.uri for t in saved}
    known_all = _dedupe(_user_top_tracks(sp) + saved)
    heard_uris = {t.uri for t in known_all}
    known = _drop(known_all, disliked_uris, spec.exclude_artists, spec.exclude_keywords)
    known = [t for t in known if _artist_allowed(t, spec.match_artists)]
    _artist_genres_for(sp, known)
    known = [
        t for t in known
        if _passes_genres(t, spec) and _passes_language(t, saved_uris, spec, allow_by_list=True)
    ]

    # Catálogo dos artistas permitidos = novas + reserva do rodízio.
    if catalog:
        extra = _drop(_dedupe(catalog), disliked_uris, spec.exclude_artists, spec.exclude_keywords)
        extra = [
            t for t in extra
            if t.uri not in heard_uris
            and _artist_allowed(t, spec.match_artists)
            and _passes_genres(t, spec)
            and _passes_language(t, saved_uris, spec)  # estrangeira nova: não
        ]
    else:
        # Sem catálogo ainda (1ª execução / falha): descoberta antiga por busca.
        disc = replace(
            spec, exclude_heard=True, include_top_tracks=False, sing_along=False,
            max_per_artist=0, size=max(spec.size * 4, 40),
        )
        extra = _curate_discovery(sp, disc, disliked_uris)
        _artist_genres_for(sp, extra)
        extra = [
            t for t in extra
            if t.uri not in saved_uris and _passes_genres(t, spec)
            and _passes_language(t, set(), spec)
        ]

    known_uris = {t.uri for t in known}
    extra = [t for t in _dedupe(extra) if t.uri not in known_uris]

    fresh_known = [t for t in known if _last_played(t, recent) is None]
    fresh_extra = [t for t in extra if _last_played(t, recent) is None]
    random.shuffle(fresh_known)
    random.shuffle(fresh_extra)
    # reuso só em último caso: a que tocou há mais tempo primeiro
    stale = sorted(
        [t for t in known + extra if _last_played(t, recent) is not None],
        key=lambda t: _last_played(t, recent) or "",
    )
    print(
        f"   🔁 acervo: {len(known)} conhecidas ({len(fresh_known)} fora do rodízio) · "
        f"{len(extra)} dos seus artistas ({len(fresh_extra)} fora do rodízio)"
    )

    chosen: list[Track] = []
    chosen_uris: set[str] = set()
    chosen_keys: set[str] = set()
    counts: dict[str, int] = {}

    def add(track: Track) -> bool:
        if len(chosen) >= spec.size or track.uri in chosen_uris:
            return False
        keys = song_keys(track.name)
        if keys & chosen_keys:  # mesma música em outra versão/medley
            return False
        # 1 por artista contando PARTICIPAÇÕES ("X, Jorge & Mateus" conta p/ os dois)
        names = {a.strip().lower() for a in track.artists.split(",") if a.strip()}
        if spec.max_per_artist and any(counts.get(n, 0) >= spec.max_per_artist for n in names):
            return False
        chosen.append(track)
        chosen_uris.add(track.uri)
        chosen_keys.update(keys)
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        return True

    # 1) as novas  2) conhecidas  3) hits dos seus artistas  4) reuso (mais antigo)
    added_new = 0
    for t in fresh_extra:
        if added_new >= max(0, spec.new_tracks):
            break
        if add(t):
            added_new += 1
    for pool in (fresh_known, fresh_extra, stale):
        for t in pool:
            if len(chosen) >= spec.size:
                break
            add(t)

    reused = sum(1 for t in chosen if _last_played(t, recent) is not None)
    if reused:
        print(f"   ⚠️ acervo curto: {reused} música(s) repetida(s) do rodízio (as mais antigas).")
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
def curate(
    sp: Spotify,
    spec: CurationSpec,
    disliked_uris=frozenset(),
    recent: dict[str, str] | None = None,
    catalog: list[Track] | None = None,
) -> list[Track]:
    """Monta a lista final de faixas, escolhendo o modo conforme a config.

    ``disliked_uris`` são faixas que você tirou de playlists antes (aprendidas
    pelo manager) — nunca voltam. Trilhas fixas (curadas a dedo) ignoram esse
    filtro, porque foram você/eu que escolhemos explicitamente.
    """
    if spec.fixed_tracks:
        return _curate_fixed(sp, spec)
    if spec.sing_along:
        return _curate_sing_along(sp, spec, disliked_uris, recent, catalog)
    if spec.mode == "discovery":
        return _curate_discovery(sp, spec, disliked_uris)
    return _curate_search(sp, spec, disliked_uris)
