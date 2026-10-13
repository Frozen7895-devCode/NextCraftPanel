// Applique le thème avant le premier rendu (évite le flash). Fichier externe : la CSP interdit les scripts inline.
try { document.documentElement.dataset.theme = localStorage.getItem("nexcraft-theme") || "dark"; } catch (e) { /* stockage indisponible */ }
