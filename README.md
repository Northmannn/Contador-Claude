# 🎵 Playlists Sazonais do Spotify

Cria e atualiza playlists do Spotify automaticamente para diferentes ocasiões
e estações do ano. Você define a "vibe" de cada playlist num arquivo de
configuração; o projeto busca as músicas, monta a playlist na sua conta e
renova o conteúdo periodicamente.

- ✅ **Cria/atualiza playlists** na sua conta (funciona em conta grátis)
- 🗓️ **Atualização sazonal automática** via GitHub Actions (hemisfério sul / Brasil)
- 🎛️ **Curadoria configurável** por busca: queries, gêneros, artistas, anos
- 🖐️ **Roda na mão** quando você quiser, ou **no automático** por agendamento

> A Spotify descontinuou o endpoint de "Recommendations" para apps novos (2024),
> então a curadoria é feita por **busca** a partir do que você define em
> `config/playlists.yaml`. Na prática: você descreve a vibe, a API monta.

---

## 1. Pré-requisitos

- Python 3.10+
- Uma conta Spotify (grátis serve)

## 2. Criar o app no Spotify

1. Acesse o [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   e clique em **Create app**.
2. Em **Redirect URI**, adicione exatamente:
   `http://127.0.0.1:8888/callback`
3. Anote o **Client ID** e o **Client Secret**.

## 3. Instalar

```bash
pip install -r requirements.txt
cp .env.example .env   # depois edite o .env com suas credenciais
```

Preencha no `.env`:

```
SPOTIPY_CLIENT_ID=...
SPOTIPY_CLIENT_SECRET=...
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

## 4. Autorizar (uma vez só)

```bash
python main.py login
```

Isso abre o navegador pra você autorizar. No fim, ele imprime um
**refresh token** — guarde-o, você vai usar no GitHub Actions.

## 5. Usar na mão

```bash
python main.py list                  # mostra as playlists e quais entram nesta estação
python main.py taste                 # mostra seus artistas/músicas mais ouvidos
python main.py whoami                # diagnóstico: conta e permissões concedidas
python main.py sync --only "🏋️ Treino da Semana"   # só uma playlist (recomendado)
python main.py sync                  # atualiza as playlists da estação atual
python main.py sync --daily          # atualiza as playlists diárias (daily: true)
python main.py sync --force          # atualiza TODAS (cuidado: muitas chamadas de API)
```

> ⚠️ A Spotify limita requisições **por app**. Prefira `--only` (uma por vez) e
> evite gerar várias seguidas, pra não bater no rate limit (HTTP 429). Detalhes
> e mais armadilhas da API estão em `AGENTS.md`.

## 6. Automatizar (GitHub Actions)

O workflow `.github/workflows/seasonal-update.yml` roda toda segunda às
06:00 (Brasília) e também pode ser disparado manualmente.

No GitHub: **Settings → Secrets and variables → Actions → New repository secret**,
crie estes secrets:

| Secret | Valor |
|---|---|
| `SPOTIPY_CLIENT_ID` | seu Client ID |
| `SPOTIPY_CLIENT_SECRET` | seu Client Secret |
| `SPOTIPY_REDIRECT_URI` | `http://127.0.0.1:8888/callback` |
| `SPOTIFY_REFRESH_TOKEN` | o refresh token do passo 4 |

Pronto: quando virar a estação, as playlists daquela estação se renovam sozinhas.

---

## Personalizando as playlists

Edite `config/playlists.yaml`. Exemplo de uma entrada:

```yaml
  - name: "🏋️ Treino da Semana"
    description: "Pique pra malhar. Renovada o ano todo."
    size: 40
    seasons: [always]          # ou [summer], [autumn, winter], etc.
    queries:
      - "workout hype"
      - "hip hop gym"
    genres: [hip-hop, electronic]
    artist_seeds: []           # ex: ["Anitta", "Drake"]
    year_range: null           # ex: "2015-2024"
```

- `seasons`: use `[always]` pra renovar o ano todo, ou estações específicas
  (`summer`, `autumn`, `winter`, `spring`). No Brasil as estações já vêm
  invertidas automaticamente (`hemisphere: southern`).
- Quer me pedir ajuda pra montar a vibe de uma playlist? Só descrever a ocasião
  que eu monto as `queries`/`genres` pra você.

## Estrutura do projeto

```
config/playlists.yaml          # suas playlists (edite aqui)
main.py                        # CLI
spotify_playlists/
  auth.py                      # login / OAuth / refresh token
  config.py                    # leitura do YAML
  curator.py                   # busca e seleciona as faixas
  manager.py                   # cria/atualiza as playlists na conta
  seasons.py                   # detecção de estação (hemisfério sul)
.github/workflows/             # agendamento automático
```

## Limitações

- **Não toca música** nem controla dispositivos (isso exigiria Spotify Premium).
  O foco é **criar e gerenciar playlists**.
- A curadoria depende da busca da Spotify — quanto melhores as `queries`,
  melhores os resultados.
