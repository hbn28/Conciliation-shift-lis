from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UploadedSpreadsheet:
    origem: str
    categoria: str
    path: Path
    nome_original: str
    sheet: str | None = None


@dataclass
class ComparisonResult:
    resumo: dict
    detalhado: Any
    qualidade_shift: Any
    auditoria: Any = None
    descartes: Any = None
