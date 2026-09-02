"""Criação e atualização das playlists na conta do usuário."""

from __future__ import annotations

import json
import os
import re

from spotipy import Spotify

from .config import Config, PlaylistDef
from .curator import Track, curate
from .seasons import current_season, season_label

# --------------------------------------------------------------------------- #
# Aprendizado: "não gosto" a partir do que você TIRA das playlists
# --------------------------------------------------------------------------- #
# Guardamos em data/ (commitado pelo workflow) o que você removeu, pra nunca
# repetir e pra medir seu gosto (quais artistas você mais descarta).
DATA_DIR = "data"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "playlist"


def _load_feedback(base: str) -> dict:
    try:
        with open(os.path.join(base, "feedback.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        data = {}
    data.setdefault("disliked_uris", [])
    data.setdefault("disliked_artists", {})  # {artista: quantas vezes você removeu}
    return data


def _save_feedback(base: str, data: dict) -> None:
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "feedback.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _load_generated(base: str, name: str) -> list[dict]:
    try:
        path = os.path.join(base, "state", f"{_slug(name)}.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("generated", [])
    except (FileNotFoundError, ValueError):
        return []


def _save_generated(base: str, name: str, tracks: list[Track]) -> None:
    os.makedirs(os.path.join(base, "state"), exist_ok=True)
    path = os.path.join(base, "state", f"{_slug(name)}.json")
    payload = {
        "generated": [
            {"uri": t.uri, "name": t.name, "artists": t.artists} for t in tracks
        ]
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _current_playlist_uris(sp: Spotify, playlist_id: str) -> set[str] | None:
    """URIs que estão AGORA na playlist. Devolve None se não conseguir ler."""
    uris: set[str] = set()
    try:
        page = sp.playlist_items(
            playlist_id, limit=100, fields="items(track(uri)),next"
        )
        while page:
            for item in page.get("items", []):
                track = item.get("track") or {}
                if track.get("uri"):
                    uris.add(track["uri"])
            page = sp.next(page) if page.get("next") else None
    except Exception:
        return None
    return uris


def _learn_from_removals(sp: Spotify, pdef: PlaylistDef, feedback: dict, base: str) -> int:
    """Compara o que a gente gerou da última vez com o que sobrou na playlist.
    O que sumiu = você removeu = vira 'não gosto'. Retorna quantas aprendeu.
    """
    playlist_id = _find_playlist_id(sp, pdef.name)
    if not playlist_id:
        return 0
    current = _current_playlist_uris(sp, playlist_id)
    last = _load_generated(base, pdef.name)
    if current is None or not last:
        return 0

    disliked = set(feedback["disliked_uris"])
    removed = [t for t in last if t["uri"] not in current]

    # Guarda anti-falso-positivo: ninguém apaga a playlist inteira todo dia.
    # Se "sumiu" quase tudo, é leitura ruim (ex.: relinking de URI), não gosto.
    if len(last) >= 3 and len(removed) >= max(3, int(0.8 * len(last))):
        print(
            f"   ⚠️ {len(removed)} de {len(last)} 'sumiram' — parece erro de leitura, "
            "NÃO vou tratar como 'não gosto'."
        )
        return 0

    for t in removed:
        if t["uri"] not in disliked:
            feedback["disliked_uris"].append(t["uri"])
            disliked.add(t["uri"])
        artist = (t.get("artists", "").split(",")[0].strip().lower())
        if artist:
            feedback["disliked_artists"][artist] = (
                feedback["disliked_artists"].get(artist, 0) + 1
            )
    return len(removed)


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


def sync_playlist(sp: Spotify, pdef: PlaylistDef, data_dir: str = DATA_DIR) -> list[Track]:
    """Cura e grava uma playlist. Retorna as faixas escolhidas.

    Se a playlist tem ``learn_removals``, antes de renovar ela LÊ o que você
    tirou (comparando com o que geramos da última vez) e guarda como 'não
    gosto' — pra nunca mais repetir e pra medir seu gosto por artista.
    """
    feedback = _load_feedback(data_dir)

    if pdef.spec.learn_removals:
        learned = _learn_from_removals(sp, pdef, feedback, data_dir)
        if learned:
            print(f"   🧠 Aprendi: você removeu {learned} música(s) — não repito mais.")

    disliked = set(feedback["disliked_uris"])
    tracks = curate(sp, pdef.spec, disliked_uris=disliked)
    playlist_id = _ensure_playlist(sp, pdef)
    _replace_tracks(sp, playlist_id, tracks)

    if pdef.spec.learn_removals:
        _save_generated(data_dir, pdef.name, tracks)
    _save_feedback(data_dir, feedback)
    return tracks


def describe_taste(sp: Spotify, top_n: int = 20) -> None:
    """Imprime seus artistas e músicas mais ouvidos em 3 janelas de tempo.

    Use pra me mostrar (copiando o resultado) o seu gosto real, e aí eu curo
    as playlists com precisão em vez de chutar.
    """
    ranges = [
        ("long_term", "ÚLTIMOS ANOS (gosto consolidado)"),
        ("medium_term", "últimos ~6 meses"),
        ("short_term", "últimas ~4 semanas"),
    ]
    for time_range, label in ranges:
        print(f"\n========== {label} ==========")
        try:
            arts = sp.current_user_top_artists(limit=top_n, time_range=time_range)
            print("Artistas mais ouvidos:")
            for i, a in enumerate(arts.get("items", []), 1):
                genres = ", ".join(a.get("genres", [])[:3]) or "—"
                print(f"  {i:2}. {a['name']}  [{genres}]")
        except Exception as exc:
            print(f"  (não consegui ler os artistas: {exc})")

        try:
            trs = sp.current_user_top_tracks(limit=top_n, time_range=time_range)
            print("Músicas mais ouvidas:")
            for i, t in enumerate(trs.get("items", []), 1):
                who = ", ".join(x["name"] for x in t.get("artists", []))
                print(f"  {i:2}. {t['name']} — {who}")
        except Exception as exc:
            print(f"  (não consegui ler as músicas: {exc})")


def describe_diagnosis(sp: Spotify, name: str, data_dir: str = DATA_DIR) -> None:
    """Diagnóstico do detector de remoções: compara o que gravamos com o que a
    API devolve ao ler a playlist. Não altera nada."""
    me = sp.me()["id"]
    print(f"\n🔎 Diagnóstico de '{name}' (usuário {me})\n")

    # 1) Todas as playlists com esse nome (duplicatas?)
    found = []
    offset = 0
    while True:
        page = sp.current_user_playlists(limit=50, offset=offset)
        for pl in page.get("items", []):
            if pl and pl["name"] == name:
                found.append(pl)
        if page.get("next"):
            offset += 50
        else:
            break
    print(f"Playlists com esse nome exato: {len(found)}")
    for pl in found:
        print(f"  - id={pl['id']} dono={pl['owner']['id']} total={pl.get('tracks', {}).get('total')}")

    pid = _find_playlist_id(sp, name)
    print(f"\n_find_playlist_id escolheu: {pid}")
    if not pid:
        return

    # 2) Leitura crua (sem filtro 'fields') vs. leitura do detector
    raw = sp.playlist_items(pid, limit=100)
    items = raw.get("items", [])
    print(f"Leitura CRUA: {len(items)} itens (total={raw.get('total')})")
    for it in items[:5]:
        tr = it.get("track") or {}
        lf = (tr.get("linked_from") or {}).get("uri")
        print(f"  • {tr.get('uri')}  linked_from={lf}  ({tr.get('name')})")
    if items:
        print("\nITEM CRU [0] (chaves):", sorted(items[0].keys()))
        print("ITEM CRU [0] (json):", json.dumps(items[0], ensure_ascii=False)[:900])

    # 2b) Endpoints alternativos pós-migração
    for path in (f"playlists/{pid}/items", f"playlists/{pid}/tracks"):
        try:
            alt = sp._get(path, limit=3)
            first = (alt.get("items") or [{}])[0]
            print(f"\nGET {path}: {len(alt.get('items', []))} itens; chaves[0]={sorted(first.keys())}")
            print("   json[0]:", json.dumps(first, ensure_ascii=False)[:600])
        except Exception as exc:
            print(f"\nGET {path}: ERRO {exc}")
    try:
        full = sp.playlist(pid)
        print("\nplaylist() chaves:", sorted(full.keys()))
    except Exception as exc:
        print(f"\nplaylist(): ERRO {exc}")

    current = _current_playlist_uris(sp, pid)
    print(f"\nLeitura do DETECTOR (_current_playlist_uris): "
          f"{'None (erro)' if current is None else f'{len(current)} uris'}")
    if current:
        for u in list(current)[:5]:
            print(f"  • {u}")

    # 3) Estado gravado da última geração
    last = _load_generated(data_dir, name)
    last_uris = {t["uri"] for t in last}
    print(f"\nEstado gravado (última geração): {len(last)} uris")
    for t in last[:5]:
        print(f"  • {t['uri']}  ({t['name']})")
    if current is not None:
        inter = last_uris & current
        print(f"\nInterseção gravado ∩ lido: {len(inter)} de {len(last_uris)}")
        raw_uris = {(it.get('track') or {}).get('uri') for it in items}
        print(f"Interseção gravado ∩ leitura crua: {len(last_uris & raw_uris)}")
        linked = {((it.get('track') or {}).get('linked_from') or {}).get('uri') for it in items}
        print(f"Interseção gravado ∩ linked_from da leitura crua: {len(last_uris & linked)}")


def describe_feedback(data_dir: str = DATA_DIR) -> None:
    """Mostra o que aprendi do que você removeu — seu gosto medido na prática."""
    feedback = _load_feedback(data_dir)
    total = len(feedback["disliked_uris"])
    artists = feedback["disliked_artists"]
    print(f"\n🧠 Aprendizado até agora: {total} música(s) que você removeu.\n")
    if not artists:
        print("Ainda não removi nada. Vá tirando das playlists o que não curtir —")
        print("eu leio, aprendo e nunca mais repito. 😉")
        return
    print("Artistas que você mais descartou (quanto maior, menos você curte):")
    for artist, count in sorted(artists.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>3}×  {artist.title()}")


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
