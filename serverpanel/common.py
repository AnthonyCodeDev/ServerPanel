"""Shared operations. Privileged commands are argv-only, never shell commands."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

VERSION = '1.0.0~beta1'
CONFIG = Path('/etc/serverpanel/config.json')
# Favoris par défaut, filtrés sur les services réellement installés à la
# première création de config.json (voir ensure_config).
DEFAULT_CONFIG = {'pinned': ['apache2', 'ssh', 'postgresql', 'mariadb', 'redis-server', 'fail2ban']}
NAME = re.compile(r'^[a-z][a-z0-9-]{1,23}$')
UNIT = re.compile(r'^[A-Za-z0-9_.@:-]+\.service$')
DOMAIN = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$', re.I)
EMAIL = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


class PanelError(Exception):
    """Expected operational failure suitable for display to an administrator."""


def run(*argv: str, timeout: int = 25, check: bool = True) -> str:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding='utf-8', errors='replace',
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PanelError(f'{argv[0]} : {exc}') from exc
    if check and done.returncode:
        raise PanelError(f'{argv[0]} (code {done.returncode}) : {done.stdout.strip()[-700:]}')
    return done.stdout


def root_only() -> None:
    if os.geteuid():
        raise PanelError('Droits administrateur requis : sudo panel')


def domain_name(value: str) -> str:
    value = value.strip().lower().rstrip('.')
    if not DOMAIN.fullmatch(value):
        raise PanelError('Domaine invalide (ex. exemple.be)')
    return value


def app_name(value: str) -> str:
    value = value.strip().lower()
    if not NAME.fullmatch(value) or value.endswith('-'):
        raise PanelError('Identifiant : 2-24 caractères (a-z, 0-9, -)')
    return value


def unit_name(value: str) -> str:
    if not UNIT.fullmatch(value) or '\n' in value:
        raise PanelError('Nom de service invalide')
    return value


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.serverpanel-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def installed_units() -> set[str]:
    """Noms de fichiers d'unités .service présents sur l'hôte (best-effort)."""
    names = set()
    for base in ('/etc/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system'):
        try:
            names.update(path.name for path in Path(base).glob('*.service'))
        except OSError:
            pass
    try:
        output = run('systemctl', 'list-unit-files', '--type=service',
                     '--no-legend', '--no-pager', timeout=10, check=False)
    except PanelError:
        output = ''
    for line in output.splitlines():
        columns = line.split()
        if columns and columns[0].endswith('.service'):
            names.add(columns[0])
    return names


def ensure_config() -> bool:
    """Crée config.json une seule fois, avec les favoris des services installés.

    Returns True when the file was created, False when it already existed
    (or when the process is not root).
    """
    # Un fichier existant (même illisible) n'est jamais touché.
    if CONFIG.exists():
        return False
    if getattr(os, 'geteuid', lambda: 0)() != 0:
        return False
    units = installed_units()
    # L'ordre de DEFAULT_CONFIG est conservé : pas de doublon possible.
    pinned = [name for name in DEFAULT_CONFIG['pinned'] if f'{name}.service' in units]
    atomic_write(CONFIG, json.dumps({'pinned': pinned}, indent=2, ensure_ascii=False) + '\n')
    return True


def config() -> dict:
    try:
        value = json.loads(CONFIG.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def pins() -> list[str]:
    raw = config().get('pinned', [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and UNIT.fullmatch(item + '.service')][:30]


def toggle_pin(service: str) -> None:
    root_only()
    name = unit_name(service).removesuffix('.service')
    data = config()
    current = pins()
    if name in current:
        current.remove(name)
        pinned = False
    else:
        current.append(name)
        pinned = True
    data['pinned'] = current
    # Keep the per-service entry aligned (config.json stays the source of truth).
    # Only existing entries are updated: a missing entry is seeded by the next sync.
    customs = data.get('custom_services')
    if isinstance(customs, dict) and name.startswith('panel-'):
        entry = customs.get(name.removeprefix('panel-'))
        if isinstance(entry, dict):
            entry['pinned'] = pinned
    atomic_write(CONFIG, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def os_name() -> str:
    """PRETTY_NAME de /etc/os-release, repli 'Debian 13' (jamais d'exception)."""
    for path in (Path('/etc/os-release'), Path('/usr/lib/os-release')):
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            if not line.startswith('PRETTY_NAME='):
                continue
            value = line.split('=', 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1].strip()
            if value:
                return value
    return 'Debian 13'


def local_ip() -> str:
    """IP locale via socket UDP (aucun paquet envoyé), repli hostname ; '' si inconnue."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # connect() UDP ne transmet rien : il fixe seulement la route locale.
            sock.settimeout(0.2)
            sock.connect(('192.0.2.1', 9))
            address = sock.getsockname()[0]
        finally:
            sock.close()
        if address and not address.startswith('127.'):
            return address
    except OSError:
        pass
    try:
        address = socket.gethostbyname(socket.gethostname())
    except OSError:
        return ''
    return '' if address.startswith('127.') else address


def _gib(kib: int) -> float:
    return round(kib / 1048576, 1)


# Séquences ANSI (couleurs, styles) retirées de la sortie fastfetch.
_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
# Structures essayées dans l'ordre : la première sortie contenant « os » gagne.
FASTFETCH_STRUCTURES = ('os:host:kernel:uptime:localip',
                        'os:host:kernel:uptime',
                        '')


def fastfetch_info(timeout: int = 4) -> dict[str, str]:
    """Infos fastfetch (os, host, kernel, uptime, ip) ; {} si indisponible."""
    # Pas de binaire : aucun subprocess n'est lancé (repli intégré côté UI).
    if shutil.which('fastfetch') is None:
        return {}
    for structure in FASTFETCH_STRUCTURES:
        argv = ['fastfetch', '--config', 'none', '--pipe', '--logo', 'none']
        if structure:
            argv += ['--structure', structure]
        try:
            output = run(*argv, timeout=timeout, check=False)
        except (PanelError, OSError):
            continue
        info: dict[str, str] = {}
        for raw in output.splitlines():
            line = _ANSI.sub('', raw).strip()
            if ':' not in line:
                continue
            key, value = line.split(':', 1)
            key = key.strip().lower()
            value = value.strip()
            if not key or not value:
                continue
            # « Local IP (eth0): … » : la première ligne l'emporte.
            if key.startswith('local ip'):
                key = 'local ip'
                if key in info:
                    continue
            info.setdefault(key, value)
        if info.get('os'):
            return info
    return {}


def stats() -> dict:
    """Read-only host metrics with safe fallbacks for non-Linux test hosts."""
    total = free = None
    try:
        memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        total = int(memory['MemTotal'].split()[0])
        free = int(memory['MemAvailable'].split()[0])
        ram = round((total - free) / total * 100)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        ram = None
    try:
        disk = shutil.disk_usage('/')
        usage = round(disk.used / disk.total * 100)
        disk_used = round(disk.used / 1073741824, 1)
        disk_total = round(disk.total / 1073741824, 1)
    except (OSError, ZeroDivisionError):
        usage = disk_used = disk_total = None
    try:
        sec = int(float(Path('/proc/uptime').read_text().split()[0]))
    except (OSError, ValueError):
        sec = 0
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        load = None
    try:
        kernel = os.uname().release
    except (AttributeError, OSError):
        kernel = ''
    return {'host': socket.gethostname(), 'ram': ram, 'disk': usage,
            'ram_used': _gib(total - free) if total is not None and free is not None else None,
            'ram_total': _gib(total) if total is not None else None,
            'disk_used': disk_used, 'disk_total': disk_total,
            'uptime': f'{sec // 86400}j {(sec % 86400) // 3600}h',
            'uptime_seconds': sec, 'load': load, 'cores': os.cpu_count() or 1,
            'kernel': kernel}
