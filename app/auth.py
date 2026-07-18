"""Login por usuário, sem banco de dados.

Os usuários e senhas ficam na variável de ambiente `APP_USERS`, no formato:

    usuario1:pbkdf2$120000$<salt>$<hash>,usuario2:pbkdf2$120000$<salt>$<hash>

O hash de cada senha é gerado com `scripts/hash_password.py` (veja o README).
Nunca colocar senha em texto puro em `APP_USERS`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from secrets import token_hex

PBKDF2_ITERATIONS = 120_000
SESSION_KEY = "usuario"


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algoritmo, iteracoes, salt, digest_esperado = stored_hash.split("$")
        if algoritmo != "pbkdf2":
            return False
        digest_calculado = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iteracoes)
        ).hex()
        return hmac.compare_digest(digest_calculado, digest_esperado)
    except (ValueError, AttributeError):
        return False


def load_users() -> dict[str, str]:
    """Lê `APP_USERS` do ambiente a cada chamada (permite testes trocarem a
    variável sem reiniciar o processo)."""
    raw = os.environ.get("APP_USERS", "")
    usuarios: dict[str, str] = {}
    for par in raw.split(","):
        par = par.strip()
        if not par or ":" not in par:
            continue
        usuario, senha_hash = par.split(":", 1)
        usuario = usuario.strip()
        if usuario:
            usuarios[usuario] = senha_hash.strip()
    return usuarios


def authenticate(usuario: str, senha: str) -> bool:
    usuarios = load_users()
    senha_hash = usuarios.get(usuario)
    if not senha_hash:
        return False
    return verify_password(senha, senha_hash)


PUBLIC_PATHS = {"/login", "/health"}
PUBLIC_PREFIXES = ("/static",)


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
