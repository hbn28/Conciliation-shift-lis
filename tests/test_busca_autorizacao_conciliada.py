"""Regressão: a busca rápida em /verificar-conciliados/busca deve usar a
mesma normalização de autorização usada ao gravar/marcar (normalize_authorization,
que faz zfill(6)), e não uma normalização mais fraca que deixa o valor sem
zero à esquerda. Antes da correção, marcar "8586" (gravado como "008586") e
buscar digitando "8586" retornava "NÃO CONCILIADA" incorretamente.

Usa o mesmo banco SQLite isolado criado em test_http_smoke.py (DATABASE_PATH
já definido via env antes do import de app.main).
"""

import os
import tempfile

_tmp_db_dir = tempfile.mkdtemp(prefix="conciliacao_test_db_")
os.environ.setdefault("DATABASE_PATH", os.path.join(_tmp_db_dir, "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.bootstrap.container import container  # noqa: E402
from app.domain.entities import ReconciliationSave  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

_RESULT_ID = "b" * 32


def _preparar_conciliacao_existente():
    unit_id = container.units.create("XX", "Unidade Teste Busca", "00000000", "EMPRESA TESTE")
    container.reconciliation_repository.database.save(ReconciliationSave(
        id=_RESULT_ID,
        unidade_id=unit_id,
        data_conciliacao="2026-07-15",
        resumo={
            "total_linhas_rede": 1,
            "total_linhas_shift": 1,
            "autorizacoes_conciliadas": [],
        },
        arquivos_importados=[],
    ))


def test_busca_autorizacao_sem_zero_a_esquerda_encontra_marcacao_canonica():
    _preparar_conciliacao_existente()
    # Simula a marcação como já gravada pelo fluxo de "/resultado/{id}",
    # que grava a autorização já no formato canônico de 6 dígitos.
    container.history.set_authorization_mark(_RESULT_ID, "008586", True)

    response = client.get("/verificar-conciliados/busca", params={"autorizacao": "8586"})

    assert response.status_code == 200
    body = response.json()
    assert body["autorizacao"] == "008586"
    assert body["conciliada"] is True
    # A busca sem vencimento não afirma "JÁ CONCILIADA" de forma binária
    # (a marcação é por parcela) — reporta quantas parcelas distintas dessa
    # autorização já foram marcadas.
    assert "1 parcela conciliada" 