from __future__ import annotations

from datetime import date

import pandas as pd

from .audit import discard_record

def validate_rede_context(
    df_rede: pd.DataFrame,
    expected_date: str,
    expected_establishment: str | None = None,
) -> dict:
    """Confirma que unidade e dia selecionados pertencem ao arquivo da Rede."""
    dates = sorted({
        value.isoformat()
        for value in df_rede["data_recebimento"].dropna()
        if isinstance(value, date)
    })
    establishments = sorted({
        str(value).strip().upper()
        for value in df_rede["estabelecimento"].dropna()
        if str(value).strip()
    })
    names = sorted({
        str(value).strip()
        for value in df_rede["nome_estabelecimento"].dropna()
        if str(value).strip()
    })

    if dates and expected_date not in dates:
        available = ", ".join(dates)
        raise ValueError(
            f"A data selecionada ({expected_date}) não corresponde ao arquivo da Rede. "
            f"Data encontrada: {available}."
        )

    expected = str(expected_establishment or "").strip().upper()
    if expected and establishments and expected not in establishments:
        available = ", ".join(establishments)
        raise ValueError(
            f"O estabelecimento da unidade selecionada ({expected}) não corresponde "
            f"ao arquivo da Rede. Estabelecimento encontrado: {available}."
        )

    return {
        "datas_recebimento": dates,
        "estabelecimentos": establishments,
        "nomes_estabelecimento": names,
    }


def validate_shift_empresa(
    df_shift: pd.DataFrame, expected_empresa: str | None
) -> list[str]:
    """Confere a empresa contábil do Shift contra a unidade selecionada.

    A coluna ``Empresa`` identifica a unidade que lançou a conta. Já
    ``Descrição Credor/Devedor`` identifica a contraparte e pode legitimamente
    apontar outra unidade ou convênio; usá-la como filtro elimina vendas válidas.

    Retorna alertas (strings), sem bloquear a conciliação — divergência de
    "Descrição Credor/Devedor" divergente é sinal de arquivo trocado, mas não impede o
    processamento, similar às demais checagens de contexto do projeto.
    """
    source_column = "empresa"
    if source_column not in df_shift.columns or not expected_empresa:
        return []
    # Comparação sem diferenciar maiúsculas/minúsculas nem espaços nas
    # pontas, igual ao que já é feito para o estabelecimento da Rede acima
    # — evita falso alerta só por causa de digitação no cadastro.
    found_raw = sorted({
        str(value).strip()
        for value in df_shift[source_column].dropna()
        if str(value).strip()
    })
    found_upper = {value.upper() for value in found_raw}
    expected = expected_empresa.strip()
    if found_raw and expected.upper() not in found_upper:
        available = ", ".join(found_raw)
        return [
            f'A empresa cadastrada para esta unidade ("{expected}") não aparece na '
            f'coluna "Empresa" do relatório do Shift. Empresa(s) encontrada(s): {available}.'
        ]
    return []


def filter_shift_empresa(
    df_shift: pd.DataFrame, expected_empresa: str | None
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Restringe um relatório multiempresa à unidade selecionada.

    Se não houver valor cadastrado, coluna de origem ou correspondência, mantém
    o comportamento anterior e não descarta linhas silenciosamente.
    """
    total = len(df_shift)
    stats = {
        "antes_filtro_empresa": total,
        "apos_filtro_empresa": total,
        "descartes": [],
    }
    source_column = "empresa"
    if source_column not in df_shift.columns or not expected_empresa:
        return df_shift, stats
    expected = expected_empresa.strip().upper()
    mask = df_shift[source_column].map(
        lambda value: str(value).strip().upper() == expected
        if value is not None and not pd.isna(value)
        else False
    )
    if not mask.any():
        available = ", ".join(sorted({
            str(value).strip()
            for value in df_shift[source_column].dropna()
            if str(value).strip()
        }))
        raise ValueError(
            f'A empresa cadastrada para a unidade ("{expected_empresa.strip()}") '
            f'não foi encontrada na coluna "Empresa" do Shift. '
            f"Empresas disponíveis: {available or 'nenhuma'}."
        )
    stats["descartes"] = [
        discard_record(
            row,
            "SHIFT",
            "EMPRESA_FORA_DO_RECORTE",
            f'Empresa diferente da unidade selecionada ("{expected_empresa.strip()}").',
        )
        for _, row in df_shift.loc[~mask].iterrows()
    ]
    filtered = df_shift.loc[mask].reset_index(drop=True)
    stats["apos_filtro_empresa"] = len(filtered)
    return filtered, stats
