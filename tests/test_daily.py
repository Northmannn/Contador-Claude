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
    """Modo antigo (fill_with_catalog: false): conhecidas frescas acabaram → reusa
    a mais antiga, sem estourar new_tracks."""
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
        size=6, new_tracks=2, fill_with_catalog=False,
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


def _known_and_catalog():
    """6 conhecidas (0 e 1 frescas; 2..5 tocaram) e 7 novas de artistas distintos."""
    known = [Track(f"u{i}", f"Música {i}", f"Artista {chr(65 + i)}", []) for i in range(6)]
    catalog = [
        Track(f"c{i}", f"Nova {i}", f"Artista {chr(71 + i)}", []) for i in range(7)
    ]

    class Sp(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            if time_range != "long_term":
                return {"items": []}
            return {"items": [_item(t.uri, t.name, t.artists) for t in known]}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    artists = [t.artists for t in known] + [t.artists for t in catalog]
    return known, catalog, Sp, artists


def test_varies_with_catalog_before_repeating():
    """Política nova (padrão): acabou conhecida fresca → entra catálogo não ouvido,
    e só repete quando o catálogo também acabar."""
    curator._GENRE_CACHE.clear()
    known, catalog, Sp, artists = _known_and_catalog()
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    recent = {f"música {i}": (now - timedelta(days=i)).isoformat(timespec="seconds") for i in range(2, 6)}
    spec = _spec(size=6, new_tracks=2, match_artists=artists)
    chosen = curator.curate(Sp(), spec, recent=recent, catalog=catalog, now=now)
    names = {t.name for t in chosen}
    assert len(chosen) == 6, names
    assert {"Música 0", "Música 1"} <= names, names            # conhecidas frescas entram
    assert not any(curator._last_played(t, recent) for t in chosen), names  # nada repetido
    assert sum(n.startswith("Nova") for n in names) == 4, names  # 2 garantidas + 2 pra variar


def test_min_gap_is_a_hard_rule():
    """Sem catálogo e só conhecidas recentes: quem tocou há menos de min_gap_days
    NÃO entra, mesmo que a lista fique curta."""
    curator._GENRE_CACHE.clear()
    known, _catalog, Sp, artists = _known_and_catalog()
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    # 0..5 tocaram há 1, 2, 3, 4, 5, 6 dias
    recent = {f"música {i}": (now - timedelta(days=i + 1)).isoformat(timespec="seconds") for i in range(6)}
    spec = _spec(size=6, new_tracks=0, min_gap_days=3, match_artists=artists)
    chosen = curator.curate(Sp(), spec, recent=recent, catalog=[], now=now)
    names = {t.name for t in chosen}
    assert names == {"Música 2", "Música 3", "Música 4", "Música 5"}, names  # 1 e 2 dias: fora
    assert len(chosen) == 4  # curta, mas sem violar a regra


def test_min_gap_reuses_oldest_first_when_forced():
    """Quando precisa repetir, repete a mais antiga e respeita o gap mínimo."""
    curator._GENRE_CACHE.clear()
    known, _catalog, Sp, artists = _known_and_catalog()
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    recent = {f"música {i}": (now - timedelta(days=i + 1)).isoformat(timespec="seconds") for i in range(6)}
    spec = _spec(size=2, new_tracks=0, min_gap_days=3, match_artists=artists)
    chosen = curator.curate(Sp(), spec, recent=recent, catalog=[], now=now)
    assert {t.name for t in chosen} == {"Música 5", "Música 4"}, [t.name for t in chosen]


def test_trend_quota_and_filters():
    """Tendências entram até o teto, com os vetos (inglês, funk, artista vetado)."""
    curator._GENRE_CACHE.clear()
    known, catalog, Sp, artists = _known_and_catalog()
    trends = [
        Track("t1", "Coração Valente", "Fulano de Tal", []),
        Track("t2", "Saudade Boa", "Beltrano", []),
        Track("t3", "Dance With Me", "Sicrano", []),        # inglês: fora
        Track("t4", "Tropa", "MC Qualquer", []),             # funk: fora
        Track("t5", "Linda Flor", "Banda Vetada", []),       # exclude_artists: fora
    ]
    spec = _spec(size=6, new_tracks=1, trend_tracks=1, match_artists=artists,
                 exclude_artists=["Banda Vetada"], portuguese_only=True)
    chosen = curator.curate(Sp(), spec, recent={}, catalog=catalog, trends=trends)
    picked = {t.name for t in chosen if t.uri.startswith("t")}
    assert len(picked) == 1 and picked <= {"Coração Valente", "Saudade Boa"}, [str(t) for t in chosen]
    spec0 = _spec(size=6, new_tracks=1, trend_tracks=0, match_artists=artists)
    chosen0 = curator.curate(Sp(), spec0, recent={}, catalog=catalog, trends=trends)
    assert not any(t.uri.startswith("t") for t in chosen0)


def test_history_audit_and_run_guard():
    """Histórico guarda as listas; o fiscal conta repetições; a trava de 2h segura
    execuções atrasadas do cron que chegam juntas."""
    from spotify_playlists.config import Config

    with tempfile.TemporaryDirectory() as d:
        t0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
        a = Track("a", "Alfa", "Artista A", [])
        b = Track("b", "Beta", "Artista B", [])
        c = Track("c", "Gama", "Artista C", [])
        e = Track("e", "Delta", "Artista D", [])
        manager._save_generated(d, "T", [a, b, c], now=t0)
        manager._save_generated(d, "T", [e, c], now=t0 + timedelta(days=1))
        manager._save_generated(d, "T", [a, e, b], now=t0 + timedelta(days=4), record_rotation=False)
        state = manager._load_state(d, "T")
        assert len(state["history"]) == 3 and state["generated_at"].startswith("2026-10-24")
        pdef = PlaylistDef(name="T", daily=True, spec=_spec(size=3, rotation_days=7, min_gap_days=3))
        rep = manager.audit_playlist(pdef, data_dir=d, days=7, now=t0 + timedelta(days=4))
        # Alfa e Beta: 4 dias (ok, mas dentro dos 7); Delta: 3 dias (no limite, ok)
        assert rep["repeats"] == 3 and rep["violations"] == 0, rep
        manager._save_generated(d, "T", [e], now=t0 + timedelta(days=5))
        rep2 = manager.audit_playlist(pdef, data_dir=d, days=7, now=t0 + timedelta(days=5))
        assert rep2["violations"] == 1, rep2  # Delta tocou há 1 dia
        assert rep2["unique"] == 4 and rep2["slots"] == 9, rep2

        cfg = Config(hemisphere="southern", market="BR", playlists=[pdef])
        assert manager.recently_generated(cfg, base=d, now=t0 + timedelta(days=5, hours=1)) == "T"
        assert manager.recently_generated(cfg, base=d, now=t0 + timedelta(days=5, hours=3)) is None


def test_explore_releases_and_trends():
    """Explorador: lançamentos entram em `releases` (crédito principal) e as
    tendências passam pelos vetos; o catálogo da diária enxerga os dois."""
    curator.CATALOG_PAUSE_S = 0

    class Sp(FakeSpotify):
        def search(self, q, type, market="BR", limit=10, offset=0):
            self.search_calls += 1
            if "year:" in q and q.startswith("artist:"):
                name = q.split('"')[1]
                return {"tracks": {"items": [
                    _item(f"r-{name}-1", f"Lançamento de {name}", name),
                    {"uri": f"r-{name}-2", "name": "Feat", "artists": [
                        {"id": "x", "name": "Outro"}, {"id": name.lower(), "name": name}]},
                ]}}
            if "pagode" in q:
                return {"tracks": {"items": [] if offset else [
                    _item("tr1", "Coração de Samba", "Grupo Novo"),
                    _item("tr2", "Love Song", "Grupo Novo"),       # inglês: fora
                    _item("tr3", "Baile Proibido", "MC Novo"),     # funk pelo nome: fora
                    _item("tr4", "Já Conhecida", "Grupo Velho"),   # skip_uris (não gosto): fora
                    _item("tr5", "Nosso Samba", "Grupo Novo"),     # sem marca de PT: fora (rigor p/ artista de fora)
                ]}}
            return {"tracks": {"items": []}}

    cache = {"artists": {"artista a": {"name": "Artista A", "fetched": None,
                                       "tracks": [{"uri": "k0", "name": "Hit", "artists": "Artista A"}]}}}
    found = curator.explore_releases(Sp(), cache, ["Artista A", "Artista B"], "BR")
    assert {k for k in found} == {"Artista A", "Artista B"}
    assert [t.name for t in found["Artista A"]] == ["Lançamento de Artista A"]  # feat fora
    assert len(cache["artists"]["artista b"]["releases"]) == 1
    names = {t.name for t in curator.catalog_tracks(cache, ["Artista A", "Artista B"])}
    assert {"Hit", "Lançamento de Artista A", "Lançamento de Artista B"} <= names

    spec = _spec(trend_queries=["pagode year:2025-2026"], portuguese_only=True, trend_tracks=2)
    new = curator.explore_trends(Sp(), cache, spec, skip_uris={"tr4"})
    assert [t.name for t in new] == ["Coração de Samba"], [str(t) for t in new]
    assert [t.name for t in curator.catalog_trends(cache)] == ["Coração de Samba"]
    assert curator.explore_trends(Sp(), cache, spec, skip_uris={"tr4"}) == []  # já guardada


def test_max_new_tracks_keeps_list_singable():
    """Teto de novas: sem conhecida fresca, a lista repete conhecidas antigas
    (≥ gap mínimo) em vez de virar uma lista de desconhecidas."""
    curator._GENRE_CACHE.clear()
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    known = [Track(f"u{i}", f"Música {i}", f"Artista {chr(65 + i)}", []) for i in range(12)]
    recent = {f"música {i}": (now - timedelta(days=4 + i % 3)).isoformat(timespec="seconds") for i in range(12)}
    catalog = [Track(f"c{i}", f"Nova {i}", f"Artista {chr(77 + i)}", []) for i in range(12)]

    class Sp(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            if time_range != "long_term":
                return {"items": []}
            return {"items": [_item(t.uri, t.name, t.artists) for t in known]}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    artists = [t.artists for t in known] + [t.artists for t in catalog]
    spec = _spec(size=10, new_tracks=2, max_new_tracks=4, min_gap_days=3, match_artists=artists)
    chosen = curator.curate(Sp(), spec, recent=recent, catalog=catalog, now=now)
    novas = [t for t in chosen if t.name.startswith("Nova")]
    assert len(chosen) == 10 and len(novas) == 4, [t.name for t in chosen]
    # sem teto, a mesma situação viraria 10 novas
    spec2 = _spec(size=10, new_tracks=2, max_new_tracks=0, min_gap_days=3, match_artists=artists)
    chosen2 = curator.curate(Sp(), spec2, recent=recent, catalog=catalog, now=now)
    assert sum(t.name.startswith("Nova") for t in chosen2) == 10


def test_sequence_blocks_and_spread():
    """Blocos na ordem da config; tendência (fora dos blocos) no fim; novas
    espalhadas, não aglomeradas."""
    spec = _spec(artist_groups=[("pagode", ["Artista A", "Artista B"]),
                                ("sertanejo", ["Artista C", "Dupla & Tal"])])
    tracks = [
        Track("s1", "S1", "Artista C", []), Track("p1", "P1", "Artista A", []),
        Track("t1", "T1", "Fulano", []), Track("pn", "PN", "Artista B", []),
        Track("s2", "S2", "Dupla & Tal", []), Track("p2", "P2", "Artista A", []),
        Track("p3", "P3", "Artista B", []), Track("sn", "SN", "Artista C", []),
    ]
    random.seed(3)
    out = curator.sequence_tracks(tracks, spec, new_uris={"pn", "sn", "t1"})
    names = [t.uri for t in out]
    assert names[-1] == "t1", names                              # tendência fecha
    assert set(names[:4]) == {"p1", "p2", "p3", "pn"}, names      # bloco pagode primeiro
    assert set(names[4:7]) == {"s1", "s2", "sn"}, names           # depois sertanejo
    assert names[0] != "pn" and names[4] != "sn", names           # nova não abre o bloco
    assert curator.sequence_tracks(tracks, _spec(), new_uris=set()) == tracks  # sem blocos: como veio


def test_config_artist_groups_feed_match_artists():
    from spotify_playlists.config import load_config
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config", "playlists.yaml"))
    bom = next(p for p in cfg.playlists if "Bom Dia" in p.name).spec
    assert [n for n, _ in bom.artist_groups][:2] == ["pagode & samba", "sertanejo"]
    assert "Só Pra Contrariar" in bom.match_artists and "Michael Bublé" in bom.match_artists
    assert len(bom.match_artists) == sum(len(a) for _, a in bom.artist_groups)
    assert bom.max_new_tracks >= bom.new_tracks > 0


def test_new_account_without_library_fills_from_catalog():
    """Conta nova (sem curtidas nem histórico): não há conhecida pra reusar e a
    lista sairia com só ``new_tracks`` músicas. Só nesse caso o catálogo completa
    o tamanho — respeitando rodízio e 1 por artista."""
    curator._GENRE_CACHE.clear()

    class Empty(FakeSpotify):
        def current_user_top_tracks(self, limit, time_range):
            return {"items": []}

        def current_user_saved_tracks(self, limit):
            return {"items": [], "next": None}

    catalog = [Track(f"c{i}", f"Hit {i}", ARTISTS[i], []) for i in range(30)]
    recent = {"hit 0": "2026-09-24T00:00:00+00:00"}  # tocou esta semana: não volta
    spec = _spec(size=10, new_tracks=4)
    chosen = curator.curate(Empty(), spec, recent=recent, catalog=catalog)
    names = [t.name for t in chosen]
    assert len(chosen) == 10, names
    assert "Hit 0" not in names, names
    assert len({curator._artist_key(t) for t in chosen}) == 10, names


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


def test_removal_weight_by_source():
    """Remoção: música NOVA do seu artista tira só a música; conhecida dá 1 ponto
    ao artista; artista de fora (tendência) sai na 1ª."""
    class Sp:
        def me(self):
            return {"id": "eu"}

        def current_user_playlists(self, limit, offset):
            return {"items": [{"name": "T", "id": "pl", "owner": {"id": "eu"}}], "next": None}

        def playlist_items(self, pid, limit):
            # 3 de 7 sobraram (4 removidas = 57%, abaixo da guarda de 80% = erro de leitura)
            return {"items": [{"item": {"uri": u}} for u in ("fica", "fica2", "fica3")], "next": None}

    with tempfile.TemporaryDirectory() as d:
        tracks = [
            Track("fica", "Fica", "Artista A", [], source="known"),
            Track("fica2", "Fica 2", "Artista E", [], source="known"),
            Track("fica3", "Fica 3", "Artista F", [], source="new"),
            Track("k", "Conhecida", "Artista B", [], source="known"),
            Track("n", "Lado B", "Artista C", [], source="new"),
            Track("t", "Moda", "Fulano", [], source="trend"),
            Track("v", "Velha", "Artista D", []),  # geração antiga, sem marca
        ]
        manager._save_generated(d, "T", tracks, now=datetime(2026, 10, 31, tzinfo=timezone.utc))
        pdef = PlaylistDef(name="T", daily=True, spec=_spec(
            size=7, match_artists=["Artista A", "Artista B", "Artista C", "Artista D",
                                   "Artista E", "Artista F"],
            dislike_artist_threshold=2))
        feedback = manager._load_feedback(d)
        assert manager._learn_from_removals(Sp(), pdef, feedback, d) == 4
        assert set(feedback["disliked_uris"]) == {"k", "n", "t", "v"}
        assert feedback["disliked_artists"] == {"artista b": 1, "fulano": 2, "artista d": 1}, feedback


def test_manual_run_counts_in_rotation_but_not_in_long_log():
    """Geração manual sem gravar: fica fora do log longo `played`, mas o que foi
    pra playlist conta no rodízio (via histórico) — lista manual não volta inteira
    na geração seguinte."""
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
        assert set(recent) == {"velha", "nova"}, recent
        assert recent["nova"] == now.isoformat(timespec="seconds")
        # fora da janela do rodízio, some
        assert manager._recent_played(state, 7, now=now + timedelta(days=8)) == {}


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
    banned = set(manager.banned_artist_names(feedback, 2))
    counts = feedback.get("disliked_artists") or {}
    assert counts, "feedback.json sem disliked_artists — o teste não exercita o veto"
    for artist, n in counts.items():  # o feedback real muda todo dia: compara com ele
        assert (artist in banned) == (n >= 2), (artist, n, banned)
    assert banned <= set(counts)


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
    """Com o estado e o catálogo reais: pelo menos new_tracks novas, nada abaixo do
    gap mínimo, nenhuma repetição enquanto houver catálogo, e sem participação,
    homônimo ou artista banido."""
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
    chosen = curator._fill_sing_along(known, extra, recent, spec, now=now)
    extra_uris = {t.uri for t in extra}
    novas = [t for t in chosen if t.uri in extra_uris]
    fresh_extra = [t for t in extra if curator._last_played(t, recent) is None]
    fresh_known = [t for t in known if curator._last_played(t, recent) is None]
    assert len(novas) >= min(spec.new_tracks, len(fresh_extra)), len(novas)
    assert len(chosen) <= spec.size
    for t in chosen:
        assert curator._is_allowlisted_main(t, spec.match_artists), t
        assert "chay suede" not in t.artists.lower(), t
        assert t.uri not in _BAD_CATALOG_URIS
        gap = curator._days_since(t, recent, now)
        assert gap is None or gap >= spec.min_gap_days, (str(t), gap)  # regra dura
    # Com catálogo fresco sobrando, não pode haver repetição nenhuma.
    if len(fresh_known) + len(fresh_extra) >= spec.size + 10:
        assert not any(curator._last_played(t, recent) for t in chosen), "repetiu com catálogo sobrando"
    assert len(known) > spec.size  # senão o teste não exercita o rodízio de verdade


def test_fixed_track_resolution_checks_artist_and_title():
    """Trilha fixa "Título | Artista": pula karaokê/homônimo/título errado e
    aceita acento, pontuação e sufixo de versão diferentes."""
    def row(uri, name, *artists):
        return {"uri": uri, "name": name, "artists": [{"id": a, "name": a} for a in artists]}

    class Sp(FakeSpotify):
        def search(self, q, type, market="BR", limit=10, offset=0):
            if "Menino" in q:
                items = [row("k", "O Menino da Porteira - Karaoke Version", "Sérgio Reis"),
                         row("x", "Menino da Porteira", "Outro Cantor"),
                         row("ok1", "Menino da Porteira - Ao Vivo", "Sérgio Reis")]
            elif "Feeling" in q:
                items = [row("ok2", "CAN'T STOP THE FEELING! (from Trolls)", "Justin Timberlake")]
            elif "Smile" in q:
                items = [row("w", "Smile", "Lady Gaga"),
                         row("ok3", "Die With A Smile", "Lady Gaga", "Bruno Mars")]
            elif "Inexistente" in q:
                items = [row("z", "Outra Coisa", "Fulano")]
            else:
                items = [row("livre", "Primeiro Resultado", "Qualquer")]
            return {"tracks": {"items": items}}

    curator.CATALOG_PAUSE_S = 0
    spec = CurationSpec(fixed_tracks=[
        "O Menino da Porteira | Sergio Reis",
        "Can't Stop the Feeling! | Justin Timberlake",
        "Die With A Smile | Lady Gaga",
        "Inexistente | Fulano",
        "texto livre antigo",
    ])
    assert [t.uri for t in curator.curate(Sp(), spec)] == ["ok1", "ok2", "ok3", "livre"]


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
