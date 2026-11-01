# Reorganização das áreas + agentes que orquestram e executam

Plano da próxima grande etapa do Growth OS. Nada de interface muda neste PR: aqui fica só o desenho,
para o owner aprovar antes do código.

**Pedido do owner (2026-11-01):** juntar as cinco áreas em três e deixar tudo interligado. Os agentes se
chamam para orquestrar o trabalho, usam as integrações configuradas e executam de verdade, dentro e
fora do OS.

---

## 1. O que muda na navegação

| Hoje | Depois | Observação |
|---|---|---|
| Plano do dia (`/plan`) | **Aprovações** (`/approvals`) | Tudo que espera uma decisão do owner, num lugar só. |
| Kanban (`/board`) | **Kanban** (`/board`) | Toda atividade dos agentes por etapa. Conteúdo vira filtro. |
| Conteúdo (`/content`) | filtro "Conteúdo" no Kanban e em Aprovações | A agenda de conteúdo vira uma visão (lista por data) dentro do Kanban. |
| Agentes (`/agents`) | aba **Agentes** em Automações | Especialidades, missão, modelo, liga/desliga. |
| Automações (`/automations`) | **Automações** | Abas: Agentes, Rotinas, Habilidades, Integrações. |

O Dashboard e as Threads ficam como estão. O seletor de projeto continua visível em todas as telas,
porque agentes, KPIs, tarefas e integrações são sempre de um projeto.

As rotas antigas viram redirecionamentos (`/plan` → `/approvals`, `/content` → `/board?tipo=conteudo`,
`/agents` → `/automations?aba=agentes`), para não quebrar links salvos nem os avisos do Telegram.

### Aprovações

Uma fila única, com filtros por **tipo** e por projeto:

- **Plano do dia**: as tarefas que os agentes propõem de manhã (o que hoje está em `/plan`).
- **Missões do chat**: trabalho que a IA montou a partir de um pedido feito numa thread (ver §3).
- **Pedidos entre agentes** que precisam de aval (ver regras em §5).
- **Conteúdo**: posts, e-mails e mensagens esperando aprovação (o que hoje é "pendente" em `/content`).
- **Ações externas**: chamadas a integrações que têm efeito fora do OS (publicar, enviar, cobrar...).

Cada cartão mostra quem pediu (agente, rotina, chat), a cadeia ("Growth pediu a Social, que quer
publicar no Instagram"), o risco, uma prévia exata do que vai acontecer e os botões aprovar, editar,
rejeitar. Itens de risco alto pedem o código 2FA no próprio cartão. Dá para aprovar em lote, como hoje.

### Kanban

Colunas por etapa, para **tudo** que os agentes fazem (tarefas, conteúdo e ações externas):

`Proposta` → `Na fila` → `Executando` → `Aguardando` → `Concluída` · `Falhou / Rejeitada`

- `Aguardando` tem o motivo no cartão: 2FA, aprovação de ação, resposta de outro agente ou horário
  agendado.
- Todo cartão que precisa de decisão tem o link "Decidir em Aprovações" (`/approvals#a<id>`). Arrastar
  uma proposta para "Na fila" continua aprovando, como hoje.
- Filtros: tipo (tarefa, conteúdo, ação externa), agente, origem (plano, chat, pedido, rotina, owner) e
  missão. Com o filtro "Conteúdo", aparece também a visão de agenda por data.

### Automações

- **Agentes**: o time do projeto, cada um com especialidade, missão, modelo, as habilidades e as
  integrações que pode usar.
- **Rotinas**: atividades com horário (como hoje) e, novo, rotinas disparadas por **evento**
  (ex.: "site caiu" → Tech investiga). Ver §3.4.
- **Habilidades**: os playbooks, como hoje.
- **Integrações**: o catálogo, agora com a lista de **operações** de cada uma (o que os agentes podem
  ler e fazer), o tipo de cada operação (leitura, interna, externa) e um botão "testar conexão".

---

## 2. Modelo de dados

Tudo continua por projeto (`project_id` em todas as tabelas novas). Migração só com `ADD COLUMN` e
tabelas novas, nunca `DROP`, no mesmo esquema de `_NEW_COLUMNS` de `app/db.py`.

### 2.1 Tabelas novas

**`missions`**: um objetivo que envolve vários agentes.

| coluna | tipo | |
|---|---|---|
| id, project_id | int | |
| title, goal | texto | o que o owner pediu e o critério de pronto |
| origin | `chat` \| `plan` \| `owner` \| `event` | |
| conversation_id | int? | a thread de onde veio |
| lead_agent | slug | quem orquestra (o agente **Coordenador**, §3.1) |
| status | `draft` \| `awaiting_approval` \| `active` \| `done` \| `failed` \| `cancelled` | |
| budget_tasks, budget_steps | int | limites (padrão 12 tarefas, 80 passos de IA) |
| used_tasks, used_steps | int | |
| created_at, finished_at | datetime | |

**`approvals`**: a fila da tela Aprovações. Uma linha por decisão, apontando para o que está sendo
decidido. Assim a tela não precisa juntar quatro tabelas, e todo "sim" e "não" fica registrado.

| coluna | tipo | |
|---|---|---|
| id, project_id | int | |
| kind | `task` \| `mission` \| `content` \| `action` | |
| ref_id | int | id da tarefa, missão, peça ou ação |
| title, preview | texto | o que o cartão mostra (prévia exata do efeito) |
| requested_by | texto | `agente:social`, `rotina:12`, `chat:34`, `owner` |
| risk | `baixo` \| `medio` \| `alto` | `alto` exige 2FA |
| status | `pending` \| `approved` \| `rejected` \| `expired` | |
| decided_via | `clique` \| `2fa` \| `herdada` \| `rotina` | `herdada` = coberta por uma missão aprovada |
| expires_at | datetime? | ações com hora certa expiram em vez de rodar atrasadas |
| created_at, decided_at | datetime | |

**`actions`**: uma chamada a uma integração. É aqui que o OS executa "de fato".

| coluna | tipo | |
|---|---|---|
| id, project_id, task_id | int | |
| integration | kind | `postiz`, `smtp`, `meta_graph`, `umami`... |
| operation | texto | `publicar_post`, `enviar_email`, `ler_visitas`... |
| effect | `read` \| `internal` \| `external` | define a política (§5) |
| params | JSON | já validado pelo esquema da operação |
| status | `requested` \| `awaiting_approval` \| `approved` \| `running` \| `done` \| `failed` \| `rejected` | |
| idempotency_key | texto único | evita publicar duas vezes num retry |
| result | JSON | resposta resumida da API |
| external_ref | texto | id ou URL do que saiu (post, e-mail) |
| attempts, error | | |
| created_at, finished_at | datetime | |

**`events`**: a linha do tempo do projeto e o "barramento" que liga uma coisa à outra.

| coluna | tipo | |
|---|---|---|
| id, project_id | int | |
| type | texto | `task.done`, `action.failed`, `kpi.updated`, `site.down`, `content.sent`... |
| subject_kind, subject_id | | o que gerou o evento |
| payload | JSON | dados pequenos (sem segredos) |
| created_at | datetime | |

O Kanban lê a atividade recente daqui, o Dashboard mostra o feed, e as rotinas por evento (§3.4)
assinam tipos de evento.

### 2.2 Colunas novas em tabelas que já existem

- `agent_tasks`: `mission_id`, `origin` (`plan` \| `chat` \| `pedido` \| `rotina` \| `owner` \| `evento`),
  `wait_reason` (`2fa` \| `acao` \| `agente` \| `horario`), `resume_count`. O status ganha `blocked`
  (esperando outro agente) além dos atuais.
- `task_links`: `wait` (bool). Quando é `true`, a tarefa que pediu fica parada esperando a resposta e
  depois **continua** com o resultado (hoje ela só recebe um recado).
- `automations`: `trigger` (`schedule` \| `event`), `event_type`, `event_filter` (JSON).
- `content_items`: `action_id`. Publicar uma peça aprovada vira uma ação na integração do canal.
- `integrations`: `allowed_agents` (slugs; vazio = time todo) e `preapproved_ops` (operações externas
  que o owner liberou sem aprovação por item, ex.: avisar no Telegram do próprio owner).

### 2.3 O que acontece com o que já existe

- `daily_plans` continua (é o "lote" da manhã), mas as tarefas dele aparecem em Aprovações pelo tipo
  "Plano do dia".
- Na migração, cada tarefa `proposed` e cada peça `pending` ganha a sua linha em `approvals`.
- `content_items` continua sendo a fonte das peças. A tela Conteúdo some, a tabela não.

---

## 3. Fluxo de orquestração

### 3.1 O Coordenador

Novo papel no catálogo de agentes: **Coordenador** (`coordenador`), um por projeto. Ele não executa
trabalho de área: ele quebra um objetivo em tarefas, escolhe os agentes, acompanha e valida o
resultado. Recebe a lista do time com as especialidades, as habilidades e as integrações de cada um.

### 3.2 Do pedido no chat até a execução

```
Owner na thread: "lança a campanha de Natal da Zombie Party"
        │
        ▼
IA da thread percebe que é trabalho de time e emite um bloco ```missao```
        │
        ▼
Coordenador do projeto monta a missão: objetivo, critério de pronto e tarefas
  (Marketing: conceito · Design: peças · Social: posts · Growth: link com UTM · CRM: e-mail)
        │
        ▼
Aprovações: a missão aparece como UM cartão (aprovar tudo, aprovar item a item, editar)
        │ aprovada
        ▼
Tarefas entram na fila respeitando dependências ("Social depois de Design")
        │
        ├─ cada agente executa no laço normal (comandos, gos-pr, gos-int)
        ├─ pedidos novos entre agentes dentro do escopo da missão: aprovação herdada
        ├─ ações externas (publicar, enviar): vão para Aprovações, a tarefa espera
        ▼
Coordenador recebe cada resultado, valida contra o critério de pronto,
pede retrabalho se precisar e fecha a missão com um resumo na thread de origem
```

O resumo final volta para a thread onde o owner pediu, então o chat continua sendo o ponto de
entrada e de acompanhamento.

### 3.3 Agentes que se chamam

O bloco ```pedido``` que já existe (PR #9) continua sendo o jeito de um agente chamar outro, com duas
melhorias:

1. **Esperar a resposta**: com `"esperar": true` a tarefa que pediu fica `blocked`. Quando a tarefa
   pedida termina, a que pediu volta para a fila com o resultado no contexto e continua de onde parou.
   Hoje ela só recebe um recado depois que já terminou.
2. **Aprovação herdada**: um pedido de risco baixo, dentro de uma missão aprovada, que não gera ação
   externa, entra direto na fila (`decided_via = herdada`). Fora de missão, ou de risco médio/alto,
   continua indo para Aprovações.

Limites que já existem continuam (3 pedidos por tarefa, profundidade 3, nunca entre projetos). Entram
também os limites da missão (`budget_tasks`, `budget_steps`). Estourou: a missão para e pede ao owner
para liberar mais.

### 3.4 Rotinas por evento

Além do horário, uma rotina pode disparar com um evento do projeto:

- `site.down` (Uptime Kuma) → Tech olha logs e containers e propõe a correção.
- `kpi.updated` com queda maior que X% → Growth investiga.
- `content.sent` → Social mede o alcance 24h depois.
- `action.failed` → o Coordenador decide se tenta de novo ou avisa o owner.

As mesmas regras de hoje valem: por padrão a execução entra como proposta; o owner pode liberar
"aprovar automático" por rotina; nunca empilha duas execuções abertas da mesma rotina.

### 3.5 Fila e paralelismo

Hoje o `TaskRunner` roda uma tarefa por vez. Proposta: `GOS_RUNNER_WORKERS` (padrão 2), com no máximo
uma tarefa rodando por agente e projeto, para que uma missão ande sem travar o resto e sem estourar os
limites dos providers. Tarefas `blocked` e `waiting` não ocupam worker.

---

## 4. Caminho de execução das integrações

### 4.1 O problema hoje

As integrações (PR #14) só entram no prompt como texto. A chave fica no `.env` do VPS e o executor tira
qualquer variável `GOS_*`/`*TOKEN*`/`*KEY*` do ambiente dos comandos. Isso é bom para a segurança, mas
na prática os agentes não conseguem chamar nenhuma API. É isso que este plano resolve, sem nunca entregar
a chave para a IA.

### 4.2 Conectores dentro do app

Novo pacote `app/connectors/`, um arquivo por integração. Cada conector declara as suas operações:

```python
OPERATIONS = {
    "ler_visitas":     Op(effect="read",     params={"dias": int}),
    "criar_rascunho":  Op(effect="internal", params={"canal": str, "texto": str, "midia": str}),
    "publicar_post":   Op(effect="external", params={"canal": str, "texto": str, "quando": str}, risk="alto"),
}
```

Quem chama a API é o processo do app, que é o único que lê a chave no `.env`. A IA só vê o esquema das
operações e o resultado resumido. Toda chamada vira uma linha em `actions` e no log de auditoria.

### 4.3 Como o agente chama: `gos-int`

Um CLI novo, no mesmo estilo de `gos-pr` e `gos-docker`, que o agente roda no laço normal:

```bash
gos-int list                                   # integrações e operações disponíveis para mim
gos-int run umami ler_visitas '{"dias": 7}'    # leitura: responde na hora
gos-int run postiz publicar_post '{...}'       # externa: responde "aguardando aprovação #57"
```

- O `gos-int` fala com um endpoint interno do app (`/internal/actions`, só na rede local do container,
  fora do Caddy) usando um **ticket da tarefa**: gerado quando a tarefa começa, preso ao projeto, ao
  agente e à tarefa, e invalidado quando ela termina. Com ele não dá para ler chaves, nem agir em outro
  projeto, nem pular a aprovação.
- **Leitura** e **interna**: executa e devolve o resultado no mesmo passo do laço.
- **Externa**: cria a ação como `awaiting_approval`, cria o cartão em Aprovações e pausa a tarefa
  (`wait_reason = acao`), como já acontece com o 2FA. Quando o owner aprova, o app executa a ação e a
  tarefa continua com o resultado. Rejeitou: a tarefa continua sabendo que foi rejeitado e por quê.

### 4.4 Coleta de dados sem IA

Leituras que alimentam o Dashboard não precisam de IA: um job do agendador roda as operações de leitura
de cada integração pronta (ex.: Umami visitas, Uptime Kuma status, Stripe MRR) antes do horário do plano
e grava em `kpi_points` com `source = <integração>`. Os agentes já acordam com números reais, e isso
gasta zero token.

### 4.5 Integrações e operações da primeira leva

Seguindo a regra de grátis/self-hosted primeiro:

| Integração | Operações | Efeito |
|---|---|---|
| Umami / Plausible | `ler_visitas`, `ler_paginas`, `ler_origens` | leitura |
| Uptime Kuma | `ler_status` (+ evento `site.down`) | leitura |
| Search Console | `ler_desempenho` | leitura |
| API do projeto | `get` em rotas liberadas na config | leitura |
| Telegram (owner) | `avisar_owner` | externa, pré-aprovável |
| Postiz | `criar_rascunho` · `agendar_post` · `publicar_post` | interna · externa · externa |
| E-mail (SMTP próprio ou Listmonk self-hosted) | `criar_rascunho` · `enviar_email` | interna · externa |
| Instagram / Facebook (Graph) | `ler_insights` · `publicar` | leitura · externa |
| Stripe (chave restrita) | `ler_receita`, `ler_assinaturas` | só leitura (sem escrita de propósito) |
| GitHub | já existe via `gos-pr` (branch + PR, nunca merge) | interna |
| Docker | já existe via `gos-docker` (restart com 2FA) | externa |

Publicar uma peça aprovada no Kanban/Aprovações vira `publicar_post` ou `enviar_email` no conector do
canal. Sem conector configurado, continua o fluxo manual de hoje ("marcar enviada").

### 4.6 Garantias de execução

- **Idempotência**: `idempotency_key` por ação; retry nunca publica duas vezes.
- **Retentativas**: até 3, com espera crescente, só para erro de rede/limite (nunca para 4xx de
  validação).
- **Limites por conector**: taxa máxima por hora e por dia (ex.: no máximo 5 posts/dia por canal),
  configurável na aba Integrações.
- **Horário**: ação agendada que não foi aprovada até a hora expira em vez de sair atrasada.
- **Prévia exata**: o cartão de aprovação mostra os parâmetros finais que vão para a API; editar o
  cartão edita os parâmetros.

---

## 5. O que precisa da aprovação do owner

| Nível | O que entra | Como decide |
|---|---|---|
| **Roda direto** | leituras de integrações, coleta de KPIs, logs, comandos `auto` do executor, recados, rascunhos internos (Postiz/e-mail), PR via `gos-pr`, pedidos de risco baixo dentro de missão aprovada | ninguém; fica no log e no Kanban |
| **Aprovação com clique** | tarefas do plano do dia, missões vindas do chat, pedidos entre agentes fora de missão ou de risco médio, execuções de rotina (salvo "aprovar automático"), toda peça de conteúdo criada por agente | cartão em Aprovações (ou arrastar no Kanban) |
| **Aprovação + 2FA** | toda **ação externa** de risco alto: publicar post, enviar e-mail para fora, mensagem para cliente, restart de container, qualquer coisa que gaste dinheiro | cartão em Aprovações com código 2FA |
| **Só o owner, fora do OS** | merge de PR (que dispara o deploy), mudança no Caddy/DNS, mudança de chaves no `.env`, dar permissão nova ao token do GitHub | o OS só prepara e avisa |
| **Bloqueado** | IA ler segredos, push na main, merge, agir em outro projeto, apagar dados, escrita no Stripe | não existe caminho |

Pré-aprovação: o owner pode marcar operações externas específicas como liberadas por integração
(`preapproved_ops`), por exemplo "avisar no meu Telegram". Publicar e enviar e-mail **não** entram nessa
lista sem ele decidir explicitamente.

**Defesa contra injeção de prompt**: o que vem de fora (posts de concorrentes, páginas, respostas de API)
entra no contexto marcado como dado, nunca como instrução. Como toda ação externa passa por aprovação,
um texto malicioso lido por um agente não consegue publicar nem enviar nada sozinho.

---

## 6. Ordem de entrega (um PR por fase)

> Andamento: fases 1 e 2 (PR #24) e 3 (PR #26) mergeadas; fase 4 (Telegram, Postiz, e-mail, publicar peça aprovada) no PR seguinte. Ficou para a fase 5: a tarefa pausar esperando a ação e continuar com o resultado.

1. **Base de dados e Aprovações (backend)**: tabelas `missions`, `approvals`, `actions`, `events`;
   colunas novas; migração dos itens pendentes; API `/api/approvals`. Testes de migração e da fila.
2. **Interface 5 → 3**: telas Aprovações, Kanban com filtro de conteúdo e agenda, Automações com a aba
   Agentes; redirecionamentos das rotas antigas; menu lateral com três itens + Dashboard.
3. **Conectores + `gos-int`**: pacote `app/connectors/`, endpoint interno com ticket por tarefa, pausa
   e retomada da tarefa por ação; primeira leva só de leitura (Umami, Uptime Kuma, Search Console,
   API do projeto) + coleta de KPIs sem IA.
4. **Ações externas**: Telegram, Postiz e e-mail, com prévia, idempotência, limites e 2FA; publicar
   peça aprovada pelo conector do canal.
5. **Orquestração**: agente Coordenador, missões a partir do chat, pedidos com espera e retomada,
   aprovação herdada, orçamento da missão, `GOS_RUNNER_WORKERS`.
6. **Rotinas por evento**: barramento de eventos, gatilhos `site.down`, `kpi.updated`, `content.sent`,
   `action.failed`.

As fases 1 e 2 entregam a reorganização que o owner pediu; 3 e 4 fazem os agentes executarem de verdade;
5 e 6 fecham o "tudo interligado".

---

## 7. Decisões em aberto para o owner

1. **Missões pedidas no chat**: o owner já pediu, então as tarefas **internas** podem começar sem
   aprovação (só as externas param)? Recomendação: sim, com o cartão em Aprovações mostrando a missão
   para acompanhar e cancelar.
2. **Primeiras ações externas**: Postiz + e-mail (SMTP) + Telegram? Recomendação: sim, Postiz e
   Listmonk rodam no VPS sem custo.
3. **2FA em toda publicação ou só clique?** Recomendação: 2FA para enviar e-mail a terceiros e para
   qualquer coisa que gaste dinheiro; clique simples para post nas redes do próprio projeto.
4. **Paralelismo**: começar com 2 workers? Recomendação: sim, dá para subir depois olhando CPU e os
   limites dos providers.
