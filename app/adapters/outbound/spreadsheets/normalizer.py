from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher

import pandas as pd


CANONICAL_ALIASES = {
    "data_recebimento": ["data do recebimento", "data recebimento"],
    "data_venda": ["data original da venda", "data da venda", "data venda"],
    "data_vencimento": ["data original de vencimento", "data vencimento"],
    "valor_bruto_original": ["valor bruto da parcela original", "valor bruto", "valor"],
    "valor_bruto_atualizado": ["valor bruto da parcela atualizada", "valor atualizado"],
    "taxa_mdr": ["taxa mdr", "mdr percentual"],
    "valor_mdr": ["valor mdr descontado", "valor mdr", "mdr descontado"],
    "valor_liquido": ["valor liquido da parcela", "valor líquido", "valor liquido"],
    "nsu": ["nsu/cv", "nsu_cv", "nsu", "cv"],
    "autorizacao": ["numero da autorizacao", "número da autorização", "autorizacao", "autorização"],
    "lote": ["resumo de vendas/numero do lote", "numero do lote", "lote"],
    "nome_estabelecimento": ["nome do estabelecimento", "nome estabelecimento"],
    "estabelecimento": ["estabelecimento", "codigo estabelecimento"],
    "numero_cartao": ["numero do cartao", "cartao"],
    "modalidade": ["modalidade", "tipo pagamento"],
    "bandeira": ["bandeira"],
    "numero_parcelas": ["numero de parcelas", "quantidade parcelas", "qtd parcelas"],
    "parcela": ["parcela", "parcela atual"],
    "banco": ["banco"],
    "agencia": ["agencia"],
    "conta": ["conta-corrente", "conta corrente", "conta"],
    "cancelamento": ["cancelamento/contestacao", "cancelamento", "contestacao"],
    "data_cancelamento": ["data do cancelamento", "data cancelamento"],
    "status": ["status", "situacao"],
}
MONEY_FIELDS = {"valor_bruto_original", "valor_bruto_atualizado", "valor_mdr", "valor_liquido"}
DATE_FIELDS = {"data_recebimento", "data_venda", "data_vencimento", "data_cancelamento"}
TEXT_FIELDS = {"lote", "nome_estabelecimento", "estabelecimento", "numero_cartao", "banco", "agencia", "conta"}


def fold_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    try:
        if any(x in text for x in ("Ã", "�")):
            text = text.encode("latin-1").decode("utf-8")
    except (UnicodeError, UnicodeEncodeError):
        pass
    text = unicodedata.normalize("NFKD", text)
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).upper().split())


def _empty(value: object) -> bool:
    return value is None or pd.isna(value) or str(value).strip().upper() in {"", "-", "NAN", "NONE", "NAT"}


def clean_identifier(value: object) -> str | None:
    if _empty(value):
        return None
    text = str(value).strip()
    text = re.sub(r"\.0+$", "", text)
    return re.sub(r"\s+", "", text).upper()


def normalize_authorization(value: object) -> str | None:
    text = clean_identifier(value)
    if text is None:
        return None
    # A Rede às vezes exporta a autorização com um zero à esquerda extra
    # (ex.: "0685866" com 7 dígitos para um código de 6). Remove zeros à
    # esquerda antes de validar o tamanho, mantendo pelo menos 1 dígito.
    stripped = text.lstrip("0") or "0"
    if len(stripped) > 6:
        raise ValueError("AUTORIZACAO_TAMANHO_INVALIDO")
    return stripped.zfill(6)


def normalize_authorization_alnum(value: object) -> str | None:
    """Normaliza a autorização do relatório financeiro do Shift, que pode
    ser numérica (mesma regra de 6 dígitos da Rede) ou alfanumérica
    (ex.: "4Y9HDN", "R09605" — nunca truncada)."""
    text = clean_identifier(value)
    if text is None:
        return None
    if text.isdigit():
        stripped = text.lstrip("0") or "0"
        if len(stripped) > 6:
            raise ValueError("AUTORIZACAO_TAMANHO_INVALIDO")
        return stripped.zfill(6)
    return text


def normalize_nsu(value: object) -> str | None:
    text = clean_identifier(value)
    if text is None:
        return None
    return (text.lstrip("0") or "0") if text.isdigit() else text


def normalize_money(value: object) -> Decimal | None:
    """Converte valores monetários em BRL (1.234,56) ou en-US (1,234.56),
    com ou sem símbolo "R$", para Decimal. Retorna None se não for
    possível interpretar o valor (nunca converte silenciosamente para 0)."""
    if _empty(value):
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    text = re.sub(r"[R$\s%]", "", str(value))
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.startswith("-"):
        negative, text = True, text[1:]

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        # O separador decimal é o último símbolo (, ou .) que aparece no texto.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        text = text.replace(".", "").replace(",", ".")
    elif has_dot and text.count(".") > 1:
        text = text.replace(".", "")

    try:
        result = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return -result if negative else result


EXCEL_EPOCH = datetime(1899, 12, 30)


def normalize_date(value: object) -> date | None:
    if _empty(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Número serial de data do Excel (dias desde 1899-12-30).
        try:
            return (EXCEL_EPOCH + pd.Timedelta(days=float(value))).date()
        except (OverflowError, ValueError, pd.errors.OutOfBoundsTimedelta):
            return None
    text = str(value).strip()
    if re.fullmatch(r"\d{5,6}(\.0+)?", text):
        try:
            return (EXCEL_EPOCH + pd.Timedelta(days=float(text))).date()
        except (OverflowError, ValueError, pd.errors.OutOfBoundsTimedelta):
            return None
    iso_like = bool(re.match(r"^\d{4}-\d{2}-\d{2}", text))
    parsed = pd.to_datetime(value, dayfirst=not iso_like, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


PARCELA_SEPARATORS = ("/", " de ", "-")


def normalize_int(value: object, part: str = "first") -> int | None:
    """Extrai um inteiro de valores como "1", "1/3", "01/03" ou "1 de 3".
    Com part="second" retorna a segunda parte (total de parcelas), se houver."""
    if _empty(value):
        return None
    text = str(value).strip().lower().replace("x", "")
    for separator in PARCELA_SEPARATORS:
        if separator in text:
            pieces = text.split(separator, 1)
            text = pieces[0] if part == "first" else pieces[1]
            break
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def normalize_brand(value: object) -> str:
    text = fold_text(value)
    if not text:
        return "NAO_INFORMADO"
    for brand, tokens in {
        "VISA": ("VISA",), "MASTERCARD": ("MASTER",),
        "ELO": ("ELO",), "AMEX": ("AMEX", "AMERICAN EXPRESS"),
        "HIPERCARD": ("HIPER",),
    }.items():
        if any(token in text for token in tokens):
            return brand
    return "OUTRA"


def normalize_modality(value: object) -> str:
    text = fold_text(value)
    if not text:
        return "NAO_INFORMADO"
    if "CREDIT" in text or "CREDITO" in text:
        return "CREDITO"
    if "DEBIT" in text or "DEBITO" in text:
        return "DEBITO"
    if "PIX" in text:
        return "PIX"
    return "OUTRA"


def _column_map(columns) -> dict[str, str]:
    normalized = {col: fold_text(col).lower() for col in columns}
    result = {}
    for canonical, aliases in CANONICAL_ALIASES.items():
        folded_aliases = [fold_text(a).lower() for a in aliases]
        exact = [col for col, value in normalized.items() if value in folded_aliases]
        if exact:
            result[canonical] = exact[0]
            continue
        scored = [
            (SequenceMatcher(None, value, alias).ratio(), col)
            for col, value in normalized.items() for alias in folded_aliases
        ]
        score, col = max(scored, default=(0, ""))
        if score >= 0.88:
            result[canonical] = col
    return result


def normalize_dataframe(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if source not in {"rede", "shift"}:
        raise ValueError("source deve ser 'rede' ou 'shift'")
    mapping = _column_map(df.columns)
    output = pd.DataFrame(index=df.index)
    output["_source"] = source
    output["_row"] = df.index + 2
    for field in CANONICAL_ALIASES:
        raw = df[mapping[field]] if field in mapping else pd.Series([None] * len(df), index=df.index)
        output[f"raw_{field}"] = raw
        if field == "autorizacao":
            values, errors = [], []
            for value in raw:
                try:
                    values.append(normalize_authorization(value))
                    errors.append(None)
                except ValueError as exc:
                    values.append(clean_identifier(value))
                    errors.append(str(exc))
            output[field] = values
            output["autorizacao_erro"] = errors
        elif field == "nsu":
            output[field] = raw.map(normalize_nsu)
        elif field in MONEY_FIELDS:
            output[field] = raw.map(normalize_money)
        elif field in DATE_FIELDS:
            output[field] = raw.map(normalize_date)
        elif field == "bandeira":
            output[field] = raw.map(normalize_brand)
        elif field == "modalidade":
            output[field] = raw.map(normalize_modality)
        elif field == "parcela":
            output[field] = raw.map(lambda v: normalize_int(v, "first"))
        elif field == "numero_parcelas":
            output[field] = raw.map(lambda v: normalize_int(v, "second"))
        elif field == "cancelamento":
            output[field] = raw.map(fold_text)
        elif field == "status":
            output[field] = raw.map(fold_text)
        else:
            output[field] = raw.map(lambda v: None if _empty(v) else fold_text(v))
    if "raw_parcela" in output.columns:
        inferred_total = output["raw_parcela"].map(lambda v: normalize_int(v, "second"))
        output["numero_parcelas"] = output["numero_parcelas"].combine_first(inferred_total)
    output["valor_bruto"] = output["valor_bruto_atualizado"].combine_first(output["valor_bruto_original"])
    output["raw_valor_bruto"] = output["raw_valor_bruto_atualizado"].combine_first(
        output["raw_valor_bruto_original"]
    )
    return output


# --- Relatório financeiro completo do Shift (cartão) ------------------------
# Colunas essenciais: sem elas não é possível conciliar cartão.
SHIFT_CARD_ESSENTIAL_COLUMNS = ["Valor bruto", "Nro autorização cartão"]
# Ao menos uma destas é necessária para identificar quais linhas são cartão.
SHIFT_CARD_IDENTIFIER_COLUMNS = ["Espécie", "Forma de pagamento/cobrança"]

OS_PATTERN = re.compile(r"OS:\s*([\w./-]+)", re.IGNORECASE)


def get_optional_column(df: pd.DataFrame, name: str) -> pd.Series:
    """Retorna a coluna se existir, ou uma série de None com o mesmo índice
    (nunca lança KeyError para coluna opcional ausente)."""
    if name in df.columns:
        return df[name]
    return pd.Series([None] * len(df), index=df.index)


def get_required_column(df: pd.DataFrame, name: str) -> pd.Series:
    if name not in df.columns:
        raise ValueError(f'O relatório do Shift não contém a coluna essencial "{name}".')
    return df[name]


def extract_os(value: object) -> str | None:
    """Extrai o número de OS de textos como "OS: 022-67324-620"."""
    if _empty(value):
        return None
    match = OS_PATTERN.search(str(value))
    return match.group(1).strip() if match else None


def parse_forma_pagamento(value: object) -> dict[str, object]:
    """Extrai operadora, modalidade, bandeira e parcelas de textos como
    "REDE 10X MASTER" ou "REDE DEBITO VISA". Formas não reconhecidas (ex.:
    "PIX ITAU (Jn)") retornam operadora identificada e o restante None."""
    empty = {"operadora": None, "modalidade": None, "bandeira": None, "parcelas_inferidas_da_forma": None}
    if _empty(value):
        return dict(empty)
    tokens = fold_text(value).split()
    if not tokens:
        return dict(empty)
    operadora = tokens[0]
    if operadora != "REDE" or len(tokens) < 2:
        return {**empty, "operadora": operadora}
    rest = tokens[1:]
    if rest[0] == "DEBITO":
        return {
            "operadora": operadora, "modalidade": "DEBITO",
            "bandeira": rest[1] if len(rest) > 1 else None,
            "parcelas_inferidas_da_forma": 1,
        }
    match = re.match(r"^(\d+)X$", rest[0])
    if match:
        return {
            "operadora": operadora, "modalidade": "CREDITO",
            "bandeira": rest[1] if len(rest) > 1 else None,
            "parcelas_inferidas_da_forma": int(match.group(1)),
        }
    return {**empty, "operadora": operadora}


def _text_or_none(series: pd.Series) -> pd.Series:
    return series.map(lambda v: None if _empty(v) else str(v).strip())


def normalize_shift_financial_report(df: pd.DataFrame) -> pd.DataFrame:
    """Gera o DataFrame canônico de cartão a partir do relatório financeiro
    completo do Shift (já filtrado para cartão por `read_shift_financial_report`).

    Tolera colunas opcionais ausentes (viram None) e lança ValueError com
    mensagem clara se faltar alguma coluna essencial. Alertas cadastrais
    (autorização vazia, parcelamento não identificado, divergência entre
    parcelas informadas e inferidas da forma de pagamento etc.) ficam na
    coluna "_alertas_normalizacao" (lista de códigos por linha), lida por
    `validate_shift`.
    """
    for column in SHIFT_CARD_ESSENTIAL_COLUMNS:
        if column not in df.columns:
            raise ValueError(f'O relatório do Shift não contém a coluna essencial "{column}".')
    if not any(col in df.columns for col in SHIFT_CARD_IDENTIFIER_COLUMNS):
        raise ValueError(
            "Não foi possível identificar pagamentos de cartão porque o relatório não "
            'possui "Espécie" nem "Forma de pagamento/cobrança".'
        )

    output = pd.DataFrame(index=df.index)
    output["_source"] = "shift"
    output["_row"] = (
        df["_source_line"]
        if "_source_line" in df.columns
        else pd.Series(df.index + 2, index=df.index)
    )
    output["origem"] = "SHIFT"
    output["_alertas_normalizacao"] = [[] for _ in range(len(df))]

    def alert(idx, codigo: str) -> None:
        output.at[idx, "_alertas_normalizacao"].append(codigo)

    raw_autorizacao = get_required_column(df, "Nro autorização cartão")
    output["raw_autorizacao"] = raw_autorizacao
    autorizacoes, erros = [], []
    for idx, value in raw_autorizacao.items():
        if _empty(value):
            autorizacoes.append(None)
            erros.append(None)
            alert(idx, "AUTORIZACAO_VAZIA")
            continue
        try:
            autorizacoes.append(normalize_authorization_alnum(value))
            erros.append(None)
        except ValueError as exc:
            autorizacoes.append(clean_identifier(value))
            erros.append(str(exc))
    output["autorizacao"] = autorizacoes
    output["autorizacao_erro"] = erros

    raw_valor_bruto = get_required_column(df, "Valor bruto")
    output["raw_valor_bruto"] = raw_valor_bruto
    output["valor_bruto"] = raw_valor_bruto.map(normalize_money)
    # Só itera as linhas com valor ausente/inválido (subconjunto pequeno na
    # prática), em vez de todas as linhas do relatório com .at[] repetido.
    valor_bruto_ausente = output["valor_bruto"].isna()
    if valor_bruto_ausente.any():
        raw_vazio = raw_valor_bruto.map(_empty)
        for idx in output.index[valor_bruto_ausente]:
            alert(idx, "VALOR_VAZIO" if raw_vazio.at[idx] else "VALOR_BRUTO_INVALIDO")

    raw_valor_liquido = get_optional_column(df, "Valor líquido")
    output["raw_valor_liquido"] = raw_valor_liquido
    output["valor_liquido"] = raw_valor_liquido.map(normalize_money)

    raw_desconto = get_optional_column(df, "Desconto")
    output["raw_valor_desconto"] = raw_desconto
    output["valor_desconto"] = raw_desconto.map(normalize_money)

    forma_existe = "Forma de pagamento/cobrança" in df.columns
    raw_forma = get_optional_column(df, "Forma de pagamento/cobrança")
    output["raw_forma_pagamento"] = raw_forma
    output["forma_pagamento"] = raw_forma.map(lambda v: None if _empty(v) else fold_text(v))
    parsed = raw_forma.map(parse_forma_pagamento)
    output["operadora"] = parsed.map(lambda d: d["operadora"])
    output["modalidade"] = parsed.map(lambda d: d["modalidade"])
    output["bandeira"] = parsed.map(
        lambda d: None if d["bandeira"] is None else normalize_brand(d["bandeira"])
    )
    output["parcelas_inferidas_da_forma"] = parsed.map(lambda d: d["parcelas_inferidas_da_forma"])
    if not forma_existe:
        for idx in output.index:
            alert(idx, "FORMA_PAGAMENTO_AUSENTE")

    raw_especie = get_optional_column(df, "Espécie")
    output["raw_especie"] = raw_especie
    output["especie"] = raw_especie.map(lambda v: None if _empty(v) else fold_text(v))

    raw_parcela = get_optional_column(df, "Número da parcela")
    raw_numero_parcelas = get_optional_column(df, "Quantidade de parcelas")
    output["raw_parcela"] = raw_parcela
    output["raw_numero_parcelas"] = raw_numero_parcelas
    output["parcela"] = raw_parcela.map(lambda v: normalize_int(v, "first"))
    output["numero_parcelas"] = raw_numero_parcelas.map(lambda v: normalize_int(v, "first"))

    # Vetorizado: calcula as máscaras uma vez (em vez de .at[] por linha) e
    # só percorre linha a linha o subconjunto que precisa de um alerta —
    # mesma lógica/branches do loop original, sem os acessos repetidos.
    parcela_col = output["parcela"]
    numero_col = output["numero_parcelas"]
    inferida_col = output["parcelas_inferidas_da_forma"]
    sem_parcela_e_numero = parcela_col.isna() & numero_col.isna()
    tem_inferida = inferida_col.notna()

    # Fallback explícito: assume 1ª/única parcela a partir da forma de
    # pagamento quando não há "Número da parcela" nem "Quantidade de
    # parcelas" no relatório.
    fallback_mask = sem_parcela_e_numero & tem_inferida
    if fallback_mask.any():
        output.loc[fallback_mask, "parcela"] = 1
        output.loc[fallback_mask, "numero_parcelas"] = inferida_col[fallback_mask]

    nao_identificado_mask = sem_parcela_e_numero & ~tem_inferida
    for idx in output.index[nao_identificado_mask]:
        alert(idx, "PARCELAMENTO_NAO_IDENTIFICADO")

    numero_int = numero_col.map(lambda v: None if _empty(v) else int(v))
    inferida_int = inferida_col.map(lambda v: None if _empty(v) else int(v))
    divergente_mask = (
        ~sem_parcela_e_numero
        & numero_int.notna() & inferida_int.notna()
        & (numero_int != inferida_int)
    )
    for idx in output.index[divergente_mask]:
        alert(idx, "DIVERGENCIA_PARCELAMENTO_FORMA_PAGAMENTO")

    raw_descricao = get_optional_column(df, "Descrição")
    output["raw_descricao"] = raw_descricao
    output["descricao"] = _text_or_none(raw_descricao)
    codigo_registro = get_optional_column(df, "Código do registro")
    output["os_shift"] = raw_descricao.map(extract_os).combine_first(
        codigo_registro.map(extract_os)
    ).combine_first(_text_or_none(codigo_registro))

    documento = get_optional_column(df, "Número do documento")
    output["documento_shift"] = _text_or_none(documento)
    output["possivel_nsu"] = output["documento_shift"]
    # NSU não é confiável neste formato de relatório; não usar como chave forte.
    output["nsu"] = None

    output["data_vencimento"] = get_optional_column(df, "Vencimento").map(normalize_date)
    # O relatório pode ser exportado por uma faixa de vencimentos que a Rede
    # liquida em conjunto em um único dia (fins de semana/agenda financeira).
    # Preservamos a data para auditoria, mas ela não é equivalência transacional
    # com "data original de vencimento" da Rede.
    output["_comparar_data_vencimento"] = False
    output["data_emissao"] = get_optional_column(df, "Data de emissão").map(normalize_date)
    output["data_lancamento"] = get_optional_column(df, "Data do lançamento").map(normalize_date)
    output["data_previsao"] = get_optional_column(df, "Data previsão").map(normalize_date)
    output["data_venda"] = output["data_emissao"]
    output["data_shift_usada_para_match"] = output["data_venda"]
    output["campo_data_shift_usado"] = "Data de emissão"
    output["data_recebimento"] = None
    output["cancelamento"] = None
    # "Situação" no financeiro do Shift descreve a conta a receber, enquanto
    # "status" na Rede descreve o evento de liquidação. Não são comparáveis.
    output["status"] = None

    output["empresa"] = _text_or_none(get_optional_column(df, "Empresa"))
    output["descricao_credor_devedor"] = _text_or_none(
        get_optional_column(df, "Descrição Credor/Devedor")
    )
    output["codigo_conta"] = _text_or_none(get_optional_column(df, "Código da conta"))
    output["codigo_registro"] = _text_or_none(get_optional_column(df, "Código do registro"))
    output["codigo_classificacao"] = _text_or_none(get_optional_column(df, "Código Classificação"))
    output["descricao_classificacao"] = _text_or_none(get_optional_column(df, "Descriçao Classificação"))
    output["origem_conta"] = _text_or_none(get_optional_column(df, "Origem da conta"))
    output["lote_caixa"] = _text_or_none(get_optional_column(df, "Lote de caixa"))
    output["tipo_documento"] = _text_or_none(get_optional_column(df, "Tipo de documento"))
    output["tipo_conta"] = _text_or_none(get_optional_column(df, "Tipo da conta"))
    output["competencia"] = _text_or_none(get_optional_column(df, "Competência"))
    output["cnpj_credor_devedor"] = _text_or_none(get_optional_column(df, "C.N.P.J Credor/Devedor"))
    # Campos que existem no schema canônico do Rede mas não têm origem
    # confiável/equivalente neste relatório do Shift.
    output["estabelecimento"] = None
    # Lote de caixa do Shift e resumo de vendas/lote da Rede têm semânticas
    # distintas; mantemos o dado original em lote_caixa, fora da comparação.
    output["lote"] = None
    output["valor_mdr"] = None

    return output
