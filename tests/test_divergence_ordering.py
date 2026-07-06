from app.application.divergence_ordering import sort_divergences


def test_divergencias_ficam_agrupadas_por_tipo_e_impacto():
    rows = [
        {"status_comparacao": "NAO_ENCONTRADO_NO_SHIFT", "valor_operacao_em_revisao": 10},
        {"status_comparacao": "DIVERGENCIA_DATA", "valor_operacao_em_revisao": 20},
        {"status_comparacao": "NAO_ENCONTRADO_NO_SHIFT", "valor_operacao_em_revisao": 30},
        {"status_comparacao": "DIVERGENCIA_DATA", "valor_operacao_em_revisao": 50},
        {"status_comparacao": "DIVERGENCIA_VALOR_BRUTO", "impacto_financeiro_confirmado": 2},
    ]
    ordered = sort_divergences(rows)
    groups = [row["grupo_divergencia"] for row in ordered]
    assert groups == [
        "Divergências de valor bruto",
        "Divergências de data",
        "Divergências de data",
        "Encontradas somente na Rede",
        "Encontradas somente na Rede",
    ]
    assert ordered[1]["valor_operacao_em_revisao"] == 50
    assert ordered[3]["valor_operacao_em_revisao"] == 30
