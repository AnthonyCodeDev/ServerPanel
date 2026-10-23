"""Shared operations. Privileged commands are argv-only, never shell commands."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path

VERSION = '1.0.0~beta1'
CONFIG = Path('/etc/serverpanel/config.json')
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
    else:
        current.append(name)
    data['pinned'] = current
    atomic_write(CONFIG, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def _gib(kib: int) -> float:
    return round(kib / 1048576, 1)


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
        import shutil
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
