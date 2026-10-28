"""CLI entrypoint: panel, panel motd, panel --version."""
from __future__ import annotations

import argparse
import os
import sys

from .common import VERSION, PanelError, ensure_config, pins, stats
from . import services


def motd() -> None:
    """Fast, read-only summary printed by pam_motd at SSH login."""
    color = sys.stdout.isatty()
    blue = '\033[34m' if color else ''
    green = '\033[32m' if color else ''
    red = '\033[31m' if color else ''
    yellow = '\033[33m' if color else ''
    dim = '\033[2m' if color else ''
    reset = '\033[0m' if color else ''
    m = stats()
    ram = f'{m["ram"]}%' if m['ram'] is not None else '?'
    disk = f'{m["disk"]}%' if m['disk'] is not None else '?'
    print()
    print(f'  {blue}PANEL{reset}  {m["host"][:32]}  {dim}Debian 13{reset}')
    print(f'  {dim}RAM {ram} · SSD {disk} · UP {m["uptime"]}{reset}')
    print()
    try:
        inventory = services.inventory(timeout=4)
    except PanelError:
        print(f'  {red}● systemd indisponible{reset}')
        print()
        print(f'  {blue}[ SUDO PANEL ]{reset}')
        print()
        return
    pinned = set(pins())
    selected = [s for s in inventory if s.managed or s.name.removesuffix('.service') in pinned]
    failed = [s for s in inventory if s.active == 'failed' and s not in selected]
    ordered = (selected + failed)[:10]
    for item in ordered:
        name = item.name.removesuffix('.service')
        if item.active == 'active':
            dot, status = green, 'ACTIF'
        elif item.active == 'failed':
            dot, status = red, 'ÉCHEC'
        else:
            dot, status = yellow, 'ARRÊT'
        startup = 'BOOT ON' if item.enabled == 'enabled' else 'BOOT OFF'
        startup_color = green if item.enabled == 'enabled' else dim
        print(f'  {dot}●{reset} {name[:30]:<30} {dot}{status:<5}{reset}  '
              f'{startup_color}{startup}{reset}')
    if not ordered:
        print(f'  {dim}aucun service suivi{reset}')
    failures = sum(s.active == 'failed' for s in inventory)
    if failures:
        print(f'\n  {red}{failures} service(s) en échec{reset}')
    print()
    print(f'  {blue}[ SUDO PANEL ]{reset}')
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='panel', description='Administration Debian 13 en terminal')
    parser.add_argument('command', choices=['motd'], nargs='?')
    parser.add_argument('--version', action='version', version=f'ServerPanel {VERSION}')
    args = parser.parse_args(argv)
    if args.command == 'motd':
        motd()
        return 0
    if os.geteuid() != 0:
        print('Droits root requis.\nRelance avec : sudo panel', file=sys.stderr)
        return 1
    # Premier lancement : crée config.json avec les favoris des services installés.
    ensure_config()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Terminal interactif requis. Résumé : panel motd', file=sys.stderr)
        return 2
    try:
        from .ui import launch
        launch()
    except (PanelError, KeyboardInterrupt) as exc:
        if isinstance(exc, PanelError):
            print(exc, file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
