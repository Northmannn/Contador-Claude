"""Testes da playlist diária: rodízio de 7 dias, catálogo, janelas manhã/noite.

Rodar:  python tests/test_daily.py        (ou: python -m pytest tests/)
Não chama o Spotify de verdade: usa um cliente falso.
"""

from __future__ import annotations

import os
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
    """7 dias x 2 listas: nenhuma música (por chave) repete dentro da semana."""
    curator._BLOCKED.clear()
    curator._GENRE_CACHE.clear()
    curator.CATALOG_PAUSE_S = 0
    with tempfile.TemporaryDirectory() as d:
        sp = FakeSpotify()
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


def test_played_log_pruned():
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 9, 24, tzinfo=timezone.utc)
        manager._save_generated(d, "T", [Track("u", "Velha", "A", [])], now=now - timedelta(days=90))
        manager._save_generated(d, "T", [Track("v", "Nova", "B", [])], now=now)
        played = manager._load_state(d, "T")["played"]
        assert "nova" in played and "velha" not in played
        assert manager._recent_played({"played": played}, 7, now=now) == {"nova": played["nova"]}


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(tests)} testes passaram")
