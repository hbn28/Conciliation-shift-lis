from __future__ import annotations

from app.application.ports.repositories import UnitRepository
from app.domain.entities import Unit


class ManageUnits:
    def __init__(self, repository: UnitRepository):
        self.repository = repository

    def list(self, active_only: bool = False) -> list[Unit]:
        return self.repository.list(active_only)

    def get(self, unit_id: int) -> Unit | None:
        return self.repository.get(unit_id)

    def create(
        self, codigo: str, nome: str, estabelecimento: str = "",
        empresa_shift: str = "",
    ) -> int:
        return self.repository.create(
            codigo, nome, estabelecimento or None, empresa_shift or None
        )

    def update(
        self, unit_id: int, codigo: str, nome: str,
        estabelecimento: str, ativa: bool, empresa_shift: str = "",
    ) -> None:
        self.repository.update(
            unit_id, codigo, nome, estabelecimento or None, ativa,
            empresa_shift or None,
        )

    def delete(self, unit_id: int) -> None:
        self.repository.delete(unit_id)

