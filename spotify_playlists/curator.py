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

import collections
import random
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

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
    source: str = ""  # na diária: "known" (acervo) | "new" (catálogo) | "trend" (explorador)

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
    # Artista sai da seleção depois de N remoções (disliked_artists). 0 desliga.
    dislike_artist_threshold: int = 2
    # --- variação (diária) ---
    min_gap_days: int = 0  # REGRA DURA: música que tocou há menos de N dias não entra (0 desliga)
    fill_with_catalog: bool = True  # acabou conhecida fresca? completa com catálogo NÃO ouvido antes de repetir
    trend_tracks: int = 0  # até N "tendências" por lista (artistas de fora, do explorador semanal); 0 desliga
    trend_queries: list[str] = field(default_factory=list)  # buscas do explorador semanal
    max_new_tracks: int = 0  # TETO de novas (catálogo) por lista: o resto é conhecida (0 = sem teto)
    # Blocos de estilo, na ordem em que a lista deve tocar (sequência, não bagunça).
    artist_groups: list[tuple[str, list[str]]] = field(default_factory=list)


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


# Palavras que, sozinhas no título, denunciam inglês. De propósito NÃO entram
# "me", "no", "do", "a" — são português. Falso positivo só tira a faixa do
# bloco de NOVAS; se ele já curte, ela continua no acervo conhecido.
_EN_TITLE_WORDS = {
    "the", "you", "your", "my", "love", "way", "feeling", "good", "heart",
    "baby", "girl", "boy", "never", "always", "dream", "tonight", "forever",
    "nobody", "haven't", "that's", "thats", "dont", "don't", "cant", "can't",
    "i'm", "it's", "we're", "you're", "wanna", "gonna", "yeah", "hello",
    "sorry", "please", "night", "kiss", "dance", "with", "this", "when",
    "what", "from", "have", "been", "will", "just", "like", "know",
    "want", "need", "time", "world", "song", "only", "back", "down", "over",
    "into", "about", "because", "they", "them", "she", "her", "his", "him",
    "our", "was", "were", "are", "but", "not", "for", "and", "yet", "met",
}


def _looks_english_title(title: str) -> bool:
    """Título claramente em inglês. Não olha o artista ('Seu' não salva a faixa)
    e ignora o que está entre parênteses ('Love Yourself' num título em PT)."""
    core = re.sub(r"[\(\[].*?[\)\]]", " ", title or "")
    core = re.split(r"\s[-–—]\s", core)[0]
    if any(m in core.lower() for m in _PT_MARKERS):
        return False
    words = set(re.findall(r"[a-z']+", core.lower()))
    return bool(words & _EN_TITLE_WORDS)


def _passes_language(
    track: Track, saved_uris: set[str], spec: CurationSpec, allow_by_list: bool = False
) -> bool:
    """Regra do idioma: português sempre; estrangeira só se já for curtida.

    - ``foreign_artists`` (ex.: Michael Bublé, Boyce Avenue): entram só no acervo
      conhecido (``allow_by_list=True``); como faixa NOVA, nunca.
    - artista brasileiro da lista de permitidos passa no acervo conhecido mesmo
      sem acento no título ("Trem das Onze" é português). No catálogo (faixa
      nova, ``allow_by_list=False``), título claramente em inglês não passa.
    - resto: gênero (se houver), heurística de texto, ou já estar nas curtidas.
    """
    if not spec.portuguese_only:
        return True
    if spec.foreign_artists and _name_matches(track.artists, spec.foreign_artists):
        return allow_by_list
    if spec.match_artists and _name_matches(track.artists, spec.match_artists):
        if allow_by_list or not _looks_english_title(track.name):
            return True
        return track.uri in saved_uris
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


# "Grupo X" e a dupla oficial "X & Y" / "X e Y" são o mesmo artista da lista.
# "Vitinho Imperador" não é "Vitinho" — sobra de nome não entra.
_GROUP_PREFIXES = ("grupo ", "os ", "as ", "banda ")
_DUO_SEPARATORS = (" & ", " e ")


def _norm_artist(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _primary_credit(artists: str) -> str:
    """Primeiro crédito (antes da vírgula): quem assina a faixa, não o feat."""
    return _norm_artist((artists or "").split(",")[0])


def _is_main_artist(artists: str, allowed: str) -> bool:
    """O artista permitido é o crédito principal, não participação nem homônimo.

    Casa o nome exato, o prefixo de grupo ('Grupo Fundo de Quintal') e a dupla
    oficial ('Chico Rey & Paraná', 'Zezé Di Camargo & Luciano'). Não casa
    'Vitinho' com 'Vitinho Imperador', nem 'Seu Jorge' no meio dos créditos.
    """
    primary = _primary_credit(artists)
    allowed_n = _norm_artist(allowed)
    if not primary or not allowed_n:
        return False
    if primary == allowed_n:
        return True
    for pref in _GROUP_PREFIXES:
        if primary == pref + allowed_n or allowed_n == pref + primary:
            return True
    for sep in _DUO_SEPARATORS:
        if primary.startswith(allowed_n + sep) or allowed_n.startswith(primary + sep):
            return True
    return False


def _is_allowlisted_main(track: Track, match_artists: list[str]) -> bool:
    """Crédito principal está na lista de permitidos (vazia = sem essa trava)."""
    if not match_artists:
        return True
    return any(_is_main_artist(track.artists, name) for name in match_artists)


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
                # só faixa em que ESTE artista é o crédito principal.
                # participação ("Papatinho, Seu Jorge") e homônimo
                # ("Vitinho Imperador") ficam de fora.
                if t and t.uri not in seen and _is_main_artist(t.artists, name):
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


# Marca o cache já filtrado por artista principal. Cache antigo (sem a marca,
# ou com participações/homônimos) é reescrito na hora, sem nova chamada à API.
CATALOG_FILTER = "artista-principal"


def catalog_artist_names(spec: CurationSpec) -> list[str]:
    """Artistas do catálogo: permitidos, sem estrangeiros e sem vetados.

    Estrangeiro só entra pelo que ele já ouve. Artista em ``exclude_artists``
    (lista da config ou aprendido pelas remoções) não gasta busca.
    """
    foreign = {a.lower() for a in spec.foreign_artists}
    return [
        a
        for a in spec.match_artists
        if a.lower() not in foreign and not _name_matches(a, spec.exclude_artists)
    ]


def scrub_catalog(cache: dict) -> bool:
    """Tira do cache a faixa em que o artista buscado não é o principal.

    Devolve True se alterou alguma coisa (pra gravar ``catalog.json`` de volta).
    Não chama a API: o que já está errado sai na hora; a próxima busca já grava
    só o crédito principal.
    """
    changed = cache.get("filtro") != CATALOG_FILTER
    cache["filtro"] = CATALOG_FILTER
    for key, entry in (cache.get("artists") or {}).items():
        if not isinstance(entry, dict):
            continue
        label = entry.get("name") or key
        for field_name in ("tracks", "releases"):
            if field_name not in entry:
                continue
            kept = [
                d
                for d in (entry.get(field_name) or [])
                if _is_main_artist(d.get("artists") or "", label)
            ]
            if kept != (entry.get(field_name) or []):
                changed = True
                entry[field_name] = kept
    return changed


def catalog_tracks(cache: dict, artists: list[str]) -> list[Track]:
    """Faixas cacheadas dos artistas pedidos (vazio se ainda não baixou).

    Mesmo num cache antigo, devolve só a faixa em que o artista é o principal.
    """
    out: list[Track] = []
    seen: set[str] = set()
    for name in artists:
        e = (cache.get("artists") or {}).get(name.lower())
        label = (e or {}).get("name") or name
        # hits (busca por popularidade) + lançamentos achados pelo explorador semanal
        for d in list((e or {}).get("tracks", [])) + list((e or {}).get("releases", [])):
            if d["uri"] in seen or not _is_main_artist(d.get("artists") or "", label):
                continue
            seen.add(d["uri"])
            out.append(
                Track(d["uri"], d["name"], d["artists"], list(d.get("artist_ids") or []))
            )
    return out


# --------------------------------------------------------------------------- #
# Explorador semanal: lançamentos dos seus artistas + tendências da cena
# --------------------------------------------------------------------------- #
# Roda 1x por semana (workflow "Explorador semanal") e guarda no catálogo:
#   * ``releases`` de cada artista permitido: faixas lançadas este ano/ano passado
#     que a busca por popularidade não traz (o refresh do TTL não apaga isto);
#   * ``trends``: faixas das ``trend_queries`` da config, de artistas de FORA da
#     lista, que passam nos mesmos vetos da diária (nome, palavra, funk, idioma).
#     Entram na lista só pela cota ``trend_tracks``; removeu uma, o artista sai.
RELEASE_YEARS_BACK = 1   # lançamentos: ano atual e o anterior
TRENDS_KEEP_DAYS = 60    # tendência que não virou curtida some do cache depois disso
TREND_PAGES = 2          # 2 x 10 resultados por busca de tendência


def _track_row(t: Track, **extra) -> dict:
    row = {"uri": t.uri, "name": t.name, "artists": t.artists, "artist_ids": t.artist_ids}
    row.update(extra)
    return row


def explore_releases(
    sp: Spotify,
    cache: dict,
    artists: list[str],
    market: str,
    now: datetime | None = None,
    budget: int | None = None,
) -> dict[str, list[Track]]:
    """Lançamentos recentes de cada artista permitido → ``releases`` no cache.

    Devolve {artista: [faixas novas desta rodada]}. Uma busca por artista, com
    pausa; para no 1º 429 e mantém o que já achou.
    """
    now = now or _utcnow()
    entries = cache.setdefault("artists", {})
    found: dict[str, list[Track]] = {}
    year = now.year
    todo = artists if budget is None else artists[: max(0, budget)]
    for name in todo:
        entry = entries.setdefault(name.lower(), {"name": name, "fetched": None, "tracks": []})
        have = {d["uri"] for d in (entry.get("tracks") or [])}
        have |= {d["uri"] for d in (entry.get("releases") or [])}
        query = f'artist:"{name}" year:{year - RELEASE_YEARS_BACK}-{year}'
        try:
            resp = sp.search(
                q=query, type="track", market=market, limit=SEARCH_MAX_LIMIT, offset=0
            )
        except SpotifyException as exc:
            if exc.http_status == 429:
                print("   ⛔ rate limit no explorador — paro por aqui e guardo o que já achei.")
                break
            continue
        except Exception:
            continue
        finally:
            time.sleep(CATALOG_PAUSE_S)
        new: list[Track] = []
        for t in (_track_from_item(it) for it in resp.get("tracks", {}).get("items", [])):
            if t and t.uri not in have and _is_main_artist(t.artists, name):
                have.add(t.uri)
                new.append(t)
        if new:
            stamp = now.isoformat(timespec="seconds")
            entry.setdefault("releases", []).extend(_track_row(t, found=stamp) for t in new)
            found[name] = new
    return found


def explore_trends(
    sp: Spotify,
    cache: dict,
    spec: CurationSpec,
    skip_uris=frozenset(),
    now: datetime | None = None,
) -> list[Track]:
    """Busca as ``trend_queries`` e guarda em ``cache["trends"]`` o que passa nos
    vetos (artista/palavra vetados, funk, título em inglês, idioma). Devolve só
    as faixas novas desta rodada. Tendência velha (``TRENDS_KEEP_DAYS``) é podada.
    """
    now = now or _utcnow()
    stamp = now.isoformat(timespec="seconds")
    trends: list[dict] = cache.setdefault("trends", [])
    cutoff = (now - timedelta(days=TRENDS_KEEP_DAYS)).isoformat(timespec="seconds")
    trends[:] = [d for d in trends if (d.get("found") or "") >= cutoff]
    have = {d["uri"] for d in trends} | set(skip_uris)
    for entry in (cache.get("artists") or {}).values():
        if isinstance(entry, dict):
            have |= {d["uri"] for d in (entry.get("tracks") or []) + (entry.get("releases") or [])}
    new_all: list[Track] = []
    for query in spec.trend_queries:
        tracks = _search_tracks(sp, query, spec.market, TREND_PAGES * SEARCH_MAX_LIMIT)
        time.sleep(CATALOG_PAUSE_S)
        tracks = _drop(tracks, frozenset(), spec.exclude_artists, spec.exclude_keywords)
        for t in tracks:
            if t.uri in have or _looks_funk(t) or _looks_english_title(t.name):
                continue
            if spec.portuguese_only and not _looks_portuguese(t.name, t.artists):
                continue
            have.add(t.uri)
            trends.append(_track_row(t, found=stamp, query=query))
            new_all.append(t)
    return new_all


def catalog_trends(cache: dict) -> list[Track]:
    """Tendências guardadas pelo explorador (vazio se nunca rodou)."""
    return [
        Track(d["uri"], d["name"], d["artists"], list(d.get("artist_ids") or []))
        for d in (cache.get("trends") or [])
        if d.get("uri")
    ]


# --------------------------------------------------------------------------- #
# Diária: rodízio e preenchimento
# --------------------------------------------------------------------------- #
def _last_played(track: Track, recent: dict[str, str]) -> str | None:
    """Última vez (ISO) que QUALQUER versão desta música tocou; None = fresca."""
    dates = [recent[k] for k in song_keys(track.name) if k in recent]
    return max(dates) if dates else None


def _days_since(track: Track, recent: dict[str, str], now: datetime) -> float | None:
    """Há quantos dias esta música tocou (qualquer versão); None = fora do rodízio."""
    last = _last_played(track, recent)
    if not last:
        return None
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400)


def _fill_sing_along(
    known: list[Track],
    extra: list[Track],
    recent: dict[str, str],
    spec: CurationSpec,
    now: datetime | None = None,
    trends: list[Track] | None = None,
) -> list[Track]:
    """Monta a lista nesta ordem de preferência:

    1. até ``trend_tracks`` tendências (artistas de fora, do explorador semanal);
    2. pelo menos ``new_tracks`` novas (catálogo dos seus artistas, nunca ouvidas);
    3. conhecidas frescas (fora do rodízio);
    4. com ``fill_with_catalog``: mais catálogo não ouvido — VARIAR antes de repetir;
    5. só então repete, começando pela que tocou há mais tempo.

    Regra dura: nada que tocou há menos de ``min_gap_days`` entra, em passo nenhum.
    """
    now = now or datetime.now(timezone.utc)
    trends = trends or []

    def gap(t: Track) -> float | None:
        return _days_since(t, recent, now)

    def fresh(pool: list[Track]) -> list[Track]:
        out = [t for t in pool if gap(t) is None]
        random.shuffle(out)
        return out

    def stale(pool: list[Track]) -> list[Track]:
        """Já tocaram no rodízio: a mais antiga primeiro, nunca abaixo do gap mínimo."""
        out = [
            t for t in pool
            if gap(t) is not None
            and (spec.min_gap_days <= 0 or (gap(t) or 0) >= spec.min_gap_days)
        ]
        return sorted(out, key=lambda t: -(gap(t) or 0))

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

    # Teto de novas: a lista tem de continuar "cantável" (maioria conhecida).
    cap_new = max(spec.max_new_tracks, spec.new_tracks) if spec.max_new_tracks > 0 else None
    used_new = 0

    def take(pool: list[Track], limit: int | None = None, new: bool = False) -> int:
        nonlocal used_new
        n = 0
        for t in pool:
            if len(chosen) >= spec.size or (limit is not None and n >= limit):
                break
            if new and cap_new is not None and used_new >= cap_new:
                break
            if add(t):
                n += 1
                if new:
                    used_new += 1
        return n

    fresh_extra = fresh(extra)
    take(fresh(trends), max(0, spec.trend_tracks))      # 1) tendências (cota)
    take(fresh_extra, max(0, spec.new_tracks), new=True)  # 2) novas garantidas
    take(fresh(known))                                  # 3) conhecidas frescas
    if spec.fill_with_catalog:
        take(fresh_extra, new=True)                     # 4) variar antes de repetir (até o teto)
    take(stale(known))                                  # 5) repete a mais antiga (≥ gap mínimo)
    if spec.fill_with_catalog:
        take(stale(extra), new=True)
    if len(chosen) < spec.size:                         # último recurso: passa do teto a ficar curta
        take(fresh_extra)

    random.shuffle(chosen)
    return chosen[: spec.size]


def _group_index(track: Track, spec: CurationSpec) -> int:
    """Bloco de estilo da faixa (ordem dos ``artist_groups``); fora deles = último."""
    for i, (_name, artists) in enumerate(spec.artist_groups):
        if _name_matches(track.artists, artists):
            return i
    return len(spec.artist_groups)


def _spread(base: list[Track], inserts: list[Track]) -> list[Track]:
    """Espalha ``inserts`` ao longo de ``base`` (novas entre conhecidas, sem aglomerar)."""
    if not base:
        return list(inserts)
    out = list(base)
    step = (len(base) + len(inserts)) / (len(inserts) + 1)
    for k, t in enumerate(inserts, 1):
        out.insert(min(len(out), int(round(k * step))), t)
    return out


def sequence_tracks(
    chosen: list[Track], spec: CurationSpec, new_uris=frozenset()
) -> list[Track]:
    """Dá sequência à lista: blocos por estilo na ordem de ``artist_groups``
    (ex.: pagode → sertanejo → MPB → internacional); dentro de cada bloco as
    conhecidas vêm embaralhadas e as novas espalhadas entre elas. Tendências
    (artistas de fora dos blocos) fecham a lista. Sem ``artist_groups``,
    devolve como veio (ordem aleatória)."""
    if not spec.artist_groups:
        return chosen
    blocks: dict[int, list[Track]] = {}
    for t in chosen:
        blocks.setdefault(_group_index(t, spec), []).append(t)
    out: list[Track] = []
    for i in sorted(blocks):
        known = [t for t in blocks[i] if t.uri not in new_uris]
        new = [t for t in blocks[i] if t.uri in new_uris]
        random.shuffle(known)
        random.shuffle(new)
        out.extend(_spread(known, new))
    return out


def _curate_sing_along(
    sp: Spotify,
    spec: CurationSpec,
    disliked_uris=frozenset(),
    recent: dict[str, str] | None = None,
    catalog: list[Track] | None = None,
    now: datetime | None = None,
    trends: list[Track] | None = None,
) -> list[Track]:
    """Modo "cantar junto".

    - CONHECIDAS: suas mais ouvidas + curtidas (dá pra cantar de cabeça).
    - NOVAS (``new_tracks`` no mínimo): músicas dos seus artistas que o Spotify
      não te viu ouvir — do catálogo cacheado (hits + lançamentos), só se o
      artista é o crédito principal.
    - TENDÊNCIAS (``trend_tracks`` no máximo): achadas pelo explorador semanal,
      artistas de fora da lista, com os mesmos vetos.
    - Rodízio: nada que tocou nos últimos ``rotation_days`` dias volta enquanto
      houver conhecida fresca ou catálogo não ouvido (``fill_with_catalog``);
      quando precisa repetir, repete a mais antiga e nunca abaixo de
      ``min_gap_days``.
    - 1 por artista, lista de permitidos, idioma, vetos e "não gosto".
    """
    recent = recent or {}
    now = now or datetime.now(timezone.utc)

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

    # Catálogo dos seus artistas = as NOVAS (nunca ouvidas).
    if catalog:
        extra = _drop(_dedupe(catalog), disliked_uris, spec.exclude_artists, spec.exclude_keywords)
        extra = [
            t for t in extra
            if t.uri not in heard_uris
            and _is_allowlisted_main(t, spec.match_artists)
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
            if t.uri not in saved_uris
            and _is_allowlisted_main(t, spec.match_artists)
            and _passes_genres(t, spec)
            and _passes_language(t, set(), spec)
        ]

    known_uris = {t.uri for t in known}
    known_keys: set[str] = set()
    for t in known:
        known_keys |= song_keys(t.name)
    # Outra gravação de música que ele já tem no acervo não conta como nova.
    extra = [
        t for t in _dedupe(extra)
        if t.uri not in known_uris and not (song_keys(t.name) & known_keys)
    ]
    extra_uris = {t.uri for t in extra}

    # Tendências (explorador semanal): artistas de fora, mesmos vetos da lista.
    trend_pool: list[Track] = []
    if trends and spec.trend_tracks > 0:
        trend_pool = _drop(_dedupe(trends), disliked_uris, spec.exclude_artists, spec.exclude_keywords)
        trend_pool = [
            t for t in trend_pool
            if t.uri not in heard_uris
            and t.uri not in extra_uris
            and not _looks_funk(t)
            and not _looks_english_title(t.name)
            and _passes_genres(t, spec)
            and not (song_keys(t.name) & known_keys)
        ]
    trend_uris = {t.uri for t in trend_pool}

    def n_fresh(pool: list[Track]) -> int:
        return sum(1 for t in pool if _last_played(t, recent) is None)

    print(
        f"   🔁 acervo: {len(known)} conhecidas ({n_fresh(known)} fora do rodízio) · "
        f"{len(extra)} no catálogo ({n_fresh(extra)} fora do rodízio) · "
        f"{len(trend_pool)} tendências ({n_fresh(trend_pool)} fora do rodízio)"
    )

    chosen = _fill_sing_along(known, extra, recent, spec, now=now, trends=trend_pool)
    chosen = sequence_tracks(chosen, spec, extra_uris | trend_uris)
    for t in chosen:  # o manager grava isto: remoção de música NOVA não pune o artista
        t.source = "trend" if t.uri in trend_uris else "new" if t.uri in extra_uris else "known"
    n_new = sum(1 for t in chosen if t.uri in extra_uris)
    n_trend = sum(1 for t in chosen if t.uri in trend_uris)
    gaps = [g for g in (_days_since(t, recent, now) for t in chosen) if g is not None]
    teto = f", teto {spec.max_new_tracks}" if spec.max_new_tracks > 0 else ""
    print(
        f"   ✨ nesta lista: {len(chosen) - n_new - n_trend} conhecidas · {n_new} novas dos "
        f"seus artistas (mínimo {max(0, spec.new_tracks)}{teto}) · {n_trend} tendências "
        f"(teto {max(0, spec.trend_tracks)})"
    )
    if spec.max_new_tracks > 0 and n_new > spec.max_new_tracks:
        print(
            f"   ⚠️ passei do teto de novas ({n_new} > {spec.max_new_tracks}): faltou conhecida "
            "que respeitasse o gap mínimo. Curtir mais músicas dos seus artistas resolve."
        )
    if spec.artist_groups:
        names = [n for n, _ in spec.artist_groups] + ["fora dos blocos"]
        counts = collections.Counter(_group_index(t, spec) for t in chosen)
        seq = " → ".join(f"{names[i]} {counts[i]}" for i in sorted(counts))
        print(f"   🎼 sequência: {seq}")
    if gaps:
        print(
            f"   🔂 repetidas do rodízio: {len(gaps)} (a mais recente tocou há "
            f"{min(gaps):.1f} dia(s); gap mínimo {spec.min_gap_days})"
        )
    else:
        print("   🔂 nenhuma repetição dentro do rodízio")
    if len(chosen) < spec.size:
        print(
            f"   ⚠️ lista com {len(chosen)} de {spec.size}: acabou tudo que respeita o "
            "rodízio e o gap mínimo."
        )
    return chosen


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
    now: datetime | None = None,
    trends: list[Track] | None = None,
) -> list[Track]:
    """Monta a lista final de faixas, escolhendo o modo conforme a config.

    ``disliked_uris`` são faixas que você tirou de playlists antes (aprendidas
    pelo manager) — nunca voltam. Trilhas fixas (curadas a dedo) ignoram esse
    filtro, porque foram você/eu que escolhemos explicitamente.
    """
    if spec.fixed_tracks:
        return _curate_fixed(sp, spec)
    if spec.sing_along:
        return _curate_sing_along(sp, spec, disliked_uris, recent, catalog, now=now, trends=trends)
    if spec.mode == "discovery":
        return _curate_discovery(sp, spec, disliked_uris)
    return _curate_search(sp, spec, disliked_uris)
