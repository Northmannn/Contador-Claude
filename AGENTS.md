# Guia do projeto para agentes de IA (Codex, Claude, etc.)

Este arquivo é o "mapa" do projeto para qualquer assistente de IA que for
ajudar a desenvolvê-lo. Leia tudo antes de mexer — ele concentra decisões e,
principalmente, **armadilhas reais da Spotify Web API** já descobertas aqui
(custaram horas de debug). O usuário fala português; responda em português.

---

## 1. O que é

Projeto em **Python** que cria e atualiza playlists do Spotify automaticamente,
por ocasião e por estação do ano, com curadoria configurável em YAML e
renovação agendada via GitHub Actions. Funciona em conta grátis para gerenciar
playlists (controle de playback exigiria Premium e está fora do escopo).

## 2. Estrutura

```
config/playlists.yaml          # TODAS as playlists do usuário (fonte da verdade)
main.py                        # CLI: login / whoami / taste / list / sync
spotify_playlists/
  auth.py        # OAuth (interativo local + refresh token p/ CI); escopos
  config.py      # lê o YAML -> PlaylistDef + CurationSpec
  curator.py     # monta a lista de faixas (modos search e discovery)
  manager.py     # cria/atualiza playlists na conta; describe_taste; escopos de sync
  seasons.py     # estação atual (hemisfério sul / Brasil)
.github/workflows/
  seasonal-update.yml   # cron semanal (playlists sazonais)
  daily-morning.yml     # cron diário 08:00 UTC = 05:00 BRT (playlists daily:true)
```

## 3. Como rodar

```bash
pip install -r requirements.txt
cp .env.example .env            # preencher Client ID/Secret (Spotify Dashboard)
python main.py login            # autoriza (abre navegador) e imprime refresh token
python main.py taste            # mostra artistas/músicas mais ouvidos do usuário
python main.py list             # lista as playlists configuradas
python main.py sync --only "NOME"   # atualiza UMA playlist (preferir isto)
python main.py sync --daily     # atualiza as playlists daily:true
python main.py sync --force     # atualiza TODAS (cuidado: muitas chamadas de API)
python main.py whoami           # diagnóstico: usuário, conta, escopos concedidos
```

`.env` necessário: `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`,
`SPOTIPY_REDIRECT_URI` (= `http://127.0.0.1:8888/callback`),
e `SPOTIFY_REFRESH_TOKEN` (preenchido pelo `login`, usado no CI).

## 4. Como a curadoria funciona

Cada item de `config/playlists.yaml` vira um `PlaylistDef` com um `CurationSpec`.
Dois modos (`mode`):

- **search** — monta por busca: `queries`, `genres`, `artist_seeds`, `year_range`.
- **discovery** — baseado no gosto do usuário:
  - `seed_from_taste: true` usa os artistas mais ouvidos (`current_user_top_artists`)
    filtrados por `match_genres` (incluir) e `exclude_genres` (excluir).
  - `include_top_tracks: true` injeta as **músicas mais ouvidas** do usuário.
  - `exclude_heard: true` remove o que ele já escutou (descoberta/surpresa).
  - `hits_only: true` usa só as faixas mais tocadas dos artistas (ex.: karaokê).

Campos comuns: `name`, `description`, `public`, `size`, `seasons`
(`[always]` ou estações), `daily` (entra no fluxo `sync --daily`).

## 5. ⚠️ ARMADILHAS DA SPOTIFY WEB API (leia antes de mexer no curator/manager)

A migração da Web API de **fevereiro/2026** e mudanças de 2024/2025 quebram
suposições antigas. O que JÁ pegamos aqui:

1. **Criar playlist:** o endpoint legado `POST /users/{id}/playlists` foi
   **removido** (403). Use `POST /me/playlists`
   (`sp.current_user_playlist_create`). Já corrigido em `manager._create_playlist`.
2. **Busca:** o `limit` máximo caiu de **50 para 10** (default 20→5). Pedir
   `limit>10` dá **400 "Invalid limit"**. `curator._search_tracks` pagina de 10
   em 10 via `offset`.
3. **Endpoints de catálogo bloqueados em Development Mode (403):**
   `GET /artists/{id}/top-tracks`, `/artists/{id}/albums`, `/albums/{id}/tracks`
   e relacionados costumam dar 403. O curator tenta e **cai na busca por nome**
   do artista. Há um cache `_BLOCKED` que para de chamar o endpoint após o 1º 403
   (economia de requisições — importante p/ rate limit).
4. **Sem "Recomendações"/"Artistas relacionados":** desativados para apps novos.
   Não existe "me dê músicas parecidas". "Parecido" = mesmos artistas/gêneros.
5. **Sem "Audio Features":** não dá pra filtrar por BPM/energia/tom. Metas tipo
   "acima de 100 BPM" ou "voz de barítono" são feitas por **gênero/artista**, não
   por medição. Seja honesto com o usuário sobre isso.
6. **Extended Quota Mode:** desde 05/2025 praticamente só para organizações
   grandes. **Não** peça isso para projeto pessoal — criar/editar playlist na
   própria conta funciona em Development Mode.
7. **Development Mode / allowlist:** a conta que autentica precisa estar em
   Settings > User Management do app (limite ~5 usuários) e o dono precisa de
   Premium. Não era a causa dos 403 acima, mas é exigido pro app funcionar.

## 6. Rate limit (IMPORTANTE)

O rate limit é **por app (Client ID)**, janela móvel de ~30s; estourar gera
HTTP **429** com `Retry-After` que pode chegar a **horas**. Regras:

- Prefira `sync --only` (uma playlist por vez). Evite `--force` (gera todas).
- Não regere em sequência; gere, ouça, só then regere.
- `auth.get_client` usa `retries=0` pra **não dormir** o Retry-After (que pode
  ser horas); `main._handle_rate_limit` mostra mensagem clara no 429.
- Um **app novo (outro Client ID)** tem cota própria — é a saída rápida se um app
  for bloqueado. Veja a seção 7.

## 7. Configurar um app Spotify novo (cota de rate limit zerada)

1. https://developer.spotify.com/dashboard → **Create app**.
2. Redirect URI **exatamente**: `http://127.0.0.1:8888/callback`. Marque **Web API**.
3. Copie Client ID/Secret para o `.env`.
4. Settings > User Management: adicione a conta Spotify (nome + e-mail).
5. `del .cache` (Windows) ou `rm .cache`; depois `python main.py login`.

## 8. Estado atual

Playlists já configuradas em `config/playlists.yaml`: sazonais (Verão/Outono/
Inverno/Primavera), Treino, Samba & Pagode (discovery), Date (cantar no carro),
Karaokê BR (barítono, hits), Bom Dia (diária 5h, exclui funk/rap), Hiperfoco
Noturno (intenso/eufórico), Aconchego Animado (top tracks do usuário) e
No Mesmo Clima — Descobertas (mesmo gosto, músicas novas).

Branch de desenvolvimento: `claude/spotify-playlist-integration-hz4sdw`
(também é a branch padrão do repositório).
