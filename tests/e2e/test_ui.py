"""Parcours utilisateur réels dans Chromium : connexion, pages, serveur, console, fichiers, éditeur, sauvegardes…"""
import re
import time
from pathlib import Path

import pytest

from .conftest import PASSWORD, wait_js

PAGES = ["dashboard", "console", "files", "players", "backups", "scheduler", "server", "diagnostics", "history", "notifications", "accounts", "settings", "account"]


def pill(ui, text, timeout=25000):
    ui.page.wait_for_selector(f"#top-status .pill:has-text('{text}')", timeout=timeout)


def test_login_logout_and_errors(ui):
    p = ui.page
    p.goto(ui.base + "/")
    p.wait_for_selector("input[name=username]")
    assert "Connexion" in p.inner_text("h1")
    p.fill("input[name=username]", "admin")
    p.fill("input[name=password]", "mauvais-mot-de-passe")
    p.click("button[type=submit]")
    p.wait_for_selector(".alert.bad")
    assert "incorrect" in p.inner_text(".alert.bad")
    ui.login()
    assert "Tableau de bord" in p.inner_text("#page-title")
    p.click("#user-btn")
    p.click("text=Se déconnecter")
    p.wait_for_selector("input[name=username]")


def test_every_page_renders_without_js_errors(ui):
    ui.login()
    for route in PAGES:
        ui.go(route)
        assert not ui.page.locator("#content .alert.bad").count(), f"{route} : {ui.page.locator('#content .alert.bad').all_inner_texts()}"
        assert ui.page.inner_text("#page-title").strip(), route
    assert ui.errors == [], ui.errors


def test_navigation_updates_active_item_and_unknown_route(ui):
    ui.login()
    ui.page.click(".nav a[data-nav=console]")
    ui.page.wait_for_selector(".console")
    assert "active" in ui.page.get_attribute(".nav a[data-nav=console]", "class")
    ui.page.goto(ui.base + "/#/inexistante")
    ui.page.wait_for_selector("text=Page introuvable")


def test_theme_toggle_persists(ui):
    ui.login()
    ui.page.click("#user-btn")
    ui.page.click("text=Thème clair")
    assert ui.page.evaluate("document.documentElement.dataset.theme") == "light"
    ui.page.reload()
    ui.page.wait_for_selector(".sidebar")
    assert ui.page.evaluate("document.documentElement.dataset.theme") == "light"
    ui.page.click("#user-btn")
    ui.page.click("text=Thème sombre")


def test_dashboard_shows_unavailable_stats_when_stopped(ui):
    ui.login()
    ui.go("dashboard")
    assert "Arrêté" in ui.page.inner_text(".hero .state")
    cards = ui.page.inner_text(".grid.g4")
    assert cards.count("indisponible") >= 3  # CPU, RAM, joueurs : jamais de fausses valeurs
    assert ui.errors == []


def test_preflight_report_modal(ui):
    ui.login()
    ui.go("dashboard")
    ui.page.click("text=Vérifier avant démarrage")
    ui.page.wait_for_selector(".modal:has-text('Rapport avant démarrage')")
    txt = ui.page.inner_text(".modal")
    assert "Java" in txt and "Port réseau" in txt and "CLUF" in txt
    ui.page.keyboard.press("Escape")
    ui.page.wait_for_selector(".modal", state="detached")


def test_start_console_commands_and_stop(ui, live):
    p = ui.page
    ui.login()
    ui.go("dashboard")
    p.click(".hero button:has-text('Démarrer')")
    pill(ui, "En ligne")
    assert "En ligne" in p.inner_text(".hero .state")
    ui.go("console")
    p.wait_for_selector(".conn.live", timeout=10000)
    p.wait_for_selector(".cl:has-text('Done (')")
    # envoi d'une commande et lecture de la réponse du processus réel
    p.fill(".cmdbar input", "list")
    p.press(".cmdbar input", "Enter")
    p.wait_for_selector(".cl:has-text('There are 0 of a max of 20 players online')")
    p.fill(".cmdbar input", "join Steve")
    p.press(".cmdbar input", "Enter")
    p.wait_for_selector(".cl:has-text('Steve joined the game')")
    # historique des commandes avec la flèche haut
    p.focus(".cmdbar input")
    p.keyboard.press("ArrowUp")
    assert p.input_value(".cmdbar input") == "join Steve"
    p.keyboard.press("ArrowUp")
    assert p.input_value(".cmdbar input") == "list"
    p.fill(".cmdbar input", "")
    # recherche et filtre par niveau
    p.fill("input[aria-label='Rechercher dans les logs']", "Steve")
    wait_js(p, "document.querySelectorAll('.console .cl').length >= 1 && [...document.querySelectorAll('.console .cl')].every(e => /steve/i.test(e.textContent))")
    assert p.locator(".console mark").count() >= 1
    p.fill("input[aria-label='Rechercher dans les logs']", "")
    p.click(".seg button[data-lvl=INFO]")  # masque INFO
    wait_js(p, "![...document.querySelectorAll('.console .cl')].some(e => e.classList.contains('INFO'))")
    p.click(".seg button[data-lvl=INFO]")
    # vider l'affichage sans effacer les logs
    p.click("button:has-text(\"Vider l'affichage\")")
    wait_js(p, "document.querySelectorAll('.console .cl').length === 0")
    # commande sensible : confirmation demandée
    p.fill(".cmdbar input", "op Steve")
    p.press(".cmdbar input", "Enter")
    p.wait_for_selector(".modal:has-text('Commande sensible')")
    p.click(".modal button:has-text('Annuler')")
    # la page Joueurs reflète le joueur réel
    ui.go("players")
    p.wait_for_selector("td:has-text('Steve')")
    # arrêt propre via la barre supérieure
    p.click("#top-actions button:has-text('Arrêter')")
    p.click(".modal button:has-text('Arrêter')")
    pill(ui, "Arrêté")
    assert ui.errors == [], ui.errors


def test_files_create_edit_with_monaco_and_history(ui, live):
    p = ui.page
    ui.login()
    ui.go("files")
    p.click("button:has-text('Nouveau fichier')")
    p.fill(".modal input", "notes.txt")
    p.click(".modal button:has-text('Créer')")
    p.wait_for_selector("td .fname:has-text('notes.txt')")
    p.dblclick("td .fname:has-text('notes.txt')") if False else p.click("td .fname:has-text('notes.txt')")
    p.wait_for_selector(".editor-host")
    p.wait_for_selector(".monaco-editor", timeout=30000)  # Monaco local chargé (hors ligne)
    p.click(".monaco-editor .view-lines")
    p.keyboard.type("Bonjour NexCraft")
    p.wait_for_selector("text=modifications non enregistrées")
    p.keyboard.press("Control+S")
    p.wait_for_selector("text=Fichier enregistré")
    assert (live["server"] / "notes.txt").read_text() == "Bonjour NexCraft"
    # 2e enregistrement : une version est conservée dans l'historique
    p.click(".monaco-editor .view-lines")
    p.keyboard.press("Control+End")
    p.keyboard.type(" v2")
    p.keyboard.press("Control+S")
    wait_js(p, "document.querySelector('.badge.warn') === null || document.querySelector('.badge.warn').hidden")
    time.sleep(0.4)
    p.click("button:has-text('Historique et comparaison')")
    p.wait_for_selector(".modal td:has-text('edit')")
    p.click(".modal button:has-text('Comparer')")
    p.wait_for_selector(".diff")
    assert "Bonjour NexCraft" in p.inner_text(".diff")
    assert ui.errors == [], ui.errors


def test_editor_warns_before_leaving_with_unsaved_changes(ui, live):
    p = ui.page
    (live["server"] / "garde.txt").write_text("contenu")
    ui.login()
    p.goto(f"{ui.base}/#/editor?path=garde.txt")
    p.wait_for_selector(".monaco-editor", timeout=30000)
    p.click(".monaco-editor .view-lines")
    p.keyboard.type("x")
    p.wait_for_selector("text=modifications non enregistrées")
    p.click(".nav a[data-nav=players]")
    p.wait_for_selector(".modal:has-text('Modifications non enregistrées')")
    p.click(".modal button:has-text('Annuler')")
    p.wait_for_selector(".editor-host")  # on reste sur l'éditeur
    assert (live["server"] / "garde.txt").read_text() == "contenu"


def test_editor_json_syntax_error_reported(ui, live):
    p = ui.page
    (live["server"] / "bad.json").write_text('{"a": 1,}')
    ui.login()
    p.goto(f"{ui.base}/#/editor?path=bad.json")
    p.wait_for_selector(".monaco-editor", timeout=30000)
    p.wait_for_selector("text=erreur(s) de syntaxe", timeout=15000)


def test_files_upload_rename_download_delete_restore(ui, live, tmp_path):
    p = ui.page
    ui.login()
    ui.go("files")
    src = tmp_path / "plugin-config.yml"
    src.write_text("a: 1\n")
    p.set_input_files("input[type=file][multiple]", str(src))
    p.wait_for_selector("td .fname:has-text('plugin-config.yml')")
    assert (live["server"] / "plugin-config.yml").read_text() == "a: 1\n"
    # menu contextuel -> renommer
    p.click("td .fname:has-text('plugin-config.yml')", button="right")
    p.click(".ctx button:has-text('Renommer')")
    p.fill(".modal input", "config-renomme.yml")
    p.click(".modal button:has-text('Renommer')")
    p.wait_for_selector("td .fname:has-text('config-renomme.yml')")
    assert (live["server"] / "config-renomme.yml").exists()
    # téléchargement
    p.click("td .fname:has-text('config-renomme.yml')", button="right")
    with p.expect_download() as dl:
        p.click(".ctx button:has-text('Télécharger')")
    assert Path(dl.value.path()).read_text() == "a: 1\n"
    # suppression -> corbeille -> restauration
    p.click("td .fname:has-text('config-renomme.yml')", button="right")
    p.click(".ctx button:has-text('Supprimer')")
    p.click(".modal button:has-text('Supprimer')")
    p.wait_for_selector("td .fname:has-text('config-renomme.yml')", state="detached")
    p.click("button:has-text('Corbeille')")
    p.wait_for_selector(".modal td:has-text('config-renomme.yml')")
    p.click(".modal button:has-text('Restaurer')")
    p.keyboard.press("Escape")
    p.wait_for_selector("td .fname:has-text('config-renomme.yml')")
    assert (live["server"] / "config-renomme.yml").exists()
    assert ui.errors == [], ui.errors


def test_files_traversal_in_url_is_refused(ui):
    ui.login()
    ui.page.goto(f"{ui.base}/#/files?path=../../etc")
    ui.page.wait_for_selector("#content .alert.bad")
    assert "etc" not in ui.page.inner_text(".ftable") if ui.page.locator(".ftable").count() else True


def test_backup_create_verify_from_ui(ui, live):
    p = ui.page
    (live["server"] / "world").mkdir(exist_ok=True)
    (live["server"] / "world" / "level.dat").write_bytes(b"x")
    ui.login()
    ui.go("backups")
    p.click("button:has-text('Nouvelle sauvegarde')")
    p.fill(".modal input[placeholder^='Note']", "test e2e")
    p.click(".modal button:has-text('Lancer la sauvegarde')")
    p.wait_for_selector("td:has-text('réussie')", timeout=20000)
    p.click("td button:has-text('Vérifier')")
    p.wait_for_selector(".toast:has-text('intègre')")
    # la restauration exige une confirmation explicite
    p.click("td button:has-text('Restaurer')")
    p.wait_for_selector(".modal:has-text('remplace les données actuelles')")
    p.click(".modal button:has-text('Restaurer')")
    p.wait_for_selector(".toast:has-text('Cochez la confirmation')")
    p.keyboard.press("Escape")
    assert ui.errors == [], ui.errors


def test_scheduler_create_task_and_economy(ui):
    p = ui.page
    ui.login()
    ui.go("scheduler")
    p.click("button:has-text('Nouvelle tâche')")
    p.fill(".modal input >> nth=0", "Annonce quotidienne")
    p.select_option(".modal select >> nth=0", "notification")
    p.fill(".modal .form-grid >> nth=2 >> input >> nth=0", "Hello e2e") if False else None
    p.locator(".modal label:has-text('Titre') ~ input").fill("Hello e2e")
    p.click(".modal button:has-text('Créer')")
    p.wait_for_selector("td b:has-text('Annonce quotidienne')")
    p.click("tr:has-text('Annonce quotidienne') button:has-text('Lancer')")
    p.wait_for_selector("td:has-text('Notification envoyée')", timeout=10000)
    # le mode économie rappelle qu'aucune économie n'est garantie
    assert "aucune économie n'est garantie" in p.inner_text("#content")
    assert ui.errors == [], ui.errors


def test_accounts_and_viewer_restrictions(ui, live, browser):
    p = ui.page
    ui.login()
    ui.go("accounts")
    p.click("button:has-text('Nouveau compte')")
    p.fill(".modal input >> nth=0", "lecteur1")
    p.fill(".modal input[type=password]", PASSWORD)
    p.click(".modal button:has-text('Créer le compte')")
    p.wait_for_selector("td b:has-text('lecteur1')")
    # connexion en tant que lecteur : pas d'administration, pas de fichiers, pas de contrôle du serveur
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p2 = ctx.new_page()
    u2 = type(ui)(p2, ui.base)
    u2.login("lecteur1")
    nav = p2.inner_text(".nav")
    assert "Tableau de bord" in nav and "Console" in nav
    assert "Comptes" not in nav and "Fichiers" not in nav and "Paramètres généraux" not in nav
    assert p2.locator("#top-actions button").count() == 0
    p2.goto(f"{ui.base}/#/accounts")
    p2.wait_for_selector("text=Accès refusé")
    p2.goto(f"{ui.base}/#/console")
    p2.wait_for_selector(".cmdbar input[disabled]")
    assert u2.errors == [], u2.errors
    ctx.close()


def test_mobile_layout_has_drawer_menu(browser, live):
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, is_mobile=True)
    page = ctx.new_page()
    u = type("U", (), {})()
    from .conftest import Ui
    u = Ui(page, live["base"])
    u.login()
    sidebar_box = page.locator("#sidebar").bounding_box()
    assert sidebar_box["x"] + sidebar_box["width"] <= 5  # menu replié hors écran
    page.click(".menu-btn")
    page.wait_for_selector("#sidebar.open")
    page.click(".nav a[data-nav=console]")
    page.wait_for_selector(".console")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert u.errors == [], u.errors
    ctx.close()


def test_revoked_session_returns_to_login_screen(ui, live):
    p = ui.page
    ui.login()
    ui.go("dashboard")
    # un autre appareil (session API) révoque toutes les autres sessions du panel
    r = live["api"].post("/api/admin/sessions/revoke-all", params={"include_self": "false"})
    assert r.status_code == 200 and r.json()["revoked"] >= 1
    p.click(".nav a[data-nav=history]")
    p.wait_for_selector("text=session a expiré", timeout=10000)
    assert p.locator("input[name=username]").count() == 1
