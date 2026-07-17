from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd


ZERO = Decimal("0")

# Conjuntos de status usados para decidir a regra de cálculo do impacto
# financeiro de uma linha de divergência. Os nomes usados aqui são os
# mesmos literais já emitidos por `matcher.py` em `status_comparacao`
# (podem aparecer isolados ou concatenados com " + " a outros status).
STATUS_NAO_ENCONTRADO_NA_REDE = "NAO_ENCONTRADO_NA_REDE"
STATUS_NAO_ENCONTRADO_NO_SHIFT = "NAO_ENCONTRADO_NO_SHIFT"
STATUS_DIVERGENCIA_VALOR_LIQUIDO = "DIVERGENCIA_VALOR_LIQUIDO"
STATUS_DIVERGENCIA_VALOR_BRUTO = "DIVERGENCIA_VALOR_BRUTO"
STATUS_DIVERGENCIA_DATA = "DIVERGENCIA_DATA"
STATUS_REVISAR_AUTORIZACAO_ALTA_CONFIANCA = "REVISAR_AUTORIZACAO_DIVERGENTE_ALTA_CONFIANCA"
STATUS_CONCILIADO_POR_AGRUPAMENTO_SHIFT = "CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO"
STATUS_AGRUPAMENTO_SHIFT_AMBIGUO = "AGRUPAMENTO_OS_AMBIGUO"
STATUS_AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL = "AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL"


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _has_status(status_comparacao: str | None, status: str) -> bool:
    if not status_comparacao:
        return False
    return status in status_comparacao.split(" + ")


def _diferenca_assinada(row: pd.Series) -> Decimal | None:
    """Calcula a diferença com sinal no padrão Rede - Shift.

    Usa valor líquido quando os dois lados existem; caso contrário usa
    valor bruto. Isso permite somar divergências em saldo líquido, como no
    exemplo: -0,02 + 0,01 = -0,01.
    """
    shift_liquido = _to_decimal(row.get("valor_liquido_shift"))
    rede_liquido = _to_decimal(row.get("valor_liquido_rede"))
    if shift_liquido is not None and rede_liquido is not None:
        return rede_liquido - shift_liquido
    shift_bruto = _to_decimal(row.get("valor_bruto_shift"))
    rede_bruto = _to_decimal(row.get("valor_bruto_rede"))
    if shift_bruto is not None and rede_bruto is not None:
        return rede_bruto - shift_bruto
    return None


def calcular_impacto_financeiro(row: dict) -> dict:
    """Calcula os campos de impacto financeiro para uma linha de divergência.

    Recebe o dict de uma linha já montada pelo matcher (`_detail` em
    `matcher.py`, com chaves como `valor_bruto_shift`, `valor_bruto_rede`,
    `valor_liquido_shift`, `valor_liquido_rede`, `status_comparacao`,
    `motivo`, `acao_recomendada`, `criterio_match`, `nivel_confianca`) e
    devolve um novo dict apenas com os campos de impacto financeiro,
    prontos para serem mesclados na linha original.

    Nunca lança exceção: campos ausentes viram `None` e o impacto vira
    zero/None quando não houver dado suficiente para calcular.
    """
    status_comparacao = row.get("status_conciliacao") or row.get("status_comparacao")

    valor_bruto_shift = _to_decimal(row.get("valor_bruto_shift"))
    valor_bruto_rede = _to_decimal(row.get("valor_bruto_rede"))
    valor_liquido_shift = _to_decimal(row.get("valor_liquido_shift"))
    valor_liquido_rede = _to_decimal(row.get("valor_liquido_rede"))

    diferenca_valor_bruto = (
        abs(valor_bruto_shift - valor_bruto_rede)
        if valor_bruto_shift is not None and valor_bruto_rede is not None
        else None
    )
    diferenca_valor_liquido = (
        abs(valor_liquido_shift - valor_liquido_rede)
        if valor_liquido_shift is not None and valor_liquido_rede is not None
        else None
    )

    # Valor "da operação": usado como referência de tamanho quando a linha
    # não tem os dois lados para comparar (ex.: só existe no Shift ou só
    # existe na Rede), ou como base para casos ambíguos/em revisão.
    valor_operacao_shift = (
        valor_liquido_shift if valor_liquido_shift is not None else valor_bruto_shift
    )
    valor_operacao_rede = (
        valor_liquido_rede if valor_liquido_rede is not None else valor_bruto_rede
    )
    valor_operacao_impacto = (
        valor_operacao_shift if valor_operacao_shift is not None else valor_operacao_rede
    )

    impacto_financeiro_confirmado = ZERO
    valor_operacao_em_revisao = ZERO

    if _has_status(status_comparacao, STATUS_NAO_ENCONTRADO_NA_REDE):
        impacto_financeiro_confirmado = valor_operacao_shift or ZERO
        valor_operacao_impacto = valor_operacao_shift
    elif _has_status(status_comparacao, STATUS_NAO_ENCONTRADO_NO_SHIFT):
        impacto_financeiro_confirmado = valor_operacao_rede or ZERO
        valor_operacao_impacto = valor_operacao_rede
    elif _has_status(status_comparacao, STATUS_REVISAR_AUTORIZACAO_ALTA_CONFIANCA):
        impacto_financeiro_confirmado = ZERO
        valor_operacao_em_revisao = (
            valor_operacao_shift if valor_operacao_shift is not None else valor_operacao_rede
        ) or ZERO
    elif _has_status(status_comparacao, STATUS_AGRUPAMENTO_SHIFT_AMBIGUO) or _has_status(
        status_comparacao, STATUS_AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL
    ):
        impacto_financeiro_confirmado = ZERO
        soma_envolvidos = ZERO
        for value in (valor_operacao_shift, valor_operacao_rede):
            if value is not None:
                soma_envolvidos += value
        valor_operacao_em_revisao = soma_envolvidos
    elif _has_status(status_comparacao, STATUS_DIVERGENCIA_VALOR_LIQUIDO):
        impacto_financeiro_confirmado = diferenca_valor_liquido or ZERO
    elif _has_status(status_comparacao, STATUS_DIVERGENCIA_VALOR_BRUTO):
        impacto_financeiro_confirmado = diferenca_valor_bruto or ZERO
    elif _has_status(status_comparacao, STATUS_DIVERGENCIA_DATA):
        # Se, além da data, também houver diferença de valor (bruto ou
        # líquido), o impacto financeiro considera essa diferença; se os
        # valores baterem, o impacto confirmado é zero (mas o valor da
        # operação segue disponível para exibição).
        if diferenca_valor_liquido:
            impacto_financeiro_confirmado = diferenca_valor_liquido
        elif diferenca_valor_bruto:
            impacto_financeiro_confirmado = diferenca_valor_bruto
        else:
            impacto_financeiro_confirmado = ZERO
    elif _has_status(status_comparacao, STATUS_CONCILIADO_POR_AGRUPAMENTO_SHIFT):
        # Não é divergência crítica; se os valores batem (ou não há como
        # comparar), impacto é zero.
        if diferenca_valor_liquido:
            impacto_financeiro_confirmado = diferenca_valor_liquido
        elif diferenca_valor_bruto:
            impacto_financeiro_confirmado = diferenca_valor_bruto
        else:
            impacto_financeiro_confirmado = ZERO
    else:
        # Fallback genérico para outros status de divergência (ex.:
        # DIVERGENCIA_AUTORIZACAO, DIVERGENCIA_NSU, DIVERGENCIA_MDR,
        # DIVERGENCIA_BANDEIRA, DIVERGENCIA_MODALIDADE, DIVERGENCIA_STATUS,
        # REVISAO_MANUAL, DIVERGENCIA_PARCELA, etc.): usa a diferença de
        # valor líquido/bruto se existir; senão fica em zero.
        if diferenca_valor_liquido:
            impacto_financeiro_confirmado = diferenca_valor_liquido
        elif diferenca_valor_bruto:
            impacto_financeiro_confirmado = diferenca_valor_bruto
        else:
            impacto_financeiro_confirmado = ZERO

    return {
        "valor_bruto_shift": valor_bruto_shift,
        "valor_bruto_rede": valor_bruto_rede,
        "diferenca_valor_bruto": diferenca_valor_bruto,
        "valor_liquido_shift": valor_liquido_shift,
        "valor_liquido_rede": valor_liquido_rede,
        "diferenca_valor_liquido": diferenca_valor_liquido,
        "valor_operacao_shift": valor_operacao_shift,
        "valor_operacao_rede": valor_operacao_rede,
        "valor_operacao_impacto": valor_operacao_impacto,
        "impacto_financeiro_confirmado": impacto_financeiro_confirmado,
        "valor_operacao_em_revisao": valor_operacao_em_revisao,
        "status_conciliacao": status_comparacao,
        "motivo": row.get("motivo"),
        "acao_recomendada": row.get("acao_recomendada", row.get("sugestao_acao")),
        "criterio_match": row.get("criterio_match"),
        "nivel_confianca": row.get("nivel_confianca"),
    }


def aplicar_impacto_financeiro(detalhado: pd.DataFrame) -> pd.DataFrame:
    """Adiciona/atualiza, em cada linha do DataFrame detalhado, as colunas
    de impacto financeiro calculadas por `calcular_impacto_financeiro`.
    Não remove nem sobrescreve outras colunas já existentes no DataFrame,
    exceto os nomes que fazem parte do próprio cálculo (ex.:
    `valor_bruto_shift` já existe e é mantido, apenas convertido/validado).
    """
    if detalhado is None or detalhado.empty:
        return detalhado
    result = detalhado.copy()
    # to_dict("records") é implementado em C e evita reconstruir uma Series
    # por linha (o que iterrows() faz); ordem das linhas é preservada, então
    # o índice de computed_df abaixo continua alinhado com result.index.
    computed_rows = [calcular_impacto_financeiro(row) for row in result.to_dict("records")]
    computed_df = pd.DataFrame(computed_rows, index=result.index)
    for col in computed_df.columns:
        result[col] = computed_df[col]
    return result


def resumo_impacto_financeiro(detalhado: pd.DataFrame) -> dict:
    """Calcula os agregados de impacto financeiro para o resumo geral:
    total por status, impacto financeiro confirmado (soma) e valor total
    em revisão (soma). Nunca conta valor em revisão como confirmado."""
    if detalhado is None or detalhado.empty:
        return {
            "impacto_financeiro_confirmado": ZERO,
            "valor_total_em_revisao": ZERO,
            "somatorio_divergencias_toleradas_conciliadas": ZERO,
            "totais_por_status_conciliacao": {},
        }
    confirmado = ZERO
    em_revisao = ZERO
    divergencias_toleradas_conciliadas = ZERO
    for value in detalhado.get("impacto_financeiro_confirmado", []):
        decimal_value = _to_decimal(value)
        if decimal_value is not None:
            confirmado += decimal_value
    for value in detalhado.get("valor_operacao_em_revisao", []):
        decimal_value = _to_decimal(value)
        if decimal_value is not None:
            em_revisao += decimal_value

    status_col = detalhado.get("status_conciliacao")
    if status_col is None:
        status_col = detalhado.get("status_comparacao")
    if status_col is not None:
        for idx, value in status_col.items():
            status_text = str(value)
            if "DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS" not in status_text:
                continue
            decimal_value = _diferenca_assinada(detalhado.loc[idx])
            if decimal_value is not None:
                divergencias_toleradas_conciliadas += decimal_value
    totais_por_status: dict[str, int] = {}
    if status_col is not None:
        for value in status_col.dropna():
            for status in str(value).split(" + "):
                totais_por_status[status] = totais_por_status.get(status, 0) + 1

    return {
        "impacto_financeiro_confirmado": confirmado,
        "valor_total_em_revisao": em_revisao,
        "somatorio_divergencias_toleradas_conciliadas": divergencias_toleradas_conciliadas,
        "totais_por_status_conciliacao": totais_por_status,
    }
