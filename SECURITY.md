# Sécurité

- Le panel nécessite `sudo` — il contrôle systemd et Apache.
- Aucune commande shell (`shell=True`), tout passe par argv.
- Chaque service tourne sous un compte dédié non-root avec isolation systemd (`NoNewPrivileges`, `PrivateTmp`, `ProtectHome`, etc.).
- SSH et les services réseau critiques sont protégés — le panel refuse de les stopper.
- Les domaines, chemins et commandes sont validés avant toute action.
- Ce panel ne remplace pas les sauvegardes ni le pare-feu.
