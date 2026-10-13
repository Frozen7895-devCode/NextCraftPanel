# Composants tiers embarqués

| Composant | Version | Licence | Emplacement |
|---|---|---|---|
| Monaco Editor | 0.52.2 | MIT | `app/static/vendor/monaco/` (licence : `LICENSE.txt` du dossier) |
| IBM Plex Sans (polices) | 5.3.0 (Fontsource) | SIL Open Font License 1.1 | `app/static/fonts/` (`LICENSE-IBM-Plex-Sans.txt`) |
| JetBrains Mono (polices) | 5.3.0 (Fontsource) | SIL Open Font License 1.1 | `app/static/fonts/` (`LICENSE-JetBrains-Mono.txt`) |

Monaco Editor est allégé (langues d'interface autres que le français supprimées, services de langage
TypeScript/CSS/HTML retirés ; JSON conservé pour la validation de syntaxe). `scripts/vendor_monaco.py`
reproduit cette préparation.

Les dépendances Python (FastAPI, Uvicorn, SQLAlchemy, argon2-cffi, psutil, python-multipart, httpx) sont
installées depuis PyPI et conservent leurs licences respectives (BSD/MIT/Apache-2.0).

NexCraft Panel n'inclut ni Java ni aucun fichier de Minecraft. Minecraft est une marque de Mojang AB ;
ce projet n'est ni affilié ni approuvé par Mojang ou Microsoft.
