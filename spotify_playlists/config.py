"""Leitura da config de playlists (config/playlists.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .curator import CurationSpec

DEFAULT_CONFIG_PATH = Path("config/playlists.yaml")

# Estações válidas + "always" (atualiza o ano todo).
VALID_SEASONS = {"summer", "autumn", "winter", "spring", "always"}


@dataclass
class PlaylistDef:
    name: str
    description: str = ""
    public: bool = False
    seasons: list[str] = field(default_factory=lambda: ["always"])
    daily: bool = False  # atualiza no fluxo diário (sync --daily), não no sazonal
    spec: CurationSpec = field(default_factory=CurationSpec)

    def runs_in_season(self, season: str) -> bool:
        return "always" in self.seasons or season in self.seasons


@dataclass
class Config:
    hemisphere: str
    market: str
    playlists: list[PlaylistDef]


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config não encontrada em {path}. Veja o exemplo em config/playlists.yaml."
        )

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    hemisphere = data.get("hemisphere", "southern")
    market = data.get("market", "BR")

    playlists: list[PlaylistDef] = []
    for raw in data.get("playlists", []):
        seasons = raw.get("seasons", ["always"])
        invalid = [s for s in seasons if s not in VALID_SEASONS]
        if invalid:
            raise ValueError(
                f"Playlist '{raw.get('name')}' tem estação inválida: {invalid}. "
                f"Use uma de: {sorted(VALID_SEASONS)}"
            )

        spec = CurationSpec(
            queries=raw.get("queries", []),
            genres=raw.get("genres", []),
            artist_seeds=raw.get("artist_seeds", []),
            year_range=raw.get("year_range"),
            market=raw.get("market", market),
            size=int(raw.get("size", 30)),
            mode=raw.get("mode", "search"),
            seed_from_taste=bool(raw.get("seed_from_taste", False)),
            exclude_heard=bool(raw.get("exclude_heard", False)),
            match_genres=raw.get("match_genres", []),
            exclude_genres=raw.get("exclude_genres", []),
            hits_only=bool(raw.get("hits_only", False)),
            include_top_tracks=bool(raw.get("include_top_tracks", False)),
            fixed_tracks=raw.get("tracks", []),
            exclude_artists=raw.get("exclude_artists", []),
            max_per_artist=int(raw.get("max_per_artist", 0)),
            new_tracks=int(raw.get("new_tracks", 0)),
            sing_along=bool(raw.get("sing_along", False)),
            learn_removals=bool(raw.get("learn_removals", False)),
            portuguese_only=bool(raw.get("portuguese_only", False)),
        )
        playlists.append(
            PlaylistDef(
                name=raw["name"],
                description=raw.get("description", ""),
                public=bool(raw.get("public", False)),
                seasons=seasons,
                daily=bool(raw.get("daily", False)),
                spec=spec,
            )
        )

    return Config(hemisphere=hemisphere, market=market, playlists=playlists)
