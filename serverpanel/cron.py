"""Tâches cron : inventaire, validation française et projection depuis la config.

config.json reste la source de vérité (clé "crons") ; /etc/cron.d/serverpanel
n'est que la projection lisible par cron. Les crontabs externes sont en lecture
seule. L'épinglage est porté par l'entrée cron elle-même (drapeau "pinned") ;
un cron externe épinglé est matérialisé dans "crons" sous une clé "ext:<hash>".
Aucune commande privilégiée n'est composée en shell ici (argv uniquement).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .common import CONFIG, PanelError, atomic_write, root_only, run

CRON_D = Path('/etc/cron.d')
MANAGED = CRON_D / 'serverpanel'
CRONTAB = Path('/etc/crontab')
SPOOL = Path('/var/spool/cron/crontabs')
_SYSLOG = Path('/var/log/syslog')

MACROS = ('@reboot', '@hourly', '@daily', '@midnight', '@weekly', '@monthly', '@yearly', '@annually')
# Projection 5 champs utilisée pour décrire, préremplir et calculer les macros.
MACRO_SCHEDULE = {
    '@reboot': '',
    '@hourly': '0 * * * *',
    '@daily': '0 0 * * *',
    '@midnight': '0 0 * * *',
    '@weekly': '0 0 * * 0',
    '@monthly': '0 0 1 * *',
    '@yearly': '0 0 1 1 *',
    '@annually': '0 0 1 1 *',
}
ALLOWED_FIELDS = frozenset({'label', 'command', 'schedule', 'user', 'enabled'})
FIELD_NAMES = ('minute', 'heure', 'jour', 'mois', 'jour de la semaine')
FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
ACCOUNT = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')
# Clé de favori : identifiant géré (panel:slug) ou empreinte de ligne externe.
PIN_RX = re.compile(r'^(?:panel|ext):[A-Za-z0-9_.-]{1,60}$')
PINNED_MAX = 50
_ITEM = re.compile(r'^(?:\*|\*/\d+|\d+|\d+-\d+)$')
_ENV_LINE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\s*=')
DAYS_FR = ('dimanche', 'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi')
MONTHS_FR = ('janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
             'septembre', 'octobre', 'novembre', 'décembre')
INTERPRETERS = {'sh', 'bash', 'dash', 'zsh', 'ksh', 'python', 'python2', 'python3', 'perl',
                'ruby', 'node', 'nodejs', 'php', 'env', 'pwsh'}
# Préfixes shell sans intérêt pour nommer un cron : ils cachent le vrai programme
# (`test -x /usr/bin/certbot …` ne doit pas s'appeler « test »).
_SHELL_NOISE = frozenset({'test', '[', ']', 'cd', 'true', 'false', 'echo', 'exec', 'if',
                          'then', 'else', 'fi', 'for', 'do', 'done', 'run-parts', 'env',
                          'nice', 'ionice', 'flock', 'timeout', 'sudo', 'su', 'sh',
                          'bash', 'dash', 'zsh'})
# Opérateurs de composition : séparateurs, jamais des programmes.
_SHELL_OPERATORS = frozenset({'&&', '||', '|', ';', '&', '(', ')', '{', '}', '!', '\\!'})
# Commandes purement décoratives : leurs arguments sont du texte, pas un programme.
_OUTPUT_ONLY = frozenset({'echo'})
# Répertoires run-parts standard : plus parlants que le nom du script interne.
_RUN_PARTS = frozenset({'cron.hourly', 'cron.daily', 'cron.weekly', 'cron.monthly'})

HEADER = ('# ServerPanel — fichier géré automatiquement.\n'
          '# Source de vérité : /etc/serverpanel/config.json (clé "crons").\n'
          '# Modifier via : sudo panel → CRON. Les éditions manuelles sont écrasées.\n'
          'SHELL=/bin/sh\n'
          'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
          'MAILTO=""\n')


@dataclass(frozen=True)
class CronJob:
    """Un cron affichable : géré par ServerPanel ou découvert sur l'hôte."""

    label: str
    command: str
    user: str
    schedule: str
    source: str
    managed: bool = False
    enabled: bool = True
    slug: str = ''
    line: int = 0

    @property
    def when(self) -> str:
        return describe(self.schedule)

    @property
    def key(self) -> str:
        """Identifiant stable de favori : slug géré, sinon empreinte de la ligne."""
        if self.managed and self.slug:
            return f'panel:{self.slug}'
        return _external_key(self.source, self.schedule, self.user, self.command)


def _external_key(source: str, schedule: str, user: str, command: str) -> str:
    """Empreinte stable d'un cron externe (même ligne = même favori)."""
    raw = '\n'.join((str(source or ''), str(schedule or ''), str(user or ''),
                     str(command or '')))
    return 'ext:' + hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:16]


# ----------------------------------------------------------------- config helpers

def _load() -> dict:
    """Lecture stricte : un JSON cassé n'est jamais écrasé par une mutation."""
    try:
        raw = CONFIG.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise PanelError(f'config.json illisible : {exc}') from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PanelError('config.json invalide (JSON)') from exc
    if not isinstance(data, dict):
        raise PanelError('config.json invalide (JSON)')
    return data


def _crons(data: dict) -> dict:
    raw = data.get('crons')
    return raw if isinstance(raw, dict) else {}


def _flag(value, default: bool = True) -> bool:
    """Booléen tolérant pour une config éditée à la main (true, "oui", 0...)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {'', '0', 'false', 'non', 'no', 'off', 'disabled'}


def _single(value) -> str:
    """Valeur forcée sur une seule ligne (config éditée à la main)."""
    return ' '.join(str(value or '').split())


def _line_value(value) -> str:
    """Valeur sûre sur une seule ligne, sans toucher aux espaces internes."""
    return str(value or '').replace('\n', ' ').replace('\r', ' ').strip()


def _save(data: dict) -> None:
    _migrate(data)
    atomic_write(CONFIG, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    try:
        _write_managed(data)
    except OSError as exc:
        raise PanelError(f'Fichier cron non réécrit : {exc}') from exc


def _pinned_allowed(values) -> list[str]:
    """Favoris valides, dédoublonnés et bornés (lecture tolérante)."""
    result: list[str] = []
    for item in values if isinstance(values, list) else []:
        if isinstance(item, str) and PIN_RX.fullmatch(item) and item not in result:
            result.append(item)
    return result[:PINNED_MAX]


def _is_ext_entry(entry) -> bool:
    """Entrée matérialisant un cron externe épinglé (drapeau "ext")."""
    return isinstance(entry, dict) and _flag(entry.get('ext'), False)


def _is_ext_key(key) -> bool:
    """Clé de favori externe ("ext:…") ou entrée dict portant le drapeau ext."""
    if isinstance(key, dict):
        return _is_ext_entry(key)
    return isinstance(key, str) and key.startswith('ext:')


def _ext(key, entry=None) -> bool:
    """Entrée externe : clé "ext:" ou drapeau "ext" (config éditée à la main).

    La clé suffit à identifier une entrée externe ; le panel pose en plus le
    drapeau "ext", mais son absence ne doit jamais transformer l'entrée en cron
    géré ni la projeter dans /etc/cron.d/serverpanel.
    """
    return _is_ext_key(key) or _is_ext_entry(entry)


def _pinned_keys(data: dict) -> list[str]:
    """Clés de favoris dans l'ordre des entrées de "crons" (jamais d'exception)."""
    result: list[str] = []
    for key, entry in _crons(data).items():
        if not isinstance(entry, dict):
            continue
        if _ext(key, entry):
            candidate = str(key)  # Une entrée ext reste épinglée même sans "pinned"
        elif _flag(entry.get('pinned'), False):
            candidate = f'panel:{key}'
        else:
            continue
        if PIN_RX.fullmatch(candidate) and candidate not in result:
            result.append(candidate)
            if len(result) >= PINNED_MAX:
                break
    return result


def _migrate(data: dict) -> bool:
    """Convertit l'ancienne clé "pinned_crons" en drapeaux dans "crons".

    Idempotent : la clé de premier niveau disparaît définitivement. Les clés
    invalides et les doublons sont ignorés. Renvoie True si la config a changé.
    """
    if not isinstance(data, dict):
        return False
    changed = 'pinned_crons' in data
    legacy = data.pop('pinned_crons', None)
    if not isinstance(legacy, list):
        return changed
    crons = _crons(data)
    created = False
    for key in _pinned_allowed(legacy):
        if _is_ext_key(key):
            if key in crons:
                continue  # Déjà matérialisé : l'entrée ext fait foi
            crons[key] = {'ext': True, 'pinned': True}
            created = True
            changed = True
            continue
        slug = key[len('panel:'):]
        entry = crons.get(slug)
        if isinstance(entry, dict) and not _ext(slug, entry):
            if not _flag(entry.get('pinned'), False):
                entry['pinned'] = True
                changed = True
    if created:
        data['crons'] = crons
    return changed


def pinned_crons() -> list[str]:
    """Favoris cron depuis les entrées de config.json (jamais d'exception remontée).

    La migration de l'ancienne clé "pinned_crons" est appliquée en mémoire
    uniquement : la lecture ne doit jamais réécrire la config (pas de root requis).
    """
    try:
        data = _load()
    except PanelError:
        return []
    _migrate(data)
    return _pinned_keys(data)


def toggle_pin(job: CronJob) -> bool:
    """Épingle ou dépingle un cron ; True s'il est désormais favori."""
    root_only()
    key = job.key
    if not PIN_RX.fullmatch(key):
        raise PanelError('Favori cron invalide')
    data = _load()
    _migrate(data)
    crons = _crons(data)
    if job.managed and job.slug:
        entry = crons.get(job.slug)
        if not isinstance(entry, dict) or _ext(job.slug, entry):
            raise PanelError(f'Cron introuvable : {job.slug}')
        if _flag(entry.get('pinned'), False):
            entry.pop('pinned', None)  # Config propre : pas de "pinned": false
            pinned = False
        else:
            entry['pinned'] = True
            pinned = True
    elif key in crons:
        # Une entrée ext ne sert qu'au favori : la désépingler la supprime.
        crons.pop(key)
        pinned = False
    else:
        if len(_pinned_keys(data)) >= PINNED_MAX:
            raise PanelError(f'Favoris cron : {PINNED_MAX} maximum')
        crons[key] = {'ext': True, 'pinned': True}
        data['crons'] = crons
        pinned = True
    _save(data)
    return pinned



# ----------------------------------------------------------------- fichier géré

def _escape_percent(command: str) -> str:
    # cron interprète % comme un saut de ligne : seul échappement nécessaire.
    return _line_value(command).replace('%', '\\%')


def _managed_content(crons: dict) -> str:
    entries = [entry for key, entry in crons.items()
               if isinstance(entry, dict) and not _ext(key, entry)]
    entries.sort(key=lambda entry: _single(entry.get('label')).casefold())
    if not entries:
        return HEADER + '\n# Aucun cron géré.\n'
    blocks = []
    for entry in entries:
        command = _line_value(entry.get('command'))
        label = _single(entry.get('label')) or default_label(command)
        schedule = _single(entry.get('schedule'))
        if _flag(entry.get('enabled', True)):
            user = _single(entry.get('user')) or 'root'
            blocks.append(f'\n# {label} — {describe(schedule)}\n'
                          f'{schedule} {user} {_escape_percent(command)}')
        else:
            # Un job désactivé ne produit aucune ligne active dans cron.
            blocks.append(f'\n# DÉSACTIVÉ — {label} · {schedule}')
    return HEADER + ''.join(f'{block}\n' for block in blocks)


def _write_managed(data: dict) -> None:
    content = _managed_content(_crons(data))
    MANAGED.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(MANAGED, content, 0o644)


def _sync_managed() -> None:
    """Best-effort : aligne /etc/cron.d/serverpanel sur config.json (root only)."""
    try:
        if getattr(os, 'geteuid', lambda: 1)() != 0:
            return
        try:
            data = _load()
        except PanelError:
            return  # Config corrompue : ne rien écrire
        content = _managed_content(_crons(data))
        try:
            actual = MANAGED.read_text(encoding='utf-8')
        except OSError:
            actual = None
        if actual == content:
            return
        MANAGED.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(MANAGED, content, 0o644)
    except Exception:
        return  # L'inventaire ne doit jamais échouer à cause de la synchro


# ----------------------------------------------------------------- validation

def _check_field(value: str, low: int, high: int, name: str) -> str:
    if not value:
        raise PanelError(f'Champ {name} vide')
    for item in value.split(','):
        if not _ITEM.fullmatch(item):
            raise PanelError(f'Champ {name} invalide : « {item} »')
        if item == '*':
            continue
        if item.startswith('*/'):
            if int(item[2:]) < 1:
                raise PanelError(f'Champ {name} : pas de 0')
            continue
        if '-' in item:
            start, end = (int(part) for part in item.split('-'))
            for number in (start, end):
                if not low <= number <= high:
                    raise PanelError(f'Champ {name} hors bornes ({low}-{high})')
            if start > end:
                raise PanelError(f'Champ {name} : intervalle inversé')
            continue
        number = int(item)
        if not low <= number <= high:
            raise PanelError(f'Champ {name} hors bornes ({low}-{high})')
    return value


def validate_schedule(text: str) -> str:
    """Macro connue ou exactement 5 champs ; renvoie la forme normalisée."""
    if not isinstance(text, str):
        raise PanelError('Planification invalide')
    if any(char in text for char in '\n\r%'):
        raise PanelError('Planification invalide (retour à la ligne ou % interdit)')
    value = ' '.join(text.split())
    if not value:
        raise PanelError('Planification vide')
    if value.startswith('@'):
        macro = value.lower()
        if macro not in MACROS:
            raise PanelError(f'Macro inconnue : {value}')
        return macro
    parts = value.split(' ')
    if len(parts) != 5:
        raise PanelError('Planification : 5 champs attendus (min heure jour mois jour-semaine)')
    for part, (low, high), name in zip(parts, FIELD_BOUNDS, FIELD_NAMES):
        _check_field(part, low, high, name)
    return value


def _int(value, low: int, high: int, name: str) -> int:
    if isinstance(value, bool):
        raise PanelError(f'{name} invalide')
    try:
        number = int(str(value).strip()) if not isinstance(value, int) else value
    except (TypeError, ValueError) as exc:
        raise PanelError(f'{name} invalide') from exc
    if not low <= number <= high:
        raise PanelError(f'{name} hors bornes ({low}-{high})')
    return number


def parse_hhmm(text: str) -> tuple[int, int]:
    """« 3 », « 3:30 », « 03:30 », « 3h30 » → (heure, minute)."""
    value = str(text or '').strip().lower().replace('h', ':')
    if value.endswith(':'):
        value += '0'
    if not re.fullmatch(r'\d{1,2}(?::\d{1,2})?', value):
        raise PanelError('Heure invalide (ex. 03:30)')
    hour, _, minute = value.partition(':')
    hour, minute = int(hour), int(minute or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise PanelError('Heure hors bornes (00:00 - 23:59)')
    return hour, minute


def schedule_daily(hour: int, minute: int = 0) -> str:
    hour = _int(hour, 0, 23, 'heure')
    minute = _int(minute, 0, 59, 'minute')
    return f'{minute} {hour} * * *'


def schedule_weekly(days: list[int], hour: int, minute: int = 0) -> str:
    hour = _int(hour, 0, 23, 'heure')
    minute = _int(minute, 0, 59, 'minute')
    values = sorted({_int(day, 0, 7, 'jour') % 7 for day in list(days or [])})
    if not values:
        raise PanelError('Au moins un jour de la semaine requis')
    return f'{minute} {hour} * * {",".join(str(day) for day in values)}'


def schedule_monthly(days: list[int], hour: int, minute: int = 0) -> str:
    hour = _int(hour, 0, 23, 'heure')
    minute = _int(minute, 0, 59, 'minute')
    values = sorted({_int(day, 1, 31, 'jour') for day in list(days or [])})
    if not values:
        raise PanelError('Au moins un jour du mois requis')
    return f'{minute} {hour} {",".join(str(day) for day in values)} * *'


def schedule_every_minutes(n: int) -> str:
    return f'*/{_int(n, 1, 59, "intervalle")} * * * *'


def schedule_every_hours(n: int, minute: int = 0) -> str:
    minute = _int(minute, 0, 59, 'minute')
    return f'{minute} */{_int(n, 1, 23, "intervalle")} * * *'


def schedule_custom(minute: str, hour: str, day: str, month: str, weekday: str) -> str:
    values = [_single(value) for value in (minute, hour, day, month, weekday)]
    for value, (low, high), name in zip(values, FIELD_BOUNDS, FIELD_NAMES):
        _check_field(value, low, high, name)
    return validate_schedule(' '.join(values))


# ----------------------------------------------------------------- description

def _fr_list(items) -> str:
    items = list(items)
    if not items:
        return ''
    if len(items) == 1:
        return items[0]
    return ', '.join(items[:-1]) + ' et ' + items[-1]


def _upper_first(value: str) -> str:
    return value[:1].upper() + value[1:]


def _ordinal(day: int) -> str:
    return '1er' if day == 1 else str(day)


def _numbers(field: str) -> list[int] | None:
    values = []
    for item in field.split(','):
        if not item.isdigit():
            return None
        values.append(int(item))
    return values


def _expand(field: str, low: int, high: int) -> set[int]:
    """Valeurs concrètes d'un champ déjà validé (jamais utilisé sur une entrée libre)."""
    result: set[int] = set()
    for item in field.split(','):
        if item == '*':
            result.update(range(low, high + 1))
        elif item.startswith('*/'):
            step = int(item[2:])
            if step < 1:
                raise ValueError(item)
            result.update(range(low, high + 1, step))
        elif '-' in item:
            start, end = (int(part) for part in item.split('-'))
            result.update(range(start, end + 1))
        else:
            result.add(int(item))
    return result


def _weekday_field(field: str) -> tuple[str, list[int]] | None:
    """('range', [1, 5]) ou ('list', [1, 3, 5]) ; None si non descriptible."""
    if field == '*':
        return None
    items = field.split(',')
    if len(items) == 1 and '-' in items[0]:
        start, end = (int(part) for part in items[0].split('-'))
        return ('range', [start % 7, end % 7])
    values = _numbers(field)
    if values is None:
        return None
    return ('list', sorted({value % 7 for value in values}))


def _day_field(field: str) -> tuple[str, list[int]] | None:
    if field == '*':
        return None
    items = field.split(',')
    if len(items) == 1 and '-' in items[0]:
        start, end = (int(part) for part in items[0].split('-'))
        return ('range', [start, end])
    values = _numbers(field)
    if values is None:
        return None
    return ('list', sorted(set(values)))


def _describe_fields(minute: str, hour: str, day: str, month: str, weekday: str,
                     short: bool) -> str | None:
    clock = f'{int(hour):02d}:{int(minute):02d}' if minute.isdigit() and hour.isdigit() else ''

    if day == '*' and month == '*' and weekday == '*':
        if minute == '*' and hour == '*':
            return 'Chaque minute'
        if minute.startswith('*/') and hour == '*':
            step = int(minute[2:])
            return f'Toutes les {step} min' if short else f'Toutes les {step} minutes'
        if minute.isdigit() and hour == '*':
            return 'Chaque heure' if short else f'Chaque heure à la minute {int(minute)}'
        if minute.isdigit() and hour.startswith('*/'):
            step = int(hour[2:])
            if short:
                return f'Toutes les {step} h'
            return f'Toutes les {step} heures, à la minute {int(minute)}'
        if minute.isdigit() and '-' in hour and len(hour.split('-')) == 2:
            start, end = (int(part) for part in hour.split('-'))
            return (f'Chaque jour de {start:02d}:{int(minute):02d} '
                    f'à {end:02d}:{int(minute):02d}')
        if clock:
            return f'Chaque jour {clock}' if short else f'Chaque jour à {clock}'
        return None

    if not clock:
        return None

    if day == '*' and month == '*' and weekday != '*':
        info = _weekday_field(weekday)
        if not info:
            return None
        kind, values = info
        if kind == 'range' and len(values) == 2 and values[0] != values[1]:
            tails = f'{DAYS_FR[values[0]]} au {DAYS_FR[values[1]]}'
            return f'Du {tails} {clock}' if short else f'Du {tails} à {clock}'
        names = [DAYS_FR[value] for value in values]
        if not names:
            return None
        if len(names) == 1:
            return f'{_upper_first(names[0])} {clock}' if short else f'Chaque {names[0]} à {clock}'
        joined = _upper_first(_fr_list(names))
        return f'{joined} {clock}' if short else f'{joined} à {clock}'

    if day != '*' and weekday == '*' and month == '*':
        info = _day_field(day)
        if not info or info[0] != 'list' or not info[1]:
            return None
        values = info[1]
        if len(values) == 1:
            if short:
                return f'Le {_ordinal(values[0])} du mois {clock}'
            return f'Le {_ordinal(values[0])} de chaque mois à {clock}'
        joined = _fr_list(str(value) for value in values)
        if short:
            return f'Les {joined} du mois {clock}'
        return f'Les {joined} de chaque mois à {clock}'

    if day != '*' and weekday == '*' and month != '*':
        days = _day_field(day)
        months = _numbers(month)
        if not days or days[0] != 'list' or len(days[1]) != 1 or not months or len(months) != 1:
            return None
        return f'Le {_ordinal(days[1][0])} {MONTHS_FR[months[0] - 1]} à {clock}'

    return None


def _describe_macro(macro: str, short: bool) -> str:
    if macro == '@reboot':
        return 'Au démarrage'
    if macro == '@hourly':
        return 'Chaque heure'
    if macro in ('@daily', '@midnight'):
        return 'Chaque jour 00:00' if short else 'Chaque jour à 00:00'
    if macro == '@weekly':
        return 'Chaque semaine (dimanche à 00:00)'
    if macro == '@monthly':
        return 'Le 1er du mois 00:00' if short else 'Le 1er de chaque mois à 00:00'
    return 'Le 1er janvier à 00:00'


def describe(schedule: str, short: bool = False) -> str:
    """Description française d'une planification (repli « Personnalisé »)."""
    value = ' '.join(str(schedule or '').split())
    macro = value.lower()
    if macro in MACROS:
        return _describe_macro(macro, short)
    parts = value.split(' ')
    if len(parts) != 5:
        return f'Personnalisé · {value}'
    try:
        text = _describe_fields(*parts, short)
    except (PanelError, ValueError):
        text = None
    return text or f'Personnalisé · {value}'


# ----------------------------------------------------------------- parse / préremplissage

def _schedule_dict(**overrides) -> dict:
    data = {'kind': 'custom', 'minute': 0, 'hour': 0, 'days': [], 'interval': 1,
            'fields': {'minute': '*', 'hour': '*', 'day': '*', 'month': '*', 'weekday': '*'}}
    data.update(overrides)
    return data


def parse_schedule(schedule: str) -> dict:
    """Décompose une planification pour préremplir l'éditeur (jamais d'exception)."""
    value = ' '.join(str(schedule or '').split())
    if not value:
        return _schedule_dict()
    text = MACRO_SCHEDULE.get(value.lower(), value)
    try:
        text = validate_schedule(text)
    except PanelError:
        return _schedule_dict()
    if text.startswith('@'):
        return _schedule_dict()
    parts = text.split(' ')
    fields = dict(zip(('minute', 'hour', 'day', 'month', 'weekday'), parts))
    result = _schedule_dict(fields=fields)
    minute, hour, day, month, weekday = parts
    simple = day == '*' and month == '*' and weekday == '*'
    if simple and minute.startswith('*/') and hour == '*':
        result.update(kind='minutes', interval=int(minute[2:]))
        return result
    if simple and minute.isdigit() and hour.startswith('*/'):
        result.update(kind='hours', minute=int(minute), interval=int(hour[2:]))
        return result
    if simple and minute.isdigit() and hour.isdigit():
        result.update(kind='daily', minute=int(minute), hour=int(hour))
        return result
    if minute.isdigit() and hour.isdigit() and day == '*' and month == '*':
        values = _numbers(weekday)
        if values:
            result.update(kind='weekly', minute=int(minute), hour=int(hour),
                          days=sorted({value % 7 for value in values}))
        return result
    if minute.isdigit() and hour.isdigit() and weekday == '*' and month == '*':
        values = _numbers(day)
        if values:
            result.update(kind='monthly', minute=int(minute), hour=int(hour),
                          days=sorted(set(values)))
    return result


# ----------------------------------------------------------------- libellés

def _shell_tokens(text: str) -> list[str]:
    """Découpe une ligne en jetons shell (shlex, repli split si guillemets cassés)."""
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def _run_parts_target(command: str) -> str:
    """« cron.daily » si la commande appelle un répertoire run-parts, sinon ''."""
    text = _single(command)
    if not text:
        return ''
    for token in _shell_tokens(text):
        word = token.strip('\'"')
        name = Path(word).name
        if name in _RUN_PARTS and word.startswith('/etc/'):
            return name
    return ''


def default_label(command: str) -> str:
    """Libellé lisible : premier basename utile (bruit shell et options ignorés)."""
    text = _single(command)
    if not text:
        return 'Cron'
    skipping = False  # arguments d'une commande purement décorative (echo)
    for token in _shell_tokens(text):
        word = token.strip('\'"')
        if not word:
            continue
        if word in _SHELL_OPERATORS:
            skipping = False
            continue
        if skipping:
            continue
        if word in _SHELL_NOISE:
            skipping = word in _OUTPUT_ONLY
            continue
        if word.startswith('-'):
            continue
        name = Path(word).name
        if not name:
            continue  # « / » ou « /etc/ » : pas de basename exploitable
        stem = name.lower().split('.')[0].rstrip('0123456789')
        if stem in INTERPRETERS:
            continue
        return name[:60]
    return 'Cron'


def _external_label(source: str, command: str, user: str) -> str:
    """Libellé d'un cron découvert sur l'hôte (jamais vide, jamais trompeur)."""
    path = Path(str(source or ''))
    if path == Path(CRONTAB):
        # Ligne de /etc/crontab : run-parts est plus parlant que « test »/« cd ».
        return _run_parts_target(command) or default_label(command)
    if path.parent == Path(CRON_D):
        return (path.name or default_label(command))[:60]
    if path.parent == Path(SPOOL):
        who = str(user or '').strip() or 'root'
        label = default_label(command)
        return who[:60] if label == 'Cron' else f'{who} · {label}'[:60]
    return default_label(command)


def _slugify(text: str) -> str:
    value = unicodedata.normalize('NFKD', str(text or ''))
    value = value.encode('ascii', 'ignore').decode('ascii').lower()
    value = re.sub(r'[^a-z0-9]+', '-', value).strip('-')
    return value[:40].strip('-') or 'cron'


def _unique_slug(crons: dict, base: str) -> str:
    slug = base
    index = 2
    while slug in crons:
        suffix = f'-{index}'
        slug = f'{base[:max(1, 40 - len(suffix))]}{suffix}'
        index += 1
    return slug


def _label(value, command: str) -> str:
    text = str(value or '').strip()
    if not text:
        text = default_label(command)
    if '\n' in text or '\r' in text:
        raise PanelError('Libellé : retour à la ligne interdit')
    if len(text) > 60:
        raise PanelError('Libellé trop long (60 caractères max)')
    return text


def _command(value) -> str:
    text = str(value or '').strip()
    if not text:
        raise PanelError('Commande vide')
    if any(char in text for char in '\n\r\x00'):
        raise PanelError('Commande : retour à la ligne ou caractère nul interdit')
    if len(text) > 900:
        raise PanelError('Commande trop longue (900 caractères max)')
    return text


def _user(value) -> str:
    text = str(value or '').strip() or 'root'
    if not ACCOUNT.fullmatch(text):
        raise PanelError('Utilisateur invalide (ex. root, www-data)')
    return text


# ----------------------------------------------------------------- mutations

def add(label: str, command: str, schedule: str, user: str = 'root') -> str:
    """Ajoute un cron géré et réécrit le fichier /etc/cron.d/serverpanel."""
    root_only()
    text = _command(command)
    name = _label(label, text)
    when = validate_schedule(schedule)
    who = _user(user)
    data = _load()
    crons = _crons(data)
    slug = _unique_slug(crons, _slugify(name))
    crons[slug] = {'label': name, 'command': text, 'schedule': when, 'user': who,
                   'enabled': True}
    data['crons'] = crons
    _save(data)
    return slug


def update(slug: str, **fields) -> None:
    root_only()
    unknown = sorted(set(fields) - ALLOWED_FIELDS)
    if unknown:
        raise PanelError(f'Champ non modifiable : {", ".join(unknown)}')
    key = str(slug or '').strip()
    data = _load()
    crons = _crons(data)
    entry = crons.get(key)
    if key in crons and _ext(key, entry):
        raise PanelError('Cron externe : lecture seule')
    if not isinstance(entry, dict):
        raise PanelError(f'Cron introuvable : {key or slug}')
    updated = dict(entry)
    if 'command' in fields:
        updated['command'] = _command(fields['command'])
    if 'schedule' in fields:
        updated['schedule'] = validate_schedule(fields['schedule'])
    if 'user' in fields:
        updated['user'] = _user(fields['user'])
    if 'enabled' in fields:
        if not isinstance(fields['enabled'], bool):
            raise PanelError('État invalide (booléen attendu)')
        updated['enabled'] = fields['enabled']
    if 'label' in fields:
        updated['label'] = _label(fields['label'], str(updated.get('command') or ''))
    # Normalisation défensive d'une entrée éventuellement éditée à la main
    updated['command'] = _command(updated.get('command', ''))
    updated['schedule'] = validate_schedule(updated.get('schedule', ''))
    updated['user'] = _user(updated.get('user', 'root'))
    if not str(updated.get('label') or '').strip():
        updated['label'] = default_label(updated['command'])
    updated['enabled'] = _flag(updated.get('enabled', True))
    crons[key] = updated
    data['crons'] = crons
    _save(data)


def remove(slug: str) -> None:
    root_only()
    key = str(slug or '').strip()
    data = _load()
    crons = _crons(data)
    if key in crons and _ext(key, crons.get(key)):
        raise PanelError('Cron externe : lecture seule')
    if key not in crons:
        raise PanelError(f'Cron introuvable : {key or slug}')
    crons.pop(key)
    data['crons'] = crons
    _save(data)


def set_enabled(slug: str, enabled: bool) -> None:
    update(slug, enabled=enabled)


# ----------------------------------------------------------------- inventaire

def _parse_job(line: str, source: str, number: int, user: str | None) -> CronJob | None:
    tokens = line.split()
    if not tokens:
        return None
    if tokens[0].startswith('@'):
        macro = tokens[0].lower()
        if macro not in MACROS:
            return None
        schedule = macro
        rest = tokens[1:]
    else:
        if len(tokens) < 6:
            return None
        try:
            schedule = validate_schedule(' '.join(tokens[:5]))
        except PanelError:
            return None
        rest = tokens[5:]
    if not rest:
        return None
    if user is None:
        # Macro sans utilisateur (ancien format) : root par défaut.
        if len(rest) == 1 and schedule.startswith('@'):
            who, command = 'root', rest[0]
        elif len(rest) >= 2:
            who, command = rest[0], ' '.join(rest[1:])
        else:
            return None
    else:
        who, command = user, ' '.join(rest)
    command = command.strip()
    if not who or not command:
        return None
    # \% est la façon dont cron protège un % : on affiche la commande réelle.
    return CronJob(label=_external_label(source, command, who), command=command.replace('\\%', '%'),
                   user=who, schedule=schedule, source=source, managed=False,
                   enabled=True, slug='', line=number)


def _read_jobs(path: Path, user: str | None = None, source: str | None = None) -> list[CronJob]:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []  # Fichier illisible : jamais d'exception remontée à l'UI
    jobs = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#') or _ENV_LINE.match(line):
            continue
        job = _parse_job(line, source or str(path), number, user)
        if job is not None:
            jobs.append(job)
    return jobs


def _iter_files(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except OSError:
        return []


def _external() -> list[CronJob]:
    jobs = _read_jobs(CRONTAB)
    for path in _iter_files(CRON_D):
        if path.name == MANAGED.name or '.' in path.name:
            continue  # cron ignore les fichiers cron.d contenant un point
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        jobs.extend(_read_jobs(path))
    for path in _iter_files(SPOOL):
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        jobs.extend(_read_jobs(path, user=path.name))
    jobs.sort(key=lambda job: (job.source, job.line))
    return jobs


def inventory() -> list[CronJob]:
    """Tous les crons : gérés (config) puis externes (lecture seule)."""
    _sync_managed()
    jobs: list[CronJob] = []
    try:
        data = _load()
    except PanelError:
        data = {}
    for slug, entry in _crons(data).items():
        if not isinstance(entry, dict) or _ext(slug, entry):
            continue
        command = _line_value(entry.get('command'))
        jobs.append(CronJob(
            label=_single(entry.get('label')) or default_label(command),
            command=command,
            user=_single(entry.get('user')) or 'root',
            schedule=_single(entry.get('schedule')),
            source=str(MANAGED),
            managed=True,
            enabled=_flag(entry.get('enabled', True)),
            slug=str(slug),
            line=0,
        ))
    jobs.sort(key=lambda job: job.label.casefold())
    return jobs + _external()


# ----------------------------------------------------------------- exécution / journaux

def available() -> bool:
    return shutil.which('crontab') is not None


def service_state() -> str:
    """État du démon cron : active / inactive / absent / unknown (jamais d'exception)."""
    if not available():
        return 'absent'
    try:
        output = run('systemctl', 'is-active', 'cron', timeout=5, check=False)
    except Exception:
        return 'unknown'
    state = (output or '').strip().splitlines()
    value = state[0].strip() if state else ''
    return value if value in {'active', 'inactive'} else 'unknown'


def run_now(job: CronJob, timeout: int = 60) -> list[str]:
    """Exécute la commande comme cron le ferait (via /bin/sh), sortie formatée."""
    root_only()
    command = str(job.command or '').strip()
    if not command:
        raise PanelError('Commande vide')
    user = str(job.user or 'root').strip() or 'root'
    if user == 'root':
        argv = ['/bin/sh', '-c', command]
    else:
        _user(user)
        argv = ['runuser', '-u', user, '--', '/bin/sh', '-c', command]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PanelError(f'Exécution impossible : {exc}') from exc
    lines = [f'$ {command}']
    lines.extend(done.stdout.splitlines() if done.stdout else [])
    lines.append(f'Code {done.returncode}')
    return lines


def logs(limit: int = 150) -> list[str]:
    """Journal cron via journalctl, sinon /var/log/syslog filtré sur CRON."""
    limit = max(1, min(int(limit), 2000))
    try:
        output = run('journalctl', '-t', 'CRON', '-n', str(limit), '--no-pager',
                     '--output=short-iso', timeout=15)
    except Exception:
        output = ''
    if (output or '').strip():
        return output.splitlines()
    try:
        text = _SYSLOG.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ['Journal cron indisponible (journalctl -t CRON).']
    lines = [line for line in text.splitlines() if 'CRON' in line]
    return lines[-limit:] or ['Aucune entrée cron dans /var/log/syslog.']


def next_run(schedule: str, now: datetime | None = None) -> datetime | None:
    """Prochaine exécution locale (datetime naïf), None si incalculable."""
    value = ' '.join(str(schedule or '').split())
    if not value:
        return None
    lowered = value.lower()
    if lowered == '@reboot':
        return None
    base = (now or datetime.now()).replace(second=0, microsecond=0)
    key = (lowered, base)
    if key in _CACHE:
        return _CACHE[key]
    result = _compute_next(lowered, base)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = result
    return result


# Cache borné : l'UI redemande la même planification à chaque rafraîchissement.
_CACHE: dict[tuple[str, datetime], datetime | None] = {}
_CACHE_MAX = 128


def _day_matches(day: datetime, dom: set[int], dow: set[int],
                 dom_restricted: bool, dow_restricted: bool) -> bool:
    in_dom = day.day in dom
    in_dow = ((day.weekday() + 1) % 7) in dow
    if dom_restricted and dow_restricted:
        return in_dom or in_dow  # Sémantique cron : OR quand les deux sont restreints
    return in_dom and in_dow


def _compute_next(value: str, base: datetime) -> datetime | None:
    text = MACRO_SCHEDULE.get(value, value)
    if not text:
        return None
    try:
        text = validate_schedule(text)
    except PanelError:
        return None
    parts = text.split(' ')
    if len(parts) != 5:
        return None
    try:
        minutes = sorted(_expand(parts[0], 0, 59))
        hours = sorted(_expand(parts[1], 0, 23))
        dom = _expand(parts[2], 1, 31)
        months = _expand(parts[3], 1, 12)
        dow = {day % 7 for day in _expand(parts[4], 0, 7)}
    except (ValueError, TypeError):
        return None
    if not minutes or not hours or not dom or not months:
        return None
    start = base + timedelta(minutes=1)
    limit = base + timedelta(days=366 * 4)
    day = start.replace(hour=0, minute=0)
    dom_restricted = parts[2] != '*'
    dow_restricted = parts[4] != '*'
    while day <= limit:
        if day.month in months and _day_matches(day, dom, dow, dom_restricted, dow_restricted):
            for hour in hours:
                for minute in minutes:
                    candidate = day.replace(hour=hour, minute=minute)
                    if candidate < start:
                        continue
                    return candidate if candidate <= limit else None
        day += timedelta(days=1)
    return None


def detail_lines(job: CronJob) -> list[str]:
    """Lignes prêtes à afficher dans le panneau de détail."""
    soon = next_run(job.schedule) if job.enabled else None
    when = soon.strftime('%d/%m/%Y %H:%M') if soon else '—'
    if not job.managed:
        state = 'EXTERNE'
    else:
        state = 'ACTIF' if job.enabled else 'DÉSACTIVÉ'
    return [
        f'{"QUAND":<11} {job.when}',
        f'{"PROCHAINE":<11} {when}',
        f'{"ÉTAT":<11} {state}',
        f'{"UTILISATEUR":<11} {job.user or "root"}',
        f'{"FICHIER":<11} {job.source}',
        f'{"COMMANDE":<11} {job.command}',
    ]
