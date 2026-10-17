"""Password hashing (Argon2id) and password policy."""
from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import settings

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def validate_password_policy(password: str) -> list[str]:
    problems: list[str] = []
    if len(password) < settings.password_min_length:
        problems.append(f"mínimo de {settings.password_min_length} caracteres")
    if not re.search(r"[a-z]", password):
        problems.append("ao menos uma letra minúscula")
    if not re.search(r"[A-Z]", password):
        problems.append("ao menos uma letra maiúscula")
    if not re.search(r"\d", password):
        problems.append("ao menos um número")
    if not re.search(r"[^\w\s]", password):
        problems.append("ao menos um símbolo")
    return problems
