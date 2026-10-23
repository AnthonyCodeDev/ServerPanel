"""Systemd inventory and isolated application units."""
from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from .common import PanelError, app_name, atomic_write, root_only, run, unit_name, config, CONFIG

UNITS = Path('/etc/systemd/system')
APPS = Path('/srv/apps')
# Stopping these units from an SSH TUI could disconnect or corrupt the host.
PROTECTED = {'ssh.service', 'sshd.service', 'systemd-logind.service', 'dbus.service',
             'networking.service', 'systemd-networkd.service', 'NetworkManager.service',
             'systemd-journald.service', 'systemd-udevd.service', 'systemd-resolved.service',
             'systemd.service'}
ALLOWED = {'start', 'stop', 'restart', 'enable', 'disable'}


@dataclass(frozen=True)
class Service:
    name: str
    active: str
    enabled: str
    description: str = ''
    fragment: str = ''
    memory: int = 0
    custom_command: str = ''
    custom_folder: str = ''

    @property
    def managed(self) -> bool:
        return self.name.startswith('panel-')

    @property
    def origin(self) -> str:
        if self.fragment.startswith(('/usr/lib/systemd/system/', '/lib/systemd/system/')):
            return 'system'
        if self.fragment.startswith('/etc/systemd/system/'):
            return 'manual'
        return 'unknown'


# ----------------------------------------------------------------- config helpers

def custom_services() -> dict:
    """Return the dict of custom service metadata stored in config.json."""
    data = config()
    raw = data.get('custom_services', {})
    return raw if isinstance(raw, dict) else {}


def save_custom_service(slug: str, command: str, folder: str) -> None:
    """Persist custom service info to config.json (does NOT touch the .service file)."""
    data = config()
    if 'custom_services' not in data or not isinstance(data['custom_services'], dict):
        data['custom_services'] = {}
    data['custom_services'][slug] = {'command': command, 'folder': folder}
    atomic_write(CONFIG, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def delete_custom_service(slug: str) -> None:
    """Remove custom service metadata from config.json."""
    data = config()
    customs = data.get('custom_services', {})
    if isinstance(customs, dict):
        customs.pop(slug, None)
        data['custom_services'] = customs
        atomic_write(CONFIG, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


# ----------------------------------------------------------------- .service file parsing

def _parse_service_file(unit_path: Path) -> dict:
    """Extract command, folder and user from a panel-* .service file."""
    info: dict = {'command': '', 'folder': '', 'user': ''}
    try:
        for line in unit_path.read_text(encoding='utf-8').splitlines():
            if line.startswith('WorkingDirectory='):
                # Undo the %% → % escape used when writing WorkingDirectory
                info['folder'] = line[len('WorkingDirectory='):].replace('%%', '%')
            elif line.startswith('ExecStart='):
                exec_start = line[len('ExecStart='):]
                try:
                    argv = shlex.split(exec_start)
                    # Undo our escaping: %% → %, $$ → $
                    argv = [a.replace('%%', '%').replace('$$', '$') for a in argv]
                    info['command'] = shlex.join(argv)
                except ValueError:
                    info['command'] = exec_start
            elif line.startswith('User='):
                info['user'] = line[len('User='):]
    except OSError:
        pass
    return info


def _sync_panel_to_config() -> None:
    """
    Config is the source of truth.

    - panel-* service file exists but slug is missing from config → read the .service
      file and register it in config (best-effort discovery).
    - slug is present in config → rebuild the .service file from config so any manual
      edit to config.json is applied to systemd automatically on next panel start.

    Only runs when the process is root (needs write access to /etc/systemd/system and
    /etc/serverpanel/config.json).  All errors are silenced so a stale config never
    prevents the inventory from loading.
    """
    if os.geteuid() != 0:
        return
    try:
        unit_files = [p for p in UNITS.glob('panel-*.service')
                      if p.is_file() and not p.is_symlink()]
    except OSError:
        return

    data = config()
    customs = data.get('custom_services', {})
    if not isinstance(customs, dict):
        customs = {}

    config_changed = False
    daemon_reload_needed = False

    for unit_path in unit_files:
        slug = unit_path.stem.removeprefix('panel-')
        if slug not in customs:
            # Service file exists but not tracked → read and register
            info = _parse_service_file(unit_path)
            if info.get('command') or info.get('folder'):
                customs[slug] = {'command': info['command'], 'folder': info['folder']}
                config_changed = True
        else:
            # Config is source of truth → rebuild .service file if content differs
            entry = customs[slug]
            stored_cmd = entry.get('command', '')
            stored_folder = entry.get('folder', '')
            if not stored_cmd or not stored_folder:
                continue
            # Re-read current user from existing file (we never change the user)
            info = _parse_service_file(unit_path)
            user = info.get('user') or f'svc-{slug}'
            try:
                argv = shlex.split(stored_cmd, posix=True)
                if not argv:
                    continue
                binary = shutil.which(argv[0])
                if binary:
                    argv[0] = str(Path(binary).resolve())
                directory_path = Path(stored_folder.strip())
                if not directory_path.is_absolute() or '..' in directory_path.parts:
                    continue
                expected = unit_content(slug, argv, directory_path, user)
                try:
                    actual = unit_path.read_text(encoding='utf-8')
                except OSError:
                    continue
                if actual != expected:
                    atomic_write(unit_path, expected)
                    daemon_reload_needed = True
            except Exception:
                continue  # Never crash inventory on sync errors

    if config_changed:
        data['custom_services'] = customs
        try:
            atomic_write(CONFIG, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
        except OSError:
            pass

    if daemon_reload_needed:
        try:
            run('systemctl', 'daemon-reload', check=False)
        except Exception:
            pass


def update_custom_service(slug: str, command: str, folder: str) -> None:
    """
    Update a panel service: rewrite config.json AND the .service file, then
    daemon-reload.  The caller is responsible for restarting the unit if needed.
    """
    root_only()
    slug = slug.removeprefix('panel-')
    name = f'panel-{slug}.service'
    unit = UNITS / name
    if not unit.is_file() or unit.is_symlink():
        raise PanelError('Fichier .service introuvable ou lien symbolique non autorisé')
    # Rebuild the .service file from the new values
    try:
        argv = shlex.split(command, posix=True)
        if not argv:
            raise PanelError('Commande vide')
        binary = shutil.which(argv[0])
        if binary:
            argv[0] = str(Path(binary).resolve())
        raw_folder = Path(folder.strip())
        if not raw_folder.is_absolute() or '..' in raw_folder.parts:
            raise PanelError('Chemin absolu requis')
        directory_path = raw_folder.resolve()
        # Keep existing user so we don't change ownership unexpectedly
        info = _parse_service_file(unit)
        user = info.get('user') or f'svc-{slug}'
        content = unit_content(slug, argv, directory_path, user)
        atomic_write(unit, content)
        run('systemctl', 'daemon-reload')
    except PanelError:
        raise
    except Exception as exc:
        raise PanelError(f'Impossible de mettre à jour le service : {exc}') from exc
    # Persist to config (source of truth)
    save_custom_service(slug, command, folder)


# ----------------------------------------------------------------- memory helper

def _memory(value: str) -> int:
    if not value.isdigit():
        return 0
    amount = int(value)
    return amount if amount < 1 << 60 else 0


# ----------------------------------------------------------------- inventory

def inventory(timeout: int = 12) -> list[Service]:
    """Throw rather than misreport every unit inactive when systemd is unreachable."""
    # Keep config in sync with .service files (root only, best-effort)
    _sync_panel_to_config()

    output = run('systemctl', 'list-unit-files', '--type=service', '--no-legend', '--no-pager', timeout=timeout)
    declared = {}
    aliases = set()
    for line in output.splitlines():
        cols = line.split()
        if len(cols) >= 2 and cols[0].endswith('.service'):
            if cols[1] == 'alias':
                aliases.add(cols[0])
            else:
                declared[cols[0]] = cols[1]
    output = run('systemctl', 'list-units', '--type=service', '--all', '--plain', '--no-legend', '--no-pager', timeout=timeout)
    active = {}
    for line in output.splitlines():
        fields = line.strip().lstrip('●-? ').split(maxsplit=4)
        if len(fields) >= 4 and fields[0].endswith('.service') and fields[1] != 'not-found':
            active[fields[0]] = (fields[2], fields[4] if len(fields) == 5 else '')
    names = sorted((set(declared) | set(active)) - aliases, key=lambda n: (not n.startswith('panel-'), n))
    meta = {}
    if names:
        try:
            output = run('systemctl', 'show', '--property=Id,FragmentPath,MemoryCurrent',
                         *names, timeout=timeout, check=False)
        except PanelError:
            output = ''
        current = {}
        for line in output.splitlines() + ['']:
            if not line.strip():
                if current.get('Id'):
                    meta[current['Id']] = current
                current = {}
                continue
            key, sep, value = line.partition('=')
            if sep and key in {'Id', 'FragmentPath', 'MemoryCurrent'}:
                current[key] = value
    customs = custom_services()
    result = []
    for n in names:
        slug = n.removeprefix('panel-').removesuffix('.service')
        info = customs.get(slug, {})
        svc = Service(
            n,
            active.get(n, ('inactive', ''))[0],
            declared.get(n, 'unknown'),
            active.get(n, ('', ''))[1],
            meta.get(n, {}).get('FragmentPath', ''),
            _memory(meta.get(n, {}).get('MemoryCurrent', '')),
            info.get('command', ''),
            info.get('folder', ''),
        )
        result.append(svc)
    return result


# ----------------------------------------------------------------- systemctl actions

def action(name: str, verb: str) -> None:
    root_only()
    unit_name(name)
    if verb not in ALLOWED:
        raise PanelError('Action non autorisée')
    if name in PROTECTED and verb in {'stop', 'disable', 'restart'}:
        raise PanelError('Service critique protégé. Administrer manuellement si nécessaire.')
    run('systemctl', verb, name, timeout=50)


# ----------------------------------------------------------------- service file generation

def _escape_systemd_arg(arg: str) -> str:
    """Escape a literal systemd ExecStart word (systemd.syntax / systemd.service)."""
    if not arg or any(c in arg for c in '\x00\r\n'):
        raise PanelError('Argument de commande invalide')
    arg = arg.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')
    return f'"{arg}"'


def parse_command(value: str) -> list[str]:
    if len(value) > 2048:
        raise PanelError('Commande trop longue')
    try:
        argv = shlex.split(value.strip(), posix=True)
    except ValueError as exc:
        raise PanelError('Guillemets non fermés dans la commande') from exc
    if not argv or len(argv) > 64:
        raise PanelError('Commande vide ou trop complexe')
    if any(x in {'&&', '||', '|', ';', '>', '>>', '<', '2>', '&'} for x in argv):
        raise PanelError('Commandes shell composées interdites : utiliser un script')
    if argv[0] in {'sudo', 'su', 'sh', 'bash', '/bin/sh', '/bin/bash', 'env'}:
        raise PanelError('Indiquer directement le programme (python3, node, npm...)')
    if any('\x00' in arg or '\n' in arg or '\r' in arg for arg in argv):
        raise PanelError('Caractères non autorisés dans la commande')
    binary = shutil.which(argv[0])
    if not binary or not Path(binary).is_file():
        raise PanelError(f'Programme introuvable : {argv[0]}')
    argv[0] = str(Path(binary).resolve())
    return argv


def unit_content(slug: str, argv: list[str], directory: Path, user: str) -> str:
    # ExecStart is never interpreted by a shell, even when arguments contain spaces.
    program = ' '.join(_escape_systemd_arg(word) for word in argv)
    working_dir = str(directory).replace('%', '%%')  # systemd WorkingDirectory uses a path, not ExecStart quoting
    return (f'[Unit]\nDescription=ServerPanel / {slug}\nAfter=network-online.target\nWants=network-online.target\n\n'
            f'[Service]\nType=exec\nUser={user}\nWorkingDirectory={working_dir}\n'
            f'ExecStart={program}\nRestart=on-failure\nRestartSec=5s\n'
            f'TimeoutStopSec=20s\nStandardOutput=journal\nStandardError=journal\n'
            f'SyslogIdentifier=panel-{slug}\nNoNewPrivileges=yes\nPrivateTmp=yes\n'
            f'ProtectSystem=full\nProtectHome=yes\nRestrictSUIDSGID=yes\nUMask=0027\n'
            f'StateDirectory=serverpanel/{slug}\n\n[Install]\nWantedBy=multi-user.target\n')


# ----------------------------------------------------------------- create / remove

def create(slug: str, command: str, directory: str, autostart: bool = True) -> str:
    root_only()
    slug = app_name(slug)
    name = f'panel-{slug}.service'
    unit = UNITS / name
    if unit.exists() or unit.is_symlink():
        raise PanelError('Service déjà existant')
    argv = parse_command(command)
    raw = Path(directory.strip())
    if not raw.is_absolute() or '..' in raw.parts or any(x in str(raw) for x in ('\n', '\r', '\0', '%')):
        raise PanelError('Chemin absolu et sûr requis')
    directory_path = raw.resolve()
    if directory_path == APPS or not directory_path.is_relative_to(APPS):
        raise PanelError('Applications limitées à /srv/apps/')
    if not directory_path.exists():
        try:
            directory_path.mkdir(parents=True, mode=0o755)
        except OSError as exc:
            raise PanelError(f'Impossible de créer le dossier : {exc}') from exc
    elif not directory_path.is_dir():
        raise PanelError('Ce chemin existe déjà mais n\'est pas un dossier')
    user = f'svc-{slug}'
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        # User is deliberately not deleted on rollback: code may already use it.
        run('useradd', '--system', '--user-group', '--home-dir', str(directory_path),
            '--shell', '/usr/sbin/nologin', user)
        account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise PanelError('Lancement sous root refusé')
    # Give the service account full ownership of its app directory
    try:
        run('chown', '-R', f'{user}:{user}', str(directory_path))
        run('chmod', '750', str(directory_path))
    except PanelError as exc:
        raise PanelError(f'Impossible de configurer les droits sur {directory_path} : {exc}') from exc
    # Verify that the dedicated account can read and write in its directory
    try:
        run('runuser', '-u', user, '--', 'test', '-r', str(directory_path))
        run('runuser', '-u', user, '--', 'test', '-w', str(directory_path))
        run('runuser', '-u', user, '--', 'test', '-x', str(directory_path))
    except PanelError as exc:
        raise PanelError(f'Accès au dossier refusé pour {user}. Vérifier les droits sur {directory_path}') from exc
    content = unit_content(slug, argv, directory_path, user)
    atomic_write(unit, content)
    # Phase 1: structural validation + registration — rollback on any failure
    try:
        run('systemd-analyze', 'verify', str(unit), timeout=30)
        run('systemctl', 'daemon-reload')
        if autostart:
            run('systemctl', 'enable', name, timeout=30)
    except PanelError:
        run('systemctl', 'disable', '--now', name, check=False)
        unit.unlink(missing_ok=True)
        run('systemctl', 'daemon-reload', check=False)
        raise
    # Persist custom service metadata (service is registered, keep even if start fails)
    save_custom_service(slug, command, str(directory_path))
    # Phase 2: first start — failure is a runtime issue, not a creation error
    try:
        run('systemctl', 'start', name, timeout=50)
    except PanelError as exc:
        raise PanelError(
            f'Service créé et {"activé au boot" if autostart else "enregistré"} · '
            f'démarrage échoué : {exc}'
        ) from exc
    return name


def logs(name: str, limit: int = 250) -> list[str]:
    unit_name(name)
    return run('journalctl', '--no-pager', '--output=short-iso', '-u', name,
               '-n', str(min(limit, 2000)), timeout=15).splitlines()


def remove(name: str) -> None:
    """Unregister a panel-owned unit; preserve code, data and service account."""
    root_only()
    unit_name(name)
    if not name.startswith('panel-'):
        raise PanelError('Seuls les services ServerPanel peuvent être supprimés')
    unit = UNITS / name
    if unit.is_symlink() or not unit.is_file():
        raise PanelError('Unité gérée introuvable ou lien symbolique non autorisé')
    original = unit.read_text(encoding='utf-8')
    run('systemctl', 'disable', '--now', name, timeout=50)
    unit.unlink()
    try:
        run('systemctl', 'daemon-reload')
    except PanelError:
        atomic_write(unit, original)
        run('systemctl', 'daemon-reload', check=False)
        raise
    # Clean up custom service metadata
    slug = name.removeprefix('panel-').removesuffix('.service')
    delete_custom_service(slug)
