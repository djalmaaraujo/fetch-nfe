# fetch-nfe

Serviço em Docker, **multi-empresa**, que baixa as NF-e (modelo 55) ligadas aos CNPJs
cadastrados direto da SEFAZ, via webservice **NFeDistribuicaoDFe**, usando certificado
**A1** por empresa. Guarda os XMLs no host organizados por CNPJ e data, indexa tudo num
SQLite (dedup por empresa) e faz a **manifestação do destinatário** (ciência da operação,
evento 210210) por uma **fila assíncrona com retries**.

Sem API paga: fala direto com o serviço gratuito e oficial da SEFAZ.
Biblioteca: [PyNFe](https://github.com/TadaSoftware/PyNFe). Dados públicos da empresa
(razão social, UF, município) vêm da [BrasilAPI](https://brasilapi.com.br) no cadastro,
com fallback automático pra [minha receita](https://minhareceita.org) quando ela
estiver fora ou rate-limitada.

Fora do escopo (por ora): auth e UI — **não exponha a API publicamente sem proteger**.

## Arquitetura

Dois containers sobre o mesmo banco/volume:

- **api** — cadastro de empresas/certificados, disparo de sincronização, consulta e
  download por data.
- **worker** — scheduler (sincroniza todas as empresas ativas em paralelo, uma thread
  por empresa) + consumidor da fila de manifestação.

Escalabilidade: o rate-limit da SEFAZ é por CNPJ/certificado, então empresas andam em
paralelo (`SYNC_WORKERS`/`FILA_WORKERS`); dentro de cada empresa o consumo é serial,
com **lock por empresa** entre processos (API e worker nunca consultam o mesmo CNPJ ao
mesmo tempo) e **NSU guardado por empresa**.

### Como funciona (importante)

A distribuição da SEFAZ é **incremental por NSU**, não por data: não existe "me dá as
notas de 01 a 15/08" — puxa-se tudo que chegou desde o último NSU. Então:

- **Sincronizar** = drenar da SEFAZ o que há de novo para cada empresa.
- **Consultar por data** = filtrar pela `dhEmi` no índice local. Datas agem sobre o
  índice, não sobre a SEFAZ.

Notas que chegam só como **resumo** entram na **fila de manifestação**; o worker envia
a ciência (210210) e o XML completo aparece num NSU posterior, capturado na
sincronização seguinte. A fila tem dedup em três camadas (não re-manifesta o que já
tem XML local nem o que já foi manifestado), retries com backoff exponencial e
recuperação de itens órfãos na subida.

## Estrutura

```
fetch-nfe/
├── certs/                 # certificados A1 (.pfx) por CNPJ — não versionado
├── data/fiscal/<cnpj>/    # saída (gerado)
│   ├── 2026-08-18/         #   NF-e completas por data de emissão
│   ├── _resumos/ _eventos/
├── state/                 # notas.db (empresas/notas/fila/NSU) + locks — não apagar
├── app/                   # config, store, empresas, nfe (núcleo), workers, api
├── Dockerfile
├── docker-compose.yml
└── .env                   # copiado de .env.example — não versionado
```

## Subir

```bash
cp .env.example .env       # ajuste se quiser; empresas são cadastradas via API
docker compose up -d --build
docker compose logs -f
```

## API (padrão em http://localhost:8742)

Documentação gerada automaticamente no padrão **OpenAPI 3.1** (nativo do FastAPI):

- **`/docs`** — Swagger UI interativo (dá pra testar os endpoints direto)
- **`/redoc`** — ReDoc (leitura)
- **`/openapi.json`** — o spec cru, pra importar em Postman/Insomnia ou gerar clients
  (ex.: `openapi-generator`, `orval`)

### Empresas / certificados

| Método | Rota | O que faz |
|---|---|---|
| POST   | `/empresas` | Cadastra/atualiza: multipart com `certificado` (.pfx) e `senha`. Valida o A1, extrai o CNPJ e a validade do próprio certificado, enriquece via BrasilAPI → minhareceita (fallback). Campos opcionais: `cnpj`, `uf` (se as duas fontes falharem), `manifestar` |
| GET    | `/empresas` | Lista (nunca retorna a senha) |
| GET    | `/empresas/{cnpj}` | Detalhe + contagens |
| PATCH  | `/empresas/{cnpj}` | Ajusta `manifestar`, `ativo`, `senha`, `uf` |
| DELETE | `/empresas/{cnpj}` | Desativa (soft delete — XMLs e certificado ficam) |

```bash
curl -X POST localhost:8000/empresas \
     -F "certificado=@meucert.pfx" -F "senha=SENHA_DO_A1"
```

### Rotina (scheduler)

| Método | Rota | O que faz |
|---|---|---|
| GET   | `/rotina` | Ativa/pausada, intervalo, última execução (com duração) e previsão da próxima |
| PATCH | `/rotina` | `{ativa?, intervalo_segundos?}` — aplica em runtime (~15s, sem restart). Pausada = o worker não fala com a SEFAZ sozinho (nem sync, nem manifestação); o sync manual continua funcionando. Mudar o intervalo recalcula a próxima execução |

```bash
curl -X PATCH localhost:8742/rotina -H 'Content-Type: application/json' \
     -d '{"ativa": false}'                      # pausa
curl -X PATCH localhost:8742/rotina -H 'Content-Type: application/json' \
     -d '{"ativa": true, "intervalo_segundos": 3600}'
```

### Sincronização e fila

| Método | Rota | O que faz |
|---|---|---|
| POST | `/sincronizar` | Dreno de todas as empresas ativas (em paralelo, background) |
| POST | `/empresas/{cnpj}/sincronizar` | Dreno de uma empresa |
| GET  | `/status` | Empresas, NSU, contagens, fila, último resultado |
| GET  | `/fila` | Situação da fila de manifestação por empresa |
| GET  | `/health` | Saúde |

### Notas

| Método | Rota | O que faz |
|---|---|---|
| GET  | `/notas` | **Busca com filtros combináveis** (ver abaixo) |
| GET  | `/notas/{chave}` | XML de uma nota |
| GET  | `/notas/{chave}/json` | A nota em **JSON estruturado** (ide, emit, dest, det[], total, cobr — sem assinatura) — ideal pra agentes |
| POST | `/reindexar` | Reprocessa os campos de busca a partir dos XMLs no disco |
| GET  | `/download?cnpj=&de=&ate=` | `.zip` dos XMLs do período (pastas por CNPJ) |

#### Filtros do `/notas`

Os campos são extraídos do XML no momento do download (colunas indexadas +
FTS5 — a busca nunca faz parse de XML). Todos opcionais e combináveis (AND):

- **Texto livre**: `q` — full-text (FTS5, ignora acentos) sobre emitente,
  destinatário, natOp, produtos e infCpl. Ex.: `q=painel mdf dexco`
- **Nota**: `nnf`, `serie`, `natop` (trecho), `tp_nf` (0=entrada, 1=saída),
  `valor_min`/`valor_max` (vNF), `de`/`ate` (dhEmi), `tipo`, `manifestada`
- **Partes**: `emitente` e `destinatario` (CNPJ exato ou trecho do nome), `uf`
- **Itens** (casa se qualquer item casar): `produto` (xProd), `ncm` (aceita
  prefixo, ex. `4411`), `cfop`, `cean`
- **Financeiro**: `venc_de`/`venc_ate` (vencimento de duplicatas)
- **Ordenação**: `ordenar` (`data`|`valor`), `ordem` (`asc`|`desc`)

### Documentos (DANFE/DACTE/DAMDFE)

| Método | Rota | O que faz |
|---|---|---|
| POST | `/danfe` | PDF do DANFE — NF-e, modelo 55 |
| POST | `/dacte` | PDF do DACTE — CT-e |
| POST | `/damdfe` | PDF do DAMDFE — MDF-e |

Cada rota aceita o XML autorizado (`nfeProc`/`cteProc`/`mdfeProc`) de duas
formas — upload multipart (`arquivo`) ou base64 (`xml_base64`, form field):

```bash
curl -F "arquivo=@nota.xml" localhost:8742/danfe -o danfe.pdf

curl --data-urlencode "xml_base64=$(base64 -w0 nota.xml)" localhost:8742/danfe -o danfe.pdf
```

DANFE NFC-e (modelo 65, cupom de venda) **não é suportado** — a lib usada
(`brazilfiscalreport`) não tem essa renderização.

#### Paginação (padrão em toda listagem)

Todo endpoint que lista registros (`/notas`, `/empresas`) usa os mesmos
parâmetros — `limite` (default 50, máx 500) e `offset` — e a mesma resposta:
`{total, limite, offset, <itens>}`, onde `total` é a contagem sem paginação.

```bash
curl "localhost:8742/notas?q=painel+mdf&valor_min=10000&de=2026-08-01"
curl "localhost:8742/notas?emitente=dexco&cfop=6129&ordenar=valor&ordem=desc"
curl "localhost:8742/notas?venc_de=2026-08-20&venc_ate=2026-08-31"   # contas a pagar
```

Cada parâmetro tem descrição no spec OpenAPI — pronto pra virar tool de MCP.

## Configuração global (.env)

Comportamento (`AMBIENTE`, `DEDUP`, default de `MANIFESTAR`), cadências
(`INTERVALO_SEGUNDOS`, `FILA_INTERVALO_SEGUNDOS`), paralelismo (`SYNC_WORKERS`,
`FILA_WORKERS`, `FILA_MAX_TENTATIVAS`) e `API_PORT`. Veja `.env.example`.
As variáveis `CNPJ/UF/CERT_PATH/CERT_SENHA` são só um *seed* de migração do modo
single-empresa antigo — cadastro normal é via `POST /empresas`.

## Observações

- **Segredos:** a senha do certificado fica no SQLite local (`state/notas.db`,
  chmod 600, fora do git) — o serviço precisa dela em claro pra assinar os eventos.
  Sem auth no escopo, proteja o host e não exponha a porta publicamente.
- **Proteção contra bloqueio da SEFAZ** (duas camadas, por empresa):
  1. *Lock*: API e rotina nunca consultam o mesmo CNPJ em paralelo.
  2. *Cooldown automático*: depois de um dreno sem novidade (137/fim do backlog)
     ou de um `656 Consumo Indevido`, aquele CNPJ não é consultado por 1h — nem
     pela rotina, nem pelo sync manual. `?forcar=true` só fura cooldown de dreno
     sem novidade; **nunca** fura um 656 (insistir durante o bloqueio reinicia o
     temporizador na SEFAZ). O controle da SEFAZ é por CNPJ do interessado, então
     matriz e filiais têm contadores independentes mesmo com o mesmo certificado.
     `cooldown_sefaz_ate` aparece em `/status` e `/empresas/{cnpj}`.
- **Matriz e filiais:** a distribuição entrega as notas em que o CNPJ cadastrado é
  DESTINATÁRIO (emitidas pela própria empresa não vêm — o emissor já as tem). Pra
  cobrir as notas recebidas por uma filial, cadastre-a como outra empresa: a SEFAZ
  aceita o certificado da matriz para CNPJs da mesma raiz (`POST /empresas` com o
  mesmo `.pfx` e `cnpj` da filial). Na busca, `cnpj` filtra por estabelecimento;
  sem `cnpj`, cruza todos.
- **Proxy de saída:** `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` no `.env` funcionam
  nativamente (requests). Atenção: proxy NÃO evita bloqueio da SEFAZ — o
  rate-limit dela é por certificado/CNPJ (mTLS), não por IP.
- **Manifestação:** o serviço envia APENAS a ciência da operação (210210) —
  "tomei conhecimento de que a nota existe". Não confirma a operação (210200),
  não desconhece (210220), não recusa (210240); nenhum desses eventos existe no
  código. A ciência serve só pra SEFAZ liberar o XML completo. Controlável por
  empresa (`manifestar`).
- **Primeira sincronização** de uma empresa parte do NSU 0 e traz o histórico
  disponível (~90 dias). Depois, só incremental.
