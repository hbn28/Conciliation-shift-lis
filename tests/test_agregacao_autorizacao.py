"""Testes para a agregação por autorização usada em `/resultado` (janela de
datas + forma de pagamento) e em `/verificar-conciliados` (soma de valores +
janela de datas), garantindo que uma mesma autorização aparecendo em várias
parcelas seja combinada corretamente (nunca só "a primeira linha").

Usa o mesmo padrão de isolamento de banco de `test_http_smoke.py`: define
DATABASE_PATH antes de importar `app.main`, para não tocar o banco real.
"""

import os
import tempfile
from decimal import Decimal

_tmp_db_dir = tempfile.mkdtemp(prefix="conciliacao_test_db_agg_")
os.environ.setdefault("DATABASE_PATH", os.path.join(_tmp_db_dir, "test.db"))

import pandas as pd  # noqa: E402

from app.main import (  # noqa: E402
    _agregar_por_autorizacao,
    _construir_linhas_verificacao,
    _formatar_janela_datas,
    _montar_contexto_resultado,
    _status_e_conciliado,
)


def test_formatar_janela_datas_com_datas_iguais():
    assert _formatar_janela_datas(["2026-06-29", "2026-06-29"]) == "29/06"


def test_formatar_janela_datas_com_datas_diferentes():
    assert _formatar_janela_datas(["2026-07-02", "2026-06-29"]) == "29/06-02/07"


def test_formatar_janela_datas_vazia():
    assert _formatar_janela_datas([None, None]) == "—"


def test_agregar_por_autorizacao_diferencia_parcelas_por_vencimento():
    # A mesma autorização aparece em 2 linhas (parcelas 1 e 2), cada uma com
    # um vencimento diferente (parcelas mensais). O vencimento é o que
    # diferencia uma parcela da outra, então o agregado deve manter duas
    # entradas separadas — não deve combinar parcelas diferentes numa janela
    # só, senão marcar uma parcela como conciliada "conciliaria" a outra
    # parcela junto.
    conciliados = [
        {
            "shift_autorizacao_normalizado": "12345",
            "shift_data_emissao_normalizado": "2026-06-29",
            "shift_data_vencimento_normalizado": "2026-07-29",
            "data_venda_rede": "2026-06-29",
            "rede_data_vencimento_normalizado": "2026-07-29",
            "modalidade_shift": "Crédito",
            "parcela_shift": 1,
            "qtd_parcelas_shift": 2,
            "valor_bruto_shift": "100.00",
        },
        {
            "shift_autorizacao_normalizado": "12345",
            "shift_data_emissao_normalizado": "2026-07-29",
            "shift_data_vencimento_normalizado": "2026-08-29",
            "data_venda_rede": "2026-07-29",
            "rede_data_vencimento_normalizado": "2026-08-29",
            "modalidade_shift": "Crédito",
            "parcela_shift": 2,
            "qtd_parcelas_shift": 2,
            "valor_bruto_shift": "100.00",
        },
    ]
    agregado = _agregar_por_autorizacao(conciliados)
    assert set(agregado.keys()) == {
        ("12345", "2026-07-29", "100.00"), ("12345", "2026-08-29", "100.00"),
    }
    parcela_1 = agregado[("12345", "2026-07-29", "100.00")]
    assert parcela_1["quantidade_linhas"] == 1
    assert parcela_1["janela_emissao_shift"] == "29/06"
    assert parcela_1["janela_vencimento_shift"] == "29/07"
    assert parcela_1["forma_pagamento"] == "Crédito"
    assert parcela_1["valor_bruto_total"] == Decimal("100.00")
    parcela_2 = agregado[("12345", "2026-08-29", "100.00")]
    assert parcela_2["quantidade_linhas"] == 1
    assert parcela_2["janela_emissao_shift"] == "29/07"
    assert parcela_2["janela_vencimento_shift"] == "29/08"


def test_agregar_por_autorizacao_soma_valor_de_transacoes_divididas_no_shift():
    # Duas linhas com a MESMA autorização e o MESMO vencimento no Shift (ex.:
    # uma parcela da Rede dividida em duas transações no Shift) continuam
    # sendo combinadas numa só entrada, e os VALORES são somados — a
    # diferenciação em entradas separadas só ocorre entre vencimentos
    # diferentes (parcelas mensais distintas).
    conciliados = [
        {
            "shift_autorizacao_normalizado": "999",
            "shift_data_emissao_normalizado": "2026-06-29",
            "shift_data_vencimento_normalizado": "2026-07-29",
            "data_venda_rede": "2026-06-29",
            "rede_data_vencimento_normalizado": "2026-07-29",
            "modalidade_shift": "Crédito",
            "valor_bruto_shift": "30.00",
        },
        {
            "shift_autorizacao_normalizado": "999",
            "shift_data_emissao_normalizado": "2026-06-29",
            "shift_data_vencimento_normalizado": "2026-07-29",
            "data_venda_rede": "2026-06-29",
            "rede_data_vencimento_normalizado": "2026-07-29",
            "modalidade_shift": "Crédito",
            "valor_bruto_shift": "20.00",
        },
    ]
    agregado = _agregar_por_autorizacao(conciliados)
    assert set(agregado.keys()) == {("999", "2026-07-29", "50.00")}
    item = agregado[("999", "2026-07-29", "50.00")]
    assert item["quantidade_linhas"] == 2
    assert item["valor_bruto_total"] == Decimal("50.00")


def test_agregar_por_autorizacao_expoe_detalhes_por_lado():
    # Painel de detalhes (ícone "i" ao lado de copiar/marcar em /resultado):
    # precisa dos valores bruto/líquido de cada lado separadamente (não só
    # o valor_bruto_total combinado) e da parcela de cada lado.
    conciliados = [{
        "shift_autorizacao_normalizado": "456",
        "shift_data_vencimento_normalizado": "2026-07-29",
        "rede_data_vencimento_normalizado": "2026-07-29",
        "valor_bruto_shift": "100.00",
        "valor_bruto_rede": "99.99",
        "valor_liquido_shift": "95.00",
        "valor_liquido_rede": "94.99",
        "parcela_shift": "1",
        "parcela_rede": "1",
    }]
    agregado = _agregar_por_autorizacao(conciliados)
    item = list(agregado.values())[0]
    assert item["valor_bruto_shift_total"] == Decimal("100.00")
    assert item["valor_bruto_rede_total"] == Decimal("99.99")
    assert item["valor_liquido_shift_total"] == Decimal("95.00")
    assert item["valor_liquido_rede_total"] == Decimal("94.99")
    assert item["parcela_shift"] == "1"
    assert item["parcela_rede"] == "1"


def test_agregar_por_autorizacao_soma_valores_por_lado_quando_ha_mais_de_uma_linha():
    conciliados = [
        {
            "shift_autorizacao_normalizado": "789",
            "shift_data_vencimento_normalizado": "2026-07-29",
            "valor_bruto_shift": "30.00",
            "valor_liquido_shift": "29.00",
        },
        {
            "shift_autorizacao_normalizado": "789",
            "shift_data_vencimento_normalizado": "2026-07-29",
            "valor_bruto_shift": "20.00",
            "valor_liquido_shift": "19.00",
        },
    ]
    agregado = _agregar_por_autorizacao(conciliados)
    item = list(agregado.values())[0]
    assert item["valor_bruto_shift_total"] == Decimal("50.00")
    assert item["valor_liquido_shift_total"] == Decimal("48.00")
    # Nenhuma linha trouxe valor da Rede: fica None (exibido como "—"),
    # nunca vira Decimal("0") por engano.
    assert item["valor_bruto_rede_total"] is None
    assert item["valor_liquido_rede_total"] is None


def test_agregar_por_autorizacao_prioriza_vencimento_shift():
    # Quando Rede e Shift discordam no vencimento, o agrupamento usa o
    # vencimento do Shift (é o lado que o usuário acompanha na tela).
    conciliados = [{
        "shift_autorizacao_normalizado": "777",
        "shift_data_vencimento_normalizado": "2026-07-29",
        "rede_data_vencimento_normalizado": "2026-07-30",
        "valor_bruto_shift": "10.00",
    }]
    agregado = _agregar_por_autorizacao(conciliados)
    assert set(agregado.keys()) == {("777", "2026-07-29", "10.00")}


def _linhas_rede_654321():
    return pd.DataFrame([
        {
            "autorizacao": "654321", "data_venda": "2026-06-29",
            "data_vencimento": "2026-07-29", "valor_bruto": "50.00",
            "valor_liquido": "48.00", "bandeira": "Visa", "modalidade": "Crédito",
            "_arquivo_verificacao": "rede.xlsx",
        },
        {
            "autorizacao": "654321", "data_venda": "2026-07-29",
            "data_vencimento": "2026-08-29", "valor_bruto": "50.00",
            "valor_liquido": "48.00", "bandeira": "Visa", "modalidade": "Crédito",
            "_arquivo_verificacao": "rede.xlsx",
        },
    ])


def test_construir_linhas_verificacao_agrupa_mesma_autorizacao():
    # Mesmo cenário do mundo real citado pelo usuário: a mesma autorização
    # pode aparecer em mais de uma linha do arquivo da Rede (parcelas com
    # vencimentos diferentes). Só quando AS DUAS parcelas estão marcadas no
    # banco é que o status vira "JÁ CONCILIADA".
    rede = _linhas_rede_654321()
    linhas = _construir_linhas_verificacao(rede, parcelas_conciliadas={
        ("654321", "2026-07-29"), ("654321", "2026-08-29"),
    })
    assert len(linhas) == 1
    linha = linhas[0]
    assert linha["autorizacao"] == "654321"
    assert linha["quantidade_linhas"] == 2
    assert linha["valor_bruto"] == Decimal("100.00")
    assert linha["conciliada"] is True
    assert linha["status"] == "JÁ CONCILIADA"


def test_construir_linhas_verificacao_marca_parcial_quando_so_uma_parcela_conciliada():
    # Regressão do bug real: antes, marcar UMA parcela em /resultado fazia a
    # autorização inteira aparecer como "JÁ CONCILIADA" em
    # /verificar-conciliados, mesmo com a outra parcela pendente. Agora deve
    # aparecer como parcialmente conciliada, e "conciliada" deve ser False
    # (para não ser contada como concluída nos totais da tela).
    rede = _linhas_rede_654321()
    linhas = _construir_linhas_verificacao(rede, parcelas_conciliadas={
        ("654321", "2026-07-29"),
    })
    assert len(linhas) == 1
    linha = linhas[0]
    assert linha["conciliada"] is False
    assert linha["status"] == "PARCIALMENTE CONCILIADA (1/2 parcelas)"


def test_status_e_conciliado_inclui_divergencia_tolerada_de_2_centavos():
    # Regressão: parcelas com diferença de até R$ 0,02 (valor bruto ou
    # líquido) ficavam de fora dos cards de cópia e da marcação de
    # autorizações em /resultado, porque a checagem antiga comparava
    # `status_comparacao` contra uma lista fixa de strings exatas e não
    # incluía "CONCILIADO_COM_DIVERGENCIA_TOLERADA" nem combinações como
    # agrupamento + tolerância.
    assert _status_e_conciliado("CONCILIADO_COM_DIVERGENCIA_TOLERADA") is True
    assert _status_e_conciliado(
        "CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO + DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS"
    ) is True
    assert _status_e_conciliado("CONCILIADO") is True
    assert _status_e_conciliado("DIVERGENCIA_VALOR_BRUTO") is False
    # Divergência tolerada somada a uma divergência real não deve contar
    # como conciliada.
    assert _status_e_conciliado("DIVERGENCIA_PARCELA + DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS") is False


def test_montar_contexto_resultado_separa_somente_no_shift_da_tabela_principal():
    # As linhas "encontradas somente no Shift" (sem correspondência na Rede)
    # saem da tabela paginada principal de divergências e vão para a seção
    # separada `somente_no_shift`, fechada por padrão na tela — não devem
    # contar em `total_divergencias` nem ocupar espaço de página.
    detalhado = [
        {"status_comparacao": "DIVERGENCIA_VALOR_BRUTO", "shift_autorizacao_normalizado": "1"},
        {"status_comparacao": "NAO_ENCONTRADO_NA_REDE", "shift_autorizacao_normalizado": "2"},
        {"status_comparacao": "NAO_ENCONTRADO_NA_REDE", "shift_autorizacao_normalizado": "3"},
        {"status_comparacao": "CONCILIADO", "shift_autorizacao_normalizado": "4"},
    ]
    contexto = _montar_contexto_resultado(
        {"resumo": {}, "detalhado": detalhado, "qualidade_shift": []},
        conciliacao=None, arquivo_rede="", page_param="1",
    )
    assert contexto["total_divergencias"] == 1
    assert [row["shift_autorizacao_normalizado"] for row in contexto["divergencias"]] == ["1"]
    assert contexto["total_somente_no_shift"] == 2
    assert {row["shift_autorizacao_normalizado"] for row in contexto["somente_no_shift"]} == {"2", "3"}


def test_construir_linhas_verificacao_nao_conciliada():
    rede = _linhas_rede_654321()
    linhas = _construir_linhas_verificacao(rede, parcelas_conciliadas=set())
    assert linhas[0]["conciliada"] is False
    assert linhas[0]["status"] == "NÃO CONCILIADA"
