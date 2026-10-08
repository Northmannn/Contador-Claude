"""Testes da playlist diária: rodízio de 7 dias, catálogo, janelas manhã/noite.

Rodar:  python tests/test_daily.py        (ou: python -m pytest tests/)
Não chama o Spotify de verdade: usa um cliente falso.
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spotipy  # noqa: E402

from spotify_playlists import curator, manager  # noqa: E402
from spotify_playlists.config import DailySlot, PlaylistDef  # noqa: E402
from spotify_playlists.curator import CurationSpec, Track, song_keys  # noqa: E402

ARTISTS = [f"Artista {chr(65 + i)}" for i in range(30)]  # Artista A..Artista Z...


def _item(uri, name, artist):
    return {"uri": uri, "name": name, "artists": [{"id": artist.lower(), "name": artist}]}


class FakeSpotify:
    """Acervo: 3 músicas conhecidas por artista; catálogo: 15 hits por artista."""

    def __init__(self, rate_limit_after: int | None = None):
        self.search_calls = 0
        self.rate_limit_after = rate_limit_after

    def current_user_top_tracks(self, limit, time_range):
        if time_range != "long_term":
            return {"items": []}
        return {"items": [
            _item(f"k-{a}-{i}", f"Conhecida {i} de {a}", a) for a in ARTISTS for i in range(3)
        ][:50]}

    def current_user_saved_tracks(self, limit):
        return {"items": [
            {"track": _item(f"s-{a}-{i}", f"Curtida {i} de {a}", a)} for a in ARTISTS for i in range(2)
        ], "next": None}

    def next(self, page):
        return None

    def current_user_top_artists(self, limit, time_range):
        return {"items": []}

    def artists(self, ids):
        raise spotipy.SpotifyException(403, -1, "Forbidden")

    def search(self, q, type, market="BR", limit=10, offset=0):
        self.search_calls += 1
        if self.rate_limit_after is not None and self.search_calls > self.rate_limit_after:
            raise spotipy.SpotifyException(429, -1, "Too Many Requests")
        if type != "track":
            return {"artists": {"items": []}}
        name = q.replace("artist:", "").strip('"')
        page = offset // 10
        items = [_item(f"c-{name}-{page}-{i}", f"Hit {page * 10 + i} de {name}", name) for i in range(10)]
        items += [_item(f"x-{page}", f"Homônimo {page}", "Outro Cantor")]  # deve ser ignorado
        return {"tracks": {"items": items[:10] if page < 2 else items[:5]}}


def _spec(**kw):
    base = dict(
        sing_along=True, size=25, new_tracks=4, max_per_artist=1, rotation_days=7,
        learn_removals=True, portuguese_only=False, match_artists=ARTISTS, market="BR",
    )
    base.update(kw)
    return CurationSpec(**base)


def test_song_keys_versions_and_medleys():
    assert song_keys("Talvez - Ao Vivo") == {"talvez"}
    assert song_keys("Talvez (feat. X)") == {"talvez"}
    assert song_keys("Para Tudo / Loucura do Seu Coração - Ao Vivo") == {
        "para tudo", "loucura do seu coração"}
    assert song_keys("(Medley Sambas) Nunca Mais Te Machucar / Primeiro Amor - Ao Vivo") == {
        "nunca mais te machucar", "primeiro amor"}
    assert song_keys("Pot-Pourri: Quem / Frenesi") == {"quem", "frenesi"}
    assert "loucura do seu coração" in song_keys("Loucura Do Seu Coração - Live")


def test_rotation_14_generations_no_repeat():
    """7 dias x 2 listas: nenhuma música (por chave) repete dentro da semana.

    O acervo conhecido precisa caber a semana (21 conhecidas x 14), porque
    catálogo não passa de new_tracks=4 por lista.
    """
    curator._BLOCKED.clear()
    curator._GENRE_CACHE.clear()
    curator.CATALOG_PAUSE_S = 0

    class Wide(FakeSpotify):
        def current_user_saved_tracks(self, limit):
            return {"items": [
                {"track": _item(f"s-{a}-{i}", f"Curtida {i} de {a}", a)}
                for a in ARTISTS for i in range(16)
            ], "next": None}

    with tempfile.TemporaryDirectory() as d:
        sp = Wide()
        cache = {"artists": {}}
        curator.refresh_catalog(sp, cache, ARTISTS, "BR", budget=100)
        catalog = curator.catalog_tracks(cache, ARTISTS)
        assert not any("Homônimo" in t.name for t in catalog), "homônimo entrou no catálogo"
        spec = _spec()
        start = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
        history: list[tuple[datetime, set[str]]] = []
        for g in range(14):
            now = start + timedelta(hours=12 * g)
            state = manager._load_state(d, "T")
            recent = manager._recent_played(state, 7, now=now)
            chosen = curator.curate(sp, spec, recent=recent, catalog=catalog)
            assert len(chosen) == 25, len(chosen)
            novas = [t for t in chosen if t.uri.startswith("c-")]
            assert len(novas) <= spec.new_tracks, (g, len(novas))
            keys = set().union(*(song_keys(t.name) for t in chosen))
            for when, prev in history:
                if now - when < timedelta(days=7):
                    assert not (keys & prev), f"repetiu na geração {g}: {keys & prev}"
            artists = [curator._artist_key(t) for t in chosen]
            assert len(artists) == len(set(artists)), "repetiu artista na lista"
            manager._save_generated(d, "T", chosen, now=now)
            history.append((now, keys))


def test_short_pool_reuses_oldest_first():
    """Acervo pequeno: completa o tamanho reusando a que tocou há mais tempo."""
    curator._GENRE_CACHE.clear()
    t = [Track(f"u{i}", f"Música {i}", f"Artista {chr(65 + i)}", []) for i in range(5)]
    recent = {"música 0": "2026-09-20T00:00:00+00:00", "música 1": "2026-09-23T00:00:00+00:00",
              "música 2": "2026-09-22T00:00:00+00:00"}

    class Sp(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            return {"items": [_item(x.uri, x.name, x.artists) for x in t]} if time_range == "long_term" else {"items": []}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    spec = _spec(size=4, new_tracks=0, match_artists=[x.artists for x in t])
    chosen = curator.curate(Sp(), spec, recent=recent, catalog=[Track("zz", "Outra", "Z", [])])
    names = {x.name for x in chosen}
    assert {"Música 3", "Música 4"} <= names, names          # frescas primeiro
    assert "Música 0" in names and "Música 2" in names, names  # depois as mais antigas
    assert "Música 1" not in names, names                     # a mais recente fica de fora


def test_catalog_budget_ttl_and_rate_limit():
    curator.CATALOG_PAUSE_S = 0
    cache = {"artists": {}}
    sp = FakeSpotify()
    assert curator.refresh_catalog(sp, cache, ARTISTS[:5], "BR", budget=2) == 2
    assert curator.refresh_catalog(sp, cache, ARTISTS[:5], "BR", budget=10) == 3
    assert curator.refresh_catalog(sp, cache, ARTISTS[:5], "BR", budget=10) == 0  # TTL
    limited = FakeSpotify(rate_limit_after=4)
    cache2 = {"artists": {}}
    n = curator.refresh_catalog(limited, cache2, ARTISTS[:5], "BR", budget=10)
    assert n == 1 and limited.search_calls == 5, (n, limited.search_calls)  # parou no 429


def test_foreign_artists_never_as_new_from_catalog():
    curator._GENRE_CACHE.clear()
    spec = _spec(portuguese_only=True, match_artists=["Michael Bublé", "Artista A"],
                 foreign_artists=["Michael Bublé"], size=3, new_tracks=2)

    class Sp(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            return {"items": [_item("b1", "Feeling Good", "Michael Bublé")]} if time_range == "long_term" else {"items": []}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    catalog = [Track("b2", "Haven't Met You Yet", "Michael Bublé", []),
               Track("a1", "Saudade de Você", "Artista A", [])]
    chosen = {t.name for t in curator.curate(Sp(), spec, recent={}, catalog=catalog)}
    assert "Feeling Good" in chosen and "Haven't Met You Yet" not in chosen, chosen


def test_one_per_artist_counts_features():
    curator._GENRE_CACHE.clear()
    spec = _spec(size=3, new_tracks=0, match_artists=["Jorge & Mateus", "Humberto & Ronaldo", "Artista A"])

    class Sp(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            if time_range != "long_term":
                return {"items": []}
            return {"items": [
                {"uri": "1", "name": "Carência", "artists": [{"id": "h", "name": "Humberto & Ronaldo"}, {"id": "j", "name": "Jorge & Mateus"}]},
                _item("2", "Amor Pra Recomeçar", "Jorge & Mateus"),
                _item("3", "Outra", "Artista A"),
            ]}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    chosen = curator.curate(Sp(), spec, recent={}, catalog=[Track("zz", "Z", "Z", [])])
    with_jm = [t for t in chosen if "Jorge & Mateus" in t.artists]
    assert len(with_jm) == 1, [t.name for t in chosen]


def test_slots():
    slots = [DailySlot("manha", 2, 12), DailySlot("noite", 17, 24)]
    at = lambda h: datetime(2026, 9, 24, (h + 3) % 24, 30, tzinfo=timezone.utc) + (
        timedelta(days=1) if h + 3 >= 24 else timedelta(0))
    assert manager.current_slot(slots, at(5)) == "2026-09-24-manha"
    assert manager.current_slot(slots, at(21)) == "2026-09-24-noite"
    assert manager.current_slot(slots, at(14)) is None
    assert manager.current_slot(slots, at(1)) is None
    with tempfile.TemporaryDirectory() as d:
        assert not manager.slot_already_done("2026-09-24-noite", d)
        manager.mark_slot_done("2026-09-24-noite", d)
        assert manager.slot_already_done("2026-09-24-noite", d)
        assert not manager.slot_already_done("2026-09-25-manha", d)


def test_new_tracks_cap_reuses_oldest_known():
    """Conhecidas frescas acabaram: reusa a mais antiga, sem estourar new_tracks."""
    curator._GENRE_CACHE.clear()
    known = [Track(f"u{i}", f"Música {i}", f"Artista {chr(65 + i)}", []) for i in range(6)]
    # 0 e 1 frescas; 2 é a mais antiga entre as tocadas; 5 a mais recente
    recent = {
        "música 2": "2026-09-18T00:00:00+00:00",
        "música 3": "2026-09-20T00:00:00+00:00",
        "música 4": "2026-09-22T00:00:00+00:00",
        "música 5": "2026-09-24T00:00:00+00:00",
    }
    # Novas de artistas que NÃO estão nas conhecidas, senão o teto de 1/artista
    # esconde a música conhecida que o teste precisa ver entrar.
    catalog = [
        Track(f"c{i}", f"Nova {i}", f"Artista {chr(71 + i)}", []) for i in range(6)
    ] + [Track("cZ", "Nova Z", "Artista Z", [])]

    class Sp(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            if time_range != "long_term":
                return {"items": []}
            return {"items": [_item(t.uri, t.name, t.artists) for t in known]}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    spec = _spec(
        size=6, new_tracks=2,
        match_artists=[t.artists for t in known] + [f"Artista {chr(71 + i)}" for i in range(6)],
    )
    chosen = curator.curate(Sp(), spec, recent=recent, catalog=catalog)
    names = {t.name for t in chosen}
    novas = {t.name for t in chosen if t.name.startswith("Nova")}
    assert len(chosen) == 6, [t.name for t in chosen]
    assert len(novas) <= 2, novas
    assert {"Música 0", "Música 1", "Música 2", "Música 3"} <= names, names
    assert "Música 5" not in names, names  # a tocada há menos tempo fica de fora
    assert "Nova Z" not in names


def test_catalog_keeps_only_main_artist():
    """Participação e homônimo ficam de fora; grupo e dupla oficial ficam."""
    curator.CATALOG_PAUSE_S = 0

    def row(uri, name, *artists):
        return {
            "uri": uri,
            "name": name,
            "artists": [{"id": a.lower(), "name": a} for a in artists],
        }

    class Sp(FakeSpotify):
        def search(self, q, type, market="BR", limit=10, offset=0):
            self.search_calls += 1
            name = q.replace("artist:", "").strip().strip('"')
            table = {
                "Seu Jorge": [
                    row("1", "Burguesinha", "Seu Jorge"),
                    row("2", "Final de Semana", "Papatinho", "Seu Jorge", "Black Alien"),
                    row("3", "That's My Way", "Edi Rock", "Seu Jorge"),
                ],
                "Vitinho": [
                    row("4", "Sobrenome", "Vitinho", "Péricles"),
                    row("5", "Eu Me Apaixonei", "Vitinho Imperador"),
                    row("6", "Biquini", "MC Kevin o Chris", "Vitinho"),
                ],
                "Wesley Safadão": [
                    row("7", "Ar Condicionado", "Wesley Safadão"),
                    row("8", "Não Diz que Acabou", "Rey Vaqueiro", "Wesley Safadão"),
                ],
                "Chico Rey": [
                    row("9", "Tranque a Porta", "Chico Rey & Paraná"),
                ],
                "Fundo de Quintal": [
                    row("10", "O Show", "Grupo Fundo De Quintal"),
                    row("11", "Coletânea", "Sambabook", "Fundo de Quintal"),
                ],
                "Zezé Di Camargo": [
                    row("12", "É o Amor", "Zezé Di Camargo & Luciano"),
                ],
                "Matheus & Kauan": [
                    row("13", "Ao Vivo E A Cores", "Matheus & Kauan", "Anitta"),
                    row("14", "Ternura", "Anitta", "Matheus & Kauan"),
                ],
            }
            items = table.get(name, [])
            if offset:
                items = []
            return {"tracks": {"items": items}}

    cache = {"artists": {}}
    artists = [
        "Seu Jorge", "Vitinho", "Wesley Safadão", "Chico Rey",
        "Fundo de Quintal", "Zezé Di Camargo", "Matheus & Kauan",
    ]
    curator.refresh_catalog(Sp(), cache, artists, "BR", budget=20)
    catalog = curator.catalog_tracks(cache, artists)
    names = {t.name for t in catalog}
    assert names == {
        "Burguesinha", "Sobrenome", "Ar Condicionado", "Tranque a Porta",
        "O Show", "É o Amor", "Ao Vivo E A Cores",
    }, names
    # cache sujo (formato antigo) também sai limpo na leitura e no scrub
    dirty = {"artists": {"vitinho": {
        "name": "Vitinho", "fetched": "2026-10-01T00:00:00+00:00",
        "tracks": [
            {"uri": "4", "name": "Sobrenome", "artists": "Vitinho, Péricles", "artist_ids": []},
            {"uri": "5", "name": "Eu Me Apaixonei", "artists": "Vitinho Imperador", "artist_ids": []},
        ],
    }}}
    assert curator.scrub_catalog(dirty)
    assert [t["name"] for t in dirty["artists"]["vitinho"]["tracks"]] == ["Sobrenome"]
    assert dirty["filtro"] == "artista-principal"
    assert not curator.scrub_catalog(dirty)


def test_english_catalog_title_blocked_known_english_stays():
    curator._GENRE_CACHE.clear()
    spec = _spec(
        portuguese_only=True, match_artists=["Artista A"], foreign_artists=[],
        size=3, new_tracks=2, max_per_artist=0,
    )

    class Sp(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            if time_range != "long_term":
                return {"items": []}
            return {"items": [_item("k1", "Feeling Good", "Artista A")]}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    catalog = [
        Track("en", "That's My Way", "Artista A", []),
        Track("pt", "Não Chora", "Artista A", []),
        Track("pt2", "Trem Das Onze", "Artista A", []),
    ]
    chosen = {t.name for t in curator.curate(Sp(), spec, recent={}, catalog=catalog)}
    assert "Feeling Good" in chosen, chosen
    assert "That's My Way" not in chosen, chosen
    assert "Não Chora" in chosen and "Trem Das Onze" in chosen, chosen


def test_disliked_artist_threshold():
    feedback = {"disliked_artists": {"chay suede": 3, "matheus & kauan": 1, "gabriel leone": 1}}
    assert manager.banned_artist_names(feedback, 2) == ["chay suede"]
    assert manager.banned_artist_names(feedback, 4) == []
    assert manager.banned_artist_names(feedback, 0) == []

    curator._GENRE_CACHE.clear()
    spec = _spec(
        size=4, new_tracks=2,
        match_artists=["Chay Suede", "Artista A", "Artista B"],
        exclude_artists=["chay suede"],
    )

    class Sp(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            if time_range != "long_term":
                return {"items": []}
            return {"items": [
                _item("k1", "Brega", "Chay Suede"),
                _item("k2", "Conhecida", "Artista A"),
            ]}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    catalog = [
        Track("c1", "Festa", "Chay Suede", []),
        Track("c2", "Saudade Nova", "Artista B", []),
    ]
    chosen = curator.curate(Sp(), spec, recent={}, catalog=catalog)
    blob = " | ".join(t.artists for t in chosen).lower()
    assert "chay suede" not in blob, [str(t) for t in chosen]
    assert any(t.name == "Saudade Nova" for t in chosen)


def test_manual_run_does_not_burn_rotation():
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        manager._save_generated(d, "T", [Track("u", "Velha", "A", [])], now=now - timedelta(days=1))
        manager._save_generated(
            d, "T", [Track("v", "Nova", "B", [])], now=now, record_rotation=False,
        )
        state = manager._load_state(d, "T")
        assert state["generated"][0]["name"] == "Nova"
        assert "velha" in state["played"] and "nova" not in state["played"]
        recent = manager._recent_played(state, 7, now=now)
        assert recent == {"velha": state["played"]["velha"]}


def test_bom_dia_config_threshold():
    from spotify_playlists.config import load_config
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config", "playlists.yaml"))
    bom = next(p for p in cfg.playlists if "Bom Dia" in p.name)
    assert bom.spec.new_tracks == 4
    assert bom.spec.dislike_artist_threshold == 2


_BAD_CATALOG_URIS = {
    "spotify:track:41sjmSYBlafAQrfcxt5387",  # Final de Semana — Papatinho, Seu Jorge
    "spotify:track:4M7bbRsVNB8iWQaX8Sbfln",  # That's My Way — Edi Rock, Seu Jorge
    "spotify:track:4SjcIkVc4cvFgURD1EHxDM",  # Eu Me Apaixonei — Vitinho Imperador
    "spotify:track:6X2ZCbvCzMXoZm8Z6rnONU",  # Biquini Cavadinho — MC Kevin, Vitinho
    "spotify:track:3U2bwJqm215AP9vErb5ADm",  # Não Diz que Acabou — Rey Vaqueiro, Wesley
    "spotify:track:5FOy6KydSZWIJwdtLUj0FS",  # Ternura — Anitta, Melly
    "spotify:track:72xZECUZM6QkUI9X1LIlln",  # Além da Lenda - O Filme — feat. Gabriel Leone
    "spotify:track:2Z0XzRVb1fOqxrGCGVJBaV",  # medley de rádio FM O Dia, feat. Arlindinho
}


def _repo_root() -> str:
    return os.path.join(os.path.dirname(__file__), "..")


def _repo_data():
    root = _repo_root()
    with open(os.path.join(root, "data", "catalog.json"), encoding="utf-8") as fh:
        catalog = json.load(fh)
    with open(os.path.join(root, "data", "feedback.json"), encoding="utf-8") as fh:
        feedback = json.load(fh)
    with open(os.path.join(root, "data", "state", "bom-dia-motiva-o.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    return catalog, feedback, state


def test_offline_catalog_file_drops_features_and_namealikes():
    """O cache versionado não pode mais guardar os casos que vazavam."""
    catalog, feedback, _state = _repo_data()
    uris = {
        t["uri"]
        for entry in (catalog.get("artists") or {}).values()
        for t in (entry.get("tracks") or [])
    }
    assert not (_BAD_CATALOG_URIS & uris), _BAD_CATALOG_URIS & uris
    assert catalog.get("filtro") == "artista-principal"
    kept = {
        (t["name"], t["artists"])
        for entry in catalog["artists"].values()
        for t in entry.get("tracks") or []
    }
    assert any(name == "Burguesinha" and artists == "Seu Jorge" for name, artists in kept)
    assert any("Chico Rey & Paraná" in artists for _name, artists in kept)
    assert any(artists.lower().startswith("grupo fundo de quintal") for _name, artists in kept)
    assert any("Zezé Di Camargo & Luciano" in artists for _name, artists in kept)
    banned = manager.banned_artist_names(feedback, 2)
    assert banned == ["chay suede"]
    assert "matheus & kauan" not in banned


def _fill_legado(known, extra, recent, spec):
    """Ordem antiga: depois das conhecidas frescas, completava com MAIS catálogo."""
    fresh_known = [t for t in known if curator._last_played(t, recent) is None]
    fresh_extra = [t for t in extra if curator._last_played(t, recent) is None]
    random.shuffle(fresh_known)
    random.shuffle(fresh_extra)
    stale = sorted(
        [t for t in known + extra if curator._last_played(t, recent) is not None],
        key=lambda t: curator._last_played(t, recent) or "",
    )
    chosen: list[Track] = []
    chosen_uris: set[str] = set()
    chosen_keys: set[str] = set()
    counts: dict[str, int] = {}

    def add(track: Track) -> bool:
        if len(chosen) >= spec.size or track.uri in chosen_uris:
            return False
        keys = song_keys(track.name)
        if keys & chosen_keys:
            return False
        names = {a.strip().lower() for a in track.artists.split(",") if a.strip()}
        if spec.max_per_artist and any(counts.get(n, 0) >= spec.max_per_artist for n in names):
            return False
        chosen.append(track)
        chosen_uris.add(track.uri)
        chosen_keys.update(keys)
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        return True

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
    return chosen


def test_offline_simulated_list_respects_new_tracks():
    """Com o estado e o catálogo reais, a lista nova não estoura new_tracks
    e não traz participação, homônimo nem artista banido."""
    import random as pyrandom
    from spotify_playlists.config import load_config

    catalog, feedback, state = _repo_data()
    root = os.path.join(os.path.dirname(__file__), "..")
    cfg = load_config(os.path.join(root, "config", "playlists.yaml"))
    spec = manager._with_banned_artists(
        next(p for p in cfg.playlists if "Bom Dia" in p.name).spec, feedback
    )
    played = state.get("played") or {}
    now = datetime.fromisoformat(max(played.values()))
    recent = manager._recent_played(state, spec.rotation_days, now=now)

    def as_track(d):
        return Track(d["uri"], d["name"], d["artists"], list(d.get("artist_ids") or []))

    known_by_uri: dict[str, Track] = {}
    extra: list[Track] = []
    for entry in (catalog.get("artists") or {}).values():
        for d in entry.get("tracks") or []:
            t = as_track(d)
            if not curator._is_allowlisted_main(t, spec.match_artists):
                continue
            if t.uri in set(feedback.get("disliked_uris") or []):
                continue
            if curator._excluded_by_artist(t, spec.exclude_artists):
                continue
            if curator._has_keyword(t, spec.exclude_keywords):
                continue
            if not curator._passes_genres(t, spec):
                continue
            keys = song_keys(t.name)
            if keys & set(played):
                known_by_uri[t.uri] = t
            else:
                extra.append(t)
    # a última geração também é "conhecida", mas só se passar no filtro novo
    for d in state.get("generated") or []:
        t = as_track(d)
        if t.uri in known_by_uri:
            continue
        if not curator._is_allowlisted_main(t, spec.match_artists):
            continue
        if curator._excluded_by_artist(t, spec.exclude_artists):
            continue
        if curator._has_keyword(t, spec.exclude_keywords):
            continue
        if not curator._passes_genres(t, spec):
            continue
        known_by_uri[t.uri] = t

    known = list(known_by_uri.values())
    pyrandom.seed(0)
    chosen = curator._fill_sing_along(known, extra, recent, spec)
    extra_uris = {t.uri for t in extra}
    novas = [t for t in chosen if t.uri in extra_uris]
    assert len(novas) <= spec.new_tracks, len(novas)
    assert len(chosen) <= spec.size
    for t in chosen:
        assert curator._is_allowlisted_main(t, spec.match_artists), t
        assert "chay suede" not in t.artists.lower(), t
        assert t.uri not in _BAD_CATALOG_URIS
    # com o rodízio real estourado, tem de reusar conhecida em vez de catálogo
    assert any(curator._last_played(t, recent) is not None for t in chosen), "não reusou conhecida"
    assert len(known) > spec.size  # senão o teste não exercita o teto de verdade


def test_played_log_pruned():
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 9, 24, tzinfo=timezone.utc)
        manager._save_generated(d, "T", [Track("u", "Velha", "A", [])], now=now - timedelta(days=90))
        manager._save_generated(d, "T", [Track("v", "Nova", "B", [])], now=now)
        played = manager._load_state(d, "T")["played"]
        assert "nova" in played and "velha" not in played
        assert manager._recent_played({"played": played}, 7, now=now) == {"nova": played["nova"]}


def report_offline_before_after() -> None:
    """Imprime a lista simulada antes (ordem antiga, catálogo sujo) e depois."""
    from spotify_playlists.config import load_config

    catalog, feedback, state = _repo_data()
    cfg = load_config(os.path.join(_repo_root(), "config", "playlists.yaml"))
    raw = next(p for p in cfg.playlists if "Bom Dia" in p.name).spec
    spec = manager._with_banned_artists(raw, feedback)
    played = state.get("played") or {}
    now = datetime.fromisoformat(max(played.values()))
    recent = manager._recent_played(state, spec.rotation_days, now=now)

    def as_track(d):
        return Track(d["uri"], d["name"], d["artists"], list(d.get("artist_ids") or []))

    def pools(use, require_main: bool):
        known_by_uri: dict[str, Track] = {}
        extra: list[Track] = []
        for entry in (catalog.get("artists") or {}).values():
            label = entry.get("name") or ""
            for d in entry.get("tracks") or []:
                t = as_track(d)
                if require_main:
                    if not curator._is_main_artist(t.artists, label):
                        continue
                    if not curator._is_allowlisted_main(t, use.match_artists):
                        continue
                elif not curator._artist_allowed(t, use.match_artists):
                    continue
                if t.uri in set(feedback.get("disliked_uris") or []):
                    continue
                if curator._excluded_by_artist(t, use.exclude_artists):
                    continue
                if curator._has_keyword(t, use.exclude_keywords):
                    continue
                if not curator._passes_genres(t, use):
                    continue
                if require_main and use.portuguese_only and curator._looks_english_title(t.name):
                    if not (song_keys(t.name) & set(played)):
                        continue
                if song_keys(t.name) & set(played):
                    known_by_uri[t.uri] = t
                else:
                    extra.append(t)
        for d in state.get("generated") or []:
            t = as_track(d)
            if t.uri in known_by_uri:
                continue
            ok = (
                curator._is_allowlisted_main(t, use.match_artists)
                if require_main
                else curator._artist_allowed(t, use.match_artists)
            )
            if not ok or curator._excluded_by_artist(t, use.exclude_artists):
                continue
            if curator._has_keyword(t, use.exclude_keywords):
                continue
            if not curator._passes_genres(t, use):
                continue
            known_by_uri[t.uri] = t
        return list(known_by_uri.values()), extra

    def show(title, chosen, extra, teto):
        extra_uris = {t.uri for t in extra}
        novas = [t for t in chosen if t.uri in extra_uris]
        print(f"\n== {title} ==")
        print(f"tamanho {len(chosen)} · não ouvidas {len(novas)} (teto {teto})")
        for t in chosen:
            tag = "NOVA" if t.uri in extra_uris else (
                "REUSO" if curator._last_played(t, recent) else "FRESCA"
            )
            print(f"  [{tag}] {t.name} — {t.artists}")

    known_old, extra_old = pools(raw, require_main=False)
    known_new, extra_new = pools(spec, require_main=True)
    random.seed(0)
    antes = _fill_legado(known_old, extra_old, recent, raw)
    random.seed(0)
    depois = curator._fill_sing_along(known_new, extra_new, recent, spec)
    show("ANTES no estado atual (participação entra, sem teto)", antes, extra_old, raw.new_tracks)
    show("DEPOIS no estado atual (principal, teto, reuso)", depois, extra_new, spec.new_tracks)

    # Disparos manuais queimam as conhecidas frescas. No código antigo o que
    # sobra de catálogo (ainda não ouvido) completa a lista inteira. Aqui o
    # catálogo solto entra como não ouvido e o acervo que já passou no filtro
    # novo fica só pra reuso.
    known_uris = {t.uri for t in known_new}
    catalogo_solto = [t for t in (known_old + extra_old) if t.uri not in known_uris]
    catalogo_limpo = [
        t for t in catalogo_solto
        if curator._is_allowlisted_main(t, spec.match_artists)
        and not curator._excluded_by_artist(t, spec.exclude_artists)
        and curator._passes_genres(t, spec)
        and not (spec.portuguese_only and curator._looks_english_title(t.name))
    ]
    random.seed(1)
    antes_vazio = _fill_legado([], catalogo_solto, {}, raw)
    random.seed(1)
    depois_vazio = curator._fill_sing_along(known_new, catalogo_limpo, recent, spec)
    show("ANTES com conhecidas frescas esgotadas", antes_vazio, catalogo_solto, raw.new_tracks)
    show("DEPOIS com conhecidas frescas esgotadas", depois_vazio, catalogo_limpo, spec.new_tracks)


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(tests)} testes passaram")
    report_offline_before_after()
