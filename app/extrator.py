# -*- coding: utf-8 -*-
"""Extração de campos de busca a partir do XML da NF-e.

Roda no momento do download (e no backfill): transforma o XML em colunas
indexadas (notas), itens, duplicatas e texto pro full-text (FTS5) — a busca
nunca faz parse de XML.
"""
import os

from lxml import etree

from . import store


def _t(raiz, caminho: str):
    """Texto do primeiro elemento no caminho relativo (ignora namespaces)."""
    expr = "." + "".join(f"/*[local-name()='{p}']" for p in caminho.split("/"))
    r = raiz.xpath(expr + "/text()")
    return r[0].strip() if r and r[0] else None


def _num(valor, conv=float):
    try:
        return conv(valor)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# NF-e completa (nfeProc/procNFe)
# --------------------------------------------------------------------------- #
def extrair_nfe(raiz) -> dict:
    inf = raiz.xpath(".//*[local-name()='infNFe']")
    if not inf:
        return {}
    inf = inf[0]

    campos = {
        "emit_cnpj": _t(inf, "emit/CNPJ") or _t(inf, "emit/CPF"),
        "emit_nome": _t(inf, "emit/xNome"),
        "emit_uf": _t(inf, "emit/enderEmit/UF"),
        "dest_cnpj": _t(inf, "dest/CNPJ") or _t(inf, "dest/CPF"),
        "dest_nome": _t(inf, "dest/xNome"),
        "dest_uf": _t(inf, "dest/enderDest/UF"),
        "nat_op": _t(inf, "ide/natOp"),
        "nnf": _num(_t(inf, "ide/nNF"), int),
        "serie": _t(inf, "ide/serie"),
        "tp_nf": _num(_t(inf, "ide/tpNF"), int),
        "valor_num": _num(_t(inf, "total/ICMSTot/vNF")),
    }

    itens = []
    for det in inf.xpath("./*[local-name()='det']"):
        itens.append({
            "n_item": _num(det.get("nItem"), int) or len(itens) + 1,
            "cprod": _t(det, "prod/cProd"),
            "xprod": _t(det, "prod/xProd"),
            "ncm": _t(det, "prod/NCM"),
            "cfop": _t(det, "prod/CFOP"),
            "cean": _t(det, "prod/cEAN"),
            "ucom": _t(det, "prod/uCom"),
            "qcom": _num(_t(det, "prod/qCom")),
            "vprod": _num(_t(det, "prod/vProd")),
        })

    duplicatas = []
    for dup in inf.xpath(".//*[local-name()='cobr']/*[local-name()='dup']"):
        duplicatas.append({
            "ndup": _t(dup, "nDup"),
            "dvenc": _t(dup, "dVenc"),
            "vdup": _num(_t(dup, "vDup")),
        })

    # Texto agregado pro full-text (nomes, natureza, produtos, complementos)
    partes = [
        campos["emit_nome"], _t(inf, "emit/xFant"), _t(inf, "emit/enderEmit/xMun"),
        campos["dest_nome"], _t(inf, "dest/enderDest/xMun"),
        campos["nat_op"], _t(inf, "infAdic/infCpl"),
    ]
    for det in inf.xpath("./*[local-name()='det']"):
        partes.append(_t(det, "prod/xProd"))
        partes.append(_t(det, "infAdProd"))
    texto = " ".join(p for p in partes if p)

    return {"campos": campos, "itens": itens, "duplicatas": duplicatas, "texto": texto}


# --------------------------------------------------------------------------- #
# Resumo (resNFe) — poucos campos, mas já dá pra filtrar
# --------------------------------------------------------------------------- #
def extrair_res(raiz) -> dict:
    campos = {
        "emit_cnpj": _t(raiz, "CNPJ") or _t(raiz, "CPF"),
        "emit_nome": _t(raiz, "xNome"),
        "tp_nf": _num(_t(raiz, "tpNF"), int),
        "valor_num": _num(_t(raiz, "vNF")),
    }
    return {"campos": campos, "itens": [], "duplicatas": [],
            "texto": campos["emit_nome"] or ""}


def extrair_e_indexar(cnpj: str, chave: str, raiz, tipo: str) -> None:
    ext = extrair_nfe(raiz) if tipo == "completa" else extrair_res(raiz)
    if ext:
        store.indexar_nota(cnpj, chave, ext)


# --------------------------------------------------------------------------- #
# JSON estruturado da nota (pra agentes não terem que ler XML)
# --------------------------------------------------------------------------- #
def xml_para_dict(el):
    """Converte a árvore XML em dict aninhado (sem namespaces, sem Signature).
    Elementos repetidos (ex.: det, dup) viram listas."""
    filhos = [f for f in el if isinstance(f.tag, str)
              and etree.QName(f).localname != "Signature"]
    if not filhos:
        return el.text.strip() if el.text and el.text.strip() else None
    d = {}
    for f in filhos:
        nome = etree.QName(f).localname
        v = xml_para_dict(f)
        if nome in d:
            if not isinstance(d[nome], list):
                d[nome] = [d[nome]]
            d[nome].append(v)
        else:
            d[nome] = v
    for k, v in el.attrib.items():
        d[f"@{k}"] = v
    return d


# --------------------------------------------------------------------------- #
# Backfill: indexa notas já baixadas (novas colunas vazias) a partir do disco
# --------------------------------------------------------------------------- #
def backfill(forcar: bool = False) -> int:
    from .nfe import log
    pendentes = store.notas_sem_indexacao(forcar)
    if not pendentes:
        return 0
    log(f"Indexação: processando {len(pendentes)} nota(s)...")
    feitas = 0
    for n in pendentes:
        try:
            if not n["caminho"] or not os.path.exists(n["caminho"]):
                continue
            raiz = etree.parse(n["caminho"]).getroot()
            extrair_e_indexar(n["cnpj_empresa"], n["chave"], raiz, n["tipo"])
            feitas += 1
        except Exception as e:
            log(f"  Erro ao indexar {n['chave']}: {e}")
    log(f"Indexação concluída: {feitas} nota(s).")
    return feitas
