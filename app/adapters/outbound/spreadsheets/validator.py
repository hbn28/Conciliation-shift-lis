from __future__ import annotations

import re

import pandas as pd


CANCEL_TOKENS = ("SIM", "CANCEL", "CONTEST")

# Duplicidade por autorização ou NSU isolados é um indício fraco: esses
# valores podem se repetir legitimamente entre vendas diferentes. A
# duplicidade forte (que de fato indica lançamento repetido) usa a chave
# composta abaixo, verificada em PAGAMENTO_DUPLICADO.
WEAK_ALERT_CODES = {
    "AUTORIZACAO_DUPLICADA", "NSU_DUPLICADO", "PARCELA_VAZIA",
    "NUMERO_PARCELAS_VAZIO", "BANDEIRA_VAZIA", "MODALIDADE_VAZIA",
    "STATUS_VAZIO", "NSU_FORMATO_INVALIDO", "VALOR_ZERADO",
    # NSU não é confiável/obrigatório no relatório financeiro do Shift.
    "NSU_VAZIO",
    # Alertas específicos do relatório financeiro (cartão) — não bloqueiam
    # a conciliação sozinhos.
    "FORMA_PAGAMENTO_AUSENTE", "PARCELAMENTO_NAO_IDENTIFICADO",
    "DIVERGENCIA_PARCELAMENTO_FORMA_PAGAMENTO", "VALOR_BRUTO_INVALIDO",
}


def _is_cancelled(value: object) -> bool:
    text = str(value or "")
    return any(token in text for token in CANCEL_TOKENS)


def _classification(code: str) -> str:
    return "ALERTA_CADASTRAL_SHIFT" if code in WEAK_ALERT_CODES else "ERRO_CADASTRAL_SHIFT"


def validate_shift(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[int, list[str]]]:
    issues: dict[int, list[str]] = {int(i): [] for i in df.index}
    required = {
        "autorizacao": "AUTORIZACAO_VAZIA", "nsu": "NSU_VAZIO",
        "valor_bruto": "VALOR_VAZIO", "parcela": "PARCELA_VAZIA",
        "numero_parcelas": "NUMERO_PARCELAS_VAZIO",
        "bandeira": "BANDEIRA_VAZIA", "modalidade": "MODALIDADE_VAZIA",
        "status": "STATUS_VAZIO",
    }
    for idx, row in df.iterrows():
        for field, code in required.items():
            value = row.get(field)
            if value is None or value == "" or value in {"NAO_INFORMADO"} or pd.isna(value):
                issues[int(idx)].append(code)
        # Parcelas canceladas/contestadas legitimamente zeram o valor
        # atualizado; nesse caso não é erro de cadastro no Shift.
        if row.get("valor_bruto") == 0 and not _is_cancelled(row.get("cancelamento")):
            issues[int(idx)].append("VALOR_ZERADO")
        if row.get("autorizacao_erro"):
            issues[int(idx)].append("AUTORIZACAO_TAMANHO_INVALIDO")
        nsu = row.get("nsu")
        if nsu and not re.fullmatch(r"[A-Z0-9-]+", str(nsu)):
            issues[int(idx)].append("NSU_FORMATO_INVALIDO")
        # Alertas já identificados na normalização (ex.: relatório financeiro
        # do Shift: parcelamento não identificado, forma de pagamento
        # ausente, divergência de parcelamento etc.).
        for code in row.get("_alertas_normalizacao") or []:
            issues[int(idx)].append(code)

    duplicate_specs = {
        "AUTORIZACAO_DUPLICADA": ["autorizacao"],
        "NSU_DUPLICADO": ["nsu"],
        "PAGAMENTO_DUPLICADO": ["autorizacao", "nsu", "valor_bruto", "parcela"],
    }
    for code, columns in duplicate_specs.items():
        valid = df[columns].notna().all(axis=1)
        duplicated = df.loc[valid].duplicated(columns, keep=False)
        for idx in duplicated[duplicated].index:
            issues[int(idx)].append(code)

    rows = []
    for idx, codes in issues.items():
        for code in dict.fromkeys(codes):
            rows.append({
                "linha_shift": int(df.loc[idx, "_row"]),
                "autorizacao_original": df.loc[idx, "raw_autorizacao"],
                "autorizacao_normalizada": df.loc[idx, "autorizacao"],
                "nsu": df.loc[idx, "nsu"],
                "problema": code,
                "classificacao": _classification(code),
            })
    return pd.DataFrame(rows), issues
