"""Testes do login por usuário (sem banco de dados).

Usa um TestClient próprio (não compartilha cookies com os outros testes) para
poder testar tanto o caminho não autenticado quanto o autenticado.
"""

import os
import tempfile

_tmp_db_dir = tempfile.mkdtemp(prefix="conciliacao_test_db_auth_")
os.environ.setdefault("DATABASE_PATH", os.path.join(_tmp_db_dir, "test_auth.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import hash_password, verify_password  # noqa: E402
from app.main import app  # noqa: E402

# NOTA: outros arquivos de teste também sobrescrevem `APP_USERS` no import do
# módulo (todos os módulos são importados na coleta, antes de qualquer teste
# rodar), então cada teste aqui usa `monkeypatch.setenv` para garantir seu
# próprio valor de `APP_USERS` no momento da chamada a /login, em vez de
# depender do valor global que "sobrou" da última importação.


def test_hash_and_verify_password_roundtrip():
    hashed = hash_password("minha-senha")
    assert verify_password("minha-senha", hashed)
    assert not verify_password("senha-errada", hashed)


def test_rota_protegida_sem_login_redireciona_para_login():
    client = TestClient(app, follow_redirects=False)
    response = client.get("/unidades")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_com_credenciais_corretas_da_acesso(monkeypatch):
    monkeypatch.setenv("APP_USERS", f"operador:{hash_password('senha-correta')}")
    client = TestClient(app)
    response = client.post(
        "/login",
        data={"usuario": "operador", "senha": "senha-correta", "next": "/unidades"},
    )
    assert response.status_code == 200
    assert "Unidades" in response.text


def test_login_com_senha_errada_nao_autentica(monkeypatch):
    monkeypatch.setenv("APP_USERS", f"operador:{hash_password('senha-correta')}")
    client = TestClient(app, follow_redirects=False)
    response = client.post(
        "/login", data={"usuario": "operador", "senha": "senha-errada", "next": "/"},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    # não deve ter criado sessão válida
    protegida = client.get("/unidades", follow_redirects=False)
    assert protegida.status_code == 303


def test_logout_limpa_sessao(monkeypatch):
    monkeypatch.setenv("APP_USERS", f"operador:{hash_password('senha-correta')}")
    client = TestClient(app)
    client.post(
        "/login", data={"usuario": "operador", "senha": "senha-correta", "next": "/"},
    )
    assert client.get("/unidades").status_code == 200
    client.post("/logout")
    resposta = client.get("/unidades", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"].startswith("/login")


def test_health_nao_exige_login():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200


def test_paginas_autenticadas_nao_podem_ser_cacheadas(monkeypatch):
    """Regressão: sem Cache-Control, um proxy/CDN no caminho pode guardar a
    resposta autenticada e servi-la depois pra quem não tem sessão válida,
    pulando o login inteiro. Isso foi observado em produção (Railway): uma
    aba anônima via a tela já logada, sem nunca ter autenticado."""
    monkeypatch.setenv("APP_USERS", f"operador:{hash_password('senha-correta')}")
    client = TestClient(app)
    client.post(
        "/login", data={"usuario": "operador", "senha": "senha-correta", "next": "/"},
    )
    resposta = client.get("/unidades")
    assert "no-store" in resposta.headers["cache-control"]


def test_pagina_de_login_tambem_nao_pode_ser_cacheada():
    client = TestClient(app)
    resposta = client.get("/login")
    assert "no-store" in resposta.headers["cache-control"]


def test_arquivo_estatico_pode_ser_cacheado():
    client = TestClient(app)
    resposta = client.get("/static/style.css")
    assert resposta.status_code == 200
    assert "no-store" not in resposta.headers.get("cache-control", "")
