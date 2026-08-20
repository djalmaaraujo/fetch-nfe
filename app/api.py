# -*- coding: utf-8 -*-
"""API HTTP: cadastro de empresas/certificados, sincronização e consulta/download
das notas. Sem auth (fora do escopo) — não exponha publicamente sem proteger."""
import io
import os
import threading
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from . import config, empresas, nfe, store, workers

app = FastAPI(
    title="fetch-nfe",
    description="Baixa NF-e via NFeDistribuicaoDFe (SEFAZ) — multi-empresa, certificado A1",
)


@app.on_event("startup")
def _startup():
    store.init()
    empresas.seed_do_env()


def _empresa_ou_404(cnpj: str) -> dict:
    emp = store.get_empresa(config.somente_numeros(cnpj))
    if not emp:
        raise HTTPException(404, "empresa não cadastrada")
    return emp


# --------------------------------------------------------------------------- #
# Empresas / certificados
# --------------------------------------------------------------------------- #
@app.post("/empresas", status_code=201)
async def cadastrar_empresa(
    certificado: UploadFile = File(..., description="arquivo .pfx/.p12 do A1"),
    senha: str = Form(...),
    cnpj: str | None = Form(None, description="opcional; extraído do certificado"),
    uf: str | None = Form(None, description="opcional; vem da BrasilAPI"),
    manifestar: bool | None = Form(None),
):
    """Cadastra (ou atualiza) uma empresa: valida o .pfx com a senha, extrai o CNPJ
    do certificado, busca razão social/UF/município na BrasilAPI e persiste."""
    conteudo = await certificado.read()
    try:
        emp = empresas.cadastrar(conteudo, senha, cnpj=cnpj, uf=uf, manifestar=manifestar)
    except empresas.ErroCadastro as e:
        raise HTTPException(422, str(e))
    return emp


@app.get("/empresas")
def listar_empresas():
    return {"empresas": store.listar_empresas()}


@app.get("/empresas/{cnpj}")
def obter_empresa(cnpj: str):
    emp = _empresa_ou_404(cnpj)
    emp["contagens"] = store.contagens(emp["cnpj"])
    emp["sincronizando"] = store.sync_em_andamento(emp["cnpj"])
    return emp


class EmpresaPatch(BaseModel):
    manifestar: bool | None = None
    ativo: bool | None = None
    senha: str | None = None   # troca de senha do certificado
    uf: str | None = None


@app.patch("/empresas/{cnpj}")
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


@app.delete("/empresas/{cnpj}")
def desativar_empresa(cnpj: str):
    """Desativação (soft delete): para de sincronizar/manifestar. Os XMLs já
    baixados e o certificado permanecem no disco."""
    emp = _empresa_ou_404(cnpj)
    store.atualizar_empresa(emp["cnpj"], {"ativo": 0})
    return {"cnpj": emp["cnpj"], "ativo": False}


# --------------------------------------------------------------------------- #
# Sincronização
# --------------------------------------------------------------------------- #
@app.post("/sincronizar")
def sincronizar_todas():
    """Dispara sincronização de todas as empresas ativas, em segundo plano."""
    ativas = store.listar_empresas(somente_ativas=True)
    threading.Thread(target=workers.sincronizar_todas, daemon=True).start()
    return {"iniciado": True, "empresas": [e["cnpj"] for e in ativas]}


@app.post("/empresas/{cnpj}/sincronizar")
def sincronizar_empresa(cnpj: str):
    emp = _empresa_ou_404(cnpj)
    if store.sync_em_andamento(emp["cnpj"]):
        return {"iniciado": False, "motivo": "já em andamento"}
    threading.Thread(target=nfe.sincronizar, args=(emp["cnpj"],), daemon=True).start()
    return {"iniciado": True, "cnpj": emp["cnpj"]}


@app.get("/status")
def status():
    lista = store.listar_empresas()
    return {
        "ambiente": config.AMBIENTE,
        "dedup": config.DEDUP,
        "fila": store.fila_status(),
        "empresas": [
            {**e, "contagens": store.contagens(e["cnpj"]),
             "sincronizando": store.sync_em_andamento(e["cnpj"])}
            for e in lista
        ],
    }


@app.get("/fila")
def fila():
    return {"fila": store.fila_status()}


@app.get("/health")
def health():
    ativas = store.listar_empresas(somente_ativas=True)
    return {"ok": True, "empresas_ativas": len(ativas)}


# --------------------------------------------------------------------------- #
# Notas
# --------------------------------------------------------------------------- #
class BaixarReq(BaseModel):
    cnpj: str | None = None
    de: str | None = None          # AAAA-MM-DD (data de emissão)
    ate: str | None = None
    tipo: str | None = None        # completa | resumo
    sincronizar: bool = True


@app.post("/baixar")
def baixar(req: BaixarReq):
    """Sincroniza (opcional) e retorna as notas do período (filtro por dhEmi no
    índice local — a SEFAZ não aceita consulta por data, só incremental por NSU)."""
    resultado_sync = None
    if req.sincronizar:
        if req.cnpj:
            resultado_sync = [nfe.sincronizar(config.somente_numeros(req.cnpj))]
        else:
            resultado_sync = workers.sincronizar_todas()
    cnpj = config.somente_numeros(req.cnpj) if req.cnpj else None
    notas = store.notas_periodo(cnpj, req.de, req.ate, req.tipo)
    return {"sincronizacao": resultado_sync, "total": len(notas), "notas": notas}


@app.get("/notas")
def notas(
    cnpj: str | None = Query(None),
    de: str | None = Query(None, description="data inicial AAAA-MM-DD"),
    ate: str | None = Query(None, description="data final AAAA-MM-DD"),
    tipo: str | None = Query(None, description="completa | resumo"),
):
    cnpj = config.somente_numeros(cnpj) if cnpj else None
    itens = store.notas_periodo(cnpj, de, ate, tipo)
    return {"total": len(itens), "notas": itens}


@app.get("/notas/{chave}")
def nota_xml(chave: str, cnpj: str | None = Query(None)):
    caminho = store.caminho_da_chave(chave, config.somente_numeros(cnpj) if cnpj else None)
    if not caminho or not os.path.exists(caminho):
        raise HTTPException(404, "nota não encontrada")
    with open(caminho, "rb") as f:
        return Response(content=f.read(), media_type="application/xml")


@app.get("/download")
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
