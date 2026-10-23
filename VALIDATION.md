# Validation

Testé sur Debian 13, Python 3.13, Apache 2.4, systemd 257.

- 37 tests unitaires — tous passent
- Création/suppression de VirtualHosts Apache avec validation syntaxique réelle
- Génération d'unités systemd vérifiée avec `systemd-analyze verify`
- Navigation curses testée dans un pseudo-terminal
- Tests de sécurité : chemins/domaines invalides, refus d'arrêt SSH, permissions

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

**Non testé ici :** boot réel avec systemd en PID 1, Let's Encrypt sur domaine public.

> Version beta — tester sur une VM avant de déployer en prod.
