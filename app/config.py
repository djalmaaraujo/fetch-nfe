# -*- coding: utf-8 -*-
"""Configuração central lida do ambiente (.env)."""
import os


def _somente_numeros(valor: str) -> str:
    return "".join(c for c in (valor or "") if c.isdigit())


CNPJ = _somente_numeros(os.getenv("CNPJ", ""))
UF = os.getenv("UF", "").strip().upper()
CERT_PATH = os.getenv("CERT_PATH", "/certs/certificado.pfx")
CERT_SENHA = os.getenv("CERT_SENHA", "")
AMBIENTE = os.getenv("AMBIENTE", "producao").strip().lower()  # producao | homologacao
HOMOLOGACAO = AMBIENTE == "homologacao"

MANIFESTAR = os.getenv("MANIFESTAR", "1").strip() == "1"
DEDUP = os.getenv("DEDUP", "1").strip() == "1"

INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "3600"))  # usado pela rotina agendada

DATA_DIR = os.getenv("DATA_DIR", "/data/fiscal")
STATE_DIR = os.getenv("STATE_DIR", "/state")
DB_PATH = os.path.join(STATE_DIR, "notas.db")
LOCK_PATH = os.path.join(STATE_DIR, "sync.lock")

# Limites de segurança por sincronização
MAX_ITERACOES = 200
PAUSA_ENTRE_CHAMADAS = 1.5
PAUSA_ENTRE_MANIFESTOS = 1.0


def problemas() -> list:
    """Retorna lista de problemas de configuração (vazio = ok)."""
    faltando = []
    if not CNPJ:
        faltando.append("CNPJ")
    if not UF:
        faltando.append("UF")
    if not CERT_SENHA:
        faltando.append("CERT_SENHA")
    if not os.path.exists(CERT_PATH):
        faltando.append(f"certificado em {CERT_PATH}")
    return faltando
