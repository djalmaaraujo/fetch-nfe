# -*- coding: utf-8 -*-
"""Índice SQLite das notas: dedup por chave, estado (NSU) e consulta por data."""
import contextlib
import fcntl
import os
import sqlite3
from datetime import datetime, timezone

from . import config


def _conn() -> sqlite3.Connection:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS notas (
                   chave       TEXT PRIMARY KEY,
                   tipo        TEXT,            -- completa | resumo
                   dh_emi      TEXT,
                   data_emi    TEXT,            -- AAAA-MM-DD (índice por data)
                   nsu         INTEGER,
                   emitente    TEXT,
                   valor       TEXT,
                   caminho     TEXT,
                   manifestada INTEGER DEFAULT 0,
                   baixado_em  TEXT
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS ix_data ON notas(data_emi)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS estado (chave TEXT PRIMARY KEY, valor TEXT)"
        )


# --------------------------------------------------------------------------- #
# Estado (NSU, última sincronização)
# --------------------------------------------------------------------------- #
def get_estado(chave: str, padrao=None):
    with _conn() as c:
        r = c.execute("SELECT valor FROM estado WHERE chave=?", (chave,)).fetchone()
        return r["valor"] if r else padrao


def set_estado(chave: str, valor) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO estado(chave,valor) VALUES(?,?) "
            "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
            (chave, str(valor)),
        )


def ultimo_nsu() -> int:
    try:
        return int(get_estado("ultimo_nsu", "0"))
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# Notas (dedup + upsert)
# --------------------------------------------------------------------------- #
def ja_tem_completa(chave: str) -> bool:
    with _conn() as c:
        r = c.execute(
            "SELECT tipo FROM notas WHERE chave=? AND tipo='completa'", (chave,)
        ).fetchone()
        return r is not None


def manifestada(chave: str) -> bool:
    with _conn() as c:
        r = c.execute(
            "SELECT manifestada FROM notas WHERE chave=? AND manifestada=1", (chave,)
        ).fetchone()
        return r is not None


def marcar_manifestada(chave: str) -> None:
    with _conn() as c:
        c.execute("UPDATE notas SET manifestada=1 WHERE chave=?", (chave,))


def upsert_nota(chave, tipo, dh_emi, nsu, emitente, valor, caminho) -> None:
    """Insere ou atualiza. 'completa' sempre sobrepõe 'resumo' da mesma chave."""
    data_emi = (dh_emi or "")[:10]
    agora = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with _conn() as c:
        atual = c.execute("SELECT tipo FROM notas WHERE chave=?", (chave,)).fetchone()
        if atual and atual["tipo"] == "completa" and tipo == "resumo":
            return  # não rebaixa uma completa para resumo
        c.execute(
            """INSERT INTO notas(chave,tipo,dh_emi,data_emi,nsu,emitente,valor,caminho,baixado_em)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(chave) DO UPDATE SET
                   tipo=excluded.tipo, dh_emi=excluded.dh_emi, data_emi=excluded.data_emi,
                   nsu=excluded.nsu, emitente=excluded.emitente, valor=excluded.valor,
                   caminho=excluded.caminho, baixado_em=excluded.baixado_em""",
            (chave, tipo, dh_emi, data_emi, nsu, emitente, valor, caminho, agora),
        )


def notas_periodo(de: str = None, ate: str = None, tipo: str = None) -> list:
    q = "SELECT chave,tipo,dh_emi,data_emi,nsu,emitente,valor,caminho,manifestada,baixado_em FROM notas WHERE 1=1"
    p = []
    if de:
        q += " AND data_emi >= ?"; p.append(de)
    if ate:
        q += " AND data_emi <= ?"; p.append(ate)
    if tipo:
        q += " AND tipo = ?"; p.append(tipo)
    q += " ORDER BY dh_emi"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, p).fetchall()]


def caminho_da_chave(chave: str):
    with _conn() as c:
        r = c.execute("SELECT caminho FROM notas WHERE chave=?", (chave,)).fetchone()
        return r["caminho"] if r else None


def contagens() -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM notas").fetchone()["n"]
        completas = c.execute(
            "SELECT COUNT(*) n FROM notas WHERE tipo='completa'"
        ).fetchone()["n"]
        resumos = c.execute(
            "SELECT COUNT(*) n FROM notas WHERE tipo='resumo'"
        ).fetchone()["n"]
    return {"total": total, "completas": completas, "resumos": resumos}


# --------------------------------------------------------------------------- #
# Lock entre processos (API x rotina) para não bater na SEFAZ em paralelo
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def trava_sync(bloqueante: bool = False):
    """Context manager. Levanta BlockingIOError se já houver sync em andamento
    (quando bloqueante=False)."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    f = open(config.LOCK_PATH, "w")
    try:
        flags = fcntl.LOCK_EX if bloqueante else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, flags)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def sync_em_andamento() -> bool:
    f = open(config.LOCK_PATH, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        f.close()
