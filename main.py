#!/usr/bin/env python3
"""CLI pra gerenciar suas playlists do Spotify.

Comandos:
    python main.py login              # autoriza e mostra o refresh token
    python main.py list               # lista as playlists configuradas
    python main.py sync               # atualiza as playlists da estação atual
    python main.py sync --force       # atualiza TODAS, ignorando a estação
    python main.py sync --only NAME   # atualiza só a playlist com esse nome
"""

from __future__ import annotations

import argparse
import sys

from spotipy.exceptions import SpotifyException

from spotify_playlists.auth import (
    describe_auth,
    get_client,
    login_and_print_refresh_token,
)
from spotify_playlists.config import load_config
from spotify_playlists.manager import describe_taste, sync_all, sync_playlist
from spotify_playlists.seasons import current_season, season_label


def _handle_rate_limit(exc: SpotifyException) -> int:
    """Mensagem amigável quando bate no rate limit (429) da Spotify."""
    retry = None
    if getattr(exc, "headers", None):
        retry = exc.headers.get("Retry-After")
    msg = "\n⛔ Rate limit da Spotify (HTTP 429): muitas requisições em pouco tempo."
    if retry and str(retry).isdigit():
        horas = int(retry) / 3600
        msg += f"\n   A Spotify pede pra esperar ~{int(retry)}s (~{horas:.1f}h)."
    msg += (
        "\n   Opções:\n"
        "   1) Esperar o tempo acima e tentar de novo (de preferência uma playlist por vez).\n"
        "   2) Usar um app NOVO (outro Client ID/Secret tem cota própria): crie no dashboard,\n"
        "      troque no .env, apague o .cache e rode 'python main.py login'."
    )
    print(msg, file=sys.stderr)
    return 1


def cmd_login(_args: argparse.Namespace) -> int:
    login_and_print_refresh_token()
    return 0


def cmd_whoami(_args: argparse.Namespace) -> int:
    describe_auth()
    return 0


def cmd_taste(_args: argparse.Namespace) -> int:
    describe_taste(get_client())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    season = current_season(config.hemisphere)
    print(f"Estação atual: {season_label(season)} | mercado: {config.market}\n")
    for p in config.playlists:
        marca = "▶" if p.runs_in_season(season) else " "
        print(f" {marca} {p.name}  [{', '.join(p.seasons)}]  ({p.spec.size} faixas)")
    print("\n▶ = será atualizada agora ao rodar 'sync'")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    try:
        sp = get_client()

        if args.only:
            match = next((p for p in config.playlists if p.name == args.only), None)
            if not match:
                print(f"Playlist '{args.only}' não está na config.", file=sys.stderr)
                return 1
            tracks = sync_playlist(sp, match)
            print(f"✅ '{match.name}': {len(tracks)} faixas atualizadas")
            return 0

        scope = "all" if args.force else "daily" if args.daily else "season"
        sync_all(sp, config, scope=scope)
        return 0
    except SpotifyException as exc:
        if exc.http_status == 429:
            return _handle_rate_limit(exc)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gerenciador de playlists do Spotify")
    parser.add_argument(
        "--config",
        default="config/playlists.yaml",
        help="Caminho da config (padrão: config/playlists.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Autoriza e mostra o refresh token").set_defaults(
        func=cmd_login
    )
    sub.add_parser("list", help="Lista as playlists configuradas").set_defaults(
        func=cmd_list
    )
    sub.add_parser(
        "whoami", help="Mostra usuário e escopos concedidos (diagnóstico)"
    ).set_defaults(func=cmd_whoami)
    sub.add_parser(
        "taste", help="Mostra seus artistas e músicas mais ouvidos"
    ).set_defaults(func=cmd_taste)

    p_sync = sub.add_parser("sync", help="Atualiza as playlists")
    p_sync.add_argument("--force", action="store_true", help="Atualiza todas, ignora estação")
    p_sync.add_argument("--daily", action="store_true", help="Atualiza só as playlists diárias")
    p_sync.add_argument("--only", metavar="NOME", help="Atualiza só esta playlist")
    p_sync.set_defaults(func=cmd_sync)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
