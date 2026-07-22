from decimal import Decimal

import pandas as pd

from app.domain.impacto_financeiro import calcular_impacto_financeiro, resumo_impacto_financeiro


def _row(status, **overrides):
    base = {
        "status_conciliacao": status,
        "status_comparacao": status,
        "valor_bruto_shift": None,
        "valor_bruto_rede": None,
        "valor_liquido_shift": None,
        "valor_liquido_rede": None,
        "motivo": "motivo teste",
        "acao_recomendada": "acao teste",
        "criterio_match": "CRITERIO_TESTE",
        "nivel_confianca": None,
    }
    base.update(overrides)
    return base


def test_nao_encontrado_na_rede_usa_valor_liquido_shift_como_impacto():
    row = _row(
        "NAO_ENCONTRADO_NA_REDE",
        valor_bruto_shift=Decimal("100.00"),
        valor_liquido_shift=Decimal("95.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("95.00")
    assert result["valor_operacao_em_revisao"] == Decimal("0")


def test_nao_encontrado_na_rede_fallback_valor_bruto_quando_sem_liquido():
    row = _row("NAO_ENCONTRADO_NA_REDE", valor_bruto_shift=Decimal("100.00"))
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("100.00")


def test_nao_encontrado_no_shift_usa_valor_liquido_rede():
    row = _row(
        "NAO_ENCONTRADO_NO_SHIFT",
        valor_bruto_rede=Decimal("50.00"),
        valor_liquido_rede=Decimal("48.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("48.00")
    assert result["valor_operacao_em_revisao"] == Decimal("0")


def test_divergencia_valor_liquido_usa_diferenca_absoluta():
    row = _row(
        "DIVERGENCIA_VALOR_LIQUIDO",
        valor_liquido_shift=Decimal("100.00"),
        valor_liquido_rede=Decimal("90.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("10.00")
    assert result["diferenca_valor_liquido"] == Decimal("10.00")


def test_divergencia_valor_bruto_usa_diferenca_absoluta():
    row = _row(
        "DIVERGENCIA_VALOR_BRUTO",
        valor_bruto_shift=Decimal("100.00"),
        valor_bruto_rede=Decimal("80.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("20.00")
    assert result["diferenca_valor_bruto"] == Decimal("20.00")


def test_divergencia_data_com_valores_iguais_impacto_zero():
    row = _row(
        "DIVERGENCIA_DATA",
        valor_bruto_shift=Decimal("100.00"),
        valor_bruto_rede=Decimal("100.00"),
        valor_liquido_shift=Decimal("95.00"),
        valor_liquido_rede=Decimal("95.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("0")
    # Valor da operação continua visível mesmo com impacto zero.
    assert result["valor_operacao_shift"] == Decimal("95.00")
    assert result["valor_operacao_rede"] == Decimal("95.00")


def test_divergencia_data_com_valor_tambem_divergente_conta_como_valor():
    row = _row(
        "DIVERGENCIA_DATA",
        valor_liquido_shift=Decimal("100.00"),
        valor_liquido_rede=Decimal("90.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("10.00")


def test_revisar_autorizacao_divergente_alta_confianca_vai_para_revisao():
    row = _row(
        "REVISAR_AUTORIZACAO_DIVERGENTE_ALTA_CONFIANCA",
        valor_bruto_shift=Decimal("77.00"),
        valor_liquido_shift=Decimal("75.19"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("0")
    assert result["valor_operacao_em_revisao"] == Decimal("75.19")


def test_conciliado_por_agrupamento_shift_sem_divergencia_impacto_zero():
    row = _row(
        "CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO",
        valor_bruto_shift=Decimal("69.00"),
        valor_bruto_rede=Decimal("69.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("0")


def test_agrupamento_shift_ambiguo_soma_valores_envolvidos_em_revisao():
    row = _row(
        "AGRUPAMENTO_OS_AMBIGUO",
        valor_liquido_shift=Decimal("30.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("0")
    assert result["valor_operacao_em_revisao"] == Decimal("30.00")


def test_ambiguo_sem_autorizacao_compativel_soma_shift_e_rede_em_revisao():
    row = _row(
        "AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL",
        valor_liquido_shift=Decimal("50.00"),
        valor_liquido_rede=Decimal("48.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("0")
    assert result["valor_operacao_em_revisao"] == Decimal("98.00")


def test_impacto_nunca_conta_valor_em_revisao_como_confirmado():
    row = _row(
        "REVISAR_AUTORIZACAO_DIVERGENTE_ALTA_CONFIANCA",
        valor_liquido_shift=Decimal("1000.00"),
    )
    result = calcular_impacto_financeiro(row)
    assert result["impacto_financeiro_confirmado"] == Decimal("0")
    assert result["valor_operacao_em_revisao"] == Decimal("1000.00")
    # confirmado e em_revisao nunca se somam nem se confundem
    assert result["impacto_financeiro_confirmado"] != result["valor_operacao_em_revisao"]


def test_campos_ausentes_viram_none_sem_quebrar():
    row = _row("CONCILIADO")
    result = calcular_impacto_financeiro(row)
    assert result["valor_bruto_shift"] is None
    assert result["diferenca_valor_bruto"] is None
    assert result["impacto_financeiro_confirmado"] == Decimal("0")


def test_resumo_soma_divergencias_toleradas_conciliadas():
    detalhado = pd.DataFrame([
        calcular_impacto_financeiro(_row(
            "CONCILIADO_COM_DIVERGENCIA_TOLERADA + DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS",
            valor_bruto_shift=Decimal("100.00"),
            valor_bruto_rede=Decimal("99.98"),
        )),
        calcular_impacto_financeiro(_row(
            "CONCILIADO_COM_DIVERGENCIA_TOLERADA + DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS",
            valor_bruto_shift=Decimal("100.00"),
            valor_bruto_rede=Decimal("100.01"),
        )),
        calcular_impacto_financeiro(_row(
            "CONCILIADO",
            valor_bruto_shift=Decimal("50.00"),
            valor_bruto_rede=Decimal("50.00"),
        )),
    ])
    resumo = resumo_impacto_financeiro(detalhado)
    # As duas linhas toleradas somam -0,02 + 0,01 = -0,01; a linha CONCILIADO
    # comum também entra na soma (é conciliada), mas como Rede e Shift têm o
    # mesmo valor, sua diferença é 0 e não altera o total.
    assert resumo["somatorio_divergencias_toleradas_conciliadas"] == Decimal("-0.01")


def test_resumo_soma_todas_as_conciliadas_rede_credito_shift_debito():
    # Regra pedida: para linhas conciliadas, Rede entra como crédito
    # (positivo) e Shift como débito (negativo) — inclui qualquer status
    # "CONCILIADO*", não só as com diferença tolerada.
    detalhado = pd.DataFrame([
        calcular_impacto_financeiro(_row(
            "CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO + DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS",
            valor_bruto_shift=Decimal("200.00"),
            valor_bruto_rede=Decimal("199.99"),
        )),
        calcular_impacto_financeiro(_row(
            "DIVERGENCIA_VALOR_BRUTO",
            valor_bruto_shift=Decimal("100.00"),
            valor_bruto_rede=Decimal("90.00"),
        )),
    ])
    resumo = resumo_impacto_financeiro(detalhado)
    # A linha CONCILIADO_POR_AGRUPAMENTO entra (rede - shift = -0.01); a
    # DIVERGENCIA_VALOR_BRUTO não é conciliada, então fica de fora da soma.
    assert resumo["somatorio_divergencias_toleradas_conciliadas"] == Decimal("-0.01")
