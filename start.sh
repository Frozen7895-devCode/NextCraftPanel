#!/usr/bin/env bash
# NexCraft Panel — démarrage (Linux / macOS).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  printf '[ERREUR] Le panel n'"'"'est pas installé. Lancez d'"'"'abord : ./install.sh\n' >&2
  exit 1
fi
exec .venv/bin/python -m app
