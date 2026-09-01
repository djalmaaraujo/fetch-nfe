import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _xml_bytes(nome: str) -> bytes:
    return (FIXTURES / nome).read_bytes()


@pytest.mark.parametrize(
    "rota, arquivo",
    [
        ("/danfe", "nfe_test_1.xml"),
        ("/dacte", "dacte_test_1.xml"),
        ("/damdfe", "mdfe_test_1.xml"),
    ],
)
def test_gera_pdf_via_upload(client, rota, arquivo):
    xml = _xml_bytes(arquivo)
    resp = client.post(rota, files={"arquivo": (arquivo, xml, "application/xml")})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:5] == b"%PDF-"


def test_danfe_via_base64(client):
    xml = _xml_bytes("nfe_test_1.xml")
    resp = client.post("/danfe", data={"xml_base64": base64.b64encode(xml).decode()})
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"


def test_danfe_sem_arquivo_nem_base64(client):
    resp = client.post("/danfe")
    assert resp.status_code == 422


def test_danfe_base64_invalido(client):
    resp = client.post("/danfe", data={"xml_base64": "não é base64 válido!!"})
    assert resp.status_code == 422


def test_danfe_xml_malformado(client):
    resp = client.post("/danfe", files={"arquivo": ("x.xml", b"isto nao e xml", "application/xml")})
    assert resp.status_code == 422
