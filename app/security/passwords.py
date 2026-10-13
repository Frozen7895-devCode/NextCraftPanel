"""Mots de passe : hachage Argon2id et politique de robustesse."""
from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # Argon2id, paramètres par défaut recommandés

# Dummy valide pour égaliser le temps de réponse quand l'utilisateur n'existe pas.
_DUMMY_HASH = _hasher.hash("nexcraft-dummy-password")

MIN_LENGTH = 10
MAX_LENGTH = 128
_COMMON = {
    "password", "password1", "motdepasse", "motdepasse1", "azertyuiop", "qwertyuiop",
    "1234567890", "123456789012", "administrator", "minecraft123", "nexcraft123",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Vérifie un mot de passe. Si le hash est None, effectue un faux calcul (timing)."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_policy(password: str, username: str = "") -> list[str]:
    """Retourne la liste des problèmes (vide si le mot de passe est acceptable)."""
    errors: list[str] = []
    if len(password) < MIN_LENGTH:
        errors.append(f"Le mot de passe doit contenir au moins {MIN_LENGTH} caractères.")
    if len(password) > MAX_LENGTH:
        errors.append(f"Le mot de passe ne doit pas dépasser {MAX_LENGTH} caractères.")
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 3:
        errors.append("Utilisez au moins 3 types de caractères parmi : minuscules, majuscules, chiffres, symboles.")
    low = password.lower()
    if username and username.lower() in low:
        errors.append("Le mot de passe ne doit pas contenir le nom d'utilisateur.")
    if low in _COMMON:
        errors.append("Ce mot de passe est trop courant.")
    return errors
