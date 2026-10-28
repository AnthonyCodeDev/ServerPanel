# ServerPanel 1.0 beta

Interface TUI pour gérer services systemd, sites Apache et crons, et voir l'état du VPS — depuis SSH, au clavier, sur Debian 13.

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
| `1` `2` `3` `4` | Changer de section : VPS, services, sites, cron |
| `N` | Nouveau service / site / cron |
| `F` | Épingler / désépingler un service ou un cron |
| `A` | Tous les services / tous les crons |
| `R` | Actualiser |
| `Q` | Quitter |

Les sections s'enchaînent dans l'ordre VPS, SERVICES, SITES, CRON (`1` à `4`). Au lancement, le panel ouvre VPS.

## Services

`N` dans SERVICES pour créer une app systemd. Le panel crée un compte dédié `svc-<nom>`, écrit l'unité et gère l'autostart.

Les unités créées sont dans `/etc/systemd/system/panel-*.service`. La suppression depuis le panel retire l'unité mais laisse le code et le compte intact.

**Note Node.js :** utiliser un chemin absolu vers node (`/opt/nodejs/v20/bin/node`) — les binaires dans `/root/.nvm` ne sont pas accessibles aux comptes de service.

### `config.json` est la source de vérité

À chaque lancement de `panel` (et à chaque `R`), la config est réconciliée avec systemd :

| Cas | Action |
|---|---|
| entrée `custom_services` sans unité | unité créée, activée au boot si `autostart`, puis démarrée — jamais de doublon |
| unité présente + entrée | unité réécrite si `command`, `folder` ou `user` changent |
| unité présente sans entrée | découverte : l'unité est enregistrée dans la config |
| entrée `pinned` | la liste `pinned` (favoris) est mise à jour |
| entrée `autostart` | `systemctl enable` / `disable` |

```json
{
  "pinned": ["apache2", "ssh"],
  "custom_services": {
    "cogniva360-frontend": {
      "command": "/usr/bin/npm run preview -- --host 127.0.0.1 --port 4173",
      "folder": "/var/www/cogniva360.com",
      "user": "www-data",
      "autostart": true,
      "pinned": true
    }
  }
}
```

Le fichier est créé automatiquement s'il est absent : au premier `install.sh`, au lancement de `panel`, ou par le paquet `.deb`. Les favoris par défaut ne contiennent que les services réellement installés sur la machine (`apache2`, `ssh`, `postgresql`, `mariadb`, `redis-server`, `fail2ban` s'ils sont présents — pas de `mariadb` s'il est absent, par exemple). Un fichier existant n'est jamais réécrit.

- `command` et `folder` sont obligatoires. `folder` doit exister ; il n'est créé automatiquement que sous `/srv/apps/`.
- `user` : compte d'exécution. Obligatoire si `folder` est hors `/srv/apps/`, sinon le panel crée et utilise `svc-<nom>` et s'approprie le dossier.
- `autostart` : `true` par défaut.
- `pinned` : alimente la liste `pinned` de premier niveau (nom complet `panel-<nom>`).

Le fichier est réservé à root : il vaut donc surface d'administration, avec les mêmes droits qu'une unité systemd. Un service arrêté n'est pas relancé par la synchro (seule une création démarre l'unité). Supprimer une entrée ne supprime pas l'unité — elle serait re-découverte : utiliser `SUPPRIMER` dans le panel.

## Sites

`N` dans SITES pour créer un VirtualHost Apache :
- **STATIQUE** : sert un dossier sous `/var/www/`
- **PROXY** : redirige vers un backend local (`127.0.0.1:PORT`)

Après création, le panel propose directement le HTTPS via Certbot.

## Cron

`N` dans CRON pour créer un job planifié. L'assistant demande la commande, un nom affiché, la fréquence, l'heure et l'utilisateur d'exécution.

Fréquences proposées : chaque jour, chaque semaine (sélection de plusieurs jours), chaque mois (un ou plusieurs jours du mois), toutes les N heures, toutes les N minutes, ou une expression cron personnalisée à 5 champs. L'heure est saisie au format `HH:MM`.

La liste est construite à partir de tous les crons de la machine : `/etc/crontab`, `/etc/cron.d/*` et `/var/spool/cron/crontabs/*`. Par défaut, la vue favoris (`[A] FAVORIS`) n'affiche que les crons gérés par le panel et les crons épinglés : les crons système non épinglés sont masqués. `F` épingle ou désépingle le cron sélectionné — l'étoile `★` et un séparateur distinguent les favoris, regroupés en tête de liste. `A` bascule la vue entre `[A] TOUS` (tout afficher) et `[A] FAVORIS` (revenir aux crons gérés et aux favoris). Les crons externes sont en lecture seule (`INFOS` + `JOURNAL`). Sur un cron géré : `MODIFIER` (commande / moment / utilisateur / nom), `EXÉCUTER maintenant` (sortie affichée), `ACTIVER` / `DÉSACTIVER`, `JOURNAL` (journalctl CRON) et `SUPPRIMER`.

Ces crons système sont inclus en lecture seule et nommés d'après ce qui les identifie vraiment : le fichier pour `/etc/cron.d/*` (`certbot`, `e2scrub_all`), la période pour les lignes `run-parts` de `/etc/crontab` (`cron.daily`, `cron.weekly`…) et le compte pour les crontabs utilisateur (`alice · job.sh`). Ils sont masqués par défaut : `A` les rend visibles, et `F` permet de les épingler pour les garder dans la vue favoris.

Les jobs sont stockés dans `/etc/serverpanel/config.json`, clé `crons`. L'épinglage est porté par l'entrée elle-même : un job géré est favori avec `"pinned": true`. Un cron externe épinglé est matérialisé dans `crons` par une entrée `ext:<hash>` marquée `{"ext": true, "pinned": true}` : elle ne sert qu'au favori, n'est jamais projetée dans `/etc/cron.d/serverpanel` et le cron externe reste en lecture seule. La clé `ext:<hash>` suffit à identifier une entrée externe, même si le drapeau `ext` est absent (config éditée à la main) ; le panel le pose systématiquement. L'ordre des clés de `crons` fait foi pour l'ordre d'affichage des favoris. L'ancienne clé de premier niveau `pinned_crons` est migrée automatiquement vers ce format à la première écriture. Le fichier `/etc/cron.d/serverpanel` est régénéré à chaque changement (`0644`, root) : toute édition manuelle est écrasée. Un job désactivé reste dans la config mais n'est pas planifié.

```json
{
  "pinned": ["ssh"],
  "crons": {
    "sauvegarde": {
      "label": "Sauvegarde nocturne",
      "command": "/usr/local/bin/backup.sh",
      "schedule": "30 2 * * *",
      "user": "root",
      "enabled": true,
      "pinned": true
    },
    "ext:0123456789abcdef": {
      "ext": true,
      "pinned": true
    }
  }
}
```

`install.sh` installe le paquet `cron` et active le service ; le `.deb` le déclare en dépendance et `postinst` l'active.

## Emplacements

| Chemin | Contenu |
|---|---|
| `/usr/bin/panel` | Commande |
| `/etc/serverpanel/config.json` | Config, favoris, services et crons gérés |
| `/srv/apps/` | Code des applications |
| `/etc/systemd/system/panel-*.service` | Unités créées |
| `/etc/apache2/sites-available/panel-*.conf` | Sites créés |
| `/etc/cron.d/serverpanel` | Crons gérés par le panel |
