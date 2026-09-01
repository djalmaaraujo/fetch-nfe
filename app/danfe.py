# -*- coding: utf-8 -*-
"""Documentos auxiliares em PDF (DANFE/DACTE/DAMDFE) a partir do XML autorizado
pela SEFAZ, via brazilfiscalreport. NFC-e (modelo 65) não é suportada pela lib."""
from brazilfiscalreport.danfe import Danfe
from brazilfiscalreport.dacte import Dacte
from brazilfiscalreport.damdfe import Damdfe

_GERADORES = {"danfe": Danfe, "dacte": Dacte, "damdfe": Damdfe}


class ErroGeracao(Exception):
    pass


def gerar_pdf(tipo: str, xml: bytes) -> bytes:
    """Renderiza o XML autorizado (nfeProc/cteProc/mdfeProc, ou a tag raiz sem
    o envelope de protocolo) no PDF do documento auxiliar correspondente."""
    classe = _GERADORES[tipo]
    try:
        documento = classe(xml=xml)
    except Exception as e:
        raise ErroGeracao(str(e)) from e
    return bytes(documento.output())
