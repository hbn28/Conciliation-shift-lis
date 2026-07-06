class DomainError(Exception):
    """Erro de regra de negócio que pode ser apresentado ao usuário."""


class NotFoundError(DomainError):
    """Entidade solicitada não existe."""


class ConflictError(DomainError):
    """Operação conflita com dados existentes."""

