# Installation depuis les sources

## Sur un VPS Debian 13

Transfère le ZIP dans `/root` via SFTP, puis :

```bash
cd /root
apt-get install -y unzip
unzip ServerPanel_sources_debian13.zip
cd ServerPanel_sources_debian13
bash install.sh
panel
```

Les modules sont copiés dans `/opt/serverpanel/`, la commande `panel` dans `/usr/local/bin/`.

## Modifier le code

Édite directement dans le dossier source, puis relance `install.sh` pour appliquer :

```bash
nano /root/ServerPanel_sources_debian13/serverpanel/ui.py
bash /root/ServerPanel_sources_debian13/install.sh
panel
```

`install.sh` ne touche pas à `/etc/serverpanel/config.json`.

## Lancer les tests

```bash
cd /root/ServerPanel_sources_debian13
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

## À savoir

- Ne pas installer le `.deb` et les sources en même temps.
- Les fins de ligne doivent être LF. Après édition Windows : `sed -i 's/\r$//' install.sh`
