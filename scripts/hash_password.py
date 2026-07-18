"""Gera o valor de hash para colocar em `APP_USERS`.

Uso:
    python scripts/hash_password.py usuario1
    (o script pede a senha sem exibir na tela)

Copie a linha impressa e adicione à variável de ambiente `APP_USERS`,
separando vários usuários com vírgula:

    APP_USERS=usuario1:pbkdf2$120000$...,usuario2:pbkdf2$120000$...
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("Uso: python scripts/hash_password.py <usuario>")
        raise SystemExit(1)
    usuario = sys.argv[1].strip()
    senha = getpass.getpass("Senha: ")
    confirmacao = getpass.getpass("Confirme a senha: ")
    if senha != confirmacao:
        print("As senhas não conferem.")
        raise SystemExit(1)
    if not senha:
        print("A senha não pode ser vazia.")
        raise SystemExit(1)
    print(f"\n{usuario}:{hash_password(senha)}")


if __name__ == "__main__":
    main()
