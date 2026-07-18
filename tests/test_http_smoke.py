"""Testes de fumaça (smoke) via HTTP para as rotas principais.

Cobrem apenas requisições GET, sem efeitos colaterais (não fazem upload nem
gravam dados). Hoje nenhuma rota tinha cobertura de teste (ver auditoria).

Usa um banco SQLite isolado em diretório temporário (via DATABASE_PATH),
definido ANTES do import de app.main — o container é montado na importação
do módulo e não pode apontar para o banco real de produção durante os
testes.
"""

import os
import tempfile

_tmp_db_dir = tempfile.mkdtemp(prefix="conciliacao_test_db_")
os.environ.setdefault("DATABASE_PATH", os.path.join(_tmp_db_dir, "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.main import app  # noqa: E402

os.environ["APP_USERS"] = f"teste:{hash_password('senha-teste')}"

client = TestClient(app)
client.post("/login", data={"usuario": "teste", "senha": "senha-teste", "next": "/"})


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_form():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_unidades_lista():
    response = client.get("/unidades")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_historico_lista():
    response = client.get("/historico")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_verificar_conciliados_form():
    response = client.get("/verificar-conciliados")
    assert response.status_code == 200
    assert "Verificar conciliados" in response.text


def test_resultado_inexistente_retorna_404():
    response = client.get("/resultado/" + "0" * 32)
    assert response.status_code == 404


def test_resultado_id_invalido_retorna_404():
    response = client.get("/resultado/id-invalido")
    assert response.status_code == 404
