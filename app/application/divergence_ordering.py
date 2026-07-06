from __future__ import annotations

from typing import Iterable


GROUP_RULES = [
    ("AGRUPAMENTO_OS_AMBIGUO", "Agrupamentos ambíguos"),
    ("AGRUPAMENTO_OS_VALOR_DIVERGENTE", "Agrupamentos com valor divergente"),
    ("AGRUPAMENTO_SHIFT_AMBIGUO", "Agrupamentos ambíguos"),
    ("REVISAR_AUTORIZACAO_DIVERGENTE", "Autorizações divergentes para revisão"),
    ("DIVERGENCIA_AUTORIZACAO", "Divergências de autorização"),
    ("DIVERGENCIA_VALOR_BRUTO", "Divergências de valor bruto"),
    ("DIVERGENCIA_VALOR_LIQUIDO", "Divergências de valor líquido"),
    ("DIVERGENCIA_MDR", "Divergências de MDR"),
    ("DIVERGENCIA_PARCELA", "Divergências de parcela"),
    ("DADOS_PARCELA_INSUFICIENTES", "Dados de parcela insuficientes"),
    ("DIVERGENCIA_DATA", "Divergências de data"),
    ("DIVERGENCIA_BANDEIRA", "Divergências de bandeira"),
    ("DIVERGENCIA_MODALIDADE", "Divergências de modalidade"),
    ("NAO_ENCONTRADO_NA_REDE", "Encontradas somente no Shift"),
    ("NAO_ENCONTRADO_NO_SHIFT", "Encontradas somente na Rede"),
    ("POSSIVEL_DUPLICIDADE", "Possíveis duplicidades"),
    ("DUPLICIDADE_EXATA_SUSPEITA", "Duplicidades exatas suspeitas"),
    ("CONCILIADO_COM_DIVERGENCIA_TOLERADA", "Divergências toleradas até R$ 0,02"),
]


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def divergence_group(status: str) -> tuple[int, str]:
    text = str(status or "")
    for position, (token, label) in enumerate(GROUP_RULES):
        if token in text:
            return position, label
    return len(GROUP_RULES), "Outras divergências"


def sort_divergences(rows: Iterable[dict]) -> list[dict]:
    prepared = []
    for original in rows:
        row = dict(original)
        rank, label = divergence_group(row.get("status_comparacao", ""))
        row["grupo_divergencia"] = label
        row["_ordem_grupo_divergencia"] = rank
        prepared.append(row)

    prepared.sort(key=lambda row: (
        row["_ordem_grupo_divergencia"],
        -_number(row.get("impacto_financeiro_confirmado")),
        -_number(row.get("valor_operacao_em_revisao")),
        str(
            row.get("shift_autorizacao_normalizado")
            or row.get("rede_autorizacao_normalizado")
            or ""
        ),
    ))
    for row in prepared:
        row.pop("_ordem_grupo_divergencia", None)
    return prepared
