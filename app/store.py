# -*- coding: utf-8 -*-
"""Banco SQLite: empresas, notas (dedup por empresa), fila de manifestação e locks.

Concorrência: WAL + busy timeout aguentam bem API + workers no mesmo arquivo.
O lock de sincronização é POR EMPRESA (arquivo em locks/), então várias empresas
sincronizam em paralelo sem nunca duplicar consulta pro mesmo CNPJ.
"""
import contextlib
import fcntl
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config


def _agora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS empresas (
                   cnpj          TEXT PRIMARY KEY,
                   razao_social  TEXT,
                   nome_fantasia TEXT,
                   uf            TEXT NOT NULL,
                   municipio     TEXT,
                   cert_path     TEXT NOT NULL,
                   cert_senha    TEXT NOT NULL,
                   cert_validade TEXT,
                   manifestar    INTEGER DEFAULT 1,
                   ativo         INTEGER DEFAULT 1,
                   ultimo_nsu    INTEGER DEFAULT 0,
                   ultima_sincronizacao TEXT,
                   ultimo_resultado     TEXT,
                   criado_em     TEXT
               )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS fila_manifestacao (
                   id           INTEGER PRIMARY KEY AUTOINCREMENT,
                   cnpj_empresa TEXT NOT NULL,
                   chave        TEXT NOT NULL,
                   status       TEXT DEFAULT 'pendente',  -- pendente|processando|ok|erro
                   tentativas   INTEGER DEFAULT 0,
                   ultimo_erro  TEXT,
                   criado_em    TEXT,
                   atualizado_em TEXT,
                   UNIQUE (cnpj_empresa, chave)
               )"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS estado (chave TEXT PRIMARY KEY, valor TEXT)")
    # Migra o esquema single-empresa (se houver) ANTES de criar a tabela nova
    _migrar_v1()
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS notas (
                   cnpj_empresa TEXT NOT NULL,
                   chave       TEXT NOT NULL,
                   tipo        TEXT,            -- completa | resumo
                   dh_emi      TEXT,
                   data_emi    TEXT,            -- AAAA-MM-DD
                   nsu         INTEGER,
                   emitente    TEXT,
                   valor       TEXT,
                   caminho     TEXT,
                   manifestada INTEGER DEFAULT 0,
                   baixado_em  TEXT,
                   PRIMARY KEY (cnpj_empresa, chave)
               )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS ix_notas_data ON notas(cnpj_empresa, data_emi)")
    _migrar_v3()
    # Banco guarda senha de certificado: restringe leitura ao dono
    with contextlib.suppress(OSError):
        os.chmod(config.DB_PATH, 0o600)


# --------------------------------------------------------------------------- #
# Migração do esquema single-empresa (v1): notas sem cnpj_empresa
# --------------------------------------------------------------------------- #
def _migrar_v1() -> None:
    with _conn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(notas)")]
        if not cols or "cnpj_empresa" in cols:
            return  # banco novo, ou já está no esquema v2
    cnpj = config.SEED_CNPJ
    if not cnpj:
        raise RuntimeError(
            "Banco no formato antigo (single-empresa) e sem CNPJ no .env para migrar."
        )
    with _conn() as c:
        c.execute("ALTER TABLE notas RENAME TO notas_v1")
        c.execute(
            """CREATE TABLE notas (
                   cnpj_empresa TEXT NOT NULL, chave TEXT NOT NULL, tipo TEXT,
                   dh_emi TEXT, data_emi TEXT, nsu INTEGER, emitente TEXT,
                   valor TEXT, caminho TEXT, manifestada INTEGER DEFAULT 0,
                   baixado_em TEXT, PRIMARY KEY (cnpj_empresa, chave))"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS ix_notas_data ON notas(cnpj_empresa, data_emi)")
        c.execute(
            """INSERT INTO notas
               SELECT ?, chave, tipo, dh_emi, data_emi, nsu, emitente, valor,
                      REPLACE(caminho, ?, ?), manifestada, baixado_em
               FROM notas_v1""",
            (cnpj, config.DATA_DIR + "/", f"{config.DATA_DIR}/{cnpj}/"),
        )
        c.execute("DROP TABLE notas_v1")
    # Move os arquivos no disco: /data/fiscal/<data|_*>/ -> /data/fiscal/<cnpj>/...
    destino_raiz = os.path.join(config.DATA_DIR, cnpj)
    os.makedirs(destino_raiz, exist_ok=True)
    for nome in os.listdir(config.DATA_DIR):
        origem = os.path.join(config.DATA_DIR, nome)
        if nome == cnpj or not os.path.isdir(origem):
            continue
        os.rename(origem, os.path.join(destino_raiz, nome))


# --------------------------------------------------------------------------- #
# Migração v3: campos de busca extraídos do XML + itens + duplicatas + FTS5
# --------------------------------------------------------------------------- #
FTS_OK = False

_COLUNAS_V3 = [
    ("emit_cnpj", "TEXT"), ("emit_nome", "TEXT"), ("emit_uf", "TEXT"),
    ("dest_cnpj", "TEXT"), ("dest_nome", "TEXT"), ("dest_uf", "TEXT"),
    ("nat_op", "TEXT"), ("nnf", "INTEGER"), ("serie", "TEXT"),
    ("tp_nf", "INTEGER"), ("valor_num", "REAL"),
]


def _migrar_v3() -> None:
    global FTS_OK
    with _conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(notas)")}
        for nome, tipo in _COLUNAS_V3:
            if nome not in cols:
                c.execute(f"ALTER TABLE notas ADD COLUMN {nome} {tipo}")
        c.execute("CREATE INDEX IF NOT EXISTS ix_notas_emit ON notas(emit_cnpj)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_notas_valor ON notas(valor_num)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS itens (
                   cnpj_empresa TEXT NOT NULL, chave TEXT NOT NULL,
                   n_item INTEGER, cprod TEXT, xprod TEXT, ncm TEXT, cfop TEXT,
                   cean TEXT, ucom TEXT, qcom REAL, vprod REAL)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS ix_itens_nota ON itens(cnpj_empresa, chave)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_itens_ncm ON itens(ncm)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_itens_cfop ON itens(cfop)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS duplicatas (
                   cnpj_empresa TEXT NOT NULL, chave TEXT NOT NULL,
                   ndup TEXT, dvenc TEXT, vdup REAL)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS ix_dup_nota ON duplicatas(cnpj_empresa, chave)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_dup_venc ON duplicatas(dvenc)")
        try:
            c.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS notas_fts USING fts5(
                       cnpj_empresa UNINDEXED, chave UNINDEXED, texto,
                       tokenize='unicode61 remove_diacritics 2')"""
            )
            FTS_OK = True
        except sqlite3.OperationalError:
            FTS_OK = False  # SQLite sem FTS5: busca `q` cai pra LIKE


def indexar_nota(cnpj: str, chave: str, ext: dict) -> None:
    """Grava os campos extraídos do XML (colunas, itens, duplicatas, FTS)."""
    campos = {k: v for k, v in ext.get("campos", {}).items() if v is not None}
    with _conn() as c:
        if campos:
            frag = ", ".join(f"{k}=?" for k in campos)
            c.execute(
                f"UPDATE notas SET {frag} WHERE cnpj_empresa=? AND chave=?",
                (*campos.values(), cnpj, chave),
            )
        c.execute("DELETE FROM itens WHERE cnpj_empresa=? AND chave=?", (cnpj, chave))
        for i in ext.get("itens", []):
            c.execute(
                """INSERT INTO itens(cnpj_empresa,chave,n_item,cprod,xprod,ncm,
                                     cfop,cean,ucom,qcom,vprod)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (cnpj, chave, i["n_item"], i["cprod"], i["xprod"], i["ncm"],
                 i["cfop"], i["cean"], i["ucom"], i["qcom"], i["vprod"]),
            )
        c.execute("DELETE FROM duplicatas WHERE cnpj_empresa=? AND chave=?", (cnpj, chave))
        for d in ext.get("duplicatas", []):
            c.execute(
                "INSERT INTO duplicatas(cnpj_empresa,chave,ndup,dvenc,vdup) VALUES(?,?,?,?,?)",
                (cnpj, chave, d["ndup"], d["dvenc"], d["vdup"]),
            )
        if FTS_OK and ext.get("texto"):
            c.execute("DELETE FROM notas_fts WHERE chave=? AND cnpj_empresa=?", (chave, cnpj))
            c.execute(
                "INSERT INTO notas_fts(cnpj_empresa,chave,texto) VALUES(?,?,?)",
                (cnpj, chave, ext["texto"]),
            )


def notas_sem_indexacao(forcar: bool = False) -> list:
    """Notas com XML no disco e campos de busca ainda vazios (ou todas, se forcar)."""
    q = "SELECT cnpj_empresa, chave, tipo, caminho FROM notas"
    if not forcar:
        q += " WHERE emit_cnpj IS NULL"
    with _conn() as c:
        return [dict(r) for r in c.execute(q).fetchall()]


def _termos_fts(q: str) -> str:
    """Sanitiza a consulta do usuário pro MATCH do FTS5 (cada termo entre aspas,
    AND implícito)."""
    import re as _re
    termos = _re.findall(r"\w+", q, _re.UNICODE)
    return " ".join(f'"{t}"' for t in termos)


def buscar_notas(f: dict, limite: int = 50, offset: int = 0,
                 ordenar: str = "data", ordem: str = "desc") -> dict:
    """Busca com filtros combináveis. Filtros de item (produto/ncm/cfop/cean) e
    de vencimento usam EXISTS nas tabelas satélites; `q` usa FTS5."""
    cond, p = ["1=1"], []

    def add(sql, *vals):
        cond.append(sql)
        p.extend(vals)

    if f.get("cnpj"):
        add("n.cnpj_empresa=?", f["cnpj"])
    if f.get("de"):
        add("n.data_emi>=?", f["de"])
    if f.get("ate"):
        add("n.data_emi<=?", f["ate"])
    if f.get("tipo"):
        add("n.tipo=?", f["tipo"])
    if f.get("manifestada") is not None:
        add("n.manifestada=?", int(f["manifestada"]))
    if f.get("emitente"):
        e = f["emitente"]
        so_dig = "".join(ch for ch in e if ch.isdigit())
        if len(so_dig) == 14:
            add("n.emit_cnpj=?", so_dig)
        else:
            add("n.emit_nome LIKE ?", f"%{e}%")
    if f.get("destinatario"):
        d = f["destinatario"]
        so_dig = "".join(ch for ch in d if ch.isdigit())
        if len(so_dig) == 14:
            add("n.dest_cnpj=?", so_dig)
        else:
            add("n.dest_nome LIKE ?", f"%{d}%")
    if f.get("uf"):
        add("n.emit_uf=?", f["uf"].upper())
    if f.get("nnf") is not None:
        add("n.nnf=?", int(f["nnf"]))
    if f.get("serie"):
        add("n.serie=?", str(f["serie"]))
    if f.get("natop"):
        add("n.nat_op LIKE ?", f"%{f['natop']}%")
    if f.get("tp_nf") is not None:
        add("n.tp_nf=?", int(f["tp_nf"]))
    if f.get("valor_min") is not None:
        add("n.valor_num>=?", float(f["valor_min"]))
    if f.get("valor_max") is not None:
        add("n.valor_num<=?", float(f["valor_max"]))

    _it = ("EXISTS (SELECT 1 FROM itens i WHERE i.cnpj_empresa=n.cnpj_empresa "
           "AND i.chave=n.chave AND {})")
    if f.get("produto"):
        add(_it.format("i.xprod LIKE ?"), f"%{f['produto']}%")
    if f.get("ncm"):
        add(_it.format("i.ncm LIKE ?"), f["ncm"] + "%")   # prefixo: capítulo/posição
    if f.get("cfop"):
        add(_it.format("i.cfop=?"), str(f["cfop"]))
    if f.get("cean"):
        add(_it.format("i.cean=?"), str(f["cean"]))

    if f.get("venc_de") or f.get("venc_ate"):
        sub, sp = [], []
        if f.get("venc_de"):
            sub.append("d.dvenc>=?"); sp.append(f["venc_de"])
        if f.get("venc_ate"):
            sub.append("d.dvenc<=?"); sp.append(f["venc_ate"])
        add("EXISTS (SELECT 1 FROM duplicatas d WHERE d.cnpj_empresa=n.cnpj_empresa "
            "AND d.chave=n.chave AND " + " AND ".join(sub) + ")", *sp)

    if f.get("q"):
        if FTS_OK:
            add("n.chave IN (SELECT chave FROM notas_fts WHERE notas_fts MATCH ?)",
                _termos_fts(f["q"]))
        else:  # fallback sem FTS5
            add("(n.emit_nome LIKE ? OR n.nat_op LIKE ? OR "
                + _it.format("i.xprod LIKE ?") + ")",
                f"%{f['q']}%", f"%{f['q']}%", f"%{f['q']}%")

    col = {"data": "n.dh_emi", "valor": "n.valor_num"}.get(ordenar, "n.dh_emi")
    direcao = "ASC" if str(ordem).lower() == "asc" else "DESC"
    where = " AND ".join(cond)

    with _conn() as c:
        total = c.execute(f"SELECT COUNT(*) t FROM notas n WHERE {where}", p).fetchone()["t"]
        linhas = c.execute(
            f"SELECT n.* FROM notas n WHERE {where} "
            f"ORDER BY {col} {direcao} LIMIT ? OFFSET ?",
            (*p, limite, offset),
        ).fetchall()
    return {"total": total, "limite": limite, "offset": offset,
            "notas": [dict(r) for r in linhas]}


def itens_da_nota(cnpj: str, chave: str) -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT n_item,cprod,xprod,ncm,cfop,cean,ucom,qcom,vprod "
            "FROM itens WHERE cnpj_empresa=? AND chave=? ORDER BY n_item",
            (cnpj, chave)).fetchall()]


# --------------------------------------------------------------------------- #
# Empresas
# --------------------------------------------------------------------------- #
_CAMPOS_EMPRESA_PUBLICOS = (
    "cnpj, razao_social, nome_fantasia, uf, municipio, cert_path, cert_validade, "
    "manifestar, ativo, ultimo_nsu, ultima_sincronizacao, ultimo_resultado, criado_em"
)


def upsert_empresa(dados: dict) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO empresas
               (cnpj, razao_social, nome_fantasia, uf, municipio, cert_path,
                cert_senha, cert_validade, manifestar, ativo, ultimo_nsu, criado_em)
               VALUES (:cnpj, :razao_social, :nome_fantasia, :uf, :municipio,
                       :cert_path, :cert_senha, :cert_validade, :manifestar, 1,
                       COALESCE(:ultimo_nsu, 0), :criado_em)
               ON CONFLICT(cnpj) DO UPDATE SET
                   razao_social=excluded.razao_social,
                   nome_fantasia=excluded.nome_fantasia,
                   uf=excluded.uf, municipio=excluded.municipio,
                   cert_path=excluded.cert_path, cert_senha=excluded.cert_senha,
                   cert_validade=excluded.cert_validade,
                   manifestar=excluded.manifestar, ativo=1""",
            {"criado_em": _agora(), "ultimo_nsu": None, **dados},
        )


def get_empresa(cnpj: str, com_senha: bool = False):
    campos = _CAMPOS_EMPRESA_PUBLICOS + (", cert_senha" if com_senha else "")
    with _conn() as c:
        r = c.execute(f"SELECT {campos} FROM empresas WHERE cnpj=?", (cnpj,)).fetchone()
        return dict(r) if r else None


def listar_empresas(somente_ativas: bool = False) -> list:
    q = f"SELECT {_CAMPOS_EMPRESA_PUBLICOS} FROM empresas"
    if somente_ativas:
        q += " WHERE ativo=1"
    with _conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY cnpj").fetchall()]


def atualizar_empresa(cnpj: str, campos: dict) -> bool:
    permitidos = {"manifestar", "ativo", "cert_senha", "uf"}
    sets = {k: v for k, v in campos.items() if k in permitidos}
    if not sets:
        return False
    frag = ", ".join(f"{k}=?" for k in sets)
    with _conn() as c:
        cur = c.execute(f"UPDATE empresas SET {frag} WHERE cnpj=?", (*sets.values(), cnpj))
        return cur.rowcount > 0


def set_ultimo_nsu(cnpj: str, nsu: int) -> None:
    with _conn() as c:
        c.execute("UPDATE empresas SET ultimo_nsu=? WHERE cnpj=?", (int(nsu), cnpj))


def registrar_sincronizacao(cnpj: str, resultado: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE empresas SET ultima_sincronizacao=?, ultimo_resultado=? WHERE cnpj=?",
            (_agora(), resultado, cnpj),
        )


# --------------------------------------------------------------------------- #
# Estado genérico (chave/valor)
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


# --------------------------------------------------------------------------- #
# Rotina (scheduler): configuração em runtime, sem restart
# --------------------------------------------------------------------------- #
def rotina_config() -> dict:
    """Config efetiva da rotina. Default: ativa, intervalo do .env."""
    ativa = get_estado("rotina_ativa")
    intervalo = get_estado("rotina_intervalo")
    return {
        "ativa": True if ativa is None else ativa == "1",
        "intervalo_segundos": int(intervalo) if intervalo else max(config.INTERVALO, 60),
    }


def set_rotina_config(ativa=None, intervalo_segundos=None) -> None:
    if ativa is not None:
        set_estado("rotina_ativa", "1" if ativa else "0")
    if intervalo_segundos is not None:
        set_estado("rotina_intervalo", int(intervalo_segundos))


def registrar_execucao_rotina(inicio_ts: float, duracao: float) -> None:
    set_estado("rotina_ultima_ts", inicio_ts)
    set_estado("rotina_ultima_execucao",
               datetime.fromtimestamp(inicio_ts).astimezone().isoformat(timespec="seconds"))
    set_estado("rotina_ultima_duracao", round(duracao, 1))


def rotina_status() -> dict:
    import time as _time
    cfg = rotina_config()
    ultima_ts = get_estado("rotina_ultima_ts")
    duracao = get_estado("rotina_ultima_duracao")
    proxima = None
    if cfg["ativa"]:
        base = float(ultima_ts) if ultima_ts else 0.0
        proxima_ts = max(base + cfg["intervalo_segundos"], _time.time())
        proxima = datetime.fromtimestamp(proxima_ts).astimezone().isoformat(timespec="seconds")
    return {
        **cfg,
        "ultima_execucao": get_estado("rotina_ultima_execucao"),
        "ultima_duracao_segundos": float(duracao) if duracao else None,
        "proxima_execucao": proxima,
    }


# --------------------------------------------------------------------------- #
# Notas (dedup por empresa)
# --------------------------------------------------------------------------- #
def ja_tem_completa(cnpj: str, chave: str) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM notas WHERE cnpj_empresa=? AND chave=? AND tipo='completa'",
            (cnpj, chave),
        ).fetchone() is not None


def manifestada(cnpj: str, chave: str) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM notas WHERE cnpj_empresa=? AND chave=? AND manifestada=1",
            (cnpj, chave),
        ).fetchone() is not None


def marcar_manifestada(cnpj: str, chave: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE notas SET manifestada=1 WHERE cnpj_empresa=? AND chave=?", (cnpj, chave)
        )


def upsert_nota(cnpj, chave, tipo, dh_emi, nsu, emitente, valor, caminho) -> None:
    """Insere/atualiza. 'completa' nunca é rebaixada para 'resumo'."""
    data_emi = (dh_emi or "")[:10]
    with _conn() as c:
        atual = c.execute(
            "SELECT tipo FROM notas WHERE cnpj_empresa=? AND chave=?", (cnpj, chave)
        ).fetchone()
        if atual and atual["tipo"] == "completa" and tipo == "resumo":
            return
        c.execute(
            """INSERT INTO notas(cnpj_empresa,chave,tipo,dh_emi,data_emi,nsu,
                                 emitente,valor,caminho,baixado_em)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cnpj_empresa,chave) DO UPDATE SET
                   tipo=excluded.tipo, dh_emi=excluded.dh_emi,
                   data_emi=excluded.data_emi, nsu=excluded.nsu,
                   emitente=excluded.emitente, valor=excluded.valor,
                   caminho=excluded.caminho, baixado_em=excluded.baixado_em""",
            (cnpj, chave, tipo, dh_emi, data_emi, nsu, emitente, valor, caminho, _agora()),
        )


def notas_periodo(cnpj=None, de=None, ate=None, tipo=None) -> list:
    q = ("SELECT cnpj_empresa,chave,tipo,dh_emi,data_emi,nsu,emitente,valor,"
         "caminho,manifestada,baixado_em FROM notas WHERE 1=1")
    p = []
    if cnpj:
        q += " AND cnpj_empresa=?"; p.append(cnpj)
    if de:
        q += " AND data_emi >= ?"; p.append(de)
    if ate:
        q += " AND data_emi <= ?"; p.append(ate)
    if tipo:
        q += " AND tipo = ?"; p.append(tipo)
    q += " ORDER BY dh_emi"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, p).fetchall()]


def caminho_da_chave(chave: str, cnpj: str = None):
    q = "SELECT caminho FROM notas WHERE chave=?"
    p = [chave]
    if cnpj:
        q += " AND cnpj_empresa=?"; p.append(cnpj)
    with _conn() as c:
        r = c.execute(q, p).fetchone()
        return r["caminho"] if r else None


def contagens(cnpj: str = None) -> dict:
    filtro, p = ("WHERE cnpj_empresa=?", [cnpj]) if cnpj else ("", [])
    with _conn() as c:
        linha = c.execute(
            f"""SELECT COUNT(*) total,
                       SUM(tipo='completa') completas,
                       SUM(tipo='resumo') resumos
                FROM notas {filtro}""", p
        ).fetchone()
    return {k: linha[k] or 0 for k in ("total", "completas", "resumos")}


# --------------------------------------------------------------------------- #
# Fila de manifestação
# --------------------------------------------------------------------------- #
def enfileirar_manifestacao(cnpj: str, chave: str) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO fila_manifestacao(cnpj_empresa,chave,criado_em,atualizado_em)
               VALUES(?,?,?,?) ON CONFLICT(cnpj_empresa,chave) DO NOTHING""",
            (cnpj, chave, _agora(), _agora()),
        )


def reivindicar_pendentes() -> dict:
    """Marca como 'processando' os itens elegíveis e retorna {cnpj: [chaves]}.
    Elegível = pendente cujo backoff exponencial (2^tentativas min) já venceu."""
    agora = datetime.now(timezone.utc).astimezone()
    por_empresa: dict = {}
    with _conn() as c:
        linhas = c.execute(
            """SELECT id, cnpj_empresa, chave, tentativas, atualizado_em
               FROM fila_manifestacao WHERE status='pendente' ORDER BY id"""
        ).fetchall()
        for r in linhas:
            try:
                base = datetime.fromisoformat(r["atualizado_em"])
            except (TypeError, ValueError):
                base = agora - timedelta(days=1)
            if r["tentativas"] > 0 and agora < base + timedelta(minutes=2 ** r["tentativas"]):
                continue  # ainda em backoff
            c.execute(
                "UPDATE fila_manifestacao SET status='processando', atualizado_em=? WHERE id=?",
                (_agora(), r["id"]),
            )
            por_empresa.setdefault(r["cnpj_empresa"], []).append(r["chave"])
    return por_empresa


def concluir_manifestacao(cnpj: str, chave: str, ok: bool, erro: str = None) -> None:
    with _conn() as c:
        if ok:
            c.execute(
                """UPDATE fila_manifestacao SET status='ok', ultimo_erro=NULL,
                       atualizado_em=? WHERE cnpj_empresa=? AND chave=?""",
                (_agora(), cnpj, chave),
            )
        else:
            c.execute(
                f"""UPDATE fila_manifestacao SET
                        tentativas=tentativas+1, ultimo_erro=?, atualizado_em=?,
                        status=CASE WHEN tentativas+1 >= {config.FILA_MAX_TENTATIVAS}
                                    THEN 'erro' ELSE 'pendente' END
                    WHERE cnpj_empresa=? AND chave=?""",
                ((erro or "")[:500], _agora(), cnpj, chave),
            )


def remover_da_fila(cnpj: str, chave: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM fila_manifestacao WHERE cnpj_empresa=? AND chave=?", (cnpj, chave)
        )


def fila_status() -> list:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT cnpj_empresa, status, COUNT(*) itens
               FROM fila_manifestacao GROUP BY cnpj_empresa, status
               ORDER BY cnpj_empresa, status"""
        ).fetchall()]


def resetar_processando_orfaos() -> None:
    """Na subida do worker: devolve pra fila itens presos em 'processando'
    (processo anterior morreu no meio)."""
    with _conn() as c:
        c.execute(
            "UPDATE fila_manifestacao SET status='pendente' WHERE status='processando'"
        )


# --------------------------------------------------------------------------- #
# Lock POR EMPRESA entre processos (API x workers)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def trava_sync(cnpj: str, bloqueante: bool = False):
    os.makedirs(config.LOCKS_DIR, exist_ok=True)
    f = open(os.path.join(config.LOCKS_DIR, f"{cnpj}.lock"), "w")
    try:
        flags = fcntl.LOCK_EX if bloqueante else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, flags)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def sync_em_andamento(cnpj: str) -> bool:
    os.makedirs(config.LOCKS_DIR, exist_ok=True)
    f = open(os.path.join(config.LOCKS_DIR, f"{cnpj}.lock"), "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        f.close()
