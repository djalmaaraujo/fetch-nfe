# -*- coding: utf-8 -*-
"""API HTTP: cadastro de empresas/certificados, sincronização e consulta/download
das notas. Sem auth (fora do escopo) — não exponha publicamente sem proteger."""
import io
import os
import threading
import zipfile

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from . import config, empresas, extrator, nfe, store, workers

app = FastAPI(
    title="fetch-nfe",
    version="1.0.0",
    description=(
        "Baixa NF-e (modelo 55) via webservice NFeDistribuicaoDFe da SEFAZ — "
        "multi-empresa, certificado A1 por CNPJ, fila assíncrona de manifestação "
        "do destinatário (ciência 210210) e consulta/download por data de emissão.\n\n"
        "A distribuição da SEFAZ é incremental por NSU; filtros de data agem sobre "
        "o índice local. Sem autenticação — não exponha publicamente sem proteger.\n\n"
        "Spec OpenAPI: `/openapi.json` · Swagger UI: `/docs` · ReDoc: `/redoc`"
    ),
    openapi_tags=[
        {"name": "empresas", "description": "Cadastro de empresas e certificados A1 (o CNPJ e a validade são extraídos do próprio .pfx; dados públicos via BrasilAPI)"},
        {"name": "sincronizacao", "description": "Dreno incremental da SEFAZ (por NSU, por empresa) e situação da fila de manifestação"},
        {"name": "rotina", "description": "Configuração em runtime do scheduler: ativa/pausada e intervalo entre execuções (aplica em ~15s, sem restart). Pausada = o worker não fala com a SEFAZ sozinho; sync manual continua disponível"},
        {"name": "notas", "description": "Consulta e download dos XMLs já baixados, filtrando por CNPJ, data de emissão e tipo"},
        {"name": "infra", "description": "Saúde do serviço"},
    ],
)


@app.on_event("startup")
def _startup():
    store.init()
    empresas.seed_do_env()


# --------------------------------------------------------------------------- #
# Paginação padrão — mesmos parâmetros e mesma resposta em toda listagem:
# {total, limite, offset, <itens>} (total = sem paginação)
# --------------------------------------------------------------------------- #
class Paginacao:
    def __init__(
        self,
        limite: int = Query(50, ge=1, le=500, description="Tamanho da página"),
        offset: int = Query(0, ge=0, description="Deslocamento (itens a pular)"),
    ):
        self.limite = limite
        self.offset = offset


def paginar(itens: list, pag: Paginacao, chave: str) -> dict:
    """Aplica a paginação padrão a uma lista em memória."""
    return {
        "total": len(itens), "limite": pag.limite, "offset": pag.offset,
        chave: itens[pag.offset:pag.offset + pag.limite],
    }


def _empresa_ou_404(cnpj: str) -> dict:
    emp = store.get_empresa(config.somente_numeros(cnpj))
    if not emp:
        raise HTTPException(404, "empresa não cadastrada")
    return emp


# --------------------------------------------------------------------------- #
# Empresas / certificados
# --------------------------------------------------------------------------- #
@app.post("/empresas", status_code=201, tags=["empresas"], summary="Cadastrar/atualizar empresa (upload do .pfx)")
async def cadastrar_empresa(
    certificado: UploadFile = File(..., description="arquivo .pfx/.p12 do A1"),
    senha: str = Form(...),
    cnpj: str | None = Form(None, description="opcional; extraído do certificado"),
    uf: str | None = Form(None, description="opcional; vem da BrasilAPI"),
    manifestar: bool | None = Form(
        None,
        description="Se true, notas que chegam só como resumo recebem o evento de "
                    "CIÊNCIA da operação (210210) — apenas 'tomei conhecimento', "
                    "não confirma nem recusa a operação — para a SEFAZ liberar o "
                    "XML completo. O serviço nunca envia confirmação (210200), "
                    "desconhecimento (210220) ou operação não realizada (210240).",
    ),
):
    """Cadastra (ou atualiza) uma empresa: valida o .pfx com a senha, extrai o CNPJ
    do certificado, busca razão social/UF/município na BrasilAPI e persiste."""
    conteudo = await certificado.read()
    try:
        emp = empresas.cadastrar(conteudo, senha, cnpj=cnpj, uf=uf, manifestar=manifestar)
    except empresas.ErroCadastro as e:
        raise HTTPException(422, str(e))
    return emp


@app.get("/empresas", tags=["empresas"], summary="Listar empresas")
def listar_empresas(pag: Paginacao = Depends()):
    return paginar(store.listar_empresas(), pag, "empresas")


@app.get("/empresas/{cnpj}", tags=["empresas"], summary="Detalhar empresa (com contagens)")
def obter_empresa(cnpj: str):
    emp = _empresa_ou_404(cnpj)
    emp["contagens"] = store.contagens(emp["cnpj"])
    emp["sincronizando"] = store.sync_em_andamento(emp["cnpj"])
    return emp


class EmpresaPatch(BaseModel):
    manifestar: bool | None = Field(
        None,
        description="Só CIÊNCIA (210210) — nunca confirma nem recusa a operação.",
    )
    ativo: bool | None = None
    senha: str | None = None   # troca de senha do certificado
    uf: str | None = None


@app.patch("/empresas/{cnpj}", tags=["empresas"], summary="Ajustar manifestar/ativo/senha/UF")
def atualizar_empresa(cnpj: str, patch: EmpresaPatch):
    emp = _empresa_ou_404(cnpj)
    campos = {}
    if patch.manifestar is not None:
        campos["manifestar"] = int(patch.manifestar)
    if patch.ativo is not None:
        campos["ativo"] = int(patch.ativo)
    if patch.senha is not None:
        campos["cert_senha"] = patch.senha
    if patch.uf is not None:
        campos["uf"] = patch.uf.upper()
    if not campos:
        raise HTTPException(422, "nada para atualizar")
    store.atualizar_empresa(emp["cnpj"], campos)
    return store.get_empresa(emp["cnpj"])


@app.delete("/empresas/{cnpj}", tags=["empresas"], summary="Desativar empresa (soft delete)")
def desativar_empresa(cnpj: str):
    """Desativação (soft delete): para de sincronizar/manifestar. Os XMLs já
    baixados e o certificado permanecem no disco."""
    emp = _empresa_ou_404(cnpj)
    store.atualizar_empresa(emp["cnpj"], {"ativo": 0})
    return {"cnpj": emp["cnpj"], "ativo": False}


# --------------------------------------------------------------------------- #
# Sincronização
# --------------------------------------------------------------------------- #
@app.post("/sincronizar", tags=["sincronizacao"], summary="Sincronizar todas as empresas ativas")
def sincronizar_todas():
    """Dispara sincronização de todas as empresas ativas, em segundo plano."""
    ativas = store.listar_empresas(somente_ativas=True)
    threading.Thread(target=workers.sincronizar_todas, daemon=True).start()
    return {"iniciado": True, "empresas": [e["cnpj"] for e in ativas]}


@app.post("/empresas/{cnpj}/sincronizar", tags=["sincronizacao"], summary="Sincronizar uma empresa")
def sincronizar_empresa(cnpj: str):
    emp = _empresa_ou_404(cnpj)
    if store.sync_em_andamento(emp["cnpj"]):
        return {"iniciado": False, "motivo": "já em andamento"}
    threading.Thread(target=nfe.sincronizar, args=(emp["cnpj"],), daemon=True).start()
    return {"iniciado": True, "cnpj": emp["cnpj"]}


@app.get("/status", tags=["sincronizacao"], summary="Status geral (empresas, NSU, fila)")
def status():
    lista = store.listar_empresas()
    return {
        "ambiente": config.AMBIENTE,
        "dedup": config.DEDUP,
        "rotina": store.rotina_status(),
        "fila": store.fila_status(),
        "empresas": [
            {**e, "contagens": store.contagens(e["cnpj"]),
             "sincronizando": store.sync_em_andamento(e["cnpj"])}
            for e in lista
        ],
    }


@app.get("/fila", tags=["sincronizacao"], summary="Fila de manifestação por empresa")
def fila():
    return {"fila": store.fila_status()}


# --------------------------------------------------------------------------- #
# Rotina (scheduler)
# --------------------------------------------------------------------------- #
class RotinaPatch(BaseModel):
    ativa: bool | None = None
    intervalo_segundos: int | None = Field(
        None, ge=60,
        description="Intervalo entre execuções. Mínimo 60s; abaixo de 3600s a "
                    "SEFAZ pode rejeitar por consumo indevido (656) quando não há novidade.",
    )


@app.get("/rotina", tags=["rotina"], summary="Configuração e situação da rotina")
def rotina():
    """Estado atual do scheduler: ativa/pausada, intervalo, última execução
    (com duração) e previsão da próxima."""
    return store.rotina_status()


@app.patch("/rotina", tags=["rotina"], summary="Ativar/pausar e ajustar o intervalo")
def configurar_rotina(patch: RotinaPatch):
    """Aplica em runtime (o worker relê a cada ~15s — sem restart). Mudança de
    intervalo recalcula a próxima execução a partir da última."""
    if patch.ativa is None and patch.intervalo_segundos is None:
        raise HTTPException(422, "nada para atualizar: informe 'ativa' e/ou 'intervalo_segundos'")
    store.set_rotina_config(patch.ativa, patch.intervalo_segundos)
    return store.rotina_status()


@app.get("/health", tags=["infra"], summary="Saúde do serviço")
def health():
    ativas = store.listar_empresas(somente_ativas=True)
    return {"ok": True, "empresas_ativas": len(ativas)}


# --------------------------------------------------------------------------- #
# Notas
# --------------------------------------------------------------------------- #
@app.get("/notas", tags=["notas"], summary="Buscar notas (filtros combináveis + full-text)")
def notas(
    q: str | None = Query(None, description="Busca textual livre (FTS5, ignora acentos) sobre emitente, destinatário, natureza da operação, produtos e informações complementares. Ex.: 'painel mdf dexco'"),
    cnpj: str | None = Query(None, description="CNPJ da empresa dona da nota (a cadastrada no serviço)"),
    de: str | None = Query(None, description="Data de emissão inicial, AAAA-MM-DD"),
    ate: str | None = Query(None, description="Data de emissão final, AAAA-MM-DD"),
    tipo: str | None = Query(None, description="'completa' (XML inteiro) ou 'resumo'"),
    emitente: str | None = Query(None, description="CNPJ (14 dígitos, match exato) ou trecho do nome do emitente"),
    destinatario: str | None = Query(None, description="CNPJ (14 dígitos) ou trecho do nome do destinatário"),
    uf: str | None = Query(None, description="UF do emitente, ex.: SP"),
    nnf: int | None = Query(None, description="Número da nota (nNF)"),
    serie: str | None = Query(None, description="Série da nota"),
    natop: str | None = Query(None, description="Trecho da natureza da operação (natOp), ex.: 'venda'"),
    tp_nf: int | None = Query(None, ge=0, le=1, description="0=entrada, 1=saída (do ponto de vista do emitente)"),
    valor_min: float | None = Query(None, description="Valor total da nota (vNF) mínimo"),
    valor_max: float | None = Query(None, description="Valor total da nota (vNF) máximo"),
    produto: str | None = Query(None, description="Trecho da descrição de um item (xProd), ex.: 'MDF'"),
    ncm: str | None = Query(None, description="NCM de um item — aceita prefixo (ex.: '4411' pega o capítulo inteiro)"),
    cfop: str | None = Query(None, description="CFOP de um item, ex.: 6129"),
    cean: str | None = Query(None, description="Código de barras (cEAN/GTIN) de um item"),
    venc_de: str | None = Query(None, description="Vencimento de duplicata a partir de, AAAA-MM-DD"),
    venc_ate: str | None = Query(None, description="Vencimento de duplicata até, AAAA-MM-DD"),
    manifestada: bool | None = Query(None, description="Filtra por notas já manifestadas (ciência enviada)"),
    ordenar: str = Query("data", description="'data' (dhEmi) ou 'valor' (vNF)"),
    ordem: str = Query("desc", description="'asc' ou 'desc'"),
    pag: Paginacao = Depends(),
):
    """Todos os filtros são opcionais e combináveis (AND). Filtros de item
    (produto/ncm/cfop/cean) casam se QUALQUER item da nota casar. Retorna
    `total` (sem paginação) + a página pedida."""
    filtros = {
        "q": q, "cnpj": config.somente_numeros(cnpj) if cnpj else None,
        "de": de, "ate": ate, "tipo": tipo, "emitente": emitente,
        "destinatario": destinatario, "uf": uf, "nnf": nnf, "serie": serie,
        "natop": natop, "tp_nf": tp_nf, "valor_min": valor_min,
        "valor_max": valor_max, "produto": produto, "ncm": ncm, "cfop": cfop,
        "cean": cean, "venc_de": venc_de, "venc_ate": venc_ate,
        "manifestada": manifestada,
    }
    return store.buscar_notas(filtros, limite=pag.limite, offset=pag.offset,
                              ordenar=ordenar, ordem=ordem)


@app.get("/notas/{chave}", tags=["notas"], summary="XML de uma nota")
def nota_xml(chave: str, cnpj: str | None = Query(None)):
    caminho = store.caminho_da_chave(chave, config.somente_numeros(cnpj) if cnpj else None)
    if not caminho or not os.path.exists(caminho):
        raise HTTPException(404, "nota não encontrada")
    with open(caminho, "rb") as f:
        return Response(content=f.read(), media_type="application/xml")


@app.get("/notas/{chave}/json", tags=["notas"],
         summary="Nota completa em JSON estruturado (pra agentes)")
def nota_json(chave: str, cnpj: str | None = Query(None)):
    """O XML da nota convertido em JSON aninhado (ide, emit, dest, det[],
    total, cobr...), sem namespaces e sem a assinatura digital. Elementos
    repetidos (det, dup) viram listas."""
    from lxml import etree as _etree
    caminho = store.caminho_da_chave(chave, config.somente_numeros(cnpj) if cnpj else None)
    if not caminho or not os.path.exists(caminho):
        raise HTTPException(404, "nota não encontrada")
    raiz = _etree.parse(caminho).getroot()
    return {"chave": chave, "nota": extrator.xml_para_dict(raiz)}


@app.post("/reindexar", tags=["notas"],
          summary="Reprocessar os campos de busca a partir dos XMLs no disco")
def reindexar(forcar: bool = Query(True, description="True reindexa tudo; False só o que falta")):
    threading.Thread(target=extrator.backfill, args=(forcar,), daemon=True).start()
    return {"iniciado": True, "forcar": forcar}


@app.get("/download", tags=["notas"], summary="ZIP dos XMLs do período")
def download_zip(
    cnpj: str | None = Query(None), de: str | None = Query(None),
    ate: str | None = Query(None), tipo: str | None = Query(None),
):
    """Baixa um .zip com os XMLs do período (multi-empresa: pastas por CNPJ)."""
    cnpj = config.somente_numeros(cnpj) if cnpj else None
    itens = store.notas_periodo(cnpj, de, ate, tipo)
    if not itens:
        raise HTTPException(404, "nenhuma nota no período")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in itens:
            cam = n["caminho"]
            if cam and os.path.exists(cam):
                z.write(cam, arcname=f"{n['cnpj_empresa']}/{n['data_emi']}/{n['chave']}.xml")
    buf.seek(0)
    nome = f"nfe_{cnpj or 'todas'}_{de or 'inicio'}_{ate or 'fim'}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={nome}"},
    )
