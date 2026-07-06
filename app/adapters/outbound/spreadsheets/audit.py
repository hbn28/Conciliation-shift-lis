from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pandas as pd


DISCARD_COLUMNS = [
    "origem", "numero_linha_original", "empresa", "autorizacao_original",
    "autorizacao_normalizada", "valor_bruto", "valor_liquido", "data_venda",
    "modalidade", "bandeira", "parcela", "qtd_parcelas",
    "arquivo_origem", "aba_origem", "data_relatorio",
    "motivo_descarte", "observacao",
]


def discard_record(
    row,
    origem: str,
    motivo: str,
    observacao: str = "",
) -> dict:
    def get(*names):
        for name in names:
            value = row.get(name)
            if value is not None and not pd.isna(value):
                return value
        return None

    return {
        "origem": origem,
        "numero_linha_original": get("_source_line", "_row", "rede_linha_original"),
        "empresa": get("empresa", "Empresa", "descricao_credor_devedor"),
        "autorizacao_original": get("raw_autorizacao", "Nro autorização cartão"),
        "autorizacao_normalizada": get("autorizacao"),
        "valor_bruto": get("valor_bruto", "Valor bruto"),
        "valor_liquido": get("valor_liquido", "Valor líquido"),
        "data_venda": get("data_venda", "Data de emissão", "data original da venda"),
        "modalidade": get("modalidade", "Forma de pagamento/cobrança"),
        "bandeira": get("bandeira"),
        "parcela": get("parcela", "Número da parcela"),
        "qtd_parcelas": get("numero_parcelas", "Quantidade de parcelas"),
        "arquivo_origem": get("rede_arquivo_origem"),
        "aba_origem": get("rede_aba_origem"),
        "data_relatorio": get("rede_data_relatorio"),
        "motivo_descarte": motivo,
        "observacao": observacao,
    }


def partition_valid_rows(
    frame: pd.DataFrame, origem: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    valid_indexes: list[int] = []
    discarded: list[dict] = []
    for idx, row in frame.iterrows():
        reason = None
        observation = ""
        authorization = row.get("autorizacao")
        gross = row.get("valor_bruto")
        if authorization is None or pd.isna(authorization) or str(authorization).strip() == "":
            reason = "AUTORIZACAO_AUSENTE"
        elif gross is None or pd.isna(gross):
            reason = "VALOR_INVALIDO"
        elif Decimal(gross) == 0:
            reason = "LINHA_ZERADA"
            observation = "Valor bruto igual a zero; mantida na auditoria."
        if reason:
            discarded.append(discard_record(row, origem, reason, observation))
        else:
            valid_indexes.append(idx)
    valid = frame.loc[valid_indexes].reset_index(drop=True)
    return valid, pd.DataFrame(discarded, columns=DISCARD_COLUMNS)


def build_audit_frame(summary: dict, discards: pd.DataFrame) -> pd.DataFrame:
    reasons = (
        Counter(discards["motivo_descarte"].dropna())
        if discards is not None and not discards.empty
        else Counter()
    )
    rows = [
        {"origem": key.split("_", 1)[0].upper(), "indicador": key, "valor": value}
        for key, value in summary.items()
        if key.startswith(("shift_", "rede_"))
        and isinstance(value, (int, float, Decimal))
    ]
    rows.extend({
        "origem": "GERAL",
        "indicador": f"motivo_descarte_{reason}",
        "valor": count,
    } for reason, count in sorted(reasons.items()))
    for key, value in summary.items():
        if not isinstance(value, dict):
            continue
        if not key.startswith((
            "linhas_lidas_por_arquivo_rede",
            "linhas_validas_por_arquivo_rede",
            "linhas_descartadas_por_arquivo_rede",
        )):
            continue
        rows.extend({
            "origem": "REDE",
            "indicador": f"{key}:{arquivo}",
            "valor": quantidade,
        } for arquivo, quantidade in value.items())
    return pd.DataFrame(rows, columns=["origem", "indicador", "valor"])
