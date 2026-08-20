# -*- coding: utf-8 -*-
"""Cadastro de empresas: valida o certificado A1, extrai CNPJ/validade do próprio
.pfx, enriquece com dados públicos da BrasilAPI e persiste no banco."""
import contextlib
import os
import re
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from . import config, store

# Fontes de dados públicos de CNPJ, em ordem de preferência. Ambas gratuitas,
# sem chave, e com os mesmos nomes de campos (razao_social/nome_fantasia/uf/
# municipio). A minhareceita (open-source) cobre o rate-limit da BrasilAPI.
FONTES_CNPJ = [
    ("brasilapi", "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"),
    ("minhareceita", "https://minhareceita.org/{cnpj}"),
]


class ErroCadastro(Exception):
    pass


def inspecionar_pfx(conteudo: bytes, senha: str) -> dict:
    """Abre o .pfx (valida a senha) e extrai CNPJ e validade do certificado."""
    try:
        _, cert, _ = pkcs12.load_key_and_certificates(conteudo, senha.encode())
    except Exception as e:
        raise ErroCadastro(f"não foi possível abrir o certificado (senha errada?): {e}")
    if cert is None:
        raise ErroCadastro("o arquivo não contém um certificado")

    validade = cert.not_valid_after_utc
    if validade < datetime.now(timezone.utc):
        raise ErroCadastro(f"certificado vencido em {validade.date().isoformat()}")

    # e-CNPJ ICP-Brasil: o CN costuma ser "RAZAO SOCIAL:CNPJ"
    cnpj = None
    cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if cns:
        m = re.search(r":(\d{14})$", cns[0].value.strip())
        if m:
            cnpj = m.group(1)
    return {
        "cnpj": cnpj,
        "validade": validade.date().isoformat(),
        "titular": cns[0].value if cns else None,
    }


def consultar_dados_publicos(cnpj: str) -> dict:
    """Dados públicos da empresa, tentando as fontes em ordem (fallback quando
    uma estiver fora ou rate-limitada). Best-effort: falhou tudo, retorna {}."""
    for nome, url in FONTES_CNPJ:
        try:
            r = requests.get(url.format(cnpj=cnpj), timeout=15)
            if r.status_code != 200:
                continue  # 429/erro nesta fonte → tenta a próxima
            d = r.json()
            dados = {
                "razao_social": d.get("razao_social"),
                "nome_fantasia": d.get("nome_fantasia"),
                "uf": (d.get("uf") or "").upper(),
                "municipio": d.get("municipio"),
            }
            if dados["razao_social"] or dados["uf"]:
                return dados
        except (requests.RequestException, ValueError):
            continue
    return {}


# Compatibilidade com o nome antigo
consultar_brasilapi = consultar_dados_publicos


def cadastrar(conteudo_pfx: bytes, senha: str, cnpj: str = None,
              uf: str = None, manifestar: bool = None) -> dict:
    """Fluxo completo de cadastro. Retorna a empresa persistida (sem senha)."""
    info = inspecionar_pfx(conteudo_pfx, senha)

    cnpj = config.somente_numeros(cnpj or info["cnpj"] or "")
    if len(cnpj) != 14:
        raise ErroCadastro(
            "não consegui extrair o CNPJ do certificado; informe o campo 'cnpj'"
        )
    if info["cnpj"] and cnpj != info["cnpj"]:
        # Mesma raiz (8 primeiros dígitos) é permitido: a SEFAZ aceita certificado
        # da matriz para consultar filiais (e vice-versa)
        if cnpj[:8] != info["cnpj"][:8]:
            raise ErroCadastro(
                f"CNPJ informado ({cnpj}) é de outra raiz que o do certificado "
                f"({info['cnpj']}) — a SEFAZ só aceita certificado da mesma raiz"
            )

    # Enriquecimento: BrasilAPI, com fallback pro que já temos no banco
    publico = consultar_brasilapi(cnpj)
    existente = store.get_empresa(cnpj) or {}
    uf_final = (uf or publico.get("uf") or existente.get("uf") or "").upper()
    if len(uf_final) != 2:
        raise ErroCadastro(
            "não consegui determinar a UF (BrasilAPI indisponível); informe o campo 'uf'"
        )
    for campo in ("razao_social", "nome_fantasia", "municipio"):
        publico.setdefault(campo, None)
        publico[campo] = publico[campo] or existente.get(campo)

    # Persiste o .pfx em CERTS_DIR/<cnpj>.pfx com permissão restrita.
    # Se já existe com o mesmo conteúdo, não regrava (o worker monta /certs ro).
    os.makedirs(config.CERTS_DIR, exist_ok=True)
    cert_path = os.path.join(config.CERTS_DIR, f"{cnpj}.pfx")
    ja_igual = False
    if os.path.exists(cert_path):
        with open(cert_path, "rb") as f:
            ja_igual = f.read() == conteudo_pfx
    if not ja_igual:
        with open(cert_path, "wb") as f:
            f.write(conteudo_pfx)
        with contextlib.suppress(OSError):
            os.chmod(cert_path, 0o600)

    store.upsert_empresa({
        "cnpj": cnpj,
        "razao_social": publico.get("razao_social"),
        "nome_fantasia": publico.get("nome_fantasia"),
        "uf": uf_final,
        "municipio": publico.get("municipio"),
        "cert_path": cert_path,
        "cert_senha": senha,
        "cert_validade": info["validade"],
        "manifestar": int(config.MANIFESTAR_PADRAO if manifestar is None else manifestar),
    })
    return store.get_empresa(cnpj)


def seed_do_env() -> None:
    """Migração suave: se o .env ainda tem CNPJ/CERT do modo single-empresa e a
    empresa não está no banco, cadastra automaticamente (aproveitando o NSU legado)."""
    c = config
    if not (c.SEED_CNPJ and c.SEED_CERT_PATH and c.SEED_CERT_SENHA):
        return
    if store.get_empresa(c.SEED_CNPJ):
        return
    if not os.path.exists(c.SEED_CERT_PATH):
        return
    with open(c.SEED_CERT_PATH, "rb") as f:
        conteudo = f.read()
    try:
        empresa = cadastrar(conteudo, c.SEED_CERT_SENHA, cnpj=c.SEED_CNPJ, uf=c.SEED_UF or None)
    except ErroCadastro as e:
        print(f"[seed] falha ao cadastrar empresa do .env: {e}", flush=True)
        return
    # NSU legado do modo single-empresa (tabela estado)
    with store._conn() as conn:
        r = conn.execute("SELECT valor FROM estado WHERE chave='ultimo_nsu'").fetchone()
        if r:
            conn.execute(
                "UPDATE empresas SET ultimo_nsu=? WHERE cnpj=? AND ultimo_nsu=0",
                (int(r["valor"]), c.SEED_CNPJ),
            )
            conn.execute("DELETE FROM estado WHERE chave='ultimo_nsu'")
    print(f"[seed] empresa {empresa['cnpj']} cadastrada a partir do .env", flush=True)
