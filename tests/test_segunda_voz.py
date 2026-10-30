"""Regras da 🎤 Bom Dia, Segunda Voz: veto por faixa, espera, versão e favoritas.

Não chama o Spotify. 'Até Que Durou' tem teto duro de 1x na janela do rodízio
(não volta nem para completar a lista).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spotipy  # noqa: E402

from spotify_playlists.config import load_config  # noqa: E402
from spotify_playlists.curator import CurationSpec, Track, curate, song_keys  # noqa: E402

NOW = datetime(2026, 10, 17, 15, tzinfo=timezone.utc)
ROOT = os.path.join(os.path.dirname(__file__), "..")


def _raw(track: Track) -> dict:
    return {
        "uri": track.uri,
        "name": track.name,
        "artists": [{"id": "x", "name": part.strip()} for part in track.artists.split(",")],
    }


class _Library:
    def __init__(self, tracks: list[Track]):
        self.tracks = tracks

    def current_user_top_tracks(self, limit, time_range):
        if time_range != "long_term":
            return {"items": []}
        return {"items": [_raw(t) for t in self.tracks]}

    def current_user_saved_tracks(self, limit):
        return {"items": [], "next": None}

    def next(self, page):
        return None

    def artists(self, ids):
        raise spotipy.SpotifyException(403, -1, "Forbidden")


def _spec(**kw) -> CurationSpec:
    base = dict(
        sing_along=True,
        size=10,
        new_tracks=0,
        max_per_artist=1,
        portuguese_only=False,
        match_artists=["Soweto", "Belo", "Seu Jorge", "Alcione", "Armandinho", "Péricles"],
    )
    base.update(kw)
    return CurationSpec(**base)


def _choose(tracks, spec, catalog=None, recent=None, now=NOW):
    return curate(
        _Library(tracks),
        spec,
        recent=recent or {},
        catalog=catalog if catalog is not None else [Track("noop", "Nope", "Ninguém", [])],
        now=now,
    )


def test_exclude_tracks_blocks_every_version_and_medley():
    tracks = [
        Track("f1", "Farol Das Estrelas - Ao Vivo", "Soweto", []),
        Track("f2", "Farol Das Estrelas / Refém Do Coração - Ao Vivo", "Belo", []),
        Track("o1", "Outra Canção", "Soweto", []),
    ]
    chosen = _choose(tracks, _spec(exclude_tracks=["Farol das Estrelas"]))
    assert [t.uri for t in chosen] == ["o1"]


def test_hold_until_blocks_before_the_day_and_releases_on_it():
    tracks = [
        Track("t1", "Tua Boca", "Belo", []),
        Track("o1", "Outra Canção", "Soweto", []),
    ]
    spec = _spec(hold_until={"Tua Boca": "2026-12-17"})
    before = _choose(tracks, spec, now=datetime(2026, 12, 16, 15, tzinfo=timezone.utc))
    assert [t.name for t in before] == ["Outra Canção"]
    # 17/12 02:00 UTC ainda é 16/12 em Brasília.
    still = _choose(tracks, spec, now=datetime(2026, 12, 17, 2, tzinfo=timezone.utc))
    assert [t.name for t in still] == ["Outra Canção"]
    on_day = _choose(tracks, spec, now=datetime(2026, 12, 17, 15, tzinfo=timezone.utc))
    assert {t.name for t in on_day} == {"Tua Boca", "Outra Canção"}


def test_only_acoustic_dvd_version_of_tive_razao():
    tracks = [
        Track("live", "Tive Razão - Live", "Seu Jorge", []),
        Track("dvd", "Tive Razão - Ao Vivo", "Seu Jorge, Ana Carolina", []),
        Track("o1", "Outra Canção", "Alcione", []),
    ]
    spec = _spec(only_versions=[{"title": "Tive Razão", "artists": ["Ana Carolina"]}])
    chosen = _choose(tracks, spec)
    assert {t.uri for t in chosen} == {"dvd", "o1"}


def test_favorite_from_catalog_does_not_spend_a_new_slot():
    known = [Track("k1", "Conhecida", "Soweto", [])]
    catalog = [
        Track("fav", "Casinha", "Armandinho", []),
        Track("n1", "Hit Novo 1", "Belo", []),
        Track("n2", "Hit Novo 2", "Alcione", []),
        Track("n3", "Hit Novo 3", "Péricles", []),
    ]
    spec = _spec(
        size=5,
        new_tracks=1,
        favorite_tracks=["Casinha"],
        match_artists=["Soweto", "Armandinho", "Belo", "Alcione", "Péricles"],
    )
    chosen = _choose(known, spec, catalog=catalog)
    uris = {t.uri for t in chosen}
    assert "fav" in uris
    assert len(uris & {"n1", "n2", "n3"}) == 1


def test_medley_favorite_does_not_swallow_a_homonym_part():
    tracks = [
        Track("v", "Verdade", "Zeca Pagodinho", []),
        Track("vai", "Vai Vadiar", "Zeca Pagodinho", []),
        Track(
            "med",
            "Velocidade da Luz / Verdade / Hoje Eu Vou Pagodear",
            "Grupo Fundo De Quintal, Arlindinho",
            [],
        ),
        Track("festa", "Vou Festejar", "Fundo de Quintal", []),
    ]
    spec = _spec(
        size=3,
        favorite_tracks=[
            "Vai Vadiar",
            "Velocidade da Luz/Verdade/Hoje Eu Vou Pagodear",
        ],
        match_artists=["Zeca Pagodinho", "Fundo de Quintal", "Arlindinho"],
    )
    uris = {t.uri for t in _choose(tracks, spec)}
    assert "vai" in uris and "med" in uris
    assert "v" not in uris


def test_favorite_is_the_track_kept_for_that_artist():
    tracks = [
        Track("other", "Outra do Sorriso", "Sorriso Maroto", []),
        Track("fav", "Loucura do Seu Coração", "Sorriso Maroto", []),
        Track("b", "Outra", "Belo", []),
    ]
    spec = _spec(
        size=2,
        favorite_tracks=["Loucura do Seu Coração"],
        match_artists=["Sorriso Maroto", "Belo"],
    )
    chosen = _choose(tracks, spec)
    uris = {t.uri for t in chosen}
    assert "fav" in uris
    assert "other" not in uris


def test_ate_que_durou_is_a_hard_weekly_cap():
    """Dentro da janela de 7 dias ela não volta, nem pra completar a lista."""
    tracks = [
        Track("a", "Até Que Durou - Ao Vivo", "Péricles", []),
        Track("o", "Outra Canção", "Soweto", []),
    ]
    spec = _spec(size=2, weekly_cap_tracks=["Até Que Durou"])
    recent = {"até que durou": "2026-10-15T15:00:00+00:00"}
    blocked = _choose(tracks, spec, recent=recent)
    assert [t.uri for t in blocked] == ["o"]
    allowed = _choose(tracks, spec, recent={})
    assert {t.uri for t in allowed} == {"a", "o"}


def test_segunda_voz_yaml_and_current_bom_dia_list():
    cfg = load_config(os.path.join(ROOT, "config", "playlists.yaml"))
    bom = next(p for p in cfg.playlists if p.name == "🌅 Bom Dia, Motivação")
    voz = next(p for p in cfg.playlists if p.name == "🎤 Bom Dia, Segunda Voz")
    assert bom.daily is True and bom.spec.new_tracks == 4
    assert bom.spec.exclude_tracks == [] and bom.spec.hold_until == {}
    assert voz.daily is False
    assert voz.spec.new_tracks == 4
    assert voz.spec.max_per_artist == 1
    assert voz.spec.rotation_days == 7
    assert voz.spec.portuguese_only is True
    assert len(voz.spec.exclude_tracks) == 9
    assert voz.spec.hold_until["Tchau e Bença"] == "2026-12-17"
    assert voz.spec.hold_until["Tua Boca"] == "2026-12-17"
    assert voz.spec.only_versions[0]["artists"] == ["Ana Carolina"]
    assert "Loucura do Seu Coração" in voz.spec.favorite_tracks
    assert "Até Que Durou" not in " ".join(voz.spec.favorite_tracks)

    with open(os.path.join(ROOT, "data", "state", "bom-dia-motiva-o.json"), encoding="utf-8") as fh:
        generated = json.load(fh)["generated"]
    library = [Track(t["uri"], t["name"], t["artists"], []) for t in generated]
    chosen = _choose(library, voz.spec, now=NOW)
    names: set[str] = set()
    for t in chosen:
        names |= song_keys(t.name)
    vetoed = {
        "farol das estrelas",
        "samba do arnesto",
        "você de volta",
        "um dia pra nós dois",
        "canarinho prisioneiro",
        "amor de cana",
        "pega esse carro",
        "caraca muleke",
        "tristinha",
        "tive razão",
        "tchau e bença",
        "tua boca",
    }
    assert not (names & vetoed), names & vetoed
    assert "até que durou" in names
    assert "loucura do seu coração" in names
    assert "casinha" in names
    assert "vai vadiar" in names
