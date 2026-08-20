# -*- coding: utf-8 -*-
"""Configuração global lida do ambiente (.env).

A partir da versão multi-empresa, CNPJ/UF/certificado são cadastrados via API e
vivem no banco. As variáveis CNPJ/UF/CERT_PATH/CERT_SENHA do .env viram apenas
um *seed*: se existirem e a empresa ainda não estiver cadastrada, ela é criada
automaticamente na subida (migração suave do modo single-empresa).
"""
import os


def somente_numeros(valor: str) -> str:
    return "".join(c for c in (valor or "") if c.isdigit())


AMBIENTE = os.getenv("AMBIENTE", "producao").strip().lower()  # producao | homologacao
HOMOLOGACAO = AMBIENTE == "homologacao"

DEDUP = os.getenv("DEDUP", "1").strip() == "1"
MANIFESTAR_PADRAO = os.getenv("MANIFESTAR", "1").strip() == "1"  # default p/ novas empresas

INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS", "3600"))     # ciclo do scheduler
FILA_INTERVALO = int(os.getenv("FILA_INTERVALO_SEGUNDOS", "30"))  # varredura da fila
SYNC_WORKERS = int(os.getenv("SYNC_WORKERS", "4"))           # empresas sincronizadas em paralelo
FILA_WORKERS = int(os.getenv("FILA_WORKERS", "4"))           # empresas manifestando em paralelo
FILA_MAX_TENTATIVAS = int(os.getenv("FILA_MAX_TENTATIVAS", "5"))

DATA_DIR = os.getenv("DATA_DIR", "/data/fiscal")
STATE_DIR = os.getenv("STATE_DIR", "/state")
CERTS_DIR = os.getenv("CERTS_DIR", "/certs")
DB_PATH = os.path.join(STATE_DIR, "notas.db")
LOCKS_DIR = os.path.join(STATE_DIR, "locks")

# Limites de segurança por sincronização (por empresa)
MAX_ITERACOES = 200
PAUSA_ENTRE_CHAMADAS = 1.5
PAUSA_ENTRE_MANIFESTOS = 1.0

# Seed opcional (modo single-empresa antigo)
SEED_CNPJ = somente_numeros(os.getenv("CNPJ", ""))
SEED_UF = os.getenv("UF", "").strip().upper()
SEED_CERT_PATH = os.getenv("CERT_PATH", "")
SEED_CERT_SENHA = os.getenv("CERT_SENHA", "")
