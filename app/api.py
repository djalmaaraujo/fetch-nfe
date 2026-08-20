# -*- coding: utf-8 -*-
"""API HTTP: dispara sincronização e consulta/baixa as notas por data."""
import io
import os
import threading
import zipfile

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from . import config, nfe, store

app = FastAPI(title="nfe-download", description="Baixa NF-e via NFeDistribuicaoDFe (SEFAZ)")


@app.on_event("startup")
def _startup():
    store.init()


class BaixarReq(BaseModel):
    de: str | None = None          # AAAA-MM-DD (por data de emissão)
    ate: str | None = None
    tipo: str | None = None        # completa | resumo
    sincronizar: bool = True       # dreno na SEFAZ antes de retornar


@app.get("/health")
def health():
    return {"ok": True, "config_ok": not config.problemas(), "pendencias": config.problemas()}


@app.get("/status")
def status():
    return {
        "ultimo_nsu": store.ultimo_nsu(),
        "ultima_sincronizacao": store.get_estado("ultima_sincronizacao"),
        "sincronizando": store.sync_em_andamento(),
        "contagens": store.contagens(),
        "ambiente": config.AMBIENTE,
        "manifestar": config.MANIFESTAR,
        "dedup": config.DEDUP,
    }


@app.post("/sincronizar")
def sincronizar():
    """Dispara um dreno da SEFAZ em segundo plano."""
    if store.sync_em_andamento():
        return {"iniciado": False, "motivo": "já em andamento"}
    threading.Thread(target=nfe.sincronizar, daemon=True).start()
    return {"iniciado": True}


@app.post("/baixar")
def baixar(req: BaixarReq):
    """Sincroniza (opcional) e retorna as notas do período pedido (por dhEmi)."""
    resultado_sync = None
    if req.sincronizar:
        resultado_sync = nfe.sincronizar()
    notas = store.notas_periodo(req.de, req.ate, req.tipo)
    return {"sincronizacao": resultado_sync, "total": len(notas), "notas": notas}


@app.get("/notas")
def notas(
    de: str | None = Query(None, description="data inicial AAAA-MM-DD"),
    ate: str | None = Query(None, description="data final AAAA-MM-DD"),
    tipo: str | None = Query(None, description="completa | resumo"),
):
    itens = store.notas_periodo(de, ate, tipo)
    return {"total": len(itens), "notas": itens}


@app.get("/notas/{chave}")
def nota_xml(chave: str):
    caminho = store.caminho_da_chave(chave)
    if not caminho or not os.path.exists(caminho):
        raise HTTPException(404, "nota não encontrada")
    with open(caminho, "rb") as f:
        return Response(content=f.read(), media_type="application/xml")


@app.get("/download")
def download_zip(
    de: str | None = Query(None), ate: str | None = Query(None),
    tipo: str | None = Query(None),
):
    """Baixa um .zip com os XMLs do período."""
    itens = store.notas_periodo(de, ate, tipo)
    if not itens:
        raise HTTPException(404, "nenhuma nota no período")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in itens:
            cam = n["caminho"]
            if cam and os.path.exists(cam):
                z.write(cam, arcname=f"{n['data_emi']}/{n['chave']}.xml")
    buf.seek(0)
    nome = f"nfe_{de or 'inicio'}_{ate or 'fim'}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={nome}"},
    )
