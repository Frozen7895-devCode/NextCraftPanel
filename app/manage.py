"""Outil de maintenance en ligne de commande (à lancer sur la machine qui héberge le panel).

    python -m app.manage list-users
    python -m app.manage reset-password NOM [--activate] [--make-admin]

Sert surtout à récupérer l'accès si le mot de passe de l'administrateur est perdu : il n'existe volontairement
aucun compte ni mot de passe par défaut. L'opération nécessite un accès au disque de la machine (donc à la base).
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select

from .config import Settings
from .database import Database
from .models import User
from .security.passwords import hash_password, validate_password_policy
from .security.permissions import default_permissions
from .security.sessions import revoke_user_sessions
from .services.audit import audit


def run(argv: list[str] | None = None, settings: Settings | None = None, out=print) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.manage", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-users", help="liste les comptes")
    rp = sub.add_parser("reset-password", help="définit un nouveau mot de passe pour un compte")
    rp.add_argument("username")
    rp.add_argument("--activate", action="store_true", help="réactive le compte s'il est désactivé")
    rp.add_argument("--make-admin", action="store_true", help="donne le rôle administrateur au compte")
    rp.add_argument("--password-stdin", action="store_true", help="lit le mot de passe sur l'entrée standard (scripts)")
    args = ap.parse_args(argv)
    settings = settings or Settings.from_env()
    if not settings.db_path.exists():
        out(f"Aucune base de données trouvée ({settings.db_path}). Lancez d'abord le panel une fois.")
        return 1
    db = Database(settings.db_path)
    try:
        with db.scope() as s:
            if args.cmd == "list-users":
                for u in s.scalars(select(User).order_by(User.username)).all():
                    out(f"{u.username:<24} {u.role:<8} {'actif' if u.is_active else 'DÉSACTIVÉ'}")
                return 0
            user = s.scalar(select(User).where(func.lower(User.username) == args.username.lower()))
            if user is None:
                out(f"Compte introuvable : {args.username}")
                return 1
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\r\n")
            else:
                password = getpass.getpass("Nouveau mot de passe : ")
                if password != getpass.getpass("Confirmer : "):
                    out("Les deux mots de passe ne correspondent pas.")
                    return 1
            errors = validate_password_policy(password, user.username)
            if errors:
                out("Mot de passe refusé : " + " ".join(errors))
                return 1
            user.password_hash = hash_password(password)
            user.must_change_password = False
            if args.activate:
                user.is_active = True
            if args.make_admin:
                user.role = "admin"
                user.permissions = default_permissions("admin")
            n = revoke_user_sessions(s, user.id)
            audit(s, "cli", "account.reset_password", user.username, f"réinitialisation en ligne de commande ({n} session(s) révoquée(s))", "local", level="warning")
            out(f"Mot de passe de « {user.username} » mis à jour ({n} session(s) fermée(s)).")
            return 0
    finally:
        db.dispose()


if __name__ == "__main__":
    sys.exit(run())
