from __future__ import annotations

import csv
import io
import re
import unicodedata
import warnings
from datetime import date
from pathlib import Path

import pandas as pd

from .audit import discard_record

EXPECTED_TERMS = {
    "autorizacao", "nsu", "valor bruto", "parcela", "bandeira",
    "modalidade", "estabelecimento", "data", "status",
}
TEXT_DTYPES = str

# --- Relatorio financeiro completo do Shift (cartao) ------------------------
SHIFT_HEADER_PREFIX = "Empresa;"
SHIFT_FOOTER_PREFIX = "Emitido por:"
SHIFT_CARD_ESSENTIAL_COLUMNS = ["Valor bruto", "Nro autorização cartão"]
SHIFT_CARD_IDENTIFIER_COLUMNS = ["Espécie", "Forma de pagamento/cobrança"]
CSV_ENCODINGS = ("utf-8-sig", "utf-8", "latin-1", "cp1252")


def _decode(raw: bytes) -> str:
    last_error = None
    for encoding in CSV_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Não foi possível decodificar o arquivo: {last_error}")


def _ignore_excel_style_warning():
    return warnings.catch_warnings()


def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _header_score(values) -> int:
    text = " | ".join(_fold(v) for v in values if pd.notna(v))
    return sum(term in text for term in EXPECTED_TERMS)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all").copy()
    useful = []
    for col in df.columns:
        name = _fold(col).strip()
        if name.startswith("unnamed") or re.fullmatch(r"coluna\s*\d+", name):
            if df[col].replace(r"^\s*$", pd.NA, regex=True).isna().all():
                continue
        useful.append(col)
    return df.loc[:, useful].reset_index(drop=True)


def _read_csv(path: Path) -> pd.DataFrame:
    raw = path.read_bytes()
    if not raw.strip():
        return pd.DataFrame()
    last_error = None
    for encoding in CSV_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        sample = "\n".join(text.splitlines()[:8])
        counts = {sep: sample.count(sep) for sep in (";", ",", "\t", "|")}
        sep = max(counts, key=counts.get)
        try:
            return _clean(pd.read_csv(
                io.StringIO(text), sep=sep, dtype=TEXT_DTYPES,
                keep_default_na=True, on_bad_lines="warn",
            ))
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
        except (pd.errors.ParserError, csv.Error) as exc:
            last_error = exc
    raise ValueError(f"Não foi possível ler o CSV: {last_error}")


def list_sheets(path: str | Path) -> list[str]:
    path = Path(path)
    if path.suffix.lower() not in {".xls", ".xlsx"}:
        return []
    return _open_excel(path).sheet_names


def _open_excel(path: Path) -> pd.ExcelFile:
    try:
        with _ignore_excel_style_warning():
            warnings.filterwarnings("ignore", message="Workbook contains no default style.*")
            return pd.ExcelFile(path)
    except Exception as exc:
        raise ValueError(
            f"Não foi possível abrir o arquivo Excel '{path.name}': {exc}"
        ) from exc


def _choose_sheet(path: Path, requested: str | None) -> str | int:
    excel = _open_excel(path)
    if requested:
        if requested not in excel.sheet_names:
            raise ValueError(f"Aba '{requested}' não encontrada. Disponíveis: {excel.sheet_names}")
        return requested
    for sheet in excel.sheet_names:
        if _fold(sheet) == "pagamentos":
            return sheet
    best = (0, excel.sheet_names[0])
    for sheet in excel.sheet_names:
        with _ignore_excel_style_warning():
            warnings.filterwarnings("ignore", message="Workbook contains no default style.*")
            preview = pd.read_excel(
                path, sheet_name=sheet, header=None, nrows=20, dtype=str
            )
        score = max((_header_score(row) for _, row in preview.iterrows()), default=0)
        if score > best[0]:
            best = (score, sheet)
    return best[1]


def _read_excel(path: Path, sheet_name: str | None) -> pd.DataFrame:
    sheet = _choose_sheet(path, sheet_name)
    with _ignore_excel_style_warning():
        warnings.filterwarnings("ignore", message="Workbook contains no default style.*")
        preview = pd.read_excel(
            path, sheet_name=sheet, header=None, nrows=25, dtype=str
        )
    scores = [_header_score(row) for _, row in preview.iterrows()]
    header = scores.index(max(scores)) if scores and max(scores) >= 2 else 0
    with _ignore_excel_style_warning():
        warnings.filterwarnings("ignore", message="Workbook contains no default style.*")
        return _clean(pd.read_excel(
            path, sheet_name=sheet, header=header, dtype=str
        ))


def _extract_report_date(path: Path) -> date | None:
    match = re.search(r"(\d{2})[.\-_](\d{2})[.\-_](\d{4})", path.name)
    if not match:
        return None
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def read_file_with_metadata(
    path: str | Path, sheet_name: str | None = None
) -> tuple[pd.DataFrame, dict]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv(path), {
            "sheet_name": None,
            "report_date": _extract_report_date(path),
        }
    if suffix in {".xls", ".xlsx"}:
        sheet = _choose_sheet(path, sheet_name)
        return _read_excel(path, sheet), {
            "sheet_name": sheet,
            "report_date": _extract_report_date(path),
        }
    raise ValueError("Formato não suportado. Envie CSV, XLS ou XLSX.")


def read_file(path: str | Path, sheet_name: str | None = None) -> pd.DataFrame:
    return read_file_with_metadata(path, sheet_name)[0]


def is_shift_financial_report(path: str | Path) -> bool:
    path = Path(path)
    if path.suffix.lower() != ".csv":
        return False
    try:
        text = _decode(path.read_bytes())
    except ValueError:
        return False
    return any(line.startswith(SHIFT_HEADER_PREFIX) for line in text.splitlines()[:60])


def read_shift_financial_report(
    path: str | Path, payment_scope: str = "card"
) -> tuple[pd.DataFrame, dict[str, int]]:
    path = Path(path)
    raw = path.read_bytes()
    if not raw.strip():
        return pd.DataFrame(), {"total_linhas": 0, "linhas_cartao": 0, "linhas_ignoradas": 0}

    text = _decode(raw)
    lines = text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines) if line.startswith(SHIFT_HEADER_PREFIX)), None
    )
    if header_idx is None:
        raise ValueError(
            "Não foi possível localizar o cabeçalho do relatório financeiro do Shift "
            f'(linha iniciada por "{SHIFT_HEADER_PREFIX}").'
        )
    footer_idx = next(
        (i for i, line in enumerate(lines) if line.startswith(SHIFT_FOOTER_PREFIX)),
        len(lines),
    )
    body_lines = [line for line in lines[header_idx:footer_idx] if line.strip()]
    if len(body_lines) < 2:
        return pd.DataFrame(), {"total_linhas": 0, "linhas_cartao": 0, "linhas_ignoradas": 0}

    body = "\n".join(body_lines)
    try:
        df = pd.read_csv(
            io.StringIO(body), sep=";", dtype=TEXT_DTYPES,
            keep_default_na=True, on_bad_lines="warn",
        )
    except pd.errors.EmptyDataError:
        return pd.DataFrame(), {"total_linhas": 0, "linhas_cartao": 0, "linhas_ignoradas": 0}
    except (pd.errors.ParserError, csv.Error) as exc:
        raise ValueError(f"Não foi possível ler o relatório financeiro do Shift: {exc}") from exc

    df["_source_line"] = df.index + header_idx + 2
    df = _clean(df)
    total_linhas = len(df)
    if df.empty:
        return df, {"total_linhas": 0, "linhas_cartao": 0, "linhas_ignoradas": 0}

    has_especie = "Espécie" in df.columns
    has_forma = "Forma de pagamento/cobrança" in df.columns
    if not has_especie and not has_forma:
        raise ValueError(
            "Não foi possível identificar pagamentos de cartão porque o relatório não "
            'possui "Espécie" nem "Forma de pagamento/cobrança".'
        )

    missing_essential = [c for c in SHIFT_CARD_ESSENTIAL_COLUMNS if c not in df.columns]
    if missing_essential:
        nomes = " e ".join(f'"{c}"' for c in missing_essential)
        raise ValueError(f"O relatório do Shift não contém a coluna essencial {nomes}.")

    if payment_scope != "card":
        return df, {"total_linhas": total_linhas, "linhas_cartao": total_linhas, "linhas_ignoradas": 0}

    mask = pd.Series(False, index=df.index)
    if has_especie:
        mask |= df["Espécie"].fillna("").str.contains("cart", case=False, na=False)
    if has_forma:
        mask |= (
            df["Forma de pagamento/cobrança"].fillna("").str.strip().str.upper().str.startswith("REDE")
        )
    card_df = df.loc[mask].reset_index(drop=True)
    ignored_df = df.loc[~mask]
    discarded = [
        discard_record(
            row,
            "SHIFT",
            "FORMA_PAGAMENTO_NAO_CARTAO",
            "Linha ignorada porque não foi identificada como pagamento de cartão Rede.",
        )
        for _, row in ignored_df.iterrows()
    ]
    stats = {
        "total_linhas": total_linhas,
        "linhas_cartao": len(card_df),
        "linhas_ignoradas": total_linhas - len(card_df),
        "descartes": discarded,
    }
    return card_df, stats
