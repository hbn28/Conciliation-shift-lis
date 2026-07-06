from __future__ import annotations

from typing import Protocol

from app.domain.entities import Reconciliation, ReconciliationSave, Unit


class UnitRepository(Protocol):
    def list(self, active_only: bool = False) -> list[Unit]: ...
    def get(self, unit_id: int) -> Unit | None: ...
    def create(
        self, codigo: str, nome: str, estabelecimento: str | None,
        empresa_shift: str | None = None,
    ) -> int: ...
    def update(
        self, unit_id: int, codigo: str, nome: str,
        estabelecimento: str | None, ativa: bool,
        empresa_shift: str | None = None,
    ) -> None: ...
    def delete(self, unit_id: int) -> None: ...


class ReconciliationRepository(Protocol):
    def save(self, data: ReconciliationSave) -> None: ...
    def get(self, result_id: str) -> Reconciliation | None: ...
    def list(
        self, unit_id: int | None = None,
        date_from: str | None = None, date_to: str | None = None,
    ) -> list[Reconciliation]: ...
    def delete(self, result_id: str) -> list[str]: ...

