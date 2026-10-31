"""Criação e atualização das playlists na conta do usuário."""

from __future__ import annotations

import collections
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from spotipy import Spotify

from .config import Config, DailySlot, PlaylistDef
from .curator import (
    CurationSpec,
    Track,
    _name_matches,
    catalog_artist_names,
    catalog_tracks,
    catalog_trends,
    curate,
    explore_releases,
    explore_trends,
    refresh_catalog,
    scrub_catalog,
    song_keys,
)
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


PLAYED_KEEP_DAYS = 60  # quanto tempo guardar o registro de "tocou quando"
HISTORY_KEEP_DAYS = 14  # gerações inteiras guardadas pro fiscal (audit)
MIN_HOURS_BETWEEN_RUNS = 2  # --slot-guard: nunca gera 2x em menos de N horas


def _state_path(base: str, name: str) -> str:
    return os.path.join(base, "state", f"{_slug(name)}.json")


def _load_state(base: str, name: str) -> dict:
    try:
        with open(_state_path(base, name), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def _recent_played(state: dict, days: int, now: datetime | None = None) -> dict[str, str]:
    """{chave_da_música: último ISO em que tocou} dentro da janela de ``days``.

    Junta o log longo ``played`` com o ``history`` (listas inteiras): tudo que
    FOI pra playlist conta no rodízio, mesmo geração manual que não gravou o
    log longo — senão a lista manual de manhã voltava inteira à noite.
    """
    if days <= 0:
        return {}
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    recent = {k: v for k, v in (state.get("played") or {}).items() if v >= cutoff}
    for h in state.get("history") or []:
        at = h.get("at") or ""
        if at < cutoff:
            continue
        for _uri, name, _artists in h.get("tracks") or []:
            for k in song_keys(name):
                if k not in recent or recent[k] < at:
                    recent[k] = at
    return recent


def banned_artist_names(feedback: dict, threshold: int) -> list[str]:
    """Artistas que você removeu vezes demais pra continuar na seleção.

    ``threshold`` é o mínimo de remoções (``>=``). 0 desliga. A chave já vem
    em minúsculas, do jeito que ``_learn_from_removals`` grava.
    """
    if threshold <= 0:
        return []
    out: list[str] = []
    for name, count in (feedback.get("disliked_artists") or {}).items():
        if name and isinstance(count, int) and count >= threshold:
            out.append(name)
    return out


def _with_banned_artists(spec: CurationSpec, feedback: dict) -> CurationSpec:
    """Soma ao veto da config os artistas acima do limiar de remoções."""
    banned = banned_artist_names(feedback, spec.dislike_artist_threshold)
    if not banned:
        return spec
    shown = ", ".join(sorted(banned))
    print(
        f"   🚫 sem estes artistas (você removeu ≥{spec.dislike_artist_threshold}×): {shown}"
    )
    return replace(spec, exclude_artists=[*spec.exclude_artists, *banned])


def _save_generated(
    base: str,
    name: str,
    tracks: list[Track],
    now: datetime | None = None,
    record_rotation: bool = True,
) -> None:
    """Guarda a geração atual (pro detector de remoções) e, se ``record_rotation``,
    registra no rodízio a data em que cada música tocou (por chave, qualquer versão).

    Disparo manual passa ``record_rotation=False`` pra atualizar a playlist sem
    queimar a semana. O cron continua gravando.
    """
    os.makedirs(os.path.join(base, "state"), exist_ok=True)
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    old = _load_state(base, name)
    played = dict(old.get("played") or {})
    if record_rotation:
        for t in tracks:
            for k in song_keys(t.name):
                played[k] = stamp
    cutoff = (now - timedelta(days=PLAYED_KEEP_DAYS)).isoformat(timespec="seconds")
    played = {k: v for k, v in sorted(played.items()) if v >= cutoff}
    # Histórico das listas inteiras (pro fiscal): o que de fato foi pra playlist,
    # com ou sem gravação no rodízio.
    hist_cutoff = (now - timedelta(days=HISTORY_KEEP_DAYS)).isoformat(timespec="seconds")
    history = [h for h in (old.get("history") or []) if (h.get("at") or "") >= hist_cutoff]
    history.append({
        "at": stamp,
        "rotation": bool(record_rotation),
        "tracks": [[t.uri, t.name, t.artists] for t in tracks],
    })
    payload = {
        "generated_at": stamp,
        "generated": [
            {"uri": t.uri, "name": t.name, "artists": t.artists, "source": getattr(t, "source", "")}
            for t in tracks
        ],
        "played": played,
        "history": history,
    }
    with open(_state_path(base, name), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def last_generated_at(base: str, name: str) -> datetime | None:
    """Quando esta playlist foi gerada pela última vez (None se nunca/estado antigo)."""
    stamp = _load_state(base, name).get("generated_at")
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def recently_generated(
    config: Config,
    hours: float = MIN_HOURS_BETWEEN_RUNS,
    base: str = DATA_DIR,
    now: datetime | None = None,
) -> str | None:
    """Nome da 1ª playlist diária gerada há menos de ``hours`` horas (ou None).

    Fecha a brecha do cron do GitHub: duas execuções atrasadas que chegam no
    mesmo minuto passavam as duas pela trava de janela e geravam 2 listas
    (queimando 50 músicas do rodízio de uma vez).
    """
    now = now or datetime.now(timezone.utc)
    for pdef in config.playlists:
        if not pdef.daily:
            continue
        when = last_generated_at(base, pdef.name)
        if when and (now - when) < timedelta(hours=hours):
            return pdef.name
    return None


# --------------------------------------------------------------------------- #
# Catálogo dos artistas permitidos (cache versionado em data/catalog.json)
# --------------------------------------------------------------------------- #
DEFAULT_CATALOG_BUDGET = 12  # artistas atualizados por geração (freio de rate limit)


def _load_catalog(base: str) -> dict:
    try:
        with open(os.path.join(base, "catalog.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {"artists": {}}


def _save_catalog(base: str, cache: dict) -> None:
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "catalog.json"), "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)


def _catalog_for(
    sp: Spotify, spec: CurationSpec, base: str, budget: int
) -> tuple[list[Track], list[Track]]:
    """Atualiza (em lote pequeno) e devolve o catálogo dos artistas permitidos.

    Estrangeiros ficam de fora: deles só entra o que você já ouve. Artista
    vetado (config ou remoções) também. Cache antigo perde participação e
    homônimo sem chamar a API de novo.
    """
    artists = catalog_artist_names(spec)
    cache = _load_catalog(base)
    scrubbed = scrub_catalog(cache)
    updated = refresh_catalog(sp, cache, artists, spec.market, budget)
    if scrubbed or updated:
        _save_catalog(base, cache)
    have = sum(1 for a in artists if (cache.get("artists") or {}).get(a.lower()))
    trends = catalog_trends(cache) if spec.trend_tracks > 0 else []
    print(
        f"   📚 catálogo: {have}/{len(artists)} artistas em cache ({updated} atualizados agora)"
        f" · {len(trends)} tendências do explorador"
    )
    return catalog_tracks(cache, artists), trends


# --------------------------------------------------------------------------- #
# Janelas da diária (manhã / noite) — 1 geração por janela
# --------------------------------------------------------------------------- #
BRT = timezone(timedelta(hours=-3))  # Brasil sem horário de verão desde 2019


def current_slot(slots: list[DailySlot], now: datetime | None = None) -> str | None:
    """Chave da janela atual, ex. '2026-09-24-noite'; None fora das janelas."""
    local = (now or datetime.now(timezone.utc)).astimezone(BRT)
    for slot in slots:
        if slot.start <= local.hour < slot.end:
            return f"{local.date().isoformat()}-{slot.name}"
    return None


def _slots_path(base: str) -> str:
    return os.path.join(base, "state", "_slots.json")


def slot_already_done(slot: str, base: str = DATA_DIR) -> bool:
    try:
        with open(_slots_path(base), encoding="utf-8") as fh:
            return json.load(fh).get("last") == slot
    except (FileNotFoundError, ValueError):
        return False


def mark_slot_done(slot: str, base: str = DATA_DIR) -> None:
    os.makedirs(os.path.join(base, "state"), exist_ok=True)
    with open(_slots_path(base), "w", encoding="utf-8") as fh:
        json.dump({"last": slot}, fh)


def _current_playlist_uris(sp: Spotify, playlist_id: str) -> set[str] | None:
    """URIs que estão AGORA na playlist. Devolve None se não conseguir ler.

    Pós-migração da Web API (fev/2026) cada entrada traz a faixa na chave
    ``item`` — a antiga ``track`` virou um booleano — e ``GET /playlists/{id}/tracks``
    passou a dar 403. Lemos os dois formatos e também o ``linked_from``
    (relinking por mercado), pra comparar com o que gravamos sem falso positivo.
    """
    uris: set[str] = set()
    try:
        page = sp.playlist_items(playlist_id, limit=100)
        while page:
            for entry in page.get("items", []):
                obj = entry.get("item")
                if not isinstance(obj, dict):
                    obj = entry.get("track")
                if not isinstance(obj, dict):
                    continue
                if obj.get("uri"):
                    uris.add(obj["uri"])
                linked = (obj.get("linked_from") or {}).get("uri")
                if linked:
                    uris.add(linked)
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
        if not artist:
            continue
        # Quanto a remoção pesa contra o ARTISTA:
        # - tendência / artista de fora da lista: sai na 1ª (conta o limiar inteiro);
        # - música NOVA de um artista seu (catálogo): só a música sai — tirar um
        #   lado B do Seu Jorge não é "não gosto do Seu Jorge";
        # - música conhecida (acervo) ou geração antiga sem a marca: 1 ponto.
        spec = pdef.spec
        source = t.get("source") or ""
        outsider = bool(spec.match_artists) and not _name_matches(
            t.get("artists", ""), spec.match_artists
        )
        if outsider or source == "trend":
            step = max(1, spec.dislike_artist_threshold)
        elif source == "new":
            step = 0
        else:
            step = 1
        if step:
            feedback["disliked_artists"][artist] = (
                feedback["disliked_artists"].get(artist, 0) + step
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


def sync_playlist(
    sp: Spotify,
    pdef: PlaylistDef,
    data_dir: str = DATA_DIR,
    catalog_budget: int = DEFAULT_CATALOG_BUDGET,
    record_rotation: bool = True,
) -> list[Track]:
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

    spec = _with_banned_artists(pdef.spec, feedback)
    disliked = set(feedback["disliked_uris"])
    now = datetime.now(timezone.utc)
    recent = _recent_played(_load_state(data_dir, pdef.name), spec.rotation_days, now=now)
    catalog = trends = None
    if spec.sing_along and spec.match_artists:
        catalog, trends = _catalog_for(sp, spec, data_dir, catalog_budget)
    tracks = curate(
        sp, spec, disliked_uris=disliked, recent=recent, catalog=catalog, now=now, trends=trends
    )
    playlist_id = _ensure_playlist(sp, pdef)
    _replace_tracks(sp, playlist_id, tracks)

    if spec.learn_removals or spec.rotation_days:
        _save_generated(data_dir, pdef.name, tracks, now=now, record_rotation=record_rotation)
    _save_feedback(data_dir, feedback)
    return tracks


# --------------------------------------------------------------------------- #
# Explorador semanal (lançamentos + tendências) e fiscal de repetição
# --------------------------------------------------------------------------- #
def explore_all(
    sp: Spotify,
    config: Config,
    only: str | None = None,
    budget: int | None = None,
    data_dir: str = DATA_DIR,
) -> dict[str, dict]:
    """Mapeia músicas novas pras playlists diárias e grava em ``data/catalog.json``.

    Lançamentos recentes de cada artista permitido + tendências das
    ``trend_queries``. Não mexe na playlist: a diária passa a usar o que foi
    achado nas próximas gerações. Retorna {playlist: {"releases": n, "trends": n}}.
    """
    feedback = _load_feedback(data_dir)
    cache = _load_catalog(data_dir)
    scrub_catalog(cache)
    report: dict[str, dict] = {}
    for pdef in config.playlists:
        if only and pdef.name != only:
            continue
        if not only and not pdef.daily:
            continue
        spec = _with_banned_artists(pdef.spec, feedback)
        if not (spec.sing_along and spec.match_artists):
            print(f"⏭️  '{pdef.name}' não usa catálogo de artistas — nada a explorar.")
            continue
        print(f"🔎 Explorando para '{pdef.name}'")
        artists = catalog_artist_names(spec)
        releases = explore_releases(sp, cache, artists, spec.market, budget=budget)
        n_rel = sum(len(v) for v in releases.values())
        print(f"   🆕 lançamentos: {n_rel} faixa(s) nova(s) de {len(releases)} artista(s)")
        for name, tracks in releases.items():
            for t in tracks[:3]:
                print(f"      • {t}")
        trends: list[Track] = []
        if spec.trend_queries:
            trends = explore_trends(sp, cache, spec, skip_uris=set(feedback["disliked_uris"]))
            total = len(cache.get("trends") or [])
            print(
                f"   📈 tendências: {len(trends)} faixa(s) nova(s) em {len(spec.trend_queries)} "
                f"busca(s) ({total} guardadas no total)"
            )
            for t in trends[:12]:
                print(f"      • {t}")
        else:
            print("   📈 tendências: sem 'trend_queries' na config — só lançamentos.")
        report[pdef.name] = {"releases": n_rel, "trends": len(trends)}
    _save_catalog(data_dir, cache)
    return report


def _song_key(name: str) -> str:
    keys = song_keys(name)
    return min(keys) if keys else name.strip().lower()


def audit_playlist(
    pdef: PlaylistDef,
    data_dir: str = DATA_DIR,
    days: int = 7,
    now: datetime | None = None,
) -> dict:
    """Fiscal de repetição: confere a última lista contra o histórico e mede a
    semana. Imprime um relatório (legível no resumo do GitHub) e devolve números.

    ``violations`` = músicas da última lista que tocaram há menos de
    ``min_gap_days``; ``repeats`` = as que tocaram dentro de ``rotation_days``.
    """
    spec = pdef.spec
    now = now or datetime.now(timezone.utc)
    history = _load_state(data_dir, pdef.name).get("history") or []
    out = {
        "playlist": pdef.name, "generations": len(history), "violations": 0,
        "repeats": 0, "unique": 0, "slots": 0, "lists": 0,
    }
    print(f"\n🧾 Fiscal de repetição — '{pdef.name}'")
    if not history:
        print("   sem histórico ainda (passa a ser gravado a partir desta versão).")
        return out

    def when(h: dict) -> datetime:
        d = datetime.fromisoformat(h["at"])
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    last = history[-1]
    last_at = when(last)
    seen: dict[str, datetime] = {}
    for h in history[:-1]:
        at = when(h)
        for _uri, name, _artists in h["tracks"]:
            for k in song_keys(name):
                if k not in seen or seen[k] < at:
                    seen[k] = at
    repeats: list[tuple[float, str, str]] = []
    for _uri, name, artists in last["tracks"]:
        gaps = [(last_at - seen[k]).total_seconds() / 86400 for k in song_keys(name) if k in seen]
        if gaps:
            repeats.append((min(gaps), name, artists))
    repeats.sort()
    violations = [r for r in repeats if spec.min_gap_days > 0 and r[0] < spec.min_gap_days]
    in_rotation = [r for r in repeats if spec.rotation_days > 0 and r[0] < spec.rotation_days]
    stamp = last_at.astimezone(BRT).strftime("%d/%m %H:%M")
    print(
        f"   última lista ({stamp}, Brasília): {len(last['tracks'])} músicas · "
        f"{len(in_rotation)} tocaram nos últimos {spec.rotation_days} dias · "
        f"{len(violations)} há menos de {spec.min_gap_days} dias"
        + (" ⛔" if violations else " ✅")
    )
    for gap, name, artists in violations[:10]:
        print(f"      ⛔ {name} — {artists} (há {gap:.1f} dia(s))")
    for gap, name, artists in [r for r in in_rotation if r not in violations][:5]:
        print(f"      🔂 {name} — {artists} (há {gap:.1f} dia(s))")

    cutoff = now - timedelta(days=days)
    week = [h for h in history if when(h) >= cutoff]
    songs: collections.Counter = collections.Counter()
    artists_c: collections.Counter = collections.Counter()
    slots = 0
    for h in week:
        for _uri, name, artists in h["tracks"]:
            slots += 1
            songs[_song_key(name)] += 1
            artists_c[artists.split(",")[0].strip()] += 1
    if slots:
        pct = 100 * len(songs) / slots
        print(
            f"   últimos {days} dias: {len(week)} listas, {slots} vagas, "
            f"{len(songs)} músicas diferentes ({pct:.0f}%), {len(artists_c)} artistas"
        )
        top = [f"{k} ({n}×)" for k, n in songs.most_common(5) if n > 1]
        if top:
            print("   mais repetidas: " + ", ".join(top))
    if violations and os.environ.get("GITHUB_ACTIONS"):
        print(
            f"::warning title=Fiscal da diária::{len(violations)} música(s) repetida(s) "
            f"com menos de {spec.min_gap_days} dias em '{pdef.name}'"
        )
    out.update(
        violations=len(violations), repeats=len(in_rotation), unique=len(songs),
        slots=slots, lists=len(week),
    )
    return out


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


def sync_all(
    sp: Spotify,
    config: Config,
    scope: str = "season",
    catalog_budget: int = DEFAULT_CATALOG_BUDGET,
    record_rotation: bool = True,
) -> dict[str, list[Track]]:
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

        tracks = sync_playlist(
            sp, pdef, catalog_budget=catalog_budget, record_rotation=record_rotation
        )
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
