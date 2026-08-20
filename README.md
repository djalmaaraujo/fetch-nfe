# nfe-download

Serviço em Docker que baixa as NF-e (modelo 55) ligadas ao seu CNPJ direto da SEFAZ,
via webservice **NFeDistribuicaoDFe**, usando certificado **A1**. Guarda os XMLs no
host organizados por data, indexa tudo num SQLite (com dedup) e — opcionalmente — faz
a **manifestação do destinatário** (ciência da operação, evento 210210) pra liberar o
XML completo de notas que vêm só como resumo.

Sem API paga: fala direto com o serviço gratuito e oficial da SEFAZ.
Biblioteca: [PyNFe](https://github.com/TadaSoftware/PyNFe).

Sobe em dois serviços que compartilham o mesmo índice:
- **api** — HTTP pra disparar sincronização e consultar/baixar por data.
- **rotina** — cron interno que sincroniza de tempos em tempos.

## Como funciona (importante)

A distribuição da SEFAZ é **incremental por NSU**, não por data: você não pede "notas
de 01 a 15/08", você puxa tudo que chegou desde o último NSU consumido. Então:

- **Sincronizar** = drenar da SEFAZ tudo que há de novo (o serviço guarda o NSU).
- **Consultar/baixar por data** = filtrar pela `dhEmi` no índice local do que já foi
  baixado. Os parâmetros de data agem sobre o índice, não sobre a SEFAZ.

Notas que vêm só como **resumo** precisam de manifestação (210210) pra liberar o XML
completo — que aparece num NSU **posterior**, capturado na sincronização seguinte. Por
isso é um serviço contínuo.

## Estrutura

```
nfe-download/
├── certs/            # certificado A1 (.pfx) — não versionado
├── data/fiscal/      # saída (gerado)
│   ├── 2026-08-18/    #   NF-e completas por data de emissão
│   ├── _resumos/      #   resumos (pendentes de manifestação)
│   └── _eventos/      #   eventos
├── state/            # notas.db (índice/estado/NSU) + lock — não apagar
├── app/              # config, store (SQLite), nfe (núcleo), api, scheduler
├── Dockerfile
├── docker-compose.yml
└── .env              # copiado de .env.example (segredos, não versionado)
```

## Configuração

```bash
cp .env.example .env
# edite: CNPJ, UF, CERT_SENHA, CERT_PATH (nome do .pfx), MANIFESTAR, INTERVALO...
```

Coloque o certificado A1 em `certs/` e aponte `CERT_PATH` pra ele
(ex.: `CERT_PATH=/certs/certificado.pfx`).

## Subir

```bash
docker compose up -d --build      # api + rotina
docker compose logs -f
```

Só a rotina (sem API): `docker compose up -d rotina`
Uma sincronização avulsa (sem deixar de pé): `docker compose run --rm -e INTERVALO_SEGUNDOS=0 rotina`

## API (padrão em http://localhost:8000)

| Método | Rota | O que faz |
|---|---|---|
| GET  | `/health` | Saúde e pendências de config |
| GET  | `/status` | Último NSU, contagens, última sincronização |
| POST | `/sincronizar` | Dispara um dreno da SEFAZ em segundo plano |
| POST | `/baixar` | Body `{de, ate, tipo, sincronizar}`: sincroniza (opcional) e retorna as notas do período |
| GET  | `/notas?de=&ate=&tipo=` | Lista as notas do período (por `dhEmi`) |
| GET  | `/notas/{chave}` | Retorna o XML de uma nota |
| GET  | `/download?de=&ate=&tipo=` | Baixa um `.zip` com os XMLs do período |

Datas em `AAAA-MM-DD`. `tipo` = `completa` | `resumo`.

Exemplos:
```bash
curl -X POST localhost:8000/sincronizar
curl "localhost:8000/notas?de=2026-08-18&ate=2026-08-18"
curl -X POST localhost:8000/baixar -H 'Content-Type: application/json' \
     -d '{"de":"2026-08-01","ate":"2026-08-19","sincronizar":true}'
curl -OJ "localhost:8000/download?de=2026-08-01&ate=2026-08-19"
```

Docs interativas: `http://localhost:8000/docs`.

## Observações

- **Segurança:** `.env`, `cert-pw.txt`, `certs/`, `data/` e `state/` estão no
  `.gitignore`. A senha do certificado fica só no `.env` local.
- **Dedup** (`DEDUP=1`): não regrava XML já baixado e não rebaixa uma nota completa
  para resumo. Índice por chave de acesso.
- **Cooldown:** não reduza demais o `INTERVALO_SEGUNDOS` — a SEFAZ bloqueia consumo
  indevido (cStat 656) se você consultar sem novidade cedo demais. 1h é o recomendado.
  A API e a rotina se coordenam por um lock pra nunca consultar em paralelo.
- **Manifestação** (`MANIFESTAR`): a ciência (210210) é um evento oficial e
  irreversível de *recebimento* — não confirma nem recusa a operação. Com `MANIFESTAR=0`
  as notas que a SEFAZ só entrega como resumo ficam como resumo.
- **Primeira execução:** parte do NSU 0 e baixa todo o histórico disponível
  (~90 dias / conforme retenção). Depois, só o incremental.
