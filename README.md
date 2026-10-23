# ServerPanel 1.0 beta

Interface TUI pour gérer services systemd, sites Apache et voir l'état du VPS — depuis SSH, au clavier, sur Debian 13.

Pas de bibliothèque externe, pas d'API, pas de port supplémentaire.

## Installation

```bash
sudo bash install.sh
sudo panel
```

## Navigation

| Touche | Action |
|---|---|
| `↑` / `↓` | Se déplacer dans la liste |
| `→` / `Tab` | Aller aux actions |
| `←` / `Échap` | Revenir |
| `Entrée` | Exécuter l'action |
| `1` `2` `3` | Changer de section |
| `N` | Nouveau service / site |
| `F` | Épingler / désépingler |
| `A` | Tous les services |
| `R` | Actualiser |
| `Q` | Quitter |

## Services

`N` dans SERVICES pour créer une app systemd. Le panel crée un compte dédié `svc-<nom>`, écrit l'unité et gère l'autostart.

Les unités créées sont dans `/etc/systemd/system/panel-*.service`. La suppression depuis le panel retire l'unité mais laisse le code et le compte intact.

**Note Node.js :** utiliser un chemin absolu vers node (`/opt/nodejs/v20/bin/node`) — les binaires dans `/root/.nvm` ne sont pas accessibles aux comptes de service.

## Sites

`N` dans SITES pour créer un VirtualHost Apache :
- **STATIQUE** : sert un dossier sous `/var/www/`
- **PROXY** : redirige vers un backend local (`127.0.0.1:PORT`)

Après création, le panel propose directement le HTTPS via Certbot.

## Emplacements

| Chemin | Contenu |
|---|---|
| `/usr/bin/panel` | Commande |
| `/etc/serverpanel/config.json` | Config, favoris, services perso |
| `/srv/apps/` | Code des applications |
| `/etc/systemd/system/panel-*.service` | Unités créées |
| `/etc/apache2/sites-available/panel-*.conf` | Sites créés |
