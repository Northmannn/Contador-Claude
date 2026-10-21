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
    # .strip()/join: remove espaços e QUEBRAS DE LINHA acidentais do secret
    # (copiar do PowerShell costuma inserir quebras no meio do token).
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")
    refresh_token = "".join(refresh_token.split())
    if refresh_token:
        token_info = oauth.refresh_access_token(refresh_token)
        return Spotify(auth=token_info["access_token"], retries=0)

    return Spotify(auth_manager=oauth, retries=0)


def _token_for_diagnosis() -> tuple[dict | None, str]:
    """Token pra diagnóstico + de onde ele veio (secret/.env ou cache local)."""
    oauth = _oauth()
    refresh_token = "".join(os.environ.get("SPOTIFY_REFRESH_TOKEN", "").split())
    if refresh_token:
        return (
            oauth.refresh_access_token(refresh_token),
            "SPOTIFY_REFRESH_TOKEN (secret do GitHub / .env)",
        )
    return oauth.get_cached_token(), "cache local (.cache do último login)"


def describe_auth() -> None:
    """Mostra usuário e escopos concedidos, SEM expor tokens. Diagnóstico.

    Com ``SPOTIFY_REFRESH_TOKEN`` (GitHub Actions ou .env) usa o refresh token —
    é assim que se confere PARA QUAL CONTA o secret aponta depois de trocar de
    conta. Sem ele, usa o cache do último ``login``.
    """
    token_info, origem = _token_for_diagnosis()
    if not token_info:
        print("Sem token em cache nem SPOTIFY_REFRESH_TOKEN. Rode 'python main.py login' primeiro.")
        return

    sp = Spotify(auth=token_info["access_token"])
    me = sp.me()
    print(f"Token de: {origem}")
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
        print("    → Rode 'python main.py login' de novo (ele apaga o cache sozinho).")
    else:
        print("\n✅ Todos os escopos necessários foram concedidos.")
        if me.get("product") != "premium":
            print("ℹ️  Conta não-premium — criar/editar playlist ainda funciona.")


def login_and_print_refresh_token() -> str:
    """Roda o fluxo interativo e devolve o refresh token (pra usar como secret).

    Sempre pede autorização no navegador: um ``.cache`` de login anterior
    (de OUTRA conta, por exemplo) faria o spotipy devolver o token antigo sem
    perguntar nada — e o secret continuaria apontando pra conta errada.
    """
    try:
        os.remove(".cache")
        print("(apaguei o .cache de um login anterior — vou pedir autorização de novo)")
    except FileNotFoundError:
        pass

    oauth = _oauth()
    token_info = oauth.get_access_token(as_dict=True)
    refresh_token = token_info["refresh_token"]
    me = Spotify(auth=token_info["access_token"]).me()

    # Salva num arquivo de texto pra copiar SEM quebras de linha do terminal
    # (copiar token comprido do PowerShell costuma corrompê-lo).
    with open("refresh_token.txt", "w", encoding="utf-8") as fh:
        fh.write(refresh_token)

    print("\n✅ Login concluído!\n")
    print(f"Conta autorizada: {me.get('display_name')}  (id: {me.get('id')})")
    print("   ⚠️  Confira se é a conta CERTA. Se não for: saia do Spotify no navegador")
    print("      (ou use 'Não é você?' na tela de autorização) e rode o login de novo.\n")
    print("Seu REFRESH TOKEN foi salvo no arquivo:  refresh_token.txt")
    print("Abra com:  notepad refresh_token.txt")
    print("Copie TODO o conteúdo e cole no secret SPOTIFY_REFRESH_TOKEN do GitHub.")
    print("(NÃO copie do terminal — a quebra de linha corrompe o token.)")
    print("⚠️  Esse arquivo é secreto e já está no .gitignore. Apague depois de usar.")
    return refresh_token
