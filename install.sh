#!/usr/bin/env bash
# NexCraft Panel — installation (Linux / macOS).
# Crée un environnement virtuel local (.venv), installe les dépendances et prépare le fichier .env.
# Ce script ne modifie ni le pare-feu, ni l'antivirus, ni aucun réglage système.
set -u
cd "$(dirname "$0")" || exit 1

fail() { printf '\n[ERREUR] %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

info "== NexCraft Panel : installation =="

PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || fail "Python 3.11 ou supérieur est introuvable.
  Debian/Ubuntu : sudo apt install python3 python3-venv python3-pip
  Fedora        : sudo dnf install python3
  macOS         : brew install python@3.12"
info "Python détecté : $($PY --version 2>&1) ($(command -v "$PY"))"

"$PY" -c 'import venv, ensurepip' >/dev/null 2>&1 || \
  fail "Les modules « venv » / « ensurepip » de Python sont absents.
  Debian/Ubuntu : sudo apt install python3-venv python3-pip"

if [ ! -x .venv/bin/python ]; then
  info "Création de l'environnement virtuel (.venv)…"
  "$PY" -m venv .venv || fail "Impossible de créer l'environnement virtuel dans $(pwd)/.venv (droits d'écriture ?)."
fi

info "Installation des dépendances (connexion Internet nécessaire)…"
.venv/bin/python -m pip install --disable-pip-version-check --quiet --upgrade pip || \
  fail "Mise à jour de pip impossible : vérifiez votre connexion Internet ou votre proxy."
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt || \
  fail "Échec de l'installation des dépendances. Vérifiez votre connexion Internet (accès à pypi.org).
  Pour une installation strictement reproductible : .venv/bin/python -m pip install -r requirements-tested.txt"

mkdir -p data server backups logs || fail "Impossible de créer les dossiers de données."
if [ ! -f .env ]; then
  cp .env.example .env || fail "Impossible de créer le fichier .env."
  info "Fichier .env créé à partir de .env.example."
fi
chmod 600 .env 2>/dev/null || true

if command -v java >/dev/null 2>&1; then
  info "Java détecté : $(java -version 2>&1 | head -n 1)"
else
  info "[Avertissement] Java est introuvable dans le PATH. Installez un JDK/JRE (ex. Temurin 21) avant de démarrer un serveur Minecraft,"
  info "                ou indiquez son chemin complet dans « Paramètres du serveur »."
fi

info ""
info "Installation terminée."
info "  1. Lancez le panel :   ./start.sh"
info "  2. Ouvrez l'adresse affichée (par défaut http://localhost:8080) et créez votre compte administrateur."
