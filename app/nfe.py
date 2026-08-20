# -*- coding: utf-8 -*-
"""Núcleo: sincroniza a distribuição da SEFAZ (dreno incremental por NSU),
salva os XMLs (com dedup) e faz a manifestação do destinatário (210210)."""
import base64
import gzip
import os
import time
import warnings
from datetime import datetime

import urllib3
from lxml import etree

from pynfe.processamento.comunicacao import ComunicacaoSefaz
from pynfe.processamento.serializacao import SerializacaoXML
from pynfe.processamento.assinatura import AssinaturaA1
from pynfe.entidades.evento import EventoManifestacaoDest
from pynfe.entidades.fonte_dados import _fonte_dados

from . import config, store

warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Helpers de XML
# --------------------------------------------------------------------------- #
def _texto(el, nome: str):
    r = el.xpath(f".//*[local-name()='{nome}']")
    return r[0].text if r else None


def _local_name(el) -> str:
    return etree.QName(el).localname


def _salvar_arquivo(caminho_rel: str, conteudo: bytes) -> str:
    destino = os.path.join(config.DATA_DIR, caminho_rel)
    if not (config.DEDUP and os.path.exists(destino)):
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        with open(destino, "wb") as f:
            f.write(conteudo)
    return destino


# --------------------------------------------------------------------------- #
# Processa um documento recebido
# --------------------------------------------------------------------------- #
def _processar(inner, conteudo, nsu, pendentes_manifesto) -> None:
    tag = _local_name(inner)

    if tag in ("nfeProc", "procNFe"):
        ids = inner.xpath(".//*[local-name()='infNFe']/@Id")
        chave = ids[0][3:] if ids else (_texto(inner, "chNFe") or nsu)
        if config.DEDUP and store.ja_tem_completa(chave):
            return
        dh = _texto(inner, "dhEmi") or _texto(inner, "dEmi") or ""
        data = dh[:10] if len(dh) >= 10 else datetime.now().strftime("%Y-%m-%d")
        emit = _texto(inner, "xNome") or ""
        valor = _texto(inner, "vNF") or ""
        caminho = _salvar_arquivo(f"{data}/{chave}.xml", conteudo)
        store.upsert_nota(chave, "completa", dh, int(nsu or 0), emit, valor, caminho)
        log(f"  NF-e completa: {data}/{chave} ({emit} R$ {valor})")

    elif tag == "resNFe":
        chave = _texto(inner, "chNFe") or nsu
        dh = _texto(inner, "dhEmi") or ""
        emit = _texto(inner, "xNome") or ""
        valor = _texto(inner, "vNF") or ""
        caminho = _salvar_arquivo(f"_resumos/{chave}.xml", conteudo)
        store.upsert_nota(chave, "resumo", dh, int(nsu or 0), emit, valor, caminho)
        if not store.ja_tem_completa(chave) and not store.manifestada(chave):
            pendentes_manifesto.add(chave)
        log(f"  Resumo: {chave} ({emit})")

    elif tag in ("procEventoNFe", "resEvento"):
        _salvar_arquivo(f"_eventos/{nsu}.xml", conteudo)
    else:
        _salvar_arquivo(f"_outros/{nsu}_{tag}.xml", conteudo)


# --------------------------------------------------------------------------- #
# Manifestação — Ciência da Operação (210210)
# --------------------------------------------------------------------------- #
def _manifestar(con, chave) -> bool:
    serializador = SerializacaoXML(_fonte_dados, homologacao=config.HOMOLOGACAO)
    evento = EventoManifestacaoDest(
        cnpj=config.CNPJ, chave=chave, data_emissao=datetime.now(),
        uf="AN", operacao=2, n_seq_evento=1,
    )
    xml = serializador.serializar_evento(evento)
    assinado = AssinaturaA1(config.CERT_PATH, config.CERT_SENHA).assinar(xml)
    resp = con.evento(modelo=55, evento=assinado)

    raiz = etree.fromstring(resp.content)
    stats = raiz.xpath(".//*[local-name()='retEvento']//*[local-name()='cStat']") \
        or raiz.xpath(".//*[local-name()='cStat']")
    cstat = stats[-1].text if stats else "?"
    motivos = raiz.xpath(".//*[local-name()='xMotivo']")
    motivo = motivos[-1].text if motivos else ""
    if cstat in ("135", "136", "573"):  # registrado / duplicidade
        log(f"  Manifestação OK ({cstat}) {chave}")
        return True
    log(f"  Manifestação FALHOU ({cstat} {motivo}) {chave}")
    return False


# --------------------------------------------------------------------------- #
# Sincronização completa (um dreno)
# --------------------------------------------------------------------------- #
def sincronizar() -> dict:
    """Executa um dreno incremental da SEFAZ. Protegido por lock entre processos."""
    problemas = config.problemas()
    if problemas:
        raise RuntimeError("Configuração incompleta: " + ", ".join(problemas))

    store.init()
    os.makedirs(config.DATA_DIR, exist_ok=True)

    resultado = {"documentos": 0, "manifestadas": 0, "cstat": None, "erro": None}

    try:
        with store.trava_sync(bloqueante=False):
            con = ComunicacaoSefaz(
                uf=config.UF, certificado=config.CERT_PATH,
                certificado_senha=config.CERT_SENHA, homologacao=config.HOMOLOGACAO,
            )
            nsu = store.ultimo_nsu()
            log(f"Sincronizando a partir do NSU {nsu} (ambiente: {config.AMBIENTE})")
            pendentes = set()

            for _ in range(config.MAX_ITERACOES):
                resp = con.consulta_distribuicao(cnpj=config.CNPJ, nsu=nsu)
                raiz = etree.fromstring(resp.content)
                ret = raiz.xpath(".//*[local-name()='retDistDFeInt']")
                if not ret:
                    log(f"Resposta inesperada: {resp.text[:400]}")
                    break
                ret = ret[0]
                cstat = _texto(ret, "cStat")
                motivo = _texto(ret, "xMotivo") or ""
                max_nsu = int(_texto(ret, "maxNSU") or 0)
                novo = int(_texto(ret, "ultNSU") or nsu)
                resultado["cstat"] = f"{cstat} {motivo}"

                if cstat == "137":
                    log(f"Nada novo (137 {motivo}).")
                    store.set_estado("ultimo_nsu", novo)
                    break
                if cstat == "656":
                    log(f"Consumo indevido (656 {motivo}).")
                    break
                if cstat != "138":
                    log(f"cStat {cstat}: {motivo}. Encerrando.")
                    break

                docs = ret.xpath(".//*[local-name()='docZip']")
                log(f"Lote: {len(docs)} doc(s). ultNSU={novo} maxNSU={max_nsu}")
                for doc in docs:
                    d_nsu = doc.get("NSU", "")
                    try:
                        conteudo = gzip.decompress(base64.b64decode(doc.text))
                        inner = etree.fromstring(conteudo)
                        _processar(inner, conteudo, d_nsu, pendentes)
                        resultado["documentos"] += 1
                    except Exception as e:
                        log(f"  Erro no NSU {d_nsu}: {e}")

                nsu = novo
                store.set_estado("ultimo_nsu", nsu)
                if nsu >= max_nsu:
                    log("Fim do backlog.")
                    break
                time.sleep(config.PAUSA_ENTRE_CHAMADAS)

            if config.MANIFESTAR and pendentes:
                log(f"Avaliando manifestação de {len(pendentes)} nota(s)...")
                for chave in sorted(pendentes):
                    # Dedup: se o XML completo já chegou (mesmo neste dreno) ou já
                    # manifestamos antes, não manifesta de novo.
                    if store.ja_tem_completa(chave) or store.manifestada(chave):
                        continue
                    try:
                        if _manifestar(con, chave):
                            store.marcar_manifestada(chave)
                            resultado["manifestadas"] += 1
                    except Exception as e:
                        log(f"  Erro ao manifestar {chave}: {e}")
                    time.sleep(config.PAUSA_ENTRE_MANIFESTOS)

            store.set_estado("ultima_sincronizacao", datetime.now().isoformat(timespec="seconds"))
            log(f"Sincronização concluída: {resultado['documentos']} doc(s), "
                f"{resultado['manifestadas']} manifestada(s).")
    except BlockingIOError:
        resultado["erro"] = "sincronização já em andamento"
        log("Sincronização já em andamento — ignorando.")
    return resultado
