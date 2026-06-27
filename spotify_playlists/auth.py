"""Autenticação com a Spotify Web API (OAuth Authorization Code).

Dois modos de uso:

* **Interativo (sua máquina):** abre o navegador uma vez, você autoriza e o
  token fica em cache. Use ``python main.py login`` para gerar o refresh token.
* **Headless (GitHub Action):** usa a variável de ambiente
  ``SPOTIFY_REFRESH_TOKEN`` para renovar o acesso sem abrir navegador.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

# Escopos necessários para gerenciar playlists, enviar capa e LER seu gosto
# (artistas/músicas mais ouvidos e curtidas) para o modo descoberta.
SCOPES = (
    "playlist-modify-public "
    "playlist-modify-private "
    "playlist-read-private "
    "ugc-image-upload "
    "user-top-read "
    "user-library-read"
)


def _oauth() -> SpotifyOAuth:
    """Monta o gerenciador OAuth a partir das variáveis de ambiente."""
    load_dotenv()
    missing = [
        var
        for var in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI")
        if not os.environ.get(var)
    ]
    if missing:
        raise RuntimeError(
            "Variáveis de ambiente faltando: "
            + ", ".join(missing)
            + ".\nCopie .env.example para .env e preencha (veja o README)."
        )

    return SpotifyOAuth(
        client_id=os.environ["SPOTIPY_CLIENT_ID"],
        client_secret=os.environ["SPOTIPY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIPY_REDIRECT_URI"],
        scope=SCOPES,
        cache_path=".cache",
        open_browser=True,
        show_dialog=True,  # força a tela de consentimento p/ garantir todos os escopos
    )


def get_client() -> Spotify:
    """Devolve um cliente Spotify autenticado, pronto pra usar.

    Em CI, prioriza ``SPOTIFY_REFRESH_TOKEN`` (sem navegador). Localmente,
    cai no fluxo interativo com cache em ``.cache``.
    """
    load_dotenv()
    oauth = _oauth()

    # retries=0: sem isso, o spotipy "dorme" o tempo do Retry-After ao bater no
    # rate limit (429) — que pode ser HORAS. Preferimos falhar na hora com uma
    # mensagem clara (tratada em main.py) a deixar o terminal travado.
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    if refresh_token:
        token_info = oauth.refresh_access_token(refresh_token)
        return Spotify(auth=token_info["access_token"], retries=0)

    return Spotify(auth_manager=oauth, retries=0)


def describe_auth() -> None:
    """Mostra usuário e escopos concedidos, SEM expor tokens. Diagnóstico."""
    oauth = _oauth()
    token_info = oauth.get_cached_token()
    if not token_info:
        print("Sem token em cache. Rode 'python main.py login' primeiro.")
        return

    sp = Spotify(auth=token_info["access_token"])
    me = sp.me()
    print(f"Usuário:  {me.get('display_name')}  (id: {me.get('id')})")
    print(f"Conta:    {me.get('product')}  | país: {me.get('country')}")

    granted = set((token_info.get("scope") or "").split())
    print("\nEscopos concedidos:")
    for scope in sorted(granted) or ["(nenhum)"]:
        print(f"  • {scope}")

    needed = {
        "playlist-modify-public",
        "playlist-modify-private",
        "playlist-read-private",
        "user-top-read",
        "user-library-read",
    }
    missing = sorted(needed - granted)
    if missing:
        print("\n⚠️  FALTAM escopos:", ", ".join(missing))
        print("    → Apague o .cache e rode 'python main.py login' de novo.")
    else:
        print("\n✅ Todos os escopos necessários foram concedidos.")
        if me.get("product") != "premium":
            print("ℹ️  Conta não-premium — criar/editar playlist ainda funciona.")


def login_and_print_refresh_token() -> str:
    """Roda o fluxo interativo e devolve o refresh token (pra usar como secret)."""
    oauth = _oauth()
    token_info = oauth.get_access_token(as_dict=True)
    refresh_token = token_info["refresh_token"]
    print("\n✅ Login concluído!\n")
    print("Guarde este REFRESH TOKEN como secret no GitHub (SPOTIFY_REFRESH_TOKEN):\n")
    print(refresh_token)
    print(
        "\nDica: ele também foi salvo em .cache (que está no .gitignore — "
        "nunca commite esse arquivo)."
    )
    return refresh_token
