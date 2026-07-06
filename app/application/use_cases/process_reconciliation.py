from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.application.ports.reconciliation import ReconciliationEngine
from app.application.ports.repositories import (
    ReconciliationRepository,
    UnitRepository,
)
from app.domain.entities import ImportedFile, ReconciliationSave
from app.domain.exceptions import DomainError, NotFoundError
from app.application.models import ComparisonResult, UploadedSpreadsheet


@dataclass(frozen=True)
class ProcessReconciliationCommand:
    result_id: str
    unit_id: int
    reconciliation_date: str
    rede_files: tuple[UploadedSpreadsheet, ...]
    shift_files: tuple[UploadedSpreadsheet, ...]
    output_dir: Path


class ProcessReconciliation:
    def __init__(
        self,
        units: UnitRepository,
        reconciliations: ReconciliationRepository,
        engine: ReconciliationEngine,
    ):
        self.units = units
        self.reconciliations = reconciliations
        self.engine = engine

    def execute(self, command: ProcessReconciliationCommand) -> ComparisonResult:
        unit = self.units.get(command.unit_id)
        if not unit:
            raise NotFoundError("Unidade não encontrada.")
        if not unit.ativa:
            raise DomainError("Selecione uma unidade ativa.")

        engine_output = self.engine.execute(
            rede_files=list(command.rede_files),
            shift_files=list(command.shift_files),
            reconciliation_date=command.reconciliation_date,
            establishment=unit.estabelecimento,
            shift_empresa=unit.empresa_shift,
            output_dir=command.output_dir,
        )
        comparison = engine_output.comparison
        comparison.resumo["data_conciliacao"] = command.reconciliation_date
        comparison.resumo["unidade"] = unit.nome
        comparison.resumo["estabelecimento_rede"] = ", ".join(
            engine_output.rede_context["estabelecimentos"]
        )
        if engine_output.rede_context.get("alertas_empresa_shift"):
            comparison.resumo["alertas_empresa_shift"] = engine_output.rede_context[
                "alertas_empresa_shift"
            ]
        self.engine.export(comparison, command.output_dir)
        self.reconciliations.save(ReconciliationSave(
            id=command.result_id,
            unidade_id=unit.id,
            data_conciliacao=command.reconciliation_date,
            resumo=comparison.resumo,
            arquivos_importados=[
                *[
                    ImportedFile(
                        id=0,
                        conciliacao_id=command.result_id,
                        origem=item.origem,
                        categoria=item.categoria,
                        nome_original=item.nome_original,
                        caminho_arquivo=str(item.path.resolve()),
                        quantidade_linhas=engine_output.rede_rows_por_arquivo.get(
                            item.origem, 0
                        ),
                        criado_em="",
                    )
                    for item in command.rede_files
                ],
                *[
                    ImportedFile(
                        id=0,
                        conciliacao_id=command.result_id,
                        origem=item.origem,
                        categoria=item.categoria,
                        nome_original=item.nome_original,
                        caminho_arquivo=str(item.path.resolve()),
                        quantidade_linhas=engine_output.shift_rows_por_arquivo.get(
                            item.origem, 0
                        ),
                        criado_em="",
                    )
                    for item in command.shift_files
                ],
            ],
        ))
        return comparison
