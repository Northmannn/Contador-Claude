"""Criação e atualização das playlists na conta do usuário."""

from __future__ import annotations

from spotipy import Spotify

from .config import Config, PlaylistDef
from .curator import Track, curate
from .seasons import current_season, season_label


def _find_playlist_id(sp: Spotify, name: str) -> str | None:
    """Procura, entre as playlists do usuário, uma com o nome exato."""
    limit = 50
    offset = 0
    while True:
        page = sp.current_user_playlists(limit=limit, offset=offset)
        for pl in page.get("items", []):
            # Só mexe em playlists que o próprio usuário criou.
            if pl and pl["name"] == name and pl["owner"]["id"] == sp.me()["id"]:
                return pl["id"]
        if page.get("next"):
            offset += limit
        else:
            return None


def _create_playlist(sp: Spotify, pdef: PlaylistDef) -> dict:
    """Cria a playlist do usuário logado via endpoint novo POST /me/playlists.

    O método antigo ``user_playlist_create()`` bate em
    ``POST /users/{id}/playlists``, removido pela migração da Web API de
    fevereiro/2026 (passou a retornar 403). Usamos o substituto
    ``current_user_playlist_create()``; em spotipy antigo que não o tenha,
    caímos direto no endpoint novo.
    """
    try:
        return sp.current_user_playlist_create(
            name=pdef.name,
            public=pdef.public,
            description=pdef.description,
        )
    except AttributeError:
        return sp._post(
            "me/playlists",
            payload={
                "name": pdef.name,
                "public": pdef.public,
                "description": pdef.description,
            },
        )


def _ensure_playlist(sp: Spotify, pdef: PlaylistDef) -> str:
    """Devolve o id da playlist, criando-a se ainda não existir."""
    existing = _find_playlist_id(sp, pdef.name)
    if existing:
        sp.playlist_change_details(
            existing, description=pdef.description, public=pdef.public
        )
        return existing

    created = _create_playlist(sp, pdef)
    return created["id"]


def _replace_tracks(sp: Spotify, playlist_id: str, tracks: list[Track]) -> None:
    """Substitui TODAS as faixas da playlist pelas novas (em lotes de 100)."""
    uris = [t.uri for t in tracks]
    if not uris:
        sp.playlist_replace_items(playlist_id, [])
        return

    # O primeiro lote substitui; os demais acrescentam (limite de 100 por chamada).
    sp.playlist_replace_items(playlist_id, uris[:100])
    for i in range(100, len(uris), 100):
        sp.playlist_add_items(playlist_id, uris[i : i + 100])


def sync_playlist(sp: Spotify, pdef: PlaylistDef) -> list[Track]:
    """Cura e grava uma playlist. Retorna as faixas escolhidas."""
    tracks = curate(sp, pdef.spec)
    playlist_id = _ensure_playlist(sp, pdef)
    _replace_tracks(sp, playlist_id, tracks)
    return tracks


def _should_sync(pdef: PlaylistDef, scope: str, season: str) -> bool:
    """Decide se a playlist entra neste fluxo.

    scope:
      - "season": playlists sazonais da estação atual (exclui as diárias)
      - "daily":  só as marcadas como diárias
      - "all":    todas (usado pelo --force)
    """
    if scope == "daily":
        return pdef.daily
    if scope == "all":
        return True
    # scope == "season"
    return not pdef.daily and pdef.runs_in_season(season)


def sync_all(sp: Spotify, config: Config, scope: str = "season") -> dict[str, list[Track]]:
    """Sincroniza as playlists conforme o ``scope`` (season | daily | all).

    Retorna um dict {nome_da_playlist: faixas} apenas das que foram atualizadas.
    """
    season = current_season(config.hemisphere)
    print(f"🗓️  Estação atual ({config.hemisphere}): {season_label(season)} | escopo: {scope}\n")

    results: dict[str, list[Track]] = {}
    for pdef in config.playlists:
        if not _should_sync(pdef, scope, season):
            print(f"⏭️  Pulando '{pdef.name}'")
            continue

        tracks = sync_playlist(sp, pdef)
        results[pdef.name] = tracks
        print(f"✅ '{pdef.name}': {len(tracks)} faixas")
        for t in tracks[:5]:
            print(f"     • {t}")
        if len(tracks) > 5:
            print(f"     … (+{len(tracks) - 5})")
        print()

    if not results:
        print("Nenhuma playlist para atualizar neste escopo.")
    return results
