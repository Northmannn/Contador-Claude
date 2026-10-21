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
  daily-morning.yml     # cron DE HORA EM HORA + trava de janela (diária 2x/dia)
  manual-sync.yml       # gera UMA playlist sob demanda (input: nome)
  diagnose.yml / taste.yml / whoami.yml  # read-only: diagnóstico / gosto / conta do secret
data/                   # estado versionado (o workflow commita de volta)
  feedback.json         # "não gosto" aprendido do que o usuário remove
  catalog.json          # cache dos hits de cada artista permitido (TTL 14 dias)
  state/<playlist>.json # última geração + "played" (música -> quando tocou)
  state/_slots.json     # última janela (manhã/noite) já gerada
tests/test_daily.py     # rodízio, catálogo, janelas — rodar antes de commitar
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
python main.py whoami           # diagnóstico: usuário, conta, escopos (com SPOTIFY_REFRESH_TOKEN = conta do secret)
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
8. **Itens de playlist mudaram de formato (set/2026, confirmado por diagnóstico):**
   cada entrada de `playlist_items` traz a faixa na chave **`item`**; a antiga
   `track` virou um **booleano** (`"track": true`). `GET /playlists/{id}/tracks`
   dá **403** (use `/items`, que é o que o spotipy 2.26 já chama) e o objeto
   de playlist não tem mais `tracks.total` (tem `items`). Ler `entry["track"]`
   devolve nada → o detector de remoções marcava 15/15 como "removidas" todo
   dia (525 falsos "não gosto"). Corrigido em `manager._current_playlist_uris`
   (lê `item` e `track`, mais `linked_from`) + guarda: se ≥80% "sumiram", é
   erro de leitura, não aprende. `python main.py diagnose "<playlist>"` (ou o
   workflow *Diagnóstico*) imprime o item cru pra checar de novo se mudar.
9. **Gêneros de artista não vêm mais:** `GET /artists` (lote) dá **403** e os
   artistas de `current_user_top_artists` chegam com `genres: []`. Qualquer
   filtro por gênero é cego. Regra no curator: artista **sem tag não é barrado
   por gênero** (senão nenhum artista do usuário vira semente e as "novas"
   caem em busca genérica = artistas aleatórios); o que segura é veto por
   **nome** (`exclude_artists`) e a heurística "MC …"/"DJ …" = funk. Idioma
   (`portuguese_only`) usa heurística de texto + exceção pras curtidas.
   No catálogo da diária, título claramente em inglês não entra como faixa
   nova mesmo de artista permitido (a heurística "parece português" sozinha
   barraria clássico sem acento, tipo "Trem das Onze", então não é exigida).

## 5b. Playlist diária (🌅 Bom Dia) — como funciona hoje

- **2x ao dia por janelas** (`daily_slots` no YAML, hora de Brasília): manhã
  02h–12h, noite 17h–24h. O cron roda **toda hora** (o GitHub atrasa 5-6h e às
  vezes descarta execuções); `sync --daily --slot-guard` só gera se a janela
  atual ainda não foi gerada. Disparo manual gera sempre e **marca** a janela,
  mas **não grava o rodízio** (não queima a semana). O input `force` do workflow
  grava o rodízio, igual ao cron. Na mão: `sync --no-record-rotation`.
- **Rodízio (`rotation_days: 7`)**: cada música gerada no cron vai pro `played`
  (por *chave de música* — `curator.song_keys`: ignora "Ao Vivo", "(feat.)",
  e separa medleys por "/"). Nada tocado em 7 dias volta. Se as conhecidas
  frescas acabam, **reusa a conhecida que tocou há mais tempo**. Faixa de
  catálogo (não ouvida) **nunca passa de `new_tracks`** — não completa a lista
  com mais catálogo.
- **Acervo conhecido** = top tracks + ~600 curtidas ∩ `match_artists`.
- **Catálogo** (hits via busca `artist:"Nome"`, até 30 por artista) só entra
  nas `new_tracks`. Cache em `data/catalog.json`, atualizado em lotes
  (`CATALOG_BUDGET`, padrão 12 artistas/geração, pausa entre chamadas, para no
  1º 429). Só fica a faixa em que o artista permitido é o **crédito principal**
  — participação ("Papatinho, Seu Jorge") e homônimo ("Vitinho Imperador")
  saem. "Grupo X" e a dupla "X & Y" contam como o mesmo artista. Cache antigo
  é filtrado na leitura (`scrub_catalog`), sem chamada nova à API.
  `foreign_artists` nunca vêm do catálogo — deles só o que ele já ouve.
- **Artista removido demais** (`disliked_artists` no `feedback.json`) sai da
  seleção quando a contagem chega em `dislike_artist_threshold` (padrão 2;
  0 desliga). Uma música tirada não bane o artista; a segunda vez, sim.
- Uma semana = 14 listas × 25 ≈ 350 músicas. Dessas, só `new_tracks` por lista
  vêm do catálogo (4 × 14 = 56). O resto sai do acervo conhecido.
- **Conta sem acervo** (trocou de conta; sem curtidas nem histórico): não há
  conhecida pra reusar e a lista sairia com 4 músicas. Só nesse caso
  `_fill_sing_along` completa com catálogo além de `new_tracks` (respeitando
  rodízio e 1 por artista) e avisa no log. Com acervo normal nunca acontece.

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

## 7b. Trocar de conta Spotify (checklist)

O usuário trocou de conta Spotify em out/2026. Só o token muda de dono —
`data/` (feedback, rodízio, catálogo) continua valendo. Os passos 1–3 só o
usuário consegue fazer: o agente não tem as credenciais (sem `.env`, sem
secrets) e o OAuth exige o navegador dele.

1. App: ou adiciona a conta nova em **User Management** do app atual, ou cria
   app novo logado na conta nova (seção 7) e troca também Client ID/Secret.
2. No PC, logado na conta nova no navegador: `python main.py login` (apaga o
   `.cache` sozinho e imprime a conta autorizada). Copiar `refresh_token.txt`
   para o secret `SPOTIFY_REFRESH_TOKEN`.
3. Workflow **Qual conta? (whoami)**: confirma para qual conta o secret aponta
   (`main.py whoami` usa o refresh token quando ele existe no ambiente).
4. Workflow **Playlist diária** (Run workflow). Playlist inexistente na conta
   nova é criada pelo nome; `_learn_from_removals` não aprende nada na 1ª vez
   porque não acha a playlist.
5. Conta sem curtidas/histórico: ver "Conta sem acervo" na seção 5b.

Erros típicos pós-troca, já com mensagem clara em `main.py`: `SpotifyOauthError`
(refresh token de outra conta/app → `invalid_grant`) e 403 "user not
registered" (conta fora da allowlist do app em Development Mode).

## 8. Estado atual

Playlists já configuradas em `config/playlists.yaml`: sazonais (Verão/Outono/
Inverno/Primavera), Treino, Samba & Pagode (discovery), Date (cantar no carro),
Karaokê BR (barítono, hits), Bom Dia (diária 5h, exclui funk/rap), Hiperfoco
Noturno (intenso/eufórico), Aconchego Animado (top tracks do usuário) e
No Mesmo Clima — Descobertas (mesmo gosto, músicas novas).

Branch de desenvolvimento: `claude/spotify-playlist-integration-hz4sdw`
(também é a branch padrão do repositório).

Em 21/10/2026: a conta do GitHub estava **bloqueada por cobrança** (seção 9),
nada rodava desde a noite de 19/10, e o usuário **trocou de conta Spotify** —
o secret `SPOTIFY_REFRESH_TOKEN` ainda apontava para a conta antiga (seção 7b).

## 9. GitHub Actions: todos os jobs "falham" em ~3 s, sem passos e sem log

Visto em 19–21/10/2026: cron e disparo manual falhando em 2-3 s, job sem
nenhum step, sem runner, download do log dá 404. Não é o código nem a
Spotify. A causa fica na **anotação** do job (API pública; o repositório é
público):

```bash
curl -s https://api.github.com/repos/Northmannn/Contador-Claude/actions/runs/<RUN_ID>/jobs   # pega o id do job
curl -s https://api.github.com/repos/Northmannn/Contador-Claude/check-runs/<JOB_ID>/annotations
```

Mensagem vista: *"The job was not started because your account is locked due
to a billing issue"* → conta do GitHub bloqueada por cobrança; resolve em
https://github.com/settings/billing e nada roda até lá (repo público não gasta
minutos, então não é cota). Fora isso, o cron do GitHub descarta execuções à
vontade (várias horas seguidas sem rodar): o `--slot-guard` já foi desenhado
pra isso — a 1ª execução que cair dentro da janela gera a lista.
