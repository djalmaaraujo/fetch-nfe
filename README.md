# fetch-nfe

Serviço em Docker, **multi-empresa**, que baixa as NF-e (modelo 55) ligadas aos CNPJs
cadastrados direto da SEFAZ, via webservice **NFeDistribuicaoDFe**, usando certificado
**A1** por empresa. Guarda os XMLs no host organizados por CNPJ e data, indexa tudo num
SQLite (dedup por empresa) e faz a **manifestação do destinatário** (ciência da operação,
evento 210210) por uma **fila assíncrona com retries**.

Sem API paga: fala direto com o serviço gratuito e oficial da SEFAZ.
Biblioteca: [PyNFe](https://github.com/TadaSoftware/PyNFe). Dados públicos da empresa
(razão social, UF, município) vêm da [BrasilAPI](https://brasilapi.com.br) no cadastro.

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
| POST   | `/empresas` | Cadastra/atualiza: multipart com `certificado` (.pfx) e `senha`. Valida o A1, extrai o CNPJ e a validade do próprio certificado, enriquece via BrasilAPI. Campos opcionais: `cnpj`, `uf` (fallback se a BrasilAPI falhar), `manifestar` |
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
| POST | `/baixar` | Body `{cnpj?, de?, ate?, tipo?, sincronizar}`: sincroniza (opcional) e retorna as notas do período |
| GET  | `/notas?cnpj=&de=&ate=&tipo=` | Lista por período (`AAAA-MM-DD`, `tipo`=`completa`\|`resumo`) |
| GET  | `/notas/{chave}` | XML de uma nota |
| GET  | `/download?cnpj=&de=&ate=` | `.zip` dos XMLs do período (pastas por CNPJ) |

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
- **Cooldown SEFAZ:** consultar sem novidade antes de ~1h gera `656 Consumo Indevido`
  (o serviço trata e espera o próximo ciclo). Não reduza `INTERVALO_SEGUNDOS` à toa.
- **Manifestação:** a ciência (210210) é evento oficial e irreversível de
  *recebimento* — não confirma nem recusa a operação. Controlável por empresa
  (`manifestar`).
- **Primeira sincronização** de uma empresa parte do NSU 0 e traz o histórico
  disponível (~90 dias). Depois, só incremental.
