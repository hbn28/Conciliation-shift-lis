from pathlib import Path

import pandas as pd
import pytest

from app.adapters.outbound.spreadsheets.file_reader import (
    is_shift_financial_report,
    read_shift_financial_report,
)
from app.adapters.outbound.spreadsheets.normalizer import (
    extract_os,
    normalize_shift_financial_report,
    parse_forma_pagamento,
)
from app.adapters.outbound.spreadsheets.matcher import compare_rede_shift
from app.adapters.outbound.spreadsheets.normalizer import normalize_dataframe

PREAMBLE = (
    "CentralLab\n"
    "Rua Victor Jurema, 556 - Cajazeiras/PB\n"
    "Relatório financeiro\n"
    "\n"
    "DATA PARA PESQUISA;Vencimento\n"
    "DATA INICIAL;26/12/2025\n"
    "DATA FINAL;29/12/2025\n"
    "\n"
)
FOOTER = "\nEmitido por: (467) ALICE SALES DE MORAI - 30/12/2025 às 11:07:27\n"

FULL_HEADER = (
    "Empresa;Descrição;Número do documento;Vencimento;Data de emissão;Valor líquido;"
    "Desconto;Tipo de documento;Espécie;Forma de pagamento/cobrança;Número da parcela;"
    "Quantidade de parcelas;Código do registro;Valor bruto;Nro autorização cartão\n"
)


def _row(
    empresa="CENTRALLAB", descricao="OS: 022-67324-620", documento="023455",
    vencimento="26/12/2025", emissao="29/04/2025", liquido="352,38", desconto="9,82",
    tipo_doc="NOTA FISCAL", especie="Cartão de Crédito/Débito", forma="REDE 10X MASTER",
    parcela="8", qtd_parcelas="10", registro="022-67324-620", bruto="362,20", auth="972902",
):
    return (
        f"{empresa};{descricao};{documento};{vencimento};{emissao};{liquido};{desconto};"
        f"{tipo_doc};{especie};{forma};{parcela};{qtd_parcelas};{registro};{bruto};{auth}\n"
    )


def _write(tmp_path: Path, body: str, header: str = FULL_HEADER) -> Path:
    path = tmp_path / "shift_financeiro.csv"
    content = PREAMBLE + header + body + FOOTER
    path.write_bytes(content.encode("utf-8-sig"))
    return path


# --- detecção e leitura bruta -----------------------------------------------

def test_detecta_relatorio_financeiro(tmp_path: Path):
    path = _write(tmp_path, _row())
    assert is_shift_financial_report(path) is True


def test_ignora_cabecalho_institucional_e_roda_pe(tmp_path: Path):
    body = _row() + (
        "CENTRALLAB;;;26/12/2025;26/12/2025;10,00;0,00;NOTA FISCAL;"
        "Trasferência Eletrônica de Fundos;PIX ITAU (Jn);;;;10,00;\n"
    )
    path = _write(tmp_path, body)
    df, stats = read_shift_financial_report(path, payment_scope="card")
    assert stats["total_linhas"] == 2
    assert stats["linhas_cartao"] == 1
    assert stats["linhas_ignoradas"] == 1
    assert "Empresa" in df.columns
    assert not df["Empresa"].astype(str).str.contains("Emitido por").any()


def test_filtra_apenas_cartao_ignora_pix(tmp_path: Path):
    body = _row() + (
        "CENTRALLAB;;;26/12/2025;26/12/2025;10,00;0,00;NOTA FISCAL;"
        "Trasferência Eletrônica de Fundos;PIX ITAU (Jn);;;;10,00;\n"
    )
    path = _write(tmp_path, body)
    df, stats = read_shift_financial_report(path, payment_scope="card")
    assert len(df) == 1
    assert stats["linhas_ignoradas"] == 1


def test_sem_espece_nem_forma_pagamento_gera_erro_claro(tmp_path: Path):
    header = (
        "Empresa;Descrição;Número do documento;Vencimento;Data de emissão;Valor líquido;"
        "Valor bruto;Nro autorização cartão\n"
    )
    body = "CENTRALLAB;OS: 1;1;26/12/2025;26/12/2025;10,00;10,00;123456\n"
    path = _write(tmp_path, body, header=header)
    with pytest.raises(ValueError, match="Espécie"):
        read_shift_financial_report(path, payment_scope="card")


def test_sem_valor_bruto_gera_erro_claro(tmp_path: Path):
    header = (
        "Empresa;Descrição;Espécie;Forma de pagamento/cobrança;Nro autorização cartão\n"
    )
    body = "CENTRALLAB;OS: 1;Cartão de Crédito/Débito;REDE 1X VISA;123456\n"
    path = _write(tmp_path, body, header=header)
    with pytest.raises(ValueError, match='"Valor bruto"'):
        read_shift_financial_report(path, payment_scope="card")


def test_sem_autorizacao_cartao_gera_erro_claro(tmp_path: Path):
    header = "Empresa;Espécie;Forma de pagamento/cobrança;Valor bruto\n"
    body = "CENTRALLAB;Cartão de Crédito/Débito;REDE 1X VISA;10,00\n"
    path = _write(tmp_path, body, header=header)
    with pytest.raises(ValueError, match="Nro autorização cartão"):
        read_shift_financial_report(path, payment_scope="card")


# --- parsing de forma de pagamento ------------------------------------------

@pytest.mark.parametrize(("forma", "operadora", "modalidade", "bandeira", "parcelas"), [
    ("REDE 10X MASTER", "REDE", "CREDITO", "MASTER", 10),
    ("REDE 5X VISA", "REDE", "CREDITO", "VISA", 5),
    ("REDE 1X ELO", "REDE", "CREDITO", "ELO", 1),
    ("REDE DEBITO MASTER", "REDE", "DEBITO", "MASTER", 1),
    ("REDE DEBITO VISA", "REDE", "DEBITO", "VISA", 1),
    ("REDE DEBITO ELO", "REDE", "DEBITO", "ELO", 1),
])
def test_parse_forma_pagamento(forma, operadora, modalidade, bandeira, parcelas):
    result = parse_forma_pagamento(forma)
    assert result["operadora"] == operadora
    assert result["modalidade"] == modalidade
    assert result["bandeira"] == bandeira
    assert result["parcelas_inferidas_da_forma"] == parcelas


def test_parse_forma_pagamento_pix_nao_reconhecida():
    result = parse_forma_pagamento("PIX ITAU (Jn)")
    assert result["modalidade"] is None
    assert result["bandeira"] is None
    assert result["parcelas_inferidas_da_forma"] is None


def test_extract_os():
    assert extract_os("OS: 022-67324-620") == "022-67324-620"
    assert extract_os(None) is None


# --- normalização completa ---------------------------------------------------

def test_normalizacao_autorizacao_numerica_com_zero_a_esquerda(tmp_path: Path):
    path = _write(tmp_path, _row(auth="5040"))
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert canon.loc[0, "autorizacao"] == "005040"


def test_normalizacao_autorizacao_alfanumerica(tmp_path: Path):
    path = _write(tmp_path, _row(auth="4Y9HDN"))
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert canon.loc[0, "autorizacao"] == "4Y9HDN"


def test_valor_bruto_virgula_decimal(tmp_path: Path):
    path = _write(tmp_path, _row(bruto="362,20"))
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert str(canon.loc[0, "valor_bruto"]) == "362.20"


def test_valor_bruto_ponto_decimal(tmp_path: Path):
    path = _write(tmp_path, _row(bruto="362.20"))
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert str(canon.loc[0, "valor_bruto"]) == "362.20"


def test_extracao_os_da_descricao(tmp_path: Path):
    path = _write(tmp_path, _row(descricao="OS: 135-67444-235"))
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert canon.loc[0, "os_shift"] == "135-67444-235"


def test_usa_numero_parcela_e_quantidade_parcelas_como_fonte_principal(tmp_path: Path):
    # forma indica 10x, mas o relatório traz parcela 8 de 10 explicitamente.
    path = _write(tmp_path, _row(forma="REDE 10X MASTER", parcela="8", qtd_parcelas="10"))
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert canon.loc[0, "parcela"] == 8
    assert canon.loc[0, "numero_parcelas"] == 10
    assert canon.loc[0, "_alertas_normalizacao"] == []


def test_alerta_divergencia_parcelamento_forma_pagamento(tmp_path: Path):
    # Quantidade de parcelas diz 3, mas a forma de pagamento indica 10x.
    path = _write(tmp_path, _row(forma="REDE 10X MASTER", parcela="1", qtd_parcelas="3"))
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert "DIVERGENCIA_PARCELAMENTO_FORMA_PAGAMENTO" in canon.loc[0, "_alertas_normalizacao"]


def test_parcelamento_nao_identificado_sem_nenhuma_fonte(tmp_path: Path):
    header = "Empresa;Espécie;Forma de pagamento/cobrança;Valor bruto;Nro autorização cartão\n"
    # Forma de pagamento vazia (sem "REDE Nx BANDEIRA" reconhecível) e sem
    # "Número da parcela"/"Quantidade de parcelas" no relatório.
    path = _write(tmp_path, "CENTRALLAB;Cartão de Crédito/Débito;;10,00;123456\n", header=header)
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert "PARCELAMENTO_NAO_IDENTIFICADO" in canon.loc[0, "_alertas_normalizacao"]


def test_forma_pagamento_ausente_gera_alerta(tmp_path: Path):
    header = "Empresa;Espécie;Valor bruto;Nro autorização cartão\n"
    path = _write(tmp_path, "CENTRALLAB;Cartão de Crédito/Débito;10,00;123456\n", header=header)
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert "FORMA_PAGAMENTO_AUSENTE" in canon.loc[0, "_alertas_normalizacao"]
    assert canon.loc[0, "bandeira"] is None
    assert canon.loc[0, "modalidade"] is None


def test_sem_valor_liquido_nao_quebra(tmp_path: Path):
    header = "Empresa;Espécie;Forma de pagamento/cobrança;Valor bruto;Nro autorização cartão\n"
    path = _write(tmp_path, "CENTRALLAB;Cartão de Crédito/Débito;REDE 1X VISA;10,00;123456\n", header=header)
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert canon.loc[0, "valor_liquido"] is None


def test_sem_numero_documento_nao_quebra(tmp_path: Path):
    header = "Empresa;Espécie;Forma de pagamento/cobrança;Valor bruto;Nro autorização cartão\n"
    path = _write(tmp_path, "CENTRALLAB;Cartão de Crédito/Débito;REDE 1X VISA;10,00;123456\n", header=header)
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert canon.loc[0, "documento_shift"] is None
    assert canon.loc[0, "possivel_nsu"] is None


def test_colunas_extras_desconhecidas_sao_ignoradas(tmp_path: Path):
    header = FULL_HEADER.strip("\n") + ";Coluna Nova Desconhecida\n"
    path = _write(tmp_path, _row().strip("\n") + ";valor qualquer\n", header=header)
    df, _ = read_shift_financial_report(path)
    canon = normalize_shift_financial_report(df)
    assert canon.loc[0, "autorizacao"] == "972902"


def test_sem_especie_mas_com_forma_pagamento(tmp_path: Path):
    header = "Empresa;Forma de pagamento/cobrança;Valor bruto;Nro autorização cartão\n"
    path = _write(tmp_path, "CENTRALLAB;REDE 1X VISA;10,00;123456\n", header=header)
    df, stats = read_shift_financial_report(path)
    assert stats["linhas_cartao"] == 1


def test_sem_forma_pagamento_mas_com_especie(tmp_path: Path):
    header = "Empresa;Espécie;Valor bruto;Nro autorização cartão\n"
    path = _write(tmp_path, "CENTRALLAB;Cartão de Crédito/Débito;10,00;123456\n", header=header)
    df, stats = read_shift_financial_report(path)
    assert stats["linhas_cartao"] == 1


# --- matcher com o novo formato ---------------------------------------------

def _rede_row(auth="972902", valor="362,20", parcela="8", total="10"):
    return {
        "numero da autorizacao": auth, "nsu": "555", "valor bruto": valor,
        "parcela": parcela, "numero de parcelas": total, "bandeira": "Master",
        "modalidade": "Crédito", "status": "paga",
    }


def test_matcher_concilia_sem_nsu_no_shift(tmp_path: Path):
    path = _write(tmp_path, _row(auth="972902", bruto="362,20", parcela="8", qtd_parcelas="10"))
    df, _ = read_shift_financial_report(path)
    shift = normalize_shift_financial_report(df)
    rede = normalize_dataframe(pd.DataFrame([_rede_row()]), "rede")
    result = compare_rede_shift(rede, shift)
    assert result.detalhado.loc[0, "status_comparacao"] == "CONCILIADO"


def test_financeiro_normaliza_master_e_nao_compara_status_lote(tmp_path: Path):
    path = _write(tmp_path, _row(forma="REDE 1X MASTER"))
    df, _ = read_shift_financial_report(path)
    shift = normalize_shift_financial_report(df)
    assert shift.loc[0, "bandeira"] == "MASTERCARD"
    assert shift.loc[0, "status"] is None
    assert shift.loc[0, "lote"] is None


def test_forma_cartao_generica_com_parcelas_nao_converte_nan_para_inteiro(tmp_path: Path):
    body = (
        _row(
            forma="CARTÃO",
            parcela="11",
            qtd_parcelas="12",
            auth="",
        )
        + _row(forma="REDE 1X VISA", auth="123456")
    )
    path = _write(tmp_path, body)
    df, _ = read_shift_financial_report(path)
    shift = normalize_shift_financial_report(df)
    assert shift.loc[0, "parcela"] == 11
    assert shift.loc[0, "numero_parcelas"] == 12
    assert shift.loc[0, "autorizacao"] is None


def test_vencimento_shift_e_preservado_mas_nao_gera_divergencia(tmp_path: Path):
    path = _write(
        tmp_path,
        _row(
            vencimento="27/12/2025",
            emissao="26/11/2025",
            auth="972902",
            bruto="362,20",
            parcela="8",
            qtd_parcelas="10",
        ),
    )
    df, _ = read_shift_financial_report(path)
    shift = normalize_shift_financial_report(df)
    assert str(shift.loc[0, "data_vencimento"]) == "2025-12-27"
    assert bool(shift.loc[0, "_comparar_data_vencimento"]) is False

    rede_row = _rede_row(auth="972902", valor="362,20", parcela="8", total="10")
    rede_row["data original da venda"] = "26/11/2025"
    rede_row["data original de vencimento"] = "29/12/2025"
    rede = normalize_dataframe(pd.DataFrame([rede_row]), "rede")
    result = compare_rede_shift(rede, shift)
    assert result.detalhado.loc[0, "status_comparacao"] == "CONCILIADO"


def test_matcher_nao_exige_estabelecimento_no_shift(tmp_path: Path):
    path = _write(tmp_path, _row(auth="972902", bruto="362,20", parcela="8", qtd_parcelas="10"))
    df, _ = read_shift_financial_report(path)
    shift = normalize_shift_financial_report(df)
    assert "estabelecimento" not in df.columns
    rede = normalize_dataframe(pd.DataFrame([_rede_row()]), "rede")
    result = compare_rede_shift(rede, shift)
    assert result.detalhado.loc[0, "status_comparacao"] == "CONCILIADO"


def test_relatorio_contabiliza_pix_ignorado(tmp_path: Path):
    body = _row() + (
        "CENTRALLAB;;;26/12/2025;26/12/2025;10,00;0,00;NOTA FISCAL;"
        "Trasferência Eletrônica de Fundos;PIX ITAU (Jn);;;;10,00;\n"
    )
    path = _write(tmp_path, body)
    _, stats = read_shift_financial_report(path, payment_scope="card")
    assert stats["linhas_ignoradas"] == 1
    assert stats["linhas_cartao"] == 1
