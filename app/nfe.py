# -*- coding: utf-8 -*-
"""Núcleo por empresa: sincroniza a distribuição da SEFAZ (dreno incremental por
NSU, guardado por empresa), salva os XMLs com dedup e ENFILEIRA a manifestação
(210210) — quem envia é o worker da fila, de forma assíncrona."""
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

from . import config, extrator, store

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


def _salvar_arquivo(cnpj: str, caminho_rel: str, conteudo: bytes) -> str:
    destino = os.path.join(config.DATA_DIR, cnpj, caminho_rel)
    if not (config.DEDUP and os.path.exists(destino)):
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        with open(destino, "wb") as f:
            f.write(conteudo)
    return destino


def _conexao(emp: dict) -> ComunicacaoSefaz:
    return ComunicacaoSefaz(
        uf=emp["uf"], certificado=emp["cert_path"],
        certificado_senha=emp["cert_senha"], homologacao=config.HOMOLOGACAO,
    )


# --------------------------------------------------------------------------- #
# Processa um documento recebido
# --------------------------------------------------------------------------- #
def _processar(emp: dict, inner, conteudo: bytes, nsu: str) -> None:
    cnpj = emp["cnpj"]
    tag = _local_name(inner)

    if tag in ("nfeProc", "procNFe"):
        ids = inner.xpath(".//*[local-name()='infNFe']/@Id")
        chave = ids[0][3:] if ids else (_texto(inner, "chNFe") or nsu)
        if config.DEDUP and store.ja_tem_completa(cnpj, chave):
            return
        dh = _texto(inner, "dhEmi") or _texto(inner, "dEmi") or ""
        data = dh[:10] if len(dh) >= 10 else datetime.now().strftime("%Y-%m-%d")
        emit = _texto(inner, "xNome") or ""
        valor = _texto(inner, "vNF") or ""
        caminho = _salvar_arquivo(cnpj, f"{data}/{chave}.xml", conteudo)
        store.upsert_nota(cnpj, chave, "completa", dh, int(nsu or 0), emit, valor, caminho)
        extrator.extrair_e_indexar(cnpj, chave, inner, "completa")
        # Chegou a completa: manifestar não é mais necessário — sai da fila
        store.remover_da_fila(cnpj, chave)
        log(f"[{cnpj}]   NF-e completa: {data}/{chave} ({emit} R$ {valor})")

    elif tag == "resNFe":
        chave = _texto(inner, "chNFe") or nsu
        dh = _texto(inner, "dhEmi") or ""
        emit = _texto(inner, "xNome") or ""
        valor = _texto(inner, "vNF") or ""
        caminho = _salvar_arquivo(cnpj, f"_resumos/{chave}.xml", conteudo)
        store.upsert_nota(cnpj, chave, "resumo", dh, int(nsu or 0), emit, valor, caminho)
        if not store.ja_tem_completa(cnpj, chave):  # não sobrescrever índice da completa
            extrator.extrair_e_indexar(cnpj, chave, inner, "resumo")
        # Dedup na origem: só entra na fila se ainda não temos a completa
        # nem manifestamos essa chave antes
        if emp["manifestar"] and not store.ja_tem_completa(cnpj, chave) \
                and not store.manifestada(cnpj, chave):
            store.enfileirar_manifestacao(cnpj, chave)
        log(f"[{cnpj}]   Resumo: {chave} ({emit})")

    elif tag in ("procEventoNFe", "resEvento"):
        _salvar_arquivo(cnpj, f"_eventos/{nsu}.xml", conteudo)
    else:
        _salvar_arquivo(cnpj, f"_outros/{nsu}_{tag}.xml", conteudo)


# --------------------------------------------------------------------------- #
# Sincronização de UMA empresa (um dreno)
# --------------------------------------------------------------------------- #
COOLDOWN_SEFAZ = 3600  # regra da SEFAZ: 1h entre consultas sem documento novo


def sincronizar(cnpj: str, forcar: bool = False) -> dict:
    emp = store.get_empresa(cnpj, com_senha=True)
    if not emp:
        return {"cnpj": cnpj, "erro": "empresa não cadastrada"}
    if not emp["ativo"]:
        return {"cnpj": cnpj, "erro": "empresa inativa"}
    if not os.path.exists(emp["cert_path"]):
        return {"cnpj": cnpj, "erro": f"certificado não encontrado: {emp['cert_path']}"}

    # Proteção contra bloqueio da SEFAZ: depois de um dreno sem novidade (137/
    # fim do backlog) ou de um 656, não consulta este CNPJ por 1h. O controle
    # da SEFAZ é por CNPJ do interessado — disciplina de consulta é a proteção.
    # forcar só fura cooldown de origem 'fim': insistir durante um 656 REINICIA
    # o temporizador do bloqueio na SEFAZ (regra oficial) — nunca vale a pena.
    info = store.cooldown_info(cnpj)
    if info and (not forcar or info["origem"] == "656"):
        if forcar and info["origem"] == "656":
            log(f"[{cnpj}] forcar ignorado: cooldown veio de 656 — insistir "
                f"reiniciaria o bloqueio na SEFAZ. Aguarde até {info['ate']}.")
        else:
            log(f"[{cnpj}] Em cooldown da SEFAZ até {info['ate']} — pulando.")
        return {"cnpj": cnpj, "erro": f"em cooldown da SEFAZ até {info['ate']}",
                "cooldown_ate": info["ate"], "cooldown_origem": info["origem"]}

    resultado = {"cnpj": cnpj, "documentos": 0, "enfileiradas": 0, "cstat": None, "erro": None}
    try:
        with store.trava_sync(cnpj, bloqueante=False):
            con = _conexao(emp)
            nsu = emp["ultimo_nsu"]
            log(f"[{cnpj}] Sincronizando a partir do NSU {nsu} ({config.AMBIENTE})")

            for _ in range(config.MAX_ITERACOES):
                resp = con.consulta_distribuicao(cnpj=cnpj, nsu=nsu)
                raiz = etree.fromstring(resp.content)
                ret = raiz.xpath(".//*[local-name()='retDistDFeInt']")
                if not ret:
                    log(f"[{cnpj}] Resposta inesperada: {resp.text[:300]}")
                    break
                ret = ret[0]
                cstat = _texto(ret, "cStat")
                motivo = _texto(ret, "xMotivo") or ""
                max_nsu = int(_texto(ret, "maxNSU") or 0)
                novo = int(_texto(ret, "ultNSU") or nsu)
                resultado["cstat"] = f"{cstat} {motivo}"

                if cstat == "137":
                    log(f"[{cnpj}] Nada novo (137).")
                    store.set_ultimo_nsu(cnpj, novo)
                    store.set_cooldown(cnpj, time.time() + COOLDOWN_SEFAZ)
                    break
                if cstat == "656":
                    log(f"[{cnpj}] Consumo indevido (656). Cooldown de 1h aplicado.")
                    store.set_cooldown(cnpj, time.time() + COOLDOWN_SEFAZ, origem="656")
                    break
                if cstat != "138":
                    log(f"[{cnpj}] cStat {cstat}: {motivo}. Encerrando.")
                    break

                docs = ret.xpath(".//*[local-name()='docZip']")
                log(f"[{cnpj}] Lote: {len(docs)} doc(s). ultNSU={novo} maxNSU={max_nsu}")
                antes_fila = _tam_fila(cnpj)
                for doc in docs:
                    d_nsu = doc.get("NSU", "")
                    try:
                        conteudo = gzip.decompress(base64.b64decode(doc.text))
                        inner = etree.fromstring(conteudo)
                        _processar(emp, inner, conteudo, d_nsu)
                        resultado["documentos"] += 1
                    except Exception as e:
                        log(f"[{cnpj}]   Erro no NSU {d_nsu}: {e}")
                resultado["enfileiradas"] += max(0, _tam_fila(cnpj) - antes_fila)

                nsu = novo
                store.set_ultimo_nsu(cnpj, nsu)
                if nsu >= max_nsu:
                    log(f"[{cnpj}] Fim do backlog.")
                    store.set_cooldown(cnpj, time.time() + COOLDOWN_SEFAZ)
                    break
                time.sleep(config.PAUSA_ENTRE_CHAMADAS)

            store.registrar_sincronizacao(cnpj, resultado["cstat"] or "sem resposta")
            log(f"[{cnpj}] Sincronização concluída: {resultado['documentos']} doc(s), "
                f"{resultado['enfileiradas']} enfileirada(s) p/ manifestação.")
    except BlockingIOError:
        resultado["erro"] = "sincronização já em andamento"
        log(f"[{cnpj}] Sincronização já em andamento — ignorando.")
    except Exception as e:
        resultado["erro"] = str(e)
        log(f"[{cnpj}] Erro na sincronização: {e}")
    return resultado


def _tam_fila(cnpj: str) -> int:
    return sum(f["itens"] for f in store.fila_status()
               if f["cnpj_empresa"] == cnpj and f["status"] == "pendente")


# --------------------------------------------------------------------------- #
# Manifestação — Ciência da Operação (210210), consumida pelo worker da fila
# --------------------------------------------------------------------------- #
def manifestar_chave(emp: dict, chave: str) -> bool:
    """Envia a ciência para uma chave. Levanta exceção em erro de comunicação."""
    serializador = SerializacaoXML(_fonte_dados, homologacao=config.HOMOLOGACAO)
    evento = EventoManifestacaoDest(
        cnpj=emp["cnpj"], chave=chave, data_emissao=datetime.now(),
        uf="AN", operacao=2, n_seq_evento=1,
    )
    xml = serializador.serializar_evento(evento)
    assinado = AssinaturaA1(emp["cert_path"], emp["cert_senha"]).assinar(xml)
    resp = _conexao(emp).evento(modelo=55, evento=assinado)

    raiz = etree.fromstring(resp.content)
    stats = raiz.xpath(".//*[local-name()='retEvento']//*[local-name()='cStat']") \
        or raiz.xpath(".//*[local-name()='cStat']")
    cstat = stats[-1].text if stats else "?"
    motivos = raiz.xpath(".//*[local-name()='xMotivo']")
    motivo = motivos[-1].text if motivos else ""
    if cstat in ("135", "136", "573"):  # registrado / duplicidade
        log(f"[{emp['cnpj']}]   Manifestação OK ({cstat}) {chave}")
        return True
    log(f"[{emp['cnpj']}]   Manifestação FALHOU ({cstat} {motivo}) {chave}")
    return False


def processar_fila_empresa(cnpj: str, chaves: list) -> dict:
    """Processa as chaves reivindicadas de UMA empresa (serial, com pausa —
    respeita o rate da SEFAZ por certificado)."""
    emp = store.get_empresa(cnpj, com_senha=True)
    resultado = {"cnpj": cnpj, "ok": 0, "falhas": 0, "puladas": 0}
    if not emp or not emp["ativo"]:
        for chave in chaves:
            store.concluir_manifestacao(cnpj, chave, ok=False, erro="empresa inativa/removida")
        resultado["falhas"] = len(chaves)
        return resultado
    for chave in chaves:
        # Dedup final: a completa pode ter chegado entre o enfileiramento e agora
        if store.ja_tem_completa(cnpj, chave) or store.manifestada(cnpj, chave):
            store.remover_da_fila(cnpj, chave)
            resultado["puladas"] += 1
            continue
        try:
            if manifestar_chave(emp, chave):
                store.marcar_manifestada(cnpj, chave)
                store.concluir_manifestacao(cnpj, chave, ok=True)
                resultado["ok"] += 1
            else:
                store.concluir_manifestacao(cnpj, chave, ok=False, erro="cStat de rejeição")
                resultado["falhas"] += 1
        except Exception as e:
            store.concluir_manifestacao(cnpj, chave, ok=False, erro=str(e))
            resultado["falhas"] += 1
        time.sleep(config.PAUSA_ENTRE_MANIFESTOS)
    return resultado
