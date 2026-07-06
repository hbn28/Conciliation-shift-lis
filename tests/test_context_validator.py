from datetime import date

import pandas as pd
import pytest

from app.adapters.outbound.spreadsheets.context_validator import (
    filter_shift_empresa,
    validate_rede_context,
    validate_shift_empresa,
)


def _frame():
    return pd.DataFrame({
        "data_recebimento": [date(2025, 12, 29)],
        "estabelecimento": ["69269769"],
        "nome_estabelecimento": ["CENTRALLAB JZ"],
    })


def test_context_matches_unit_and_date():
    context = validate_rede_context(_frame(), "2025-12-29", "69269769")
    assert context["estabelecimentos"] == ["69269769"]


def test_context_rejects_wrong_date():
    with pytest.raises(ValueError, match="não corresponde"):
        validate_rede_context(_frame(), "2025-12-30", "69269769")


def test_context_rejects_wrong_establishment():
    with pytest.raises(ValueError, match="estabelecimento"):
        validate_rede_context(_frame(), "2025-12-29", "999")


def test_validate_shift_empresa_sem_divergencia():
    df_shift = pd.DataFrame({"empresa": ["CENTRALLAB (Jn)", "CENTRALLAB (Jn)"]})
    assert validate_shift_empresa(df_shift, "CENTRALLAB (Jn)") == []


def test_validate_shift_empresa_gera_alerta_quando_diverge():
    df_shift = pd.DataFrame({"empresa": ["OUTRA UNIDADE (Jn)"]})
    alertas = validate_shift_empresa(df_shift, "CENTRALLAB (Jn)")
    assert len(alertas) == 1
    assert "CENTRALLAB (Jn)" in alertas[0]
    assert "OUTRA UNIDADE (Jn)" in alertas[0]


def test_validate_shift_empresa_nao_bloqueia_sem_cadastro():
    df_shift = pd.DataFrame({"empresa": ["QUALQUER (Jn)"]})
    # Unidade sem "empresa_shift" cadastrado: não há o que conferir, sem alerta.
    assert validate_shift_empresa(df_shift, None) == []


def test_validate_shift_empresa_sem_coluna_nao_quebra():
    df_shift = pd.DataFrame({"autorizacao": ["123456"]})
    assert validate_shift_empresa(df_shift, "CENTRALLAB (Jn)") == []


def test_validate_shift_empresa_ignora_maiusculas_e_espacos():
    df_shift = pd.DataFrame({"empresa": ["  centrallab (jn)  "]})
    assert validate_shift_empresa(df_shift, "CENTRALLAB (Jn)") == []


def test_filter_shift_empresa_restringe_relatorio_multiempresa():
    frame = pd.DataFrame({
        "empresa": ["CENTRALLAB (Jn)", "CENTRALLAB (Cz)", "CENTRALLAB (Cz)"],
        "autorizacao": ["1", "2", "3"],
    })
    filtered, stats = filter_shift_empresa(frame, " centrallab (cz) ")
    assert filtered["autorizacao"].tolist() == ["2", "3"]
    assert stats["antes_filtro_empresa"] == 3
    assert stats["apos_filtro_empresa"] == 2
    assert len(stats["descartes"]) == 1
    assert stats["descartes"][0]["motivo_descarte"] == "EMPRESA_FORA_DO_RECORTE"


def test_empresa_tem_prioridade_sobre_credor_devedor():
    frame = pd.DataFrame({
        "empresa": ["MATRIZ", "MATRIZ"],
        "descricao_credor_devedor": ["CENTRALLAB (Jn)", "CENTRALLAB (Cz)"],
        "autorizacao": ["1", "2"],
    })
    with pytest.raises(ValueError, match='coluna "Empresa"'):
        filter_shift_empresa(frame, "CENTRALLAB (Cz)")
