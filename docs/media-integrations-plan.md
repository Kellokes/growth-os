# Plano de integrações de mídia

Pesquisa de 2026-11-02 para automatizar o processo de mídia de cada projeto (takeone, zombie-party, a-ilha):
coletar dados de mercado e de concorrentes, gerar peças (imagem, vídeo, motion), aprovar, guardar e publicar.

Este documento é só o plano. Nenhum código ou configuração foi alterado.

## Como ler as marcas

| Marca | Significa |
|---|---|
| ✅ | Conferido na documentação oficial do fornecedor em 2026-11-02 (link ao lado). |
| 🟡 | Vem de fonte não oficial (blog, parceiro). Confirme no painel do fornecedor antes de confiar. |
| ❓ | Suposição minha, sem fonte conferida nesta pesquisa. |

Preços e limites mudam. Os números abaixo valem para a data da pesquisa.

## O fluxo ponta a ponta

1. **Coleta** (semanal, rotina já existente "Olhar o que os concorrentes postaram"): o agente Social lê posts e números de
   concorrentes; o agente Marketing lê os anúncios ativos deles; o Growth lê tendências de busca.
2. **Pauta**: o Social transforma o que funciona nos concorrentes em pauta da semana e manda pedidos para o Design.
3. **Geração**: o Design gera imagem estática, vídeo ou motion e grava o arquivo.
4. **Aprovação**: a peça aparece em Aprovações com prévia. Nada sai do OS sem você aprovar (regra que já existe:
   operações `external` só rodam depois da aprovação, e o Telegram avisa).
5. **Publicação**: o OS sobe a mídia para o Postiz e agenda no canal escolhido.
6. **Leitura de resultado**: no dia seguinte o OS lê alcance, views e engajamento de cada post e devolve para os KPIs do projeto.

## Onde cada chave mora

Há dois lugares diferentes, e isso evita confusão:

- **Chaves de app das redes sociais** (Meta, TikTok, YouTube, LinkedIn, X...) vão no **env do Postiz**
  (`/opt/postiz`), com os nomes que o Postiz exige. O Growth OS nunca vê essas chaves.
- **Chaves que o Growth OS usa** vão no `.env` do growth-os no VPS, no formato que o código já gera
  (`app/routines.py`, `secret_env_name`): `GOS_INT_<PROJETO>_<KIND>_<SEGREDO>`. `<PROJETO>` é o slug do projeto em
  maiúsculas, com `-` virando `_` (ex.: `ZOMBIE_PARTY`). Depois de mudar o `.env`, reinicie o growth-os.

**Importante:** hoje o código só lê as chaves dos kinds que estão no catálogo (`umami`, `meta_graph`, `postiz`,
`telegram`, `email`, `stripe`, `project_api`...). Os kinds marcados como **novo** abaixo (`meta_ads_library`,
`youtube_data`, `apify`, `pexels`, `fal`, `higgsfield`, `elevenlabs`) ainda precisam entrar no catálogo e ganhar um
conector. Os nomes de variável aqui são os que o código vai gerar quando isso for feito, então já pode criar as chaves.
Algumas chaves (Apify, Pexels, fal) servem para todos os projetos; como o padrão é por projeto, por enquanto repita o
mesmo valor em cada projeto que for usar, ou decidimos depois criar chaves globais.

## Ordem de configuração (resumo)

Comece pelo que depende de aprovação de terceiros, porque isso corre em paralelo enquanto você configura o resto.

| # | Sistema | Para quê | Custo | Agente | Demora |
|---|---|---|---|---|---|
| 0 | Pré-requisitos (Postiz em HTTPS, páginas de termos e privacidade) | Exigido por Meta e TikTok | Grátis | — | 1 h |
| 1 | Verificação de identidade da Meta (Ad Library) | Liberar a API de anúncios | Grátis | Marketing | 1 a 3 dias úteis 🟡 |
| 2 | App do TikTok + pedido de auditoria | Postar público no TikTok | Grátis | Social | Dias ou semanas ❓ |
| 3 | Armazenamento de mídia | URL pública que as redes baixam | Grátis | Design, Social | 1 h |
| 4 | App da Meta (Instagram + Facebook) | Publicar, ler insights e ler concorrentes | Grátis | Social | 1 h |
| 5 | Telegram do owner | Avisar que tem plano para aprovar | Grátis | todos | 10 min |
| 6 | OpenRouter (imagem e vídeo) | Gerar peças | Por uso | Design | 10 min |
| 7 | Pexels | Fotos e vídeos de banco, grátis | Grátis | Design | 5 min |
| 8 | Google Cloud: YouTube | Publicar Shorts e ler concorrentes | Grátis (cota) | Social, Growth | 30 min |
| 9 | LinkedIn | Publicar no perfil e na página | Grátis | Social | 30 min + formulário |
| 10 | Apify | Coletar TikTok, Instagram e anúncios onde não há API | US$ 5/mês grátis | Social, Marketing | 15 min |
| 11 | Remotion (no VPS) | Motion graphics com template | Grátis até 3 pessoas | Design | meio dia |
| 12 | Opcionais: fal.ai, Higgsfield, ElevenLabs, X, Threads, Pinterest, Google Trends | Mais modelos, voz e canais | Varia | Design, Social, Growth | — |

---

## 0. Pré-requisitos

1. **Postiz no ar em HTTPS** em `https://postiz.takeonestudio.io`, conforme `/mnt/project-files/postiz/configurar-postiz.md`
   (mudança no Caddy segue o procedimento seguro: backup, Caddyfile.novo com todos os domínios, validate, `cat`, reload,
   curl em todos os sites). ✅ O TikTok recusa redirect em `http://` ([Postiz/TikTok](https://docs.postiz.com/providers/tiktok)).
2. **Página de Termos de Uso e de Política de Privacidade** públicas em HTTPS (pode ser no takeonestudio.io). O TikTok
   exige as duas e um arquivo de verificação de domínio ✅ ([Postiz/TikTok](https://docs.postiz.com/providers/tiktok)).
   A Meta pede a URL de privacidade no app ❓.
3. Contas **profissionais** (Business ou Creator) no Instagram de cada projeto, ligadas a uma Página do Facebook ✅
   ([Meta, publicação](https://developers.facebook.com/docs/instagram-platform/content-publishing)).

## 1. Verificação de identidade da Meta (começar hoje)

- **Para que serve:** a [API da Biblioteca de Anúncios](https://developers.facebook.com/docs/graph-api/reference/ads_archive/)
  mostra os anúncios dos concorrentes.
- **O que ela entrega no Brasil:** ✅ fora da UE, a API só devolve anúncios de **temas sociais, eleições ou política**.
  Anúncios comerciais comuns de concorrentes brasileiros **não vêm pela API**. Eles aparecem no site da Biblioteca de
  Anúncios, então para concorrentes comerciais a coleta vai pelo Apify (item 10). Ainda vale fazer a verificação porque
  é grátis e serve para concorrentes que anunciam na UE.
- **Passo a passo** 🟡 ([Supermetrics](https://docs.supermetrics.com/docs/meta-ad-library-connection-guide); a página
  oficial bloqueia leitura automática):
  1. Com a sua conta pessoal do Facebook, abra facebook.com/ID e confirme a identidade (documento com foto, país, SMS).
  2. Espere a aprovação (1 a 3 dias úteis).
  3. Depois, no app da Meta (item 4), gere um token com a permissão `ads_read`.
- **Variável:** `GOS_INT_<PROJETO>_META_ADS_LIBRARY_ACCESS_TOKEN` (kind **novo** `meta_ads_library`).
- **Limites:** ✅ a doc não dá número, só o erro 613 quando passa do limite.
- **Agente:** Marketing.

## 2. App do TikTok e auditoria (começar hoje)

- **Para que serve:** publicar vídeos e fotos no TikTok pelo Postiz.
- **Passo a passo** ✅ ([Postiz/TikTok](https://docs.postiz.com/providers/tiktok),
  [TikTok Content Posting](https://developers.tiktok.com/doc/content-posting-api-get-started)):
  1. Crie uma conta em developers.tiktok.com e um app.
  2. Adicione os produtos **Login Kit** e **Content Posting API**, e ligue o **Direct Post**.
  3. Peça os escopos `user.info.basic`, `user.info.profile`, `video.create`, `video.publish`, `video.upload`.
  4. Redirect URI: `https://postiz.takeonestudio.io/integrations/social/tiktok`.
  5. Cadastre como site verificado o domínio de onde a mídia vai sair (item 3).
  6. Teste e depois envie o app para auditoria.
- **Variáveis (env do Postiz):** `TIKTOK_CLIENT_ID`, `TIKTOK_CLIENT_SECRET`.
- **Limites até a auditoria:** ✅ todo post sai **privado** (SELF_ONLY); no máximo 5 usuários postando em 24 h, e a conta
  precisa estar privada. Depois da auditoria, todas as privacidades ficam liberadas.
- **Saída enquanto não audita:** ✅ o modo "Upload without posting" manda o vídeo para a caixa de entrada do TikTok, e
  você publica pelo celular em até 24 h. ❓ Não confirmei se esse modo também fica preso ao privado antes da auditoria.
- **Agente:** Social.

## 3. Armazenamento dos arquivos gerados

- **Por que precisa:** ✅ Instagram e TikTok **baixam** a mídia de uma URL pública em HTTPS no momento da publicação
  ([Meta](https://developers.facebook.com/docs/instagram-platform/content-publishing),
  [Postiz/TikTok](https://docs.postiz.com/providers/tiktok)). Instagram só aceita imagem em **JPEG** ✅.
- **Recomendação (grátis, sem chave nova):** o Growth OS grava cada peça num volume próprio (`/data/media/<projeto>/`),
  mostra a prévia em Aprovações só para você (atrás do login) e, depois de aprovada, sobe o arquivo para o Postiz por
  `POST /public/v1/upload` ✅ ([Postiz API](https://docs.postiz.com/public-api)). Quem expõe a URL pública é o Postiz.
- **Duas opções para o Postiz guardar a mídia** ✅ ([Postiz, variáveis](https://docs.postiz.com/configuration/reference)):

  | Opção | Variáveis no env do Postiz | Custo | Quando usar |
  |---|---|---|---|
  | A. Local + Caddy | `STORAGE_PROVIDER=local`, `UPLOAD_DIRECTORY`, `NEXT_PUBLIC_UPLOAD_STATIC_DIRECTORY` | Grátis, usa o disco do VPS | Começo. A pasta de uploads precisa ser servida em HTTPS pelo Caddy. |
  | B. Cloudflare R2 | `STORAGE_PROVIDER=cloudflare`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ACCESS_KEY`, `CLOUDFLARE_SECRET_ACCESS_KEY`, `CLOUDFLARE_BUCKETNAME`, `CLOUDFLARE_BUCKET_URL`, `CLOUDFLARE_REGION=auto` | ✅ 10 GB/mês, 1 milhão de escritas, 10 milhões de leituras e saída grátis ([R2](https://developers.cloudflare.com/r2/pricing/)) | Quando vídeo começar a encher o disco. Você já usa Cloudflare no zombie-party. |

- **R2, passo a passo** ❓ (fluxo do painel, não conferido passo a passo): Cloudflare > R2 > Create bucket
  (`growth-os-media`) > Settings > ligar domínio público (ex.: `media-os.takeonestudio.io`) > R2 > Manage API Tokens >
  token com "Object Read & Write" só nesse bucket.
- ❓ O `media.takeonestudio.io` do takeone parece um storage S3 próprio, mas o Postiz só fala com local ou R2, então não
  serve direto aqui.
- **Agentes:** Design grava, Social usa na publicação.

## 4. App da Meta (Instagram + Facebook): um app para tudo

Um único app da Meta, conectado pelo **Login do Facebook**, cobre publicação, insights dos seus posts e leitura de
concorrentes. A leitura de concorrentes (Business Discovery) só existe na versão com Login do Facebook ✅, por isso
prefira essa opção à "Instagram Standalone".

- **Passo a passo** ✅ ([Postiz/Instagram](https://docs.postiz.com/providers/instagram)):
  1. developers.facebook.com > Create App > caso de uso **"Other"**.
  2. Adicione **Facebook Login for Business**.
  3. Redirect URIs: `https://postiz.takeonestudio.io/integrations/social/instagram` e
     `https://postiz.takeonestudio.io/integrations/social/facebook`.
  4. Permissões: `instagram_basic`, `pages_show_list`, `pages_read_engagement`, `business_management`,
     `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights`.
  5. Coloque o app em env do Postiz e reinicie. No Postiz: Add channel > Instagram (e Facebook).
- **Precisa de App Review?** ✅ Sem App Review, só contas com papel no app (admin, desenvolvedor, testador) conseguem
  publicar. Como as contas são suas, basta você ser admin do app e adicionar cada Instagram como **Instagram Tester**
  (App Roles, e aceitar o convite nas configurações do Instagram). App Review só se um dia outra pessoa for usar.
- **Variáveis (env do Postiz):** `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET`.
- **Variável do Growth OS (leitura de insights e concorrentes):** `GOS_INT_<PROJETO>_META_GRAPH_ACCESS_TOKEN`
  (kind `meta_graph`, já existe no catálogo, falta o conector de leitura). Permissões do token: `instagram_basic`,
  `instagram_manage_insights`, `pages_read_engagement` ✅
  ([Business Discovery](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery),
  [Insights](https://developers.facebook.com/docs/instagram-platform/insights)).
  ❓ Para o token não vencer a cada 60 dias, gere por um **System User** do Business Manager (Configurações do negócio >
  Usuários do sistema > Gerar token, escolhendo o app e as permissões acima).
- **O que dá para ler dos concorrentes** ✅: seguidores, número de posts e, por post, curtidas, comentários e views.
  Só de contas Business ou Creator, e não de contas com restrição de idade.
- **Limites** ✅: 100 posts publicados por API em 24 h por conta (a mesma página cita 50 na parte de carrossel; confira em
  `/<IG_ID>/content_publishing_limit`). Carrossel até 10 itens e conta como 1 post.
- **Lacuna no Growth OS:** o conector do Postiz manda só texto e não manda `post_type`, então o Instagram vai falhar até
  isso ser corrigido (ver "O que falta no código").
- **Agente:** Social.

## 5. Telegram do owner (avisos de aprovação)

- **Para que serve:** avisar no celular quando um plano ou peça espera aprovação. Já está no catálogo (kind `telegram`).
- **Passo a passo** ❓ (fluxo padrão do Telegram): fale com @BotFather > `/newbot` > copie o token. Mande qualquer
  mensagem ao seu bot e abra `https://api.telegram.org/bot<token>/getUpdates` para ver o seu `chat_id`.
- **Variável:** `GOS_INT_<PROJETO>_TELEGRAM_BOT_TOKEN`; o `chat_id` vai na tela de Integrações.
- **Custo:** grátis. **Agentes:** todos (Coordenador principalmente).

## 6. OpenRouter para imagem e vídeo (reaproveita a chave que já existe)

- **Para que serve:** gerar imagem estática e vídeo curto sem abrir conta em cada fornecedor.
- **Imagem** ✅ ([OpenRouter, imagem](https://openrouter.ai/docs/features/multimodal/image-generation)):
  `POST /api/v1/images` com `model`, `prompt`, `aspect_ratio`, `n` e `input_references` (imagem de referência). Volta em
  base64. Modelos citados: `openai/gpt-image-2`, `google/gemini-2.5-flash-image`, `black-forest-labs/flux.2-pro`,
  `bytedance-seed/seedream-4.5`. Lista atual em `GET /api/v1/images/models`.
- **Vídeo** ✅ ([OpenRouter, vídeo](https://openrouter.ai/docs/features/multimodal/video-generation)): `POST /api/v1/videos`
  devolve um job; consulta em `GET /api/v1/videos/{id}` ou recebe webhook (`callback_url`). Exemplos: `google/veo-3.1`,
  `minimax/hailuo-3`, `alibaba/wan-2.7`. Preço de exemplo: Veo 3.1 a US$ 0,50 por segundo (US$ 0,75 em 1080p), ou seja,
  ~US$ 4 por um vídeo de 8 s.
- **Variável:** a chave atual `GOS_OPENROUTER_API_KEY` já serve. ❓ Recomendo criar uma **segunda chave com limite de
  crédito** no painel do OpenRouter só para mídia, para um agente não gastar o saldo do chat. Se fizer isso, o nome
  seria `GOS_INT_<PROJETO>_OPENROUTER_API_KEY` (kind **novo** `openrouter_media`).
- **Agente:** Design.

## 7. Pexels (banco de imagens e vídeos grátis)

- **Para que serve:** fundo, b-roll e fotos reais para misturar com o que a IA gera.
- **Passo a passo** ✅ ([Pexels API](https://www.pexels.com/api/documentation/)): crie conta no pexels.com > pexels.com/api >
  "Your API Key". A chave sai na hora.
- **Variável:** `GOS_INT_<PROJETO>_PEXELS_API_KEY` (kind **novo** `pexels`).
- **Custo e limites** ✅: grátis; 200 pedidos por hora e 20 mil por mês. Pede crédito visível ao Pexels e ao fotógrafo.
- **Agente:** Design.

## 8. Google Cloud: YouTube (publicar e ler concorrentes)

Um projeto no Google Cloud serve para as duas coisas.

- **Publicar Shorts pelo Postiz** ✅ ([Postiz/YouTube](https://docs.postiz.com/providers/youtube)):
  1. console.cloud.google.com > novo projeto `growth-os`.
  2. Ative **YouTube Data API v3**, **YouTube Analytics API** e **YouTube Reporting API**.
  3. Tela de consentimento OAuth: tipo **External**, adicione você como usuário de teste (não precisa publicar o app).
  4. Credenciais > OAuth client ID > **Web application** > redirect
     `https://postiz.takeonestudio.io/integrations/social/youtube`.
  5. Se o canal for de uma Brand Account em Google Workspace, marque o app como confiável no admin.google.com e espere
     ~5 h antes de conectar.
  - **Variáveis (env do Postiz):** `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`.
- **Ler vídeos de concorrentes (Growth OS):** no mesmo projeto, Credenciais > **API key**, restrita à YouTube Data API v3.
  - **Variável:** `GOS_INT_<PROJETO>_YOUTUBE_DATA_API_KEY` (kind **novo** `youtube_data`).
- **Cota** ✅ ([cota](https://developers.google.com/youtube/v3/determine_quota_cost), página atualizada em 2026-10-15):
  100 buscas (`search.list`) por dia, 100 uploads (`videos.insert`) por dia e 10 mil unidades para o resto
  (`videos.list` e `channels.list` custam 1). Isso mudou em relação ao modelo antigo (busca custava 100). ✅ Vídeos
  enviados por projetos não verificados **não** ficam mais presos ao privado
  ([videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert)).
- **Custo:** grátis. **Agentes:** Social (publicar, concorrentes), Growth (tendências de tema).

## 9. LinkedIn (perfil e página da empresa)

- **Passo a passo** ✅ ([Postiz/LinkedIn](https://docs.postiz.com/providers/linkedin)):
  1. linkedin.com/developers > Create app, ligado à página da empresa.
  2. Products: **Share on LinkedIn**, **Sign In with LinkedIn using OpenID Connect** e **Advertising API** (preencha o
     formulário; sem ele o token não renova).
  3. Confira que aparecem os escopos `openid`, `profile`, `w_member_social`, `r_basicprofile`, `rw_organization_admin`,
     `w_organization_social`, `r_organization_social`. Faltando um, o Postiz dá `NotEnoughScopes`.
  4. Redirects: `.../integrations/social/linkedin` e `.../integrations/social/linkedin-page`.
- **Variáveis (env do Postiz):** `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`.
- **Custo:** grátis. **Agente:** Social.

## 10. Apify (coleta onde não há API oficial)

- **Para que serve:** TikTok não tem API de dados para empresas no Brasil ✅ (a Research API é só para pesquisa acadêmica
  ou sem fins lucrativos; no Brasil, só para estudos de segurança de jovens,
  [TikTok Research API](https://developers.tiktok.com/products/research-api/)). E a API da Meta não mostra anúncios
  comerciais no Brasil (item 1). O Apify roda "actors" prontos que coletam perfis e posts públicos do TikTok e do
  Instagram e os anúncios da Biblioteca de Anúncios.
- **Passo a passo** ✅ ([Apify API](https://docs.apify.com/platform/integrations/api)): crie conta em apify.com > Console >
  Settings > **API & Integrations** > crie um token só para o Growth OS. O token vai no header `Authorization`.
- **Variável:** `GOS_INT_<PROJETO>_APIFY_API_TOKEN` (kind **novo** `apify`).
- **Custo** ✅ ([preços](https://apify.com/pricing)): plano grátis com US$ 5 de uso por mês (para quando acaba, sem cobrar);
  Starter US$ 19/mês. ❓ Uma coleta semanal de ~10 concorrentes por projeto deve caber no grátis, mas cada actor cobra
  diferente; o conector deve ter teto de gasto por execução.
- **Risco** ❓: coletar dados públicos de redes pode violar os termos da rede. Use só leitura de perfis públicos, sem
  login, e com baixa frequência.
- **Agentes:** Social (posts dos concorrentes), Marketing (anúncios dos concorrentes).

## 11. Remotion no VPS (motion graphics)

- **Para que serve:** vídeos com template de marca (texto animado, números, carrossel virando vídeo, legenda). O Design
  escreve ou ajusta um template em React e o VPS renderiza o MP4. Junta bem com as imagens e vídeos gerados nos itens 6 e 7.
- **Licença** ✅ ([Remotion FAQ](https://www.remotion.dev/docs/license/faq)): grátis para pessoa física e para empresa de
  até **3 pessoas no total**. Acima disso: US$ 25/mês por pessoa que escreve código Remotion, ou US$ 0,01 por render com
  mínimo de US$ 100/mês para automações. ❓ Confirme o tamanho da Take One antes.
- **Chave:** nenhuma. Roda num container no VPS (Node + Chromium + ffmpeg). ❓ Render de vídeo pesa na CPU; vale limitar
  a um render por vez.
- **Agente:** Design.

## 12. Opcionais (quando o básico estiver rodando)

| Sistema | Para quê | Chave e variável | Custo | Agente |
|---|---|---|---|---|
| fal.ai | Modelos que o OpenRouter não tem (Kling, Seedance, Flux schnell barato) | fal.ai > Dashboard > Keys. `GOS_INT_<PROJETO>_FAL_API_KEY` (**novo**) | ✅ por uso: Flux schnell US$ 0,003/MP; Kling 2.5 Turbo Pro US$ 0,07/s; Kling 3 Pro US$ 0,14/s ([preços](https://fal.ai/pricing)) | Design |
| Higgsfield | Vídeo com presets de marketing, UGC e anúncios | ✅ console.higgsfield.ai, credencial com key id e secret, header `Authorization: Key id:secret` ([auth](https://docs.higgsfield.ai/docs/authentication.md)). `GOS_INT_<PROJETO>_HIGGSFIELD_API_KEY` guardando `id:secret` (**novo**) | ❓ créditos; não conferi | Design |
| ElevenLabs | Narração e voz para Reels e Shorts | elevenlabs.io > API Keys. `GOS_INT_<PROJETO>_ELEVENLABS_API_KEY` (**novo**) | ✅ 10 mil caracteres grátis (~10 min); Starter US$ 6/mês. ❓ Uso comercial no plano grátis não está claro na página ([preços](https://elevenlabs.io/pricing/api)) | Design |
| X (Twitter) | Publicar no X | env do Postiz: `X_API_KEY`, `X_API_SECRET` ✅ ([Postiz](https://docs.postiz.com/configuration/reference)) | 🟡 pago por uso, sem plano grátis: ~US$ 0,015 por post e US$ 0,20 se tiver link ([OpenTweet](https://opentweet.io/x-api-pricing)) | Social |
| Threads | Publicar no Threads | env do Postiz: `THREADS_APP_ID`, `THREADS_APP_SECRET` ✅ | ❓ grátis (app da Meta) | Social |
| Pinterest | Publicar pins | env do Postiz: `PINTEREST_CLIENT_ID`, `PINTEREST_CLIENT_SECRET` ✅ | ❓ grátis | Social |
| Google Trends API | Tendência de busca por tema | ❓ API em alpha desde 2025, acesso por lista de espera ([anúncio](https://developers.google.com/search/blog/2025/07/trends-api)) | ❓ grátis | Growth |

---

## O que falta no código para "ligar" tudo

Configurar as chaves é a sua parte. Para o fluxo rodar sozinho, o Growth OS ainda precisa destas mudanças (cada uma é um
PR pequeno; posso fazer na ordem abaixo quando você quiser):

1. **Postiz com mídia:** subir o arquivo por `/upload`, mandar `image: [{id, path}]`, `post_type` do Instagram e
   `who_can_reply_post` do X, e esconder canais desativados. Sem isso Instagram e TikTok falham.
2. **Armazenamento de peças:** volume `/data/media`, prévia na tela de Aprovações e ligação peça → post.
3. **Conectores de geração:** `openrouter_media` (imagem e vídeo, com teto de gasto por dia e por projeto) e `pexels`.
4. **Conectores de coleta:** leitura do `meta_graph` (insights próprios e Business Discovery), `youtube_data`, `apify`
   (com teto de gasto) e `meta_ads_library`.
5. **Leitura de resultado dos posts** voltando para os KPIs do projeto.
6. **Remotion** como serviço no docker-compose, com templates por projeto.

Regras que continuam valendo: tudo por projeto; gerar e ler rodam na hora; publicar, agendar e gastar acima do teto
pedem a sua aprovação; nenhuma chave aparece na tela, no banco ou para a IA.

## Checklist rápido

- [ ] Postiz em HTTPS + páginas de termos e privacidade
- [ ] Verificação de identidade na Meta (facebook.com/ID)
- [ ] App TikTok criado e auditoria pedida
- [ ] Armazenamento do Postiz (local + Caddy ou R2)
- [ ] App da Meta + Instagram Testers + canais no Postiz
- [ ] Token de System User da Meta no `.env` do growth-os
- [ ] Bot do Telegram
- [ ] Chave do OpenRouter só para mídia (opcional)
- [ ] Chave do Pexels
- [ ] Projeto Google Cloud: OAuth para o Postiz + API key de dados
- [ ] App LinkedIn + formulário da Advertising API
- [ ] Token do Apify
- [ ] Reiniciar Postiz e growth-os depois de mexer nos envs
