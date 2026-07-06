from decimal import Decimal

import pandas as pd

from app.adapters.outbound.spreadsheets.matcher import compare_rede_shift
from app.adapters.outbound.spreadsheets.normalizer import normalize_dataframe


def _row(auth, nsu, value, installment="1", total="1"):
    return {
        "numero da autorizacao": auth,
        "nsu": nsu,
        "valor bruto": value,
        "parcela": installment,
        "numero de parcelas": total,
        "estabelecimento": "10",
        "bandeira": "Visa",
        "modalidade": "Crédito",
        "status": "paga",
    }


def _compare(rede_rows, shift_rows):
    rede_source = pd.DataFrame(rede_rows)
    shift_source = pd.DataFrame(shift_rows)
    rede = normalize_dataframe(rede_source, "rede")
    shift = normalize_dataframe(shift_source, "shift")
    if "__arquivo_origem" in rede_source.columns:
        rede["rede_arquivo_origem"] = [row.get("__arquivo_origem") for row in rede_rows]
        rede["rede_aba_origem"] = [row.get("__aba_origem") for row in rede_rows]
        rede["rede_data_relatorio"] = [row.get("__data_relatorio") for row in rede_rows]
        rede["rede_linha_original"] = rede["_row"]
        rede["rede_duplicado_entre_arquivos"] = [row.get("__duplicado", False) for row in rede_rows]
        rede["rede_arquivos_duplicados"] = [row.get("__arquivos_duplicados") for row in rede_rows]
        rede["criterio_deduplicacao_rede"] = [row.get("__criterio_dedup") for row in rede_rows]
    else:
        rede["rede_arquivo_origem"] = None
        rede["rede_aba_origem"] = None
        rede["rede_data_relatorio"] = None
        rede["rede_linha_original"] = rede["_row"]
        rede["rede_duplicado_entre_arquivos"] = False
        rede["rede_arquivos_duplicados"] = None
        rede["criterio_deduplicacao_rede"] = None
    if "__os_shift" in shift_source.columns:
        shift["os_shift"] = [row.get("__os_shift") for row in shift_rows]
    if "__codigo_registro" in shift_source.columns:
        shift["codigo_registro"] = [row.get("__codigo_registro") for row in shift_rows]
    if "__codigo_conta" in shift_source.columns:
        shift["codigo_conta"] = [row.get("__codigo_conta") for row in shift_rows]
    return compare_rede_shift(rede, shift)


def _row_secundaria(
    auth,
    valor_bruto,
    valor_liquido,
    data_venda="26/11/2025",
    modalidade="Crédito",
    bandeira="Mastercard",
    parcela="1",
    total="2",
    status="paga",
    **extras,
):
    row = {
        "numero da autorizacao": auth,
        "valor bruto": valor_bruto,
        "valor liquido da parcela": valor_liquido,
        "data original da venda": data_venda,
        "modalidade": modalidade,
        "bandeira": bandeira,
        "parcela": parcela,
        "numero de parcelas": total,
        "estabelecimento": "10",
        "status": status,
    }
    row.update(extras)
    return row


def test_leading_zero_is_conciliated():
    result = _compare([_row("055040", "123", "10,00")], [_row("55040", "123", "10,00")])
    assert result.detalhado.loc[0, "status_comparacao"] == "CONCILIADO"


def test_value_divergence():
    result = _compare([_row("123456", "123", "10,00")], [_row("123456", "123", "11,00")])
    assert "DIVERGENCIA_VALOR_BRUTO" in result.detalhado.loc[0, "status_comparacao"]


def test_installment_divergence_is_suggested():
    result = _compare([_row("123456", "123", "10,00", "2")], [_row("123456", "123", "10,00", "1")])
    statuses = " ".join(result.detalhado["status_comparacao"])
    assert "DADOS_PARCELA_INSUFICIENTES" in statuses


def test_parcela_compativel_por_restantes_2_2_vs_3_3_concilia():
    rede_row = _row("123456", "123", "10,00", "2", "2")
    shift_row = _row("123456", "123", "10,00", "3", "3")
    result = _compare([rede_row], [shift_row])
    detail = result.detalhado.loc[0]
    assert detail["criterio_parcela"] == "COMPATIVEL_POR_PARCELAS_RESTANTES"
    assert detail["parcelas_compativeis"] is True
    assert "CONCILIADO_COM_PARCELA_COMPATIVEL_POR_RESTANTES" in detail["status_comparacao"]


def test_parcela_compativel_por_restantes_no_meio_do_parcelamento():
    rede_row = _row("123456", "123", "10,00", "1", "2")
    shift_row = _row("123456", "123", "10,00", "2", "3")
    result = _compare([rede_row], [shift_row])
    assert result.detalhado.loc[0, "criterio_parcela"] == "COMPATIVEL_POR_PARCELAS_RESTANTES"


def test_parcela_com_saldo_restante_diferente_e_divergencia_real():
    rede_row = _row("123456", "123", "10,00", "2", "2")
    shift_row = _row("123456", "123", "10,00", "2", "3")
    result = _compare([rede_row], [shift_row])
    detail = result.detalhado.loc[0]
    assert detail["criterio_parcela"] == "DIVERGENCIA_PARCELAS_RESTANTES"
    assert "DIVERGENCIA_PARCELA" in detail["status_comparacao"]


def test_sem_autorizacao_compativel_nao_usa_fallback_por_data_e_valor():
    result = _compare([_row("111111", "123", "10,00")], [_row("222222", "999", "10,00")])
    statuses = " ".join(result.detalhado["status_comparacao"])
    assert "NAO_ENCONTRADO_NA_REDE" in statuses
    assert "NAO_ENCONTRADO_NO_SHIFT" in statuses


def test_diferenca_de_dois_centavos_e_conciliada_mas_permanece_no_painel():
    result = _compare([_row("123456", "123", "10,02")], [_row("123456", "", "10,00")])
    detail = result.detalhado.loc[0]
    assert detail["status_comparacao"] == "CONCILIADO_COM_DIVERGENCIA_TOLERADA"
    assert detail["diferenca_valor_bruto"] == Decimal("0.02")


def test_duas_linhas_shift_podem_formar_uma_parcela_rede():
    rede_row = _row("123456", "123", "69,00")
    shift_a = {**_row("123456", "", "34,50"), "__os_shift": "OS1"}
    shift_b = {**_row("123456", "", "34,50"), "__os_shift": "OS2"}
    result = _compare([rede_row], [shift_a, shift_b])
    detail = result.detalhado.loc[0]
    assert result.resumo["total_agrupamentos_shift"] == 1
    assert detail["status_comparacao"] == "CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO"
    assert detail["linhas_shift_agrupadas"] == 2
    assert detail["lista_os_shift"] == "OS1 | OS2"
    assert detail["valor_bruto_shift_agrupado"] == Decimal("69.00")


def test_caso_real_680826_concilia_por_agrupamento_rastreavel():
    rede = [_row_secundaria("0680826", "71,50", "69,82", parcela="1", total="2")]
    shift = [
        _row_secundaria("680826", "13,00", "12,69", parcela="1", total="2", __os_shift="OS1"),
        _row_secundaria("680826", "58,50", "57,13", parcela="1", total="2", __os_shift="OS2"),
    ]
    result = _compare(rede, shift)
    detail = result.detalhado.loc[0]
    assert detail["status_comparacao"] == "CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO"
    assert detail["valor_bruto_shift_agrupado"] == Decimal("71.50")
    assert detail["valor_liquido_shift_agrupado"] == Decimal("69.82")


def test_agrupamento_com_bandeiras_conflitantes_e_ambiguo():
    rede = [_row_secundaria("123456", "30,00", "29,00", total="1")]
    shift = [
        _row_secundaria("123456", "10,00", "9,50", bandeira="Visa", total="1", __os_shift="OS1"),
        _row_secundaria("123456", "20,00", "19,50", bandeira="Mastercard", total="1", __os_shift="OS2"),
    ]
    result = _compare(rede, shift)
    shift_rows = result.detalhado[result.detalhado["linha_shift"].notna()]
    assert shift_rows["status_comparacao"].str.contains("AGRUPAMENTO_OS_AMBIGUO").all()


def test_agrupamento_mesma_autorizacao_soma_diverge_da_rede():
    rede = [_row_secundaria("0195933", "100,00", "100,00", total="1")]
    shift = [
        _row_secundaria("195933", "50,00", "50,00", total="1", __os_shift="OS1"),
        _row_secundaria("195933", "30,00", "30,00", total="1", __os_shift="OS2"),
        _row_secundaria("195933", "19,00", "19,00", total="1", __os_shift="OS3"),
    ]
    result = _compare(rede, shift)
    detail = result.detalhado.loc[0]
    assert "AGRUPAMENTO_OS_VALOR_DIVERGENTE" in detail["status_comparacao"]
    assert detail["valor_liquido_shift_agrupado"] == Decimal("99.00")
    assert detail["valor_liquido_rede"] == Decimal("100.00")
    assert detail["diferenca_valor_liquido"] == Decimal("1.00")


def test_agrupamento_mesma_autorizacao_com_parcelas_incompativeis_fica_ambiguo():
    rede = [_row_secundaria("0195933", "100,00", "100,00", parcela="1", total="2")]
    shift = [
        _row_secundaria("195933", "50,00", "50,00", parcela="1", total="2", __os_shift="OS1"),
        _row_secundaria("195933", "50,00", "50,00", parcela="2", total="2", __os_shift="OS2"),
    ]
    result = _compare(rede, shift)
    shift_rows = result.detalhado[result.detalhado["linha_shift"].notna()]
    assert shift_rows["status_comparacao"].str.contains("AGRUPAMENTO_OS_AMBIGUO").all()


def test_duplicidade_exata_suspeita_nao_vira_multiplas_os_legitimas():
    rede = [_row_secundaria("0195933", "100,00", "100,00", total="1")]
    shift = [
        _row_secundaria(
            "195933", "50,00", "50,00", total="1",
            __os_shift="OS1", __codigo_registro="REG1",
        ),
        _row_secundaria(
            "195933", "50,00", "50,00", total="1",
            __os_shift="OS1", __codigo_registro="REG1",
        ),
    ]
    result = _compare(rede, shift)
    assert result.detalhado["status_comparacao"].str.contains("DUPLICIDADE_EXATA_SUSPEITA").any()


def test_multiplas_os_legitimas_sem_rede_mantem_flag():
    shift = [
        _row_secundaria("195933", "100,00", "100,00", total="1", __os_shift="OS1"),
        _row_secundaria("195933", "100,00", "100,00", total="1", __os_shift="OS2"),
    ]
    rede = [_row_secundaria("111111", "50,00", "50,00", total="1")]
    result = _compare(rede, shift)
    shift_row = result.detalhado[result.detalhado["linha_rede"].isna()].iloc[0]
    assert "NAO_ENCONTRADO_NA_REDE" in shift_row["status_comparacao"]
    assert "MULTIPLAS_OS_MESMA_AUTORIZACAO" in shift_row["status_comparacao"]


def test_match_no_segundo_arquivo_rede_preserva_origem():
    rede = [
        _row_secundaria("111111", "50,00", "50,00", total="1", __arquivo_origem="rede-01.xlsx"),
        _row_secundaria("0195933", "100,00", "100,00", total="1", __arquivo_origem="rede-02.xlsx"),
    ]
    shift = [_row_secundaria("195933", "100,00", "100,00", total="1")]
    result = _compare(rede, shift)
    detail = result.detalhado.loc[0]
    assert detail["status_comparacao"] == "CONCILIADO"
    assert detail["rede_arquivo_origem"] == "rede-02.xlsx"


def test_duplicidade_entre_arquivos_rede_fica_rastreavel_no_detalhado():
    rede = [_row_secundaria(
        "0195933", "100,00", "100,00", total="1",
        __arquivo_origem="rede-01.xlsx",
        __duplicado=True,
        __arquivos_duplicados="rede-01.xlsx | rede-02.xlsx",
        __criterio_dedup="AUTORIZACAO+NSU+VALORES+DATAS+PARCELA+BANDEIRA+MODALIDADE",
    )]
    shift = [_row_secundaria("195933", "100,00", "100,00", total="1")]
    result = _compare(rede, shift)
    detail = result.detalhado.loc[0]
    assert bool(detail["rede_duplicado_entre_arquivos"]) is True
    assert detail["rede_arquivos_duplicados"] == "rede-01.xlsx | rede-02.xlsx"
