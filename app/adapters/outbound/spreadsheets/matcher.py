from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

import pandas as pd

from app.application.models import ComparisonResult
from app.domain.impacto_financeiro import aplicar_impacto_financeiro, resumo_impacto_financeiro
from .validator import WEAK_ALERT_CODES, validate_shift


TOLERANCIA_VALOR_BRUTO = Decimal("0.02")
TOLERANCIA_VALOR_LIQUIDO = Decimal("0.02")
MONEY_TOLERANCE = TOLERANCIA_VALOR_BRUTO
MONEY_FIELDS = {"valor_bruto", "valor_liquido", "valor_mdr"}
COMPARE_FIELDS = [
    "autorizacao", "nsu", "estabelecimento", "lote", "valor_bruto",
    "valor_liquido", "valor_mdr", "modalidade", "bandeira",
    "numero_parcelas", "parcela", "status", "data_venda",
    "data_recebimento", "data_vencimento",
]
# "parcela" e "numero_parcelas" saem da checagem genérica de divergência:
# elas têm regra própria (parcelas restantes, ver `parcelas_compativeis_por_restantes`)
# porque a Shift pode registrar +1 parcela em pagamentos mistos (cartão +
# dinheiro/PIX/outra forma). "data_venda" também sai: a divergência de data
# considera só "data_vencimento" (a data de venda/emissão segue exibida no
# relatório para auditoria, só não gera mais status de divergência sozinha).
# Continuam em COMPARE_FIELDS só para os campos "_original"/"_normalizado"
# do relatório (auditoria).
FIELDS_FOR_GENERIC_DIVERGENCE = [
    field for field in COMPARE_FIELDS
    if field not in {"parcela", "numero_parcelas", "data_venda"}
]
STATUS_BY_FIELD = {
    "autorizacao": "DIVERGENCIA_AUTORIZACAO",
    "nsu": "DIVERGENCIA_NSU",
    "valor_bruto": "DIVERGENCIA_VALOR_BRUTO",
    "valor_liquido": "DIVERGENCIA_VALOR_LIQUIDO",
    "valor_mdr": "DIVERGENCIA_MDR",
    "bandeira": "DIVERGENCIA_BANDEIRA",
    "modalidade": "DIVERGENCIA_MODALIDADE",
    "status": "DIVERGENCIA_STATUS",
    "data_venda": "DIVERGENCIA_DATA",
    "data_recebimento": "DIVERGENCIA_DATA",
    "data_vencimento": "DIVERGENCIA_DATA",
    "estabelecimento": "REVISAO_MANUAL",
    "lote": "REVISAO_MANUAL",
}

# Prioridade de um critério de parcela quando é preciso escolher, entre
# várias parcelas da Rede com a mesma autorização, qual é a correspondente.
PARCELA_CRITERIO_RANK = {
    "EXATA": 3,
    "COMPATIVEL_POR_PARCELAS_RESTANTES": 2,
    "DADOS_PARCELA_INSUFICIENTES": 1,
    "DIVERGENCIA_PARCELAS_RESTANTES": 0,
}


# =============================================================================
# Fase 1 — normalização (auxiliares usados na fase de candidatos/validação).
# A normalização "pesada" de cada campo já acontece em `normalizer.py`; as
# funções abaixo tratam da comparação/derivação usada especificamente pelo
# matching (autorização e parcelas restantes), incluindo casos de dados ruins.
# =============================================================================

def normalizar_autorizacao(valor: str | int | None) -> str | None:
    """Normaliza um número de autorização para fins de comparação Rede x Shift.

    Regras: mantém só dígitos, remove espaços e qualquer caractere não
    numérico do "esqueleto" usado na comparação, remove zeros à esquerda
    (ex.: "0281309" e "281309" tornam-se equivalentes). Autorizações
    alfanuméricas (ex.: "4Y9HDN") são mantidas como estão, só maiúsculas e
    sem espaços — no relatório financeiro do Shift a autorização pode não
    ser puramente numérica. Valor ausente/vazio nunca vira "0": retorna
    `None`, e `None` nunca é tratado como igual a outro `None` no matching.
    """
    if valor is None:
        return None
    text = str(valor).strip()
    if not text or text.upper() in {"NAN", "NONE", "NAT", "-"}:
        return None
    text = re.sub(r"\s+", "", text).upper()
    text = re.sub(r"\.0+$", "", text)  # ex.: "281309.0" vindo de planilha
    if text.isdigit():
        text = text.lstrip("0")
    if not text:
        return None
    return text


def calcular_parcelas_restantes(parcela_atual: object, total_parcelas: object) -> int | None:
    """Calcula quantas parcelas ainda restam (incluindo a atual como já paga).

    ``parcelas_restantes = total_parcelas - parcela_atual``

    Retorna `None` (nunca lança exceção) quando os dados não permitem um
    cálculo confiável: valor ausente, não numérico, total zero/negativo,
    parcela atual zero/negativa, ou parcela atual maior que o total (dado
    inconsistente — mais seguro tratar como "dados insuficientes" do que
    arriscar um número errado).
    """
    if parcela_atual is None or total_parcelas is None:
        return None
    if isinstance(parcela_atual, float) and pd.isna(parcela_atual):
        return None
    if isinstance(total_parcelas, float) and pd.isna(total_parcelas):
        return None
    try:
        parcela_atual = int(str(parcela_atual).strip())
        total_parcelas = int(str(total_parcelas).strip())
    except (TypeError, ValueError):
        return None
    if total_parcelas <= 0 or parcela_atual <= 0:
        return None
    if parcela_atual > total_parcelas:
        return None
    return total_parcelas - parcela_atual


def parcelas_compativeis_por_restantes(
    shift_parcela: object, shift_total: object,
    rede_parcela: object, rede_total: object,
) -> tuple[bool, str, int | None, int | None]:
    """Decide se a parcela do Shift e da Rede são compatíveis.

    Prioridade:
    1. EXATA — parcela e total idênticos nos dois lados.
    2. COMPATIVEL_POR_PARCELAS_RESTANTES — números brutos diferentes, mas o
       saldo de parcelas restantes bate (cobre o caso de pagamento misto na
       Shift, que registra uma parcela "a mais" que a Rede).
    3. DIVERGENCIA_PARCELAS_RESTANTES — saldo de parcelas restantes diferente.
    4. DADOS_PARCELA_INSUFICIENTES — não foi possível calcular o saldo de um
       dos lados (dado ausente, inválido, ou parcela atual > total).

    Retorna (compativel, criterio, parcelas_restantes_shift, parcelas_restantes_rede).
    """
    restantes_shift = calcular_parcelas_restantes(shift_parcela, shift_total)
    restantes_rede = calcular_parcelas_restantes(rede_parcela, rede_total)

    if restantes_shift is None or restantes_rede is None:
        return False, "DADOS_PARCELA_INSUFICIENTES", restantes_shift, restantes_rede

    try:
        exata = (
            int(shift_parcela) == int(rede_parcela)
            and int(shift_total) == int(rede_total)
        )
    except (TypeError, ValueError):
        exata = False
    if exata:
        return True, "EXATA", restantes_shift, restantes_rede

    if restantes_shift == restantes_rede:
        return True, "COMPATIVEL_POR_PARCELAS_RESTANTES", restantes_shift, restantes_rede

    return False, "DIVERGENCIA_PARCELAS_RESTANTES", restantes_shift, restantes_rede


def _key(row, fields) -> tuple | None:
    values = tuple(row.get(field) for field in fields)
    return values if all(value is not None and not pd.isna(value) for value in values) else None


def _equal(a, b, field) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return False
    if field in MONEY_FIELDS:
        return Decimal(a) == Decimal(b)
    return a == b


def _index(df, fields):
    index = defaultdict(list)
    for idx, row in df.iterrows():
        key = _key(row, fields)
        if key is not None:
            index[key].append(idx)
    return index


def _unique_conciliation_key(row) -> tuple | None:
    """Chave estável para não contar a mesma transação conciliada mais de uma vez."""
    keys = (
        row.get("shift_autorizacao_normalizado") or row.get("rede_autorizacao_normalizado"),
        row.get("data_venda_shift") or row.get("data_venda_rede"),
        row.get("valor_bruto_shift") if row.get("valor_bruto_shift") is not None else row.get("valor_bruto_rede"),
        row.get("valor_liquido_shift") if row.get("valor_liquido_shift") is not None else row.get("valor_liquido_rede"),
        row.get("parcela_shift") if row.get("parcela_shift") is not None else row.get("parcela_rede"),
        row.get("qtd_parcelas_shift") if row.get("qtd_parcelas_shift") is not None else row.get("qtd_parcelas_rede"),
    )
    if not any(value is not None and not pd.isna(value) for value in keys):
        return None
    normalized = []
    for value in keys:
        if value is None or pd.isna(value):
            normalized.append(None)
        else:
            normalized.append(str(value))
    return tuple(normalized)


def _collapse_shift_splits(
    df_rede: pd.DataFrame, df_shift: pd.DataFrame
) -> tuple[pd.DataFrame, int, int]:
    """Consolida múltiplas O.S. do Shift com a mesma autorização E a mesma
    parcela (mesmo `parcela`/`numero_parcelas`) — ou seja, o mesmo pagamento
    fisicamente dividido em mais de um registro (ex.: pagamento fracionado).

    O agrupamento acontece antes da classificação como duplicidade/faltante,
    preservando rastreabilidade das linhas originais. Se campos críticos do
    grupo (empresa, data usada no match, modalidade, bandeira) divergirem
    dentro da MESMA parcela, o grupo fica marcado como ambíguo e segue para
    revisão manual em vez de ser conciliado automaticamente.

    Importante: a checagem de ambiguidade é por (autorização + parcela), não
    pela autorização inteira. Uma venda parcelada normal repete a mesma
    autorização em cada parcela (parcela 1, 2, 3...), cada uma com seu
    próprio vencimento (tipicamente ~30 dias de diferença uma da outra) —
    isso não é ambíguo, são transações distintas que devem ser conciliadas
    cada uma com sua parcela correspondente na Rede. Bug real corrigido
    aqui: antes, a checagem agrupava só por autorização e incluía "parcela"
    na assinatura comparada, então parcelas diferentes (normais) já bastavam
    para marcar TODAS as linhas daquela autorização como
    AGRUPAMENTO_OS_AMBIGUO, jogando parcelas legítimas para revisão manual
    em vez de conciliá-las normalmente.
    """
    original_count = len(df_shift)
    if "autorizacao" not in df_shift.columns:
        return df_shift, original_count, 0

    df_shift = df_shift.copy()
    df_shift["_agrupamento_shift_ambiguo"] = False
    df_shift["_duplicidade_exata_suspeita"] = False
    df_shift["_multiplas_os_mesma_autorizacao"] = False
    df_shift["_criterio_agrupamento"] = None
    df_shift["_motivo_agrupamento"] = None
    df_shift["_autorizacao_repetida_mesmo_vencimento"] = False
    installment_fields = [
        field for field in ("parcela", "numero_parcelas") if field in df_shift.columns
    ]
    # Assinatura comparada dentro de cada (autorização, parcela): parcela e
    # numero_parcelas NÃO entram aqui porque já são o critério que define o
    # subgrupo — divergência entre parcelas diferentes não é ambiguidade.
    signature_fields = [
        field for field in ("empresa", "data_shift_usada_para_match", "modalidade", "bandeira")
        if field in df_shift.columns
    ]
    duplicate_signature_fields = [
        field for field in
        [
            "os_shift", "codigo_registro", "valor_bruto", "valor_liquido",
            "data_shift_usada_para_match", "parcela", "numero_parcelas",
        ]
        if field in df_shift.columns
    ]
    installment_group_fields = ["autorizacao", *installment_fields]
    for _, installment_group in df_shift.groupby(installment_group_fields, dropna=False, sort=False):
        if len(installment_group) < 2:
            continue
        if signature_fields:
            signatures = installment_group[signature_fields].astype(str).drop_duplicates()
        else:
            signatures = pd.DataFrame([["__SEM_CRITICOS__"]])
        if len(signatures) > 1:
            df_shift.loc[installment_group.index, "_agrupamento_shift_ambiguo"] = True
            continue
        if duplicate_signature_fields:
            duplicates = installment_group[duplicate_signature_fields].astype(str).duplicated(keep=False)
            if duplicates.any():
                df_shift.loc[installment_group.index[duplicates], "_duplicidade_exata_suspeita"] = True

    # Alerta (não bloqueia, não soma automaticamente): mesma autorização e
    # mesmo vencimento no Shift, mas com números de parcela diferentes. Não
    # tratamos como a mesma parcela dividida (regra acima já cobre isso) nem
    # como parcelas normais e distintas sem mais análise — vencimentos
    # coincidirem exatamente quando a parcela é diferente é um padrão
    # suspeito de erro de cadastro (ex.: a mesma cobrança lançada duas vezes
    # com números de parcela diferentes por engano). Cada linha ainda segue
    # o fluxo normal de conciliação contra a Rede; isso só marca a linha
    # para revisão visível no relatório.
    if "data_vencimento" in df_shift.columns and "parcela" in df_shift.columns:
        dados_validos = df_shift["autorizacao"].notna() & df_shift["data_vencimento"].notna()
        for _, vencimento_group in df_shift.loc[dados_validos].groupby(
            ["autorizacao", "data_vencimento"], sort=False
        ):
            if len(vencimento_group) < 2:
                continue
            if vencimento_group["parcela"].dropna().nunique() > 1:
                df_shift.loc[vencimento_group.index, "_autorizacao_repetida_mesmo_vencimento"] = True

    drop_indexes: set[int] = set()
    replacements: list[pd.Series] = []
    group_fields = [
        field for field in [
            "autorizacao", "empresa", "data_shift_usada_para_match",
            "modalidade", "bandeira", "parcela", "numero_parcelas",
        ]
        if field in df_shift.columns
    ]
    grouped = df_shift.groupby(group_fields, dropna=False, sort=False)
    for _, group in grouped:
        if len(group) < 2:
            continue
        if bool(group.iloc[0].get("_agrupamento_shift_ambiguo", False)):
            continue
        unique_os = group.get("os_shift", pd.Series(dtype=object)).dropna().astype(str).nunique()
        if unique_os <= 1:
            continue

        combined = group.iloc[0].copy()
        for field in ("valor_bruto", "valor_liquido", "valor_mdr", "valor_desconto"):
            if field in group.columns:
                values = group[field].dropna()
                combined[field] = (
                    sum((Decimal(value) for value in values), Decimal("0.00"))
                    if len(values)
                    else None
                )
        combined["_row"] = group["_row"].iloc[0]
        combined["_rows_agrupadas"] = ", ".join(
            str(value) for value in group["_row"].tolist()
        )
        combined["_linhas_agrupadas_shift"] = len(group)
        combined["_codigos_conta_agrupados"] = " | ".join(
            str(value) for value in group.get("codigo_conta", pd.Series(dtype=object)).dropna()
        )
        combined["_codigos_registro_agrupados"] = " | ".join(
            str(value) for value in group.get("codigo_registro", pd.Series(dtype=object)).dropna()
        )
        combined["_autorizacoes_originais_agrupadas"] = " | ".join(
            str(value) for value in group.get("raw_autorizacao", pd.Series(dtype=object)).dropna()
        )
        combined["_os_shift_agrupadas"] = " | ".join(
            str(value) for value in group.get("os_shift", pd.Series(dtype=object)).dropna()
        )
        combined["_valores_brutos_originais_agrupados"] = " | ".join(
            str(value) for value in group["valor_bruto"].dropna()
        )
        combined["_valores_liquidos_originais_agrupados"] = " | ".join(
            str(value) for value in group.get("valor_liquido", pd.Series(dtype=object)).dropna()
        )
        combined["_descricoes_originais_agrupadas"] = " | ".join(
            str(value) for value in group.get("descricao", pd.Series(dtype=object)).dropna()
        )
        combined["_multiplas_os_mesma_autorizacao"] = True
        combined["_criterio_agrupamento"] = (
            "AUTORIZACAO+EMPRESA+DATA_SHIFT+Bandeira+MODALIDADE+PARCELA+TOTAL_PARCELAS"
        )
        combined["_motivo_agrupamento"] = (
            "Múltiplas O.S. da Shift com a mesma autorização foram consolidadas antes do matching."
        )
        replacements.append(combined)
        drop_indexes.update(int(index) for index in group.index)

    if not replacements:
        return df_shift, original_count, 0
    remaining = df_shift.drop(index=list(drop_indexes)).copy()
    collapsed = pd.concat([remaining, pd.DataFrame(replacements)], ignore_index=True)
    return collapsed, original_count, len(replacements)


# =============================================================================
# Fase 2 — geração de candidatos (somente por autorização normalizada).
# =============================================================================

def _autorizacao_index(df: pd.DataFrame) -> dict[str, list[int]]:
    # Evita iterrows() (reconstrói uma Series com todas as colunas por
    # linha); usa só a coluna necessária, vetorizada com .map().
    index = defaultdict(list)
    if df.empty or "autorizacao" not in df.columns:
        return index
    autorizacoes = df["autorizacao"].map(normalizar_autorizacao)
    for idx, auth in zip(df.index, autorizacoes):
        if auth:
            index[auth].append(idx)
    return index


def _score_candidate(shift, rede) -> tuple[bool, int, bool, bool, bool]:
    """Ordena candidatos com a mesma autorização (ex.: várias parcelas da
    mesma venda) para escolher a correspondência mais provável: valor
    dentro da tolerância > melhor critério de parcela > vencimento igual
    (é o que diferencia uma parcela da outra quando autorização, valor e
    parcela empatam) > NSU igual > data de venda igual. Usado só para
    desempate; a autorização já é garantida igual."""
    valor_shift, valor_rede = shift.get("valor_bruto"), rede.get("valor_bruto")
    valor_ok = (
        valor_shift is not None and valor_rede is not None
        and not pd.isna(valor_shift) and not pd.isna(valor_rede)
        and abs(Decimal(valor_shift) - Decimal(valor_rede)) <= MONEY_TOLERANCE
    )
    _, criterio_parcela, _, _ = parcelas_compativeis_por_restantes(
        shift.get("parcela"), shift.get("numero_parcelas"),
        rede.get("parcela"), rede.get("numero_parcelas"),
    )
    parcela_rank = PARCELA_CRITERIO_RANK.get(criterio_parcela, -1)
    venc_shift, venc_rede = shift.get("data_vencimento"), rede.get("data_vencimento")
    vencimento_ok = bool(
        venc_shift is not None and venc_rede is not None
        and not pd.isna(venc_shift) and not pd.isna(venc_rede)
        and venc_shift == venc_rede
    )
    nsu_shift, nsu_rede = shift.get("nsu"), rede.get("nsu")
    nsu_ok = bool(
        nsu_shift and nsu_rede and not pd.isna(nsu_shift) and not pd.isna(nsu_rede)
        and nsu_shift == nsu_rede
    )
    data_shift, data_rede = shift.get("data_venda"), rede.get("data_venda")
    data_ok = bool(
        data_shift is not None and data_rede is not None
        and not pd.isna(data_shift) and not pd.isna(data_rede)
        and data_shift == data_rede
    )
    return (valor_ok, parcela_rank, vencimento_ok, nsu_ok, data_ok)


# =============================================================================
# Fase 2b — correspondência secundária (usada só quando a autorização não
# bateu ou está ausente). Nunca conciliada automaticamente: gera apenas um
# alerta de "possível correspondência com autorização divergente" para
# revisão manual, com exigência estrita de unicidade 1 para 1.
# =============================================================================

# Tolerância para valor líquido no fluxo secundário. Mantida separada de
# MONEY_TOLERANCE (mesmo valor hoje) para poder ser ajustada de forma
# independente sem afetar a tolerância de centavos do fluxo principal.
SECONDARY_VALOR_LIQUIDO_TOLERANCE = Decimal("0.02")
CRITERIO_ALERTA_SECUNDARIO = (
    "VALOR_BRUTO+VALOR_LIQUIDO(TOLERANCIA)+DATA_VENDA+MODALIDADE+BANDEIRA+PARCELA"
)


def _rede_status_normal(rede) -> bool:
    """Só permite usar uma linha da Rede como candidata de alta confiança se
    ela representar uma venda/pagamento normal: valor bruto diferente de
    zero e sem indicação de cancelamento, contestação, desagendamento ou
    estorno em `status`/`cancelamento`."""
    valor = rede.get("valor_bruto")
    if valor is None or pd.isna(valor) or Decimal(valor) == 0:
        return False
    text = f"{rede.get('status') or ''} {rede.get('cancelamento') or ''}".upper()
    return not any(token in text for token in ("CANCEL", "CONTEST", "DESAGEND", "ESTORN"))


def _rede_status_normal_mask(df: pd.DataFrame) -> pd.Series:
    """Versão vetorizada de `_rede_status_normal`, para filtrar um
    DataFrame inteiro de uma vez em vez de linha a linha."""
    if df.empty:
        return pd.Series([], dtype=bool, index=df.index)
    valor = df["valor_bruto"] if "valor_bruto" in df.columns else pd.Series(None, index=df.index)
    valor_ok = valor.map(
        lambda v: v is not None and not pd.isna(v) and Decimal(v) != 0
    )
    status = (df["status"] if "status" in df.columns else pd.Series(None, index=df.index)).fillna("")
    cancelamento = (
        df["cancelamento"] if "cancelamento" in df.columns else pd.Series(None, index=df.index)
    ).fillna("")
    text = (status.astype(str) + " " + cancelamento.astype(str)).str.upper()
    proibido = text.str.contains("CANCEL|CONTEST|DESAGEND|ESTORN", regex=True)
    return valor_ok & ~proibido


def gerar_chave_secundaria_sem_autorizacao(row) -> tuple | None:
    """Chave composta exata (valor bruto, data da venda, modalidade,
    bandeira) usada só para localizar candidatos quando a autorização não é
    uma chave utilizável. Valor líquido (tolerância) e parcela (regra de
    parcelas restantes) são validados à parte, pois não são igualdade
    exata. Retorna `None` se faltar qualquer um dos quatro campos."""
    valor_bruto, data_venda = row.get("valor_bruto"), row.get("data_venda")
    modalidade, bandeira = row.get("modalidade"), row.get("bandeira")
    if any(
        value is None or (isinstance(value, float) and pd.isna(value))
        for value in (valor_bruto, data_venda, modalidade, bandeira)
    ):
        return None
    return (Decimal(valor_bruto), data_venda, modalidade, bandeira)


def _campo_igual_chave(coluna: pd.Series, valor_chave) -> pd.Series:
    """Compara uma coluna inteira a um valor de chave, com o mesmo critério
    de "presente" usado por `gerar_chave_secundaria_sem_autorizacao`
    (None ou float NaN contam como ausente)."""
    return coluna.map(
        lambda v: v is not None
        and not (isinstance(v, float) and pd.isna(v))
        and v == valor_chave
    )


def buscar_candidatos_secundarios(shift_row, rede_pool: pd.DataFrame) -> list[int]:
    """Candidatos Rede para uma linha do Shift sem autorização compatível.

    Exige, simultaneamente: mesma chave composta exata (valor bruto, data
    da venda, modalidade, bandeira), valor líquido presente nos dois lados
    e dentro da tolerância, parcela compatível (exata ou por parcelas
    restantes) e status da Rede de venda normal (não cancelada/contestada/
    desagendada/zerada). Não usa fallback: se qualquer critério não puder
    ser confirmado (ex.: valor líquido ausente), a linha não vira candidata.

    Implementação: os filtros vetorizáveis (status normal, chave composta,
    tolerância do valor líquido) reduzem `rede_pool` a um subconjunto pequeno
    antes de qualquer iteração linha a linha — evita repetir esses cálculos
    para cada uma das potencialmente milhares de linhas da Rede a cada
    chamada (esta função é chamada uma vez por linha pendente do Shift).
    Só a checagem de parcela (que já é uma função auxiliar reaproveitada)
    continua sendo feita candidato a candidato, sobre o subconjunto já
    filtrado.
    """
    chave_shift = gerar_chave_secundaria_sem_autorizacao(shift_row)
    if chave_shift is None:
        return []
    valor_liquido_shift = shift_row.get("valor_liquido")
    if valor_liquido_shift is None or pd.isna(valor_liquido_shift):
        return []
    if rede_pool.empty:
        return []

    valor_bruto_chave, data_venda_chave, modalidade_chave, bandeira_chave = chave_shift

    status_ok = _rede_status_normal_mask(rede_pool)
    if not status_ok.any():
        return []

    valor_bruto_col = rede_pool.get("valor_bruto", pd.Series(None, index=rede_pool.index))
    valor_bruto_match = valor_bruto_col.map(
        lambda v: v is not None and not pd.isna(v) and Decimal(v) == valor_bruto_chave
    )
    data_venda_match = _campo_igual_chave(
        rede_pool.get("data_venda", pd.Series(None, index=rede_pool.index)), data_venda_chave
    )
    modalidade_match = _campo_igual_chave(
        rede_pool.get("modalidade", pd.Series(None, index=rede_pool.index)), modalidade_chave
    )
    bandeira_match = _campo_igual_chave(
        rede_pool.get("bandeira", pd.Series(None, index=rede_pool.index)), bandeira_chave
    )

    candidato_mask = status_ok & valor_bruto_match & data_venda_match & modalidade_match & bandeira_match
    if not candidato_mask.any():
        return []

    candidatos_df = rede_pool.loc[candidato_mask]
    valor_liquido_col = candidatos_df.get("valor_liquido", pd.Series(None, index=candidatos_df.index))
    valor_liquido_ok = valor_liquido_col.map(
        lambda v: v is not None and not pd.isna(v)
        and abs(Decimal(v) - Decimal(valor_liquido_shift)) <= SECONDARY_VALOR_LIQUIDO_TOLERANCE
    )
    candidatos_df = candidatos_df.loc[valor_liquido_ok]
    if candidatos_df.empty:
        return []

    candidatos = []
    for rede_idx, rede_row in candidatos_df.iterrows():
        compativel, _, _, _ = parcelas_compativeis_por_restantes(
            shift_row.get("parcela"), shift_row.get("numero_parcelas"),
            rede_row.get("parcela"), rede_row.get("numero_parcelas"),
        )
        if compativel:
            candidatos.append(rede_idx)
    return candidatos


def _mapear_candidatos_secundarios(shift_pending, df_shift, rede_pool):
    """Constrói os dois mapas necessários para validar unicidade 1-para-1:
    shift -> [rede candidatos] e rede -> [shift candidatos]."""
    shift_to_rede: dict[int, list[int]] = {}
    rede_to_shift: dict[int, list[int]] = defaultdict(list)
    for shift_idx in shift_pending:
        rede_candidatos = buscar_candidatos_secundarios(df_shift.loc[shift_idx], rede_pool)
        if rede_candidatos:
            shift_to_rede[shift_idx] = rede_candidatos
            for rede_idx in rede_candidatos:
                rede_to_shift[rede_idx].append(shift_idx)
    return shift_to_rede, rede_to_shift


def validar_unicidade_candidato(
    shift_idx: int, shift_to_rede: dict, rede_to_shift: dict
) -> tuple[bool, list[int], list[int]]:
    """Valida a regra de unicidade 1 para 1: só é seguro sugerir um par
    quando o candidato Rede também aponta de volta para exatamente esse
    candidato Shift (nenhum dos dois lados é compartilhado)."""
    rede_candidatos = shift_to_rede.get(shift_idx, [])
    if len(rede_candidatos) != 1:
        return False, rede_candidatos, []
    rede_idx = rede_candidatos[0]
    shift_candidatos = rede_to_shift.get(rede_idx, [])
    return len(shift_candidatos) == 1, rede_candidatos, shift_candidatos


def classificar_alerta_autorizacao_divergente(
    unico: bool, rede_candidatos: list[int], shift_candidatos: list[int]
) -> str:
    if unico:
        return "REVISAR_AUTORIZACAO_DIVERGENTE_ALTA_CONFIANCA"
    if rede_candidatos or shift_candidatos:
        return "AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL"
    return "SEM_AUTORIZACAO_COMPATIVEL"


# =============================================================================
# Fase 4 — classificação / motivo textual.
# =============================================================================

def _action(statuses):
    if statuses and all(
        status.startswith("CONCILIADO")
        or status == "DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS"
        for status in statuses
    ):
        return "Sem ação necessária"
    rules = [
        (
            "CONCILIADO_COM_DIVERGENCIA_TOLERADA",
            "Diferença de até R$ 0,02 aceita; manter para auditoria",
        ),
        ("ERRO_CADASTRAL_SHIFT", "Corrigir cadastro no Shift"),
        ("AGRUPAMENTO_OS_AMBIGUO", "Revisão manual obrigatória"),
        ("AGRUPAMENTO_OS_VALOR_DIVERGENTE", "Conferir soma das O.S. agrupadas"),
        ("AGRUPAMENTO_SHIFT_AMBIGUO", "Revisão manual obrigatória"),
        ("DUPLICIDADE_EXATA_SUSPEITA", "Revisar possível linha duplicada no Shift"),
        (
            "AUTORIZACAO_REPETIDA_MESMO_VENCIMENTO_PARCELA_DIFERENTE",
            "Confirmar no Shift: mesma autorização e mesmo vencimento com números de "
            "parcela diferentes — verificar se não é a mesma cobrança lançada duas vezes.",
        ),
        ("REVISAR_AUTORIZACAO_DIVERGENTE_ALTA_CONFIANCA", "Revisar autorização divergente manualmente."),
        (
            "AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL",
            "Revisar manualmente: múltiplos candidatos possíveis, sem correspondência automática.",
        ),
        ("DIVERGENCIA_PARCELA", "Verificar parcela"),
        ("DADOS_PARCELA_INSUFICIENTES", "Conferir parcela/quantidade de parcelas manualmente"),
        ("DIVERGENCIA_VALOR_BRUTO", "Verificar valor bruto"),
        ("SEM_AUTORIZACAO_COMPATIVEL", "Verificar se a venda foi processada ou lançada na unidade correta"),
        ("NAO_ENCONTRADO_NA_REDE", "Verificar se a venda foi processada ou lançada na unidade correta"),
        ("NAO_ENCONTRADO_NO_SHIFT", "Cadastrar ou justificar a transação no Shift"),
    ]
    return next((action for status, action in rules if status in statuses), "Revisão manual obrigatória")


def _gerar_motivo(
    criterio_match: str, criterio_parcela: str | None, divergent_fields: list[str],
) -> str:
    """Explica em texto por que a linha recebeu o status atribuído (usado
    na coluna "motivo" do relatório detalhado)."""
    if criterio_match == "AUTORIZACAO_AUSENTE_NO_SHIFT":
        return "Autorização ausente ou inválida no Shift; não é possível localizar correspondência."
    if criterio_match == "SEM_AUTORIZACAO_COMPATIVEL":
        return "Sem autorização compatível na Rede. Fallback por data e valor não permitido."
    if criterio_match == "SEM_CORRESPONDENCIA_NO_SHIFT":
        return "Autorização da Rede não encontrada em nenhuma linha do Shift."
    if criterio_match == "REVISAR_AUTORIZACAO_DIVERGENTE_ALTA_CONFIANCA":
        return (
            "Possível correspondência 1 para 1 encontrada por valor bruto, valor líquido, "
            "data de venda, modalidade, bandeira e parcela compatível, porém a autorização "
            "é divergente. Revisão manual obrigatória."
        )
    if criterio_match == "AUTORIZACAO_NORMALIZADA_AGRUPAMENTO_SHIFT":
        return (
            "Múltiplas linhas da Shift com mesma autorização e campos críticos "
            "compatíveis foram somadas e conciliadas com uma única transação da Rede."
        )
    if criterio_match == "AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL":
        return (
            "Sem autorização compatível e mais de uma transação corresponde por valor bruto, "
            "valor líquido, data de venda, modalidade, bandeira e parcela. Não é seguro parear "
            "automaticamente; revise manualmente."
        )

    parts = ["Autorização compatível."]
    if criterio_parcela == "EXATA":
        parts.append("Parcela e quantidade de parcelas idênticas.")
    elif criterio_parcela == "COMPATIVEL_POR_PARCELAS_RESTANTES":
        parts.append(
            "Parcelas diferentes no bruto, mas equivalentes por parcelas restantes "
            "(possível pagamento misto na Shift, ex.: cartão + dinheiro/PIX)."
        )
    elif criterio_parcela == "DIVERGENCIA_PARCELAS_RESTANTES":
        parts.append("Saldo de parcelas restantes diferente entre Shift e Rede.")
    elif criterio_parcela == "DADOS_PARCELA_INSUFICIENTES":
        parts.append("Dados de parcela ausentes ou inválidos em um dos lados; não comparados automaticamente.")

    if "valor_bruto" in divergent_fields:
        parts.append("Valor bruto divergente.")
    if "data_venda" in divergent_fields:
        parts.append("Data da venda divergente.")
    other = [f for f in divergent_fields if f not in {"valor_bruto", "data_venda"}]
    if other:
        parts.append(f"Outros campos divergentes: {', '.join(other)}.")
    return " ".join(parts)


def _detail(
    shift, rede, statuses, fields, match_type,
    criterio_parcela=None, restantes_shift=None, restantes_rede=None,
    parcelas_compativeis=None, motivo="",
    nivel_confianca=None, criterio_alerta=None, unico_candidato=None,
):
    row = {
        "tipo_correspondencia": match_type,
        "criterio_match": match_type,
        "status_comparacao": " + ".join(dict.fromkeys(statuses)),
        "status_conciliacao": " + ".join(dict.fromkeys(statuses)),
        "campos_divergentes": ", ".join(dict.fromkeys(fields)),
        "sugestao_acao": _action(statuses),
        "acao_recomendada": _action(statuses),
        "motivo": motivo,
        "criterio_parcela": criterio_parcela,
        "parcelas_restantes_shift": restantes_shift,
        "parcelas_restantes_rede": restantes_rede,
        "parcelas_compativeis": parcelas_compativeis,
        "nivel_confianca": nivel_confianca,
        "criterio_alerta": criterio_alerta,
        "unico_candidato": unico_candidato,
        "houve_agrupamento_shift": bool(
            shift is not None and (shift.get("_linhas_agrupadas_shift") or 0) > 1
        ),
        "linhas_shift_agrupadas": (
            None if shift is None else shift.get("_linhas_agrupadas_shift", 1)
        ),
        "ids_linhas_shift": None if shift is None else shift.get(
            "_rows_agrupadas", str(shift.get("_row", ""))
        ),
        "codigos_conta_shift": None if shift is None else shift.get(
            "_codigos_conta_agrupados", shift.get("codigo_conta")
        ),
        "codigos_registro_shift": None if shift is None else shift.get(
            "_codigos_registro_agrupados", shift.get("codigo_registro")
        ),
        "autorizacoes_originais_shift": None if shift is None else shift.get(
            "_autorizacoes_originais_agrupadas", shift.get("raw_autorizacao")
        ),
        "valores_brutos_originais_shift": None if shift is None else shift.get(
            "_valores_brutos_originais_agrupados", shift.get("valor_bruto")
        ),
        "valores_liquidos_originais_shift": None if shift is None else shift.get(
            "_valores_liquidos_originais_agrupados", shift.get("valor_liquido")
        ),
        "descricoes_originais_shift": None if shift is None else shift.get(
            "_descricoes_originais_agrupadas", shift.get("descricao")
        ),
        "valor_bruto_shift_agrupado": None if shift is None else shift.get("valor_bruto"),
        "valor_liquido_shift_agrupado": None if shift is None else shift.get("valor_liquido"),
        "quantidade_os_agrupadas": None if shift is None else shift.get("_linhas_agrupadas_shift", 1),
        "lista_os_shift": None if shift is None else shift.get(
            "_os_shift_agrupadas",
            shift.get("os_shift"),
        ),
        "lista_codigos_conta_shift": None if shift is None else shift.get(
            "_codigos_conta_agrupados", shift.get("codigo_conta")
        ),
        "lista_codigos_registro_shift": None if shift is None else shift.get(
            "_codigos_registro_agrupados", shift.get("codigo_registro")
        ),
        "lista_linhas_originais_shift": None if shift is None else shift.get(
            "_rows_agrupadas", str(shift.get("_row", ""))
        ),
        "criterio_agrupamento": None if shift is None else shift.get("_criterio_agrupamento"),
        "motivo_agrupamento": None if shift is None else shift.get("_motivo_agrupamento"),
        "data_shift_usada_para_match": None if shift is None else shift.get(
            "data_shift_usada_para_match", shift.get("data_venda")
        ),
        "campo_data_shift_usado": None if shift is None else shift.get(
            "campo_data_shift_usado", "data_venda"
        ),
        "data_rede_usada_para_match": None if rede is None else rede.get("data_venda"),
        "campo_data_rede_usado": None if rede is None else "Data original da venda",
        "rede_arquivo_origem": None if rede is None else rede.get("rede_arquivo_origem"),
        "rede_aba_origem": None if rede is None else rede.get("rede_aba_origem"),
        "rede_linha_original": None if rede is None else rede.get("rede_linha_original", rede.get("_row")),
        "rede_data_relatorio": None if rede is None else rede.get("rede_data_relatorio"),
        "rede_data_recebimento": None if rede is None else rede.get("data_recebimento"),
        "rede_duplicado_entre_arquivos": None if rede is None else rede.get("rede_duplicado_entre_arquivos"),
        "rede_arquivos_duplicados": None if rede is None else rede.get("rede_arquivos_duplicados"),
        "criterio_deduplicacao_rede": None if rede is None else rede.get("criterio_deduplicacao_rede"),
    }
    for prefix, source in (("shift", shift), ("rede", rede)):
        for field in COMPARE_FIELDS:
            row[f"{prefix}_{field}_original"] = None if source is None else source.get(f"raw_{field}", source.get(field))
            row[f"{prefix}_{field}_normalizado"] = None if source is None else source.get(field)
        row[f"linha_{prefix}"] = None if source is None else source.get("_row")
    for field in MONEY_FIELDS:
        shift_value = None if shift is None else shift.get(field)
        rede_value = None if rede is None else rede.get(field)
        present = (
            shift_value is not None
            and rede_value is not None
            and not pd.isna(shift_value)
            and not pd.isna(rede_value)
        )
        row[f"diferenca_{field}"] = (
            Decimal(rede_value) - Decimal(shift_value) if present else None
        )
    # Aliases "curtos" pedidos explicitamente para o relatório detalhado
    # (mesmos valores das colunas _normalizado/_original acima, só com o
    # nome de campo antes do prefixo shift/rede em vez de depois).
    row["autorizacao_shift_original"] = row["shift_autorizacao_original"]
    row["autorizacao_rede_original"] = row["rede_autorizacao_original"]
    row["autorizacao_shift_normalizada"] = row["shift_autorizacao_normalizado"]
    row["autorizacao_rede_normalizada"] = row["rede_autorizacao_normalizado"]
    row["valor_bruto_shift"] = row["shift_valor_bruto_normalizado"]
    row["valor_bruto_rede"] = row["rede_valor_bruto_normalizado"]
    row["valor_liquido_shift"] = row["shift_valor_liquido_normalizado"]
    row["valor_liquido_rede"] = row["rede_valor_liquido_normalizado"]
    row["diferenca_valor_liquido"] = row["diferenca_valor_liquido"]
    row["data_venda_shift"] = row["shift_data_venda_normalizado"]
    row["data_venda_rede"] = row["rede_data_venda_normalizado"]
    row["modalidade_shift"] = row["shift_modalidade_normalizado"]
    row["modalidade_rede"] = row["rede_modalidade_normalizado"]
    row["bandeira_shift"] = row["shift_bandeira_normalizado"]
    row["bandeira_rede"] = row["rede_bandeira_normalizado"]
    row["parcela_shift"] = row["shift_parcela_normalizado"]
    row["parcela_rede"] = row["rede_parcela_normalizado"]
    row["qtd_parcelas_shift"] = row["shift_numero_parcelas_normalizado"]
    row["qtd_parcelas_rede"] = row["rede_numero_parcelas_normalizado"]
    return row


# =============================================================================
# Orquestração das 4 fases.
# =============================================================================

def compare_rede_shift(df_rede: pd.DataFrame, df_shift: pd.DataFrame) -> ComparisonResult:
    if df_rede.empty and df_shift.empty:
        raise ValueError("Os dois arquivos estão vazios ou a aba selecionada não contém dados válidos.")
    if df_rede.empty:
        raise ValueError("O arquivo da Rede está vazio ou a aba selecionada não contém dados válidos.")
    if df_shift.empty:
        raise ValueError("O arquivo do Shift está vazio ou a aba selecionada não contém dados válidos.")

    # Fase 1 (normalização) já ocorreu em normalizer.py antes de chegar aqui.
    df_shift, original_shift_count, consolidated_count = _collapse_shift_splits(
        df_rede, df_shift
    )
    quality, quality_map = validate_shift(df_shift)

    # Fase 2 — candidatos: autorização normalizada é a única chave forte.
    # Não há fallback por data+valor nem por NSU isolado: sem autorização
    # compatível, a transação nunca vira match automático.
    rede_auth_index = _autorizacao_index(df_rede)

    used_rede, details = set(), []
    pending_secondary: list[tuple[int, list[str]]] = []
    for shift_idx, shift in df_shift.iterrows():
        statuses: list[str] = []
        divergent: list[str] = []

        if bool(shift.get("_agrupamento_shift_ambiguo", False)):
            details.append(_detail(
                shift,
                None,
                ["AGRUPAMENTO_OS_AMBIGUO"],
                ["agrupamento_shift"],
                "AGRUPAMENTO_OS_AMBIGUO",
                motivo=(
                    "Existem múltiplas O.S. do Shift com a mesma autorização, "
                    "mas campos críticos incompatíveis. Nenhum agrupamento automático foi feito."
                ),
            ))
            continue

        if bool(shift.get("_autorizacao_repetida_mesmo_vencimento", False)):
            # Mesma autorização e mesmo vencimento no próprio arquivo do
            # Shift, mas com números de parcela diferentes: padrão suspeito
            # de erro de cadastro (ex.: a mesma cobrança lançada duas vezes
            # com parcelas diferentes por engano). Não concilia automático
            # contra a Rede — vai direto para revisão manual, como o
            # agrupamento ambíguo de O.S.
            details.append(_detail(
                shift,
                None,
                ["AUTORIZACAO_REPETIDA_MESMO_VENCIMENTO_PARCELA_DIFERENTE"],
                ["autorizacao", "parcela"],
                "AUTORIZACAO_REPETIDA_MESMO_VENCIMENTO_PARCELA_DIFERENTE",
                motivo=(
                    "Mesma autorização e mesmo vencimento no arquivo do Shift, mas com "
                    "números de parcela diferentes. Revisão manual obrigatória antes de "
                    "conciliar — confirmar se não é a mesma cobrança lançada duas vezes."
                ),
            ))
            continue

        quality_codes = quality_map[int(shift_idx)]
        if quality_codes:
            # Alertas fracos (ex.: AUTORIZACAO_DUPLICADA/NSU_DUPLICADO isolados,
            # campos auxiliares vazios) não devem, sozinhos, impedir a
            # conciliação. Só problemas cadastrais fortes viram ERRO_CADASTRAL_SHIFT.
            if any(code not in WEAK_ALERT_CODES for code in quality_codes):
                statuses.append("ERRO_CADASTRAL_SHIFT")
            if any("DUPLICAD" in code for code in quality_codes):
                statuses.append("POSSIVEL_DUPLICIDADE")
        if bool(shift.get("_duplicidade_exata_suspeita", False)):
            statuses.append("DUPLICIDADE_EXATA_SUSPEITA")

        shift_auth = normalizar_autorizacao(shift.get("autorizacao"))
        candidates = (
            [i for i in rede_auth_index.get(shift_auth, []) if i not in used_rede]
            if shift_auth else []
        )
        grouped_shift = bool(shift.get("_multiplas_os_mesma_autorizacao", False))

        if not candidates:
            if grouped_shift:
                details.append(_detail(
                    shift,
                    None,
                    statuses + [
                        "NAO_ENCONTRADO_NA_REDE",
                        "MULTIPLAS_OS_MESMA_AUTORIZACAO",
                    ],
                    [],
                    "AGRUPAMENTO_OS_SEM_CORRESPONDENCIA_REDE",
                    motivo=(
                        "Múltiplas O.S. da Shift possuem a mesma autorização, "
                        "mas não foi encontrada transação correspondente na Rede."
                    ),
                ))
                continue
            # Sem autorização compatível (ou ausente): não vira faltante
            # direto ainda — primeiro tenta a correspondência secundária
            # (Fase 2b) por chave composta segura, sem nunca marcar como
            # CONCILIADO. Só depois de testar todos os pendentes é que os
            # que sobrarem sem par único viram faltante de verdade.
            pending_secondary.append((shift_idx, statuses))
            continue

        # Fase 3 — validação: entre os candidatos com autorização compatível,
        # escolhe o melhor por valor/parcela/NSU/data e valida os demais campos.
        if grouped_shift and len(candidates) > 1:
            details.append(_detail(
                shift,
                None,
                statuses + ["AGRUPAMENTO_OS_AMBIGUO"],
                ["autorizacao"],
                "AGRUPAMENTO_OS_AMBIGUO",
                motivo=(
                    "Há mais de uma transação da Rede com a mesma autorização para "
                    "um agrupamento de múltiplas O.S. do Shift. Revisão manual necessária."
                ),
            ))
            continue
        best_idx = max(candidates, key=lambda i: _score_candidate(shift, df_rede.loc[i]))
        used_rede.add(best_idx)
        rede = df_rede.loc[best_idx]
        match_type = (
            "AUTORIZACAO_NORMALIZADA_AGRUPAMENTO_SHIFT"
            if grouped_shift
            else "AUTORIZACAO_COMPATIVEL"
        )

        compativel, criterio_parcela, restantes_shift, restantes_rede = (
            parcelas_compativeis_por_restantes(
                shift.get("parcela"), shift.get("numero_parcelas"),
                rede.get("parcela"), rede.get("numero_parcelas"),
            )
        )
        if grouped_shift and criterio_parcela == "DIVERGENCIA_PARCELAS_RESTANTES":
            details.append(_detail(
                shift,
                rede,
                statuses + ["AGRUPAMENTO_OS_AMBIGUO"],
                ["parcela"],
                "AGRUPAMENTO_OS_AMBIGUO",
                criterio_parcela=criterio_parcela,
                restantes_shift=restantes_shift,
                restantes_rede=restantes_rede,
                parcelas_compativeis=compativel,
                motivo=(
                    "Múltiplas O.S. com a mesma autorização encontraram transação na Rede, "
                    "mas as parcelas são incompatíveis para conciliação automática."
                ),
            ))
            continue
        if criterio_parcela == "DIVERGENCIA_PARCELAS_RESTANTES":
            statuses.append("DIVERGENCIA_PARCELA")
            divergent.append("parcela")
        elif criterio_parcela == "DADOS_PARCELA_INSUFICIENTES":
            statuses.append("DADOS_PARCELA_INSUFICIENTES")
        elif criterio_parcela == "COMPATIVEL_POR_PARCELAS_RESTANTES":
            statuses.append("CONCILIADO_COM_PARCELA_COMPATIVEL_POR_RESTANTES")
        # "EXATA" não adiciona status: parcela plenamente compatível.

        tolerated_money_difference = False
        for field in FIELDS_FOR_GENERIC_DIVERGENCE:
            if (
                field == "data_vencimento"
                and not bool(shift.get("_comparar_data_vencimento", True))
            ):
                continue
            shift_value, rede_value = shift.get(field), rede.get(field)
            shift_present = shift_value is not None and not pd.isna(shift_value)
            rede_present = rede_value is not None and not pd.isna(rede_value)
            if not shift_present or not rede_present:
                # Campo ausente em um dos lados não deve gerar divergência crítica.
                continue
            if field in MONEY_FIELDS:
                difference = abs(Decimal(shift_value) - Decimal(rede_value))
                if difference == 0:
                    continue
                divergent.append(field)
                if difference <= MONEY_TOLERANCE:
                    tolerated_money_difference = True
                    continue
                statuses.append(STATUS_BY_FIELD.get(field, "REVISAO_MANUAL"))
            elif not _equal(shift_value, rede_value, field):
                divergent.append(field)
                statuses.append(STATUS_BY_FIELD.get(field, "REVISAO_MANUAL"))

        cancel_text = f"{shift.get('cancelamento') or ''} {rede.get('cancelamento') or ''}"
        if any(token in cancel_text for token in ("SIM", "CANCEL", "CONTEST")):
            statuses.append("CANCELAMENTO_CONTESTACAO")
        if grouped_shift and any(status in statuses for status in ("DIVERGENCIA_VALOR_BRUTO", "DIVERGENCIA_VALOR_LIQUIDO")):
            statuses = [
                status for status in statuses
                if status not in {"DIVERGENCIA_VALOR_BRUTO", "DIVERGENCIA_VALOR_LIQUIDO"}
            ]
            statuses.append("AGRUPAMENTO_OS_VALOR_DIVERGENTE")
        if grouped_shift and not statuses and not tolerated_money_difference:
            statuses = ["CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO"]
        elif grouped_shift and tolerated_money_difference and not statuses:
            statuses = [
                "CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO",
                "DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS",
            ]
        elif tolerated_money_difference and not statuses:
            statuses = ["CONCILIADO_COM_DIVERGENCIA_TOLERADA"]
        elif tolerated_money_difference:
            statuses.append("DIVERGENCIA_TOLERADA_ATE_2_CENTAVOS")
        if not statuses:
            statuses = ["CONCILIADO"]

        motivo = _gerar_motivo(match_type, criterio_parcela, divergent)
        details.append(_detail(
            shift, rede, statuses, divergent, match_type,
            criterio_parcela=criterio_parcela,
            restantes_shift=restantes_shift, restantes_rede=restantes_rede,
            parcelas_compativeis=compativel, motivo=motivo,
        ))

    # Fase 2b/3b — correspondência secundária para quem sobrou sem
    # autorização compatível. Nunca conciliada automaticamente; só gera um
    # alerta de revisão manual quando há par único (1 Shift para 1 Rede).
    rede_pool = df_rede.loc[[i for i in df_rede.index if i not in used_rede]]
    shift_to_rede, rede_to_shift = _mapear_candidatos_secundarios(
        [idx for idx, _ in pending_secondary], df_shift, rede_pool
    )
    for shift_idx, statuses in pending_secondary:
        shift = df_shift.loc[shift_idx]
        unico, rede_candidatos, shift_candidatos = validar_unicidade_candidato(
            shift_idx, shift_to_rede, rede_to_shift
        )
        match_type = classificar_alerta_autorizacao_divergente(
            unico, rede_candidatos, shift_candidatos
        )
        motivo = _gerar_motivo(match_type, None, [])

        if match_type == "REVISAR_AUTORIZACAO_DIVERGENTE_ALTA_CONFIANCA":
            rede_idx = rede_candidatos[0]
            used_rede.add(rede_idx)
            rede = df_rede.loc[rede_idx]
            row_statuses = statuses + [match_type]
            compativel, criterio_parcela, restantes_shift, restantes_rede = (
                parcelas_compativeis_por_restantes(
                    shift.get("parcela"), shift.get("numero_parcelas"),
                    rede.get("parcela"), rede.get("numero_parcelas"),
                )
            )
            details.append(_detail(
                shift, rede, row_statuses, ["autorizacao"], match_type,
                motivo=motivo, nivel_confianca="ALTA",
                criterio_alerta=CRITERIO_ALERTA_SECUNDARIO, unico_candidato=True,
                criterio_parcela=criterio_parcela,
                restantes_shift=restantes_shift,
                restantes_rede=restantes_rede,
                parcelas_compativeis=compativel,
            ))
            continue

        if match_type == "AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL":
            row_statuses = statuses + [match_type, "NAO_ENCONTRADO_NA_REDE"]
            details.append(_detail(
                shift, None, row_statuses, ["autorizacao"], match_type,
                motivo=motivo, nivel_confianca="AMBIGUA",
                criterio_alerta=CRITERIO_ALERTA_SECUNDARIO, unico_candidato=False,
            ))
            continue

        # Sem candidato nenhum na regra secundária: faltante "normal".
        row_statuses = statuses + ["NAO_ENCONTRADO_NA_REDE"]
        details.append(_detail(
            shift, None, row_statuses, [], match_type, motivo=motivo,
            nivel_confianca="SEM_CORRESPONDENCIA", unico_candidato=False,
        ))

    for rede_idx, rede in df_rede.iterrows():
        if rede_idx not in used_rede:
            motivo = _gerar_motivo("SEM_CORRESPONDENCIA_NO_SHIFT", None, [])
            details.append(_detail(
                None, rede, ["NAO_ENCONTRADO_NO_SHIFT"], [], "SEM_CORRESPONDENCIA_NO_SHIFT",
                motivo=motivo,
            ))

    detailed = pd.DataFrame(details)
    if "parcelas_compativeis" in detailed.columns:
        detailed["parcelas_compativeis"] = detailed["parcelas_compativeis"].astype(object)
    detailed = aplicar_impacto_financeiro(detailed)
    status_col = detailed["status_comparacao"]
    shift_side = detailed["linha_shift"].notna() if "linha_shift" in detailed.columns else pd.Series([False] * len(detailed))
    status_shift = detailed.loc[shift_side, "status_comparacao"] if len(detailed) else pd.Series(dtype=str)
    conciliados_shift = detailed.loc[
        shift_side & status_col.str.startswith("CONCILIADO", na=False)
    ] if len(detailed) else pd.DataFrame()
    chaves_conciliadas = {
        _unique_conciliation_key(row)
        for _, row in conciliados_shift.iterrows()
    }
    chaves_conciliadas.discard(None)
    autorizacoes_conciliadas = sorted({
        str(key[0]).strip()
        for key in chaves_conciliadas
        if key and key[0]
    })
    total_conciliado_unico = len(chaves_conciliadas)
    amount_rede = df_rede["valor_bruto"].dropna().sum()
    amount_shift = df_shift["valor_bruto"].dropna().sum()
    liquid_rede = df_rede["valor_liquido"].dropna().sum()
    liquid_shift = df_shift["valor_liquido"].dropna().sum()
    summary = {
        "total_linhas_rede": len(df_rede),
        "total_linhas_shift": len(df_shift),
        "total_linhas_shift_originais": original_shift_count,
        "total_agrupamentos_shift": consolidated_count,
        "total_linhas_agrupadas_shift": (
            original_shift_count - len(df_shift) + consolidated_count
        ),
        "total_conciliado": total_conciliado_unico,
        "total_conciliado_shift": total_conciliado_unico,
        "total_conciliado_agrupamento_os": int(
            status_shift.str.contains("CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO").sum()
        ) if not status_shift.empty else 0,
        "autorizacoes_conciliadas": autorizacoes_conciliadas,
        "total_divergencia_tolerada": int(
            status_col.str.contains("DIVERGENCIA_TOLERADA").sum()
        ),
        "total_parcela_compativel_por_restantes": int(
            status_col.str.contains("CONCILIADO_COM_PARCELA_COMPATIVEL_POR_RESTANTES").sum()
        ),
        "total_so_rede": int(status_col.str.contains("NAO_ENCONTRADO_NO_SHIFT").sum()),
        "total_so_shift": int(status_col.str.contains("NAO_ENCONTRADO_NA_REDE").sum()),
        "total_com_divergencia": int(status_col.str.contains(
            "DIVERGENCIA|REVISAO_MANUAL|DADOS_PARCELA_INSUFICIENTES"
            "|REVISAR_AUTORIZACAO_DIVERGENTE|AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL"
            "|AGRUPAMENTO_SHIFT_AMBIGUO|AGRUPAMENTO_OS_AMBIGUO"
            "|AGRUPAMENTO_OS_VALOR_DIVERGENTE|DUPLICIDADE_EXATA_SUSPEITA"
            "|AUTORIZACAO_REPETIDA_MESMO_VENCIMENTO_PARCELA_DIFERENTE",
            regex=True,
        ).sum()),
        "total_agrupamento_shift_ambiguo": int(
            status_col.str.contains("AGRUPAMENTO_SHIFT_AMBIGUO|AGRUPAMENTO_OS_AMBIGUO").sum()
        ),
        "total_revisar_autorizacao_divergente": int(
            status_col.str.contains("REVISAR_AUTORIZACAO_DIVERGENTE_ALTA_CONFIANCA").sum()
        ),
        "total_ambiguo_sem_autorizacao": int(
            status_col.str.contains("AMBIGUO_SEM_AUTORIZACAO_COMPATIVEL").sum()
        ),
        "total_erro_cadastral_shift": int(status_col.str.contains("ERRO_CADASTRAL_SHIFT").sum()),
        "total_duplicidade": int(status_col.str.contains(
            "POSSIVEL_DUPLICIDADE|DUPLICIDADE_EXATA_SUSPEITA"
        ).sum()),
        "total_autorizacao_repetida_mesmo_vencimento": int(status_col.str.contains(
            "AUTORIZACAO_REPETIDA_MESMO_VENCIMENTO_PARCELA_DIFERENTE"
        ).sum()),
        "total_cancelamento_contestacao": int(status_col.str.contains("CANCELAMENTO_CONTESTACAO").sum()),
        "valor_bruto_total_rede": amount_rede,
        "valor_bruto_total_shift": amount_shift,
        "valor_liquido_total_rede": liquid_rede,
        "valor_liquido_total_shift": liquid_shift,
        "diferenca_total_valor_bruto": amount_rede - amount_shift,
        "diferenca_total_valor_liquido": liquid_rede - liquid_shift,
    }
    if "valor_desconto" in df_shift.columns:
        summary["valor_desconto_total_shift"] = df_shift["valor_desconto"].dropna().sum()
    # Quantidade por bandeira/modalidade/forma de pagamento (quando o Shift
    # trouxer essas informações, ex.: relatório financeiro de cartão).
    for field, key in (
        ("bandeira", "quantidade_por_bandeira_shift"),
        ("modalidade", "quantidade_por_modalidade_shift"),
        ("forma_pagamento", "quantidade_por_forma_pagamento_shift"),
    ):
        if field in df_shift.columns and df_shift[field].notna().any():
            summary[key] = df_shift[field].dropna().value_counts().to_dict()
    summary.update(resumo_impacto_financeiro(detailed))
    return ComparisonResult(summary, detailed, quality)
