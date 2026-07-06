from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.application.models import ComparisonResult, UploadedSpreadsheet


@dataclass(frozen=True)
class EngineOutput:
    comparison: ComparisonResult
    rede_rows: int
    rede_rows_por_arquivo: dict[str, int]
    shift_rows: int
    shift_rows_por_arquivo: dict[str, int]
    rede_context: dict


class ReconciliationEngine(Protocol):
    def execute(
        self,
        rede_files: list[UploadedSpreadsheet],
        shift_files: list[UploadedSpreadsheet],
        reconciliation_date: str,
        establishment: str | None,
        output_dir: Path,
        shift_empresa: str | None = None,
    ) -> EngineOutput: ...

    def export(self, comparison: ComparisonResult, output_dir: Path) -> None: ...
