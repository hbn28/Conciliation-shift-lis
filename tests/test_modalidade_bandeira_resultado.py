import os
import tempfile

_db_dir = tempfile.mkdtemp(prefix="conciliacao_test_modalidade_bandeira_")
os.environ.setdefault("DATABASE_PATH", os.path.join(_db_dir, "test.db"))

from app.main import _montar_contexto_resultado  # noqa: E402


def _linha(status, autorizacao, modalidade, bandeira):
    return {
        "status_comparacao": status,
        "shift_autorizacao_normalizado": autorizacao,
        "rede_autorizacao_normalizado": autorizacao,
        "modalidade_shift": modalidade,
        "bandeira_shift": bandeira,
        "valor_bruto_shift": "10.00",
    }


def test_modalidade_bandeira_filtra_autorizacoes_e_divergencias():
    data = {
        "detalhado": [
            _linha("CONCILIADO", "111", "CREDITO", "VISA"),
            _linha("CONCILIADO", "222", "DEBITO", "MASTERCARD"),
            _linha("DIVERGENCIA_VALOR_BRUTO", "333", "CREDITO", "VISA"),
            _linha("DIVERGENCIA_VALOR_BRUTO", "444", "DEBITO", "MASTERCARD"),
        ],
        "resumo": {},
        "qualidade_shift": [],
        "auditoria": [],
        "descartes": [],
    }

    contexto = _montar_contexto_resultado(
        data, None, "", "1", filtros={"modalidade_bandeira": "CREDITO|VISA"},
    )

    assert contexto["total_divergencias"] == 1
    assert contexto["divergencias"][0]["shift_autorizacao_normalizado"] == "333"
    assert [chave[0] for chave in contexto["autorizacoes_conciliadas"]] == ["111"]
    assert set(contexto["modalidades_bandeiras_disponiveis"]) == {
        "CREDITO|VISA", "DEBITO|MASTERCARD",
    }
