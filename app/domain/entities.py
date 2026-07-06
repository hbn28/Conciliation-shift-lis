from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Unit:
    id: int
    codigo: str
    nome: str
    estabelecimento: str | None
    ativa: bool
    criado_em: str
    atualizado_em: str
    # Valor exato da coluna "Empresa" no relatório do Shift para
    # esta unidade (ex.: "CENTRALLAB (Jn)"). Usado para conferir que o
    # arquivo do Shift enviado corresponde ao mesmo estabelecimento da Rede
    # cadastrado para esta unidade, já que a Rede agrupa por regional e o
    # Shift identifica cada unidade individualmente. O nome da propriedade é
    # mantido por compatibilidade com o banco já existente.
    empresa_shift: str | None = None


@dataclass(frozen=True)
class ImportedFile:
    id: int
    conciliacao_id: str
    origem: str
    categoria: str
    nome_original: str
    caminho_arquivo: str
    quantidade_linhas: int
    criado_em: str


@dataclass(frozen=True)
class Reconciliation:
    id: str
    unidade_id: int
    data_conciliacao: str
    status: str
    resumo: dict
    criado_em: str
    unidade_codigo: str = ""
    unidade_nome: str = ""
    arquivos: dict[str, ImportedFile] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationSave:
    id: str
    unidade_id: int
    data_conciliacao: str
    resumo: dict
    arquivos_importados: list[ImportedFile]
