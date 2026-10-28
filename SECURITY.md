# Sécurité

- Le panel nécessite `sudo` — il contrôle systemd et Apache.
- Aucune commande shell (`shell=True`), tout passe par argv.
- Chaque service tourne sous un compte dédié non-root avec isolation systemd (`NoNewPrivileges`, `PrivateTmp`, `ProtectHome`, etc.).
- SSH et les services réseau critiques sont protégés — le panel refuse de les stopper.
- Les domaines, chemins et commandes sont validés avant toute action.
- Cron : le panel n'écrit que des fichiers root (`/etc/cron.d/serverpanel`, `0644`), et les expressions comme les commandes sont validées (pas de saut de ligne, longueur bornée, bornes cron). Les commandes planifiées sont exécutées par cron sous l'utilisateur choisi ; l'exécution « maintenant » passe explicitement par `/bin/sh -c`, comme cron.
- Ce panel ne remplace pas les sauvegardes ni le pare-feu.
