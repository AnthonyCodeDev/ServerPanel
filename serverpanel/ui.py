"""Keyboard-first curses interface. No other runtime dependency."""
from __future__ import annotations

import curses
import textwrap
import time
from dataclasses import dataclass
from typing import Callable

from . import cron, services, sites
from .common import (PanelError, fastfetch_info, local_ip, os_name, pins, stats,
                     toggle_pin)

# Extra curses color pairs for BOOT badges (white-on-green / white-on-red)
_BOOT_ON_PAIR = 8
_BOOT_OFF_PAIR = 9

TABS = ('VPS', 'SERVICES', 'SITES', 'CRON')
TAB_VPS, TAB_SERVICES, TAB_SITES, TAB_CRON = range(len(TABS))
KEY_ENTER = (10, 13, curses.KEY_ENTER)
MIN_H, MIN_W = 17, 56
IDLE = 15000
TOP_MEMORY = 5

# Assistant de planification cron : libellés affichés et valeurs cron associées.
WEEKDAY_LABELS = ('LUNDI', 'MARDI', 'MERCREDI', 'JEUDI', 'VENDREDI', 'SAMEDI', 'DIMANCHE')
WEEKDAY_VALUES = (1, 2, 3, 4, 5, 6, 0)
CRON_KINDS = ('CHAQUE JOUR', 'CHAQUE SEMAINE', 'CHAQUE MOIS',
              'TOUTES LES X HEURES', 'TOUTES LES X MINUTES', 'PERSONNALISÉ')
CRON_KIND_KEYS = ('daily', 'weekly', 'monthly', 'hours', 'minutes', 'custom')
CRON_FIELDS = (('MINUTE', 'minute', 'ex. 0 · */5'),
               ('HEURE', 'hour', 'ex. 3 · 9-17'),
               ('JOUR DU MOIS', 'day', 'ex. 1 · 1,15'),
               ('MOIS', 'month', 'ex. * · 1-12'),
               ('JOUR SEMAINE', 'weekday', 'ex. * · 1-5'))


def shorten(value: str, width: int) -> str:
    if width <= 0:
        return ''
    return value if len(value) <= width else value[:width - 1] + '…'


def memory_label(value: int) -> str:
    """Empreinte mémoire compacte : 56Mo, 1Go, 1.15Go, 12.5Go ('' si inconnue)."""
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return ''
    if amount <= 0:
        return ''
    mo = amount / 1048576
    if mo < 1024:
        return f'{mo:.0f}Mo' if mo >= 10 else f'{mo:.1f}Mo'
    go = amount / 1073741824
    digits = 2 if go < 10 else 1
    text = f'{go:.{digits}f}'.rstrip('0').rstrip('.')
    return f'{text}Go'


@dataclass
class Palette:
    normal: int = 0
    good: int = 0
    bad: int = 0
    blue: int = 0
    selected: int = 0
    dim: int = 0
    warning: int = 0
    boot_on: int = 0   # white on green
    boot_off: int = 0  # white on red


class Panel:
    def __init__(self, window):
        self.win = window
        self.colors = Palette()
        self.tab = TAB_VPS
        self.index = 0
        self.scroll = 0
        self.action_index = 0
        # VPS n'a pas de liste : l'ouverture se fait sur la barre d'onglets.
        self.focus = 'tabs'
        self.all_units = False
        self.cron_all = False
        self.cron_pins: set[str] = set()
        self.message = ''
        self.message_error = False
        self.message_until = 0.0
        self.services: list[services.Service] = []
        self.sites: list[sites.Site] = []
        self.service_error = ''
        self.jobs: list[cron.CronJob] = []
        self.cron_error = ''
        self.cron_service = 'unknown'
        # Infos fastfetch (os, host, kernel, uptime, ip) mises en cache 30 s :
        # aucun subprocess n'est lancé à chaque rendu.
        self._fastfetch: dict[str, str] = {}
        self._fastfetch_at = 0.0

    # ---------------------------------------------------------------- terminal

    def setup(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.win.keypad(True)
        curses.noecho()
        curses.cbreak()
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except (AttributeError, curses.error):
            pass
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                pairs = [(curses.COLOR_WHITE, -1), (curses.COLOR_GREEN, -1),
                         (curses.COLOR_RED, -1), (curses.COLOR_BLUE, -1),
                         (curses.COLOR_WHITE, curses.COLOR_BLUE),
                         (curses.COLOR_WHITE, -1), (curses.COLOR_YELLOW, -1)]
                for number, (fg, bg) in enumerate(pairs, 1):
                    curses.init_pair(number, fg, bg)
                curses.init_pair(_BOOT_ON_PAIR, curses.COLOR_WHITE, curses.COLOR_GREEN)
                curses.init_pair(_BOOT_OFF_PAIR, curses.COLOR_WHITE, curses.COLOR_RED)
                self.colors = Palette(*[curses.color_pair(i) for i in range(1, 8)],
                                       curses.color_pair(_BOOT_ON_PAIR),
                                       curses.color_pair(_BOOT_OFF_PAIR))
            except curses.error:
                pass
        self.colors.dim |= curses.A_DIM

    def idle(self):
        try:
            self.win.timeout(IDLE)
        except (AttributeError, curses.error):
            pass

    def blocking(self):
        try:
            self.win.timeout(-1)
        except (AttributeError, curses.error):
            pass

    def put(self, y: int, x: int, text, style: int = 0, bold: bool = False):
        height, width = self.win.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width - 1 or not str(text):
            return
        try:
            self.win.addnstr(y, x, str(text), max(1, width - 1 - x),
                             style | (curses.A_BOLD if bold else 0))
        except curses.error:
            pass

    def fill(self, y: int, x: int, height: int, width: int, style: int = 0):
        screen_h, screen_w = self.win.getmaxyx()
        height = max(0, min(height, screen_h - y))
        width = max(0, min(width, screen_w - x))
        for row in range(height):
            self.put(y + row, x, ' ' * width, style)

    def center(self, y: int, text: str, style: int = 0, bold: bool = False):
        width = self.win.getmaxyx()[1]
        text = str(text)[:max(1, width - 2)]
        self.put(y, max(1, (width - len(text)) // 2), text, style, bold)

    def horizontal(self, y: int, x: int, width: int, style: int = 0):
        self.put(y, x, '─' * max(0, width), style)

    def box(self, y: int, x: int, height: int, width: int, *, title: str = '',
            color: int | None = None):
        screen_h, screen_w = self.win.getmaxyx()
        width = max(6, min(width, screen_w - x - 1))
        height = max(3, min(height, screen_h - y))
        self.fill(y, x, height, width, self.colors.normal)
        self.put(y, x, '┌' + '─' * (width - 2) + '┐', self.colors.dim)
        self.put(y + height - 1, x, '└' + '─' * (width - 2) + '┘', self.colors.dim)
        for row in range(1, height - 1):
            self.put(y + row, x, '│', self.colors.dim)
            self.put(y + row, x + width - 1, '│', self.colors.dim)
        if title:
            self.put(y, x + 2, f' {title} '[:width - 4],
                     color if color is not None else self.colors.blue, True)

    def flash(self, message: str, *, error: bool = False):
        self.message = ' '.join(str(message).split())[:220]
        self.message_error = error
        self.message_until = time.monotonic() + (8 if error else 5)

    # ------------------------------------------------------------------- data

    def refresh_data(self):
        try:
            self.services = services.inventory()
            self.service_error = ''
        except PanelError as exc:
            self.services = []
            self.service_error = str(exc)
        self.show_sync_report()
        try:
            self.sites = sites.inventory()
        except (OSError, PanelError) as exc:
            self.sites = []
            self.flash(str(exc), error=True)
        # Cron : jamais d'erreur affichée en boucle, l'onglet reste utilisable.
        try:
            self.jobs = cron.inventory()
            self.cron_error = ''
        except Exception as exc:  # l'inventaire ne doit jamais casser le refresh
            self.jobs = []
            self.cron_error = str(exc)
        try:
            self.cron_service = cron.service_state()
        except Exception:
            self.cron_service = 'unknown'
        # Favoris cron : jamais d'exception remontée par le rafraîchissement.
        try:
            self.cron_pins = set(cron.pinned_crons())
        except Exception:
            self.cron_pins = set()

    def show_sync_report(self):
        """Surface what the config → systemd sync just did (created units, errors)."""
        messages = services.take_sync_messages()
        if not messages:
            return
        errors = [text for is_error, text in messages if is_error]
        infos = [text for is_error, text in messages if not is_error]
        if errors:
            extra = f' (+{len(errors) - 1} autre·s)' if len(errors) > 1 else ''
            self.flash(errors[0] + extra, error=True)
        elif infos:
            extra = f' (+{len(infos) - 1} autre·s)' if len(infos) > 1 else ''
            self.flash(infos[0] + extra)

    def service_rows(self) -> list[services.Service]:
        if self.all_units:
            return self.services
        pinned_names = set(pins())
        running = sorted((s for s in self.services if s.active == 'active'),
                         key=lambda s: s.memory, reverse=True)
        heavy = {s.name for s in running[:TOP_MEMORY]}
        visible = [s for s in self.services
                   if s.managed or s.active == 'failed' or s.origin == 'manual'
                   or s.enabled == 'enabled' or s.name in heavy
                   or s.name.removesuffix('.service') in pinned_names]
        def _sort_key(s):
            short = s.name.removesuffix('.service')
            is_pinned = short in pinned_names
            is_stopped = s.active not in ('active', 'failed', 'activating', 'deactivating', 'reloading')
            return (not is_pinned, is_stopped, s.name)
        return sorted(visible, key=_sort_key)

    def rows(self) -> list:
        if self.tab == TAB_SERVICES:
            return self.service_rows()
        if self.tab == TAB_SITES:
            return self.sites
        if self.tab == TAB_CRON:
            rows = self.jobs if self.cron_all else [
                job for job in self.jobs if job.managed or job.key in self.cron_pins]
            return sorted(rows, key=lambda job: (job.key not in self.cron_pins,
                                                 not job.managed,
                                                 (job.label or '').casefold()))
        return []

    def actions(self) -> list[tuple[str, str]]:
        rows = self.rows()
        if self.tab == TAB_SERVICES:
            if not rows:
                return [('NOUVEAU', 'new')]
            item = rows[min(self.index, len(rows) - 1)]
            result = [('DÉMARRER', 'start'), ('STOPPER', 'stop'),
                      ('RESTART', 'restart'), ('LOGS', 'logs'),
                      ('ACTIVÉ AU BOOT' if item.enabled == 'enabled' else 'DÉSACTIVÉ AU BOOT', 'auto')]
            name = item.name.removesuffix('.service')
            result.append(('RETIRER ÉPINGLE' if name in pins() else 'ÉPINGLER', 'pin'))
            if item.managed:
                result.append(('INFOS', 'infos'))
                result.append(('SUPPRIMER', 'remove'))
            return result
        if self.tab == TAB_SITES:
            rows = self.rows()
            if not rows:
                return []
            item = rows[min(self.index, len(rows) - 1)]
            if not item.managed:
                return []
            result = [('ACTIVER' if not item.enabled else 'DÉSACTIVER', 'toggle'),
                      ('HTTPS' if not item.https else 'HTTPS OK', 'https'),
                      ('LOGS', 'logs'),
                      ('SUPPRIMER', 'remove')]
            return result
        if self.tab == TAB_CRON:
            if not rows:
                return [('NOUVEAU', 'new')]
            item = rows[min(self.index, len(rows) - 1)]
            pin = 'RETIRER ÉPINGLE' if item.key in self.cron_pins else 'ÉPINGLER'
            if not item.managed:
                return [('INFOS', 'infos'), ('JOURNAL', 'logs'), (pin, 'pin')]
            return [('MODIFIER', 'edit'), ('EXÉCUTER', 'run'),
                    ('DÉSACTIVER' if item.enabled else 'ACTIVER', 'toggle'),
                    ('JOURNAL', 'logs'), (pin, 'pin'), ('SUPPRIMER', 'remove')]
        return []

    def change_tab(self, delta: int):
        self.tab = (self.tab + delta) % len(TABS)
        self.index = self.scroll = self.action_index = 0
        if self.tab == TAB_VPS:
            # Pas de liste sur VPS : le focus revient toujours aux onglets.
            self.focus = 'tabs'
        elif self.focus != 'tabs':
            self.focus = 'list'

    def set_tab(self, tab: int):
        if tab != self.tab:
            self.tab = tab
            self.index = self.scroll = self.action_index = 0
            # VPS n'a pas de liste : le focus reste sur les onglets.
            self.focus = 'tabs' if tab == TAB_VPS else 'list'

    def sidebar_width(self, width: int) -> int:
        """Largeur commune des panneaux latéraux : 40 % du terminal, 22 mini."""
        return max(22, round(width * 0.40))

    # ----------------------------------------------------------------- render

    def header(self, width: int):
        m = stats()
        self.put(0, 2, 'PANEL', self.colors.blue, True)
        host = m['host'][:max(4, width - 18)]
        self.put(0, max(10, width - 2 - len(host)), host, self.colors.dim)
        x = 2
        for index, name in enumerate(TABS):
            label = f'[{name}]'
            if index == self.tab:
                style = self.colors.selected if self.focus == 'tabs' else self.colors.blue
            else:
                style = self.colors.dim if self.focus == 'tabs' else self.colors.blue
            self.put(1, x, label, style, True)
            x += len(label) + 2
        self.horizontal(2, 2, width - 4, self.colors.dim)

    def section(self, separator: int):
        if self.tab == TAB_SERVICES:
            title = f'SERVICES  {len(self.rows())}'
            self.put(3, 2, title, self.colors.blue, True)
            new_label = '[N] NOUVEAU'
            new_x = separator - len(new_label) - 1
            if new_x > 16:
                self.put(3, new_x, new_label, self.colors.blue, True)
                scope = '[A] AUTO' if self.all_units else '[A] TOUS'
                scope_x = new_x - len(scope) - 3
                if scope_x > 13:
                    self.put(3, scope_x, scope, self.colors.dim)
        elif self.tab == TAB_SITES:
            self.put(3, 2, f'SITES  {len(self.rows())}', self.colors.blue, True)
            new_label = '[N] NOUVEAU'
            new_x = separator - len(new_label) - 1
            if new_x > 14:
                self.put(3, new_x, new_label, self.colors.blue, True)
        elif self.tab == TAB_CRON:
            title = f'CRON  {len(self.rows())}'
            self.put(3, 2, title, self.colors.blue, True)
            limit = 2 + len(title) + 2
            # Empilement de droite à gauche : [N] NOUVEAU, badge d'état, puis [A].
            # [A] indique l'action proposée : TOUS en mode favoris, FAVORIS sinon.
            right = separator - 1
            new_label = '[N] NOUVEAU'
            scope = '[A] FAVORIS' if self.cron_all else '[A] TOUS'
            badge = self.cron_service_badge()
            new_x = right - len(new_label)
            show_new = new_x >= limit
            after_new = new_x - 2 if show_new else right
            badge_x = after_new - (len(badge[0]) if badge else 0)
            show_badge = bool(badge) and badge_x >= limit
            after_badge = badge_x - 2 if show_badge else after_new
            scope_x = after_badge - len(scope)
            if scope_x < limit and show_badge:
                # Ligne étroite : le filtre prime, le badge d'état s'efface.
                show_badge = False
                scope_x = after_new - len(scope)
            if show_new:
                self.put(3, new_x, new_label, self.colors.blue, True)
            if show_badge:
                self.put(3, badge_x, badge[0], badge[1])
            if scope_x >= limit:
                self.put(3, scope_x, scope, self.colors.dim)
        else:
            self.put(3, 2, 'SYSTÈME', self.colors.blue, True)

    def cron_service_badge(self) -> tuple[str, int] | None:
        """État du démon cron, affiché seulement si la ligne le permet."""
        if self.cron_service == 'active':
            return 'SERVICE ACTIF', self.colors.good
        if self.cron_service == 'inactive':
            return 'SERVICE ARRÊTÉ', self.colors.bad
        if self.cron_service == 'absent':
            return 'CRON ABSENT', self.colors.bad
        return None

    def render(self):
        self.win.erase()
        height, width = self.win.getmaxyx()
        if height < MIN_H or width < MIN_W:
            self.center(max(1, height // 2), 'TERMINAL TROP PETIT', self.colors.bad, True)
            self.center(max(2, height // 2 + 1), f'MINIMUM {MIN_W} × {MIN_H}', self.colors.dim)
            self.win.refresh()
            return
        if self.message and time.monotonic() > self.message_until:
            self.message = ''
        self.header(width)
        if self.tab == TAB_VPS:
            self.render_vps(height, width)
            self.render_message(height, width)
            self.win.refresh()
            return
        # Même largeur pour SERVICES, SITES et CRON : 40 % du terminal.
        side_width = self.sidebar_width(width)
        side_x = width - side_width - 2
        separator = side_x - 2
        actions = self.actions()
        rows = self.rows()
        self.index = min(self.index, max(0, len(rows) - 1))
        self.action_index = min(self.action_index, max(0, len(actions) - 1))
        self.section(separator)
        self.render_list(rows, height, separator)
        for row in range(4, height - 1):
            self.put(row, separator, '│', self.colors.dim)
        if self.tab == TAB_SITES:
            self.render_site_sidebar(actions, height, side_x, side_width)
        elif self.tab == TAB_CRON:
            self.render_cron_sidebar(actions, height, side_x, side_width)
        else:
            self.render_actions(actions, height, side_x, side_width)
        self.render_message(height, width)
        self.win.refresh()

    def list_visible(self, height: int | None = None) -> int:
        if height is None:
            height = self.win.getmaxyx()[0]
        return max(1, height - 5)

    def clamp_list_scroll(self, total: int, visible: int):
        max_scroll = max(0, total - visible)
        self.scroll = max(0, min(self.scroll, max_scroll))
        if self.index >= self.scroll + visible:
            self.scroll = min(max_scroll, self.index - visible + 1)
        if self.index < self.scroll:
            self.scroll = self.index

    def scroll_list(self, delta: int):
        if self.tab == TAB_VPS or not delta:
            return
        rows = self.rows()
        total = len(rows)
        if total <= 0:
            return
        if self.focus == 'tabs':
            if delta > 0:
                self.focus = 'list'
            return
        self.index = max(0, min(total - 1, self.index + delta))
        self.clamp_list_scroll(total, self.list_visible())

    def mouse_wheel_delta(self, key) -> int:
        if key in (getattr(curses, 'KEY_SR', -999),):
            return -1
        if key in (getattr(curses, 'KEY_SF', -998),):
            return 1
        if key != curses.KEY_MOUSE:
            return 0
        try:
            _id, _x, _y, _z, bstate = curses.getmouse()
        except (curses.error, AttributeError, TypeError, ValueError):
            return 0
        if bstate & getattr(curses, 'BUTTON_SHIFT', 0):
            return 0
        if bstate & curses.BUTTON4_PRESSED:
            return -1
        if bstate & getattr(curses, 'BUTTON5_PRESSED', 0x00200000):
            return 1
        return 0

    def empty_label(self) -> str:
        if self.tab == TAB_SERVICES:
            return 'AUCUN SERVICE'
        if self.tab == TAB_CRON:
            if not cron.available():
                return 'CRON NON INSTALLÉ · sudo bash install.sh'
            if not self.cron_all:
                return 'AUCUN CRON FAVORI'
            return 'AUCUN CRON'
        return 'AUCUN SITE'

    def row_pinned(self, item, pinned_names: set, pinned_keys: set) -> bool:
        """Un favori : épingle de service (nom) ou favori cron (clé stable)."""
        if self.tab == TAB_SERVICES:
            return item.name.removesuffix('.service') in pinned_names
        if self.tab == TAB_CRON:
            return item.key in pinned_keys
        return False

    def render_list(self, rows: list, height: int, separator: int):
        visible = self.list_visible(height)
        total = len(rows)
        self.clamp_list_scroll(total, visible)
        if not rows:
            y = 4 + max(0, (visible - 2) // 2)
            if self.tab == TAB_CRON:
                # Centré dans la liste seule : ne jamais déborder sur la sidebar.
                label = shorten(self.empty_label(), max(1, separator - 4))
                self.put(y, 2 + max(0, (separator - 2 - len(label)) // 2),
                         label, self.colors.dim)
            else:
                self.center(y, self.empty_label(), self.colors.dim)
            return
        pinned_names = set(pins()) if self.tab == TAB_SERVICES else set()
        pinned_keys = set(self.cron_pins) if self.tab == TAB_CRON else set()
        has_pins = bool(pinned_names or pinned_keys)
        last_was_pinned = None
        render_y = 4  # screen row, separate from data index to account for separator lines
        for offset, item in enumerate(rows[self.scroll:self.scroll + visible]):
            index = self.scroll + offset
            focused = (index == self.index) and self.focus != 'tabs'
            # Draw separator between pinned and non-pinned sections
            if has_pins:
                is_pinned = self.row_pinned(item, pinned_names, pinned_keys)
                if last_was_pinned is None:
                    last_was_pinned = is_pinned
                elif last_was_pinned and not is_pinned:
                    if render_y < height - 1:
                        self.put(render_y, 2, '  ───────────────', self.colors.dim)
                    render_y += 1
                    last_was_pinned = False
                else:
                    last_was_pinned = is_pinned
            if render_y >= height - 1:
                break
            self.put(render_y, 2, '▌' if focused else ' ', self.colors.blue if focused else self.colors.normal)
            if self.tab == TAB_SERVICES:
                self.render_service(item, render_y, focused, separator, pinned_names)
            elif self.tab == TAB_CRON:
                self.render_cron(item, render_y, focused, separator, pinned_keys)
            else:
                self.render_site(item, render_y, focused, separator)
            render_y += 1

    def render_service(self, item: services.Service, y: int, focused: bool, separator: int,
                       pinned_names: set | None = None):
        if separator >= 48:
            auto_x = separator - 8
            status_x = auto_x - 9
        else:
            auto_x = None
            status_x = separator - 6
        mem_x = status_x - 9
        show_memory = mem_x - 4 >= 8
        if show_memory:
            name_area = max(4, mem_x - 4 - 1)
        else:
            name_area = max(4, status_x - 4 - 1)
        short = item.name.removesuffix('.service')
        is_pinned = bool(pinned_names and short in pinned_names)
        prefix = '★ ' if is_pinned else '  '
        available_name = max(1, name_area - len(prefix))
        name = prefix + shorten(item.name, available_name)
        if focused:
            self.put(y, 4, f'{name:<{name_area}}', self.colors.selected, True)
        else:
            name_color = self.colors.good if is_pinned else self.colors.normal
            self.put(y, 4, name, name_color)
        if item.active == 'active':
            label, color = 'ACTIF', self.colors.good
        elif item.active == 'failed':
            label, color = 'ÉCHEC', self.colors.bad
        elif item.active in ('inactive', 'dead'):
            label, color = 'ARRÊT', self.colors.warning
        elif item.active == 'activating':
            label, color = 'INIT…', self.colors.warning
        elif item.active == 'deactivating':
            label, color = 'STOP…', self.colors.warning
        elif item.active == 'reloading':
            label, color = 'RECHG', self.colors.warning
        else:
            label, color = item.active.upper()[:5], self.colors.dim
        self.put(y, status_x, f'{label:>5}', color, True)
        if show_memory:
            mem_text = memory_label(item.memory)
            if mem_text:
                self.put(y, mem_x, f'{mem_text:>7}', self.colors.dim)
        if auto_x is not None:
            badge = ' BOOT '
            if item.enabled == 'enabled':
                badge_color = self.colors.boot_on
            elif item.enabled == 'disabled':
                badge_color = self.colors.boot_off
            else:
                badge_color = self.colors.dim
            self.put(y, auto_x, f'{badge:>6}', badge_color, True)

    def render_site(self, item: sites.Site, y: int, focused: bool, separator: int):
        status_x = separator - 6
        show_target = separator >= 92
        if show_target:
            name_area = max(10, min(38, (status_x - 4) // 2))
            target_x = 4 + name_area + 2
            target_width = status_x - target_x - 1
            show_target = target_width >= 8
        else:
            name_area = max(4, status_x - 5)
        name = shorten(item.domain, name_area)
        if focused:
            self.put(y, 4, f'{name:<{name_area}}', self.colors.selected, True)
        else:
            self.put(y, 4, name, self.colors.normal)
        if show_target:
            self.put(y, target_x, shorten(item.target, target_width), self.colors.dim)
        if item.https:
            label, color = 'HTTPS', self.colors.good
        elif item.enabled:
            label, color = 'HTTP', self.colors.warning
        else:
            label, color = 'OFF', self.colors.bad
        self.put(y, status_x, f'{label:>5}', color, True)

    # ------------------------------------------------------------------- cron

    def wrap_text(self, value, width: int, limit: int | None = None) -> list[str]:
        """Découpe un texte sur `width` colonnes (textwrap, jamais hors bornes)."""
        width = max(4, int(width))
        text = ' '.join(str(value).split())
        lines = textwrap.wrap(text, width=width) or ['']
        if limit is not None:
            limit = max(1, int(limit))
            if len(lines) > limit:
                lines = lines[:limit]
                lines[-1] = shorten(lines[-1] + '…', width)
        return lines

    def render_cron(self, item: cron.CronJob, y: int, focused: bool, separator: int,
                    pinned_keys: set | None = None):
        status_x = separator - 6
        available = max(4, status_x - 4)
        name_area = max(4, available // 2)
        schedule_x = 4 + name_area + 1
        schedule_width = status_x - schedule_x - 1
        if schedule_width < 12:
            # Pas la place pour la planification : le libellé récupère la zone.
            schedule_width = 0
            name_area = max(4, status_x - 5)
        is_pinned = bool(pinned_keys and item.key in pinned_keys)
        prefix = '★ ' if is_pinned else '  '
        available_name = max(1, name_area - len(prefix))
        name = prefix + shorten(item.label or item.command or 'cron', available_name)
        if focused:
            self.put(y, 4, f'{name:<{name_area}}', self.colors.selected, True)
        else:
            name_color = self.colors.good if is_pinned else self.colors.normal
            self.put(y, 4, name, name_color)
        if schedule_width:
            when = shorten(cron.describe(item.schedule, short=True), schedule_width)
            self.put(y, schedule_x, when, self.colors.dim)
        if not item.managed:
            label, color = 'SYST', self.colors.dim
        elif item.enabled:
            label, color = 'ACTIF', self.colors.good
        else:
            label, color = 'OFF', self.colors.warning
        self.put(y, status_x, f'{label:>5}', color, True)

    def cron_next_label(self, item: cron.CronJob) -> str:
        if not item.enabled:
            return '—'
        try:
            soon = cron.next_run(item.schedule)
        except (PanelError, OSError, ValueError):
            return '—'
        return soon.strftime('%d/%m %H:%M') if soon else '—'

    def cron_detail_fields(self, item: cron.CronJob) -> list[tuple[str, str, int]]:
        if not item.managed:
            state, state_color = 'EXTERNE', self.colors.dim
        elif item.enabled:
            state, state_color = 'ACTIF', self.colors.good
        else:
            state, state_color = 'DÉSACTIVÉ', self.colors.warning
        return [
            ('QUAND', item.when, self.colors.normal),
            ('PROCHAINE', self.cron_next_label(item), self.colors.dim),
            ('ÉTAT', state, state_color),
            ('UTILISATEUR', item.user or 'root', self.colors.normal),
            ('FICHIER', item.source, self.colors.dim),
            ('COMMANDE', item.command, self.colors.normal),
        ]

    def render_cron_sidebar(self, actions: list[tuple[str, str]], height: int, x: int,
                            width: int):
        inner = max(8, width - 1)
        label_width = 12
        title_style = self.colors.selected if self.focus == 'actions' else self.colors.blue
        self.put(4, x, 'ACTIONS', title_style, True)
        if actions and self.focus != 'actions':
            self.put(4, x + 8, '⏎', self.colors.dim)
        y = 5
        for index, (label, key) in enumerate(actions):
            if y >= height - 1:
                break
            chip = f'[{label}]'[:inner]
            if self.focus == 'actions' and index == self.action_index:
                self.put(y, x, f'{chip:<{inner}}'[:inner], self.colors.selected, True)
            else:
                self.put(y, x, chip, self.cron_action_color(key, label), True)
            y += 1
        rows = self.rows()
        if not rows:
            if y < height - 1:
                self.put(y, x, 'Aucun cron', self.colors.dim)
                y += 1
            if y < height - 1:
                self.put(y, x, '[N] AJOUTER', self.colors.blue, True)
            return
        item = rows[min(self.index, len(rows) - 1)]
        if not item.managed and y < height - 1:
            self.put(y, x, 'LECTURE SEULE', self.colors.dim, True)
            y += 1
        if y < height - 12:
            y += 1  # respiration avant le détail, uniquement si la place le permet
        if y >= height - 1:
            return
        self.put(y, x, 'DÉTAIL', self.colors.blue, True)
        y += 1
        for label, value, color in self.cron_detail_fields(item):
            room = height - 1 - y
            if room <= 0:
                break
            value_width = max(4, inner - label_width)
            # FICHIER tient sur une ligne (tronquée), le reste se replie.
            parts = self.wrap_text(value, value_width, 1 if label == 'FICHIER' else room)
            for number, part in enumerate(parts):
                if y >= height - 1:
                    break
                if number == 0:
                    self.put(y, x, f'{label:<{label_width}}'[:label_width], self.colors.blue, True)
                self.put(y, x + label_width, part[:value_width], color)
                y += 1

    def cron_action_color(self, key: str, label: str) -> int:
        if key == 'remove':
            return self.colors.bad
        if key == 'toggle':
            return self.colors.good if label == 'ACTIVER' else self.colors.warning
        return self.colors.normal

    def render_site_sidebar(self, actions: list[tuple[str, str]], height: int, x: int, width: int):
        inner = max(8, width - 1)
        title_style = self.colors.selected if self.focus == 'actions' else self.colors.blue
        self.put(4, x, 'ACTIONS', title_style, True)
        if actions and self.focus != 'actions':
            self.put(4, x + 8, '⏎', self.colors.dim)
        y = 5
        if actions:
            col = x
            limit = x + inner
            for index, (label, key) in enumerate(actions):
                chip = f'[{label}]'
                if col > x and col + len(chip) > limit:
                    y += 1
                    col = x
                if key == 'remove':
                    color = self.colors.bad
                elif key == 'https':
                    color = self.colors.good if label == 'HTTPS OK' else self.colors.warning
                else:
                    color = self.colors.normal
                if self.focus == 'actions' and index == self.action_index:
                    color = self.colors.selected
                self.put(y, col, chip[:inner], color, True)
                col += len(chip) + 1
            y += 2
        elif not self.rows():
            self.put(y, x, 'Aucun site', self.colors.dim)
            y += 2
        else:
            self.put(y, x, 'Lecture seule', self.colors.dim)
            y += 2
        self.render_site_config(height, x, width, start_y=y)

    def render_site_config(self, height: int, x: int, width: int, start_y: int = 4):
        inner = max(8, width - 1)
        self.put(start_y, x, 'APACHE', self.colors.blue, True)
        rows = self.rows()
        if not rows:
            return
        item = rows[min(self.index, len(rows) - 1)]
        shown = item.filename
        if item.https:
            shown = f'{item.filename} + ssl'
        self.put(start_y, x + 7, shorten(shown, max(1, inner - 7)), self.colors.dim)
        try:
            text = sites.config_text(item)
        except PanelError as exc:
            self.put(start_y + 2, x, shorten(str(exc), inner), self.colors.bad)
            return
        wrapped: list[str] = []
        for raw in text.splitlines() or ['']:
            line = raw.replace('\t', '    ')
            if not line:
                wrapped.append('')
                continue
            while len(line) > inner:
                wrapped.append(line[:inner])
                line = line[inner:]
            wrapped.append(line)
        y = start_y + 1
        last = height - 2
        room = max(1, last - y + 1)
        if len(wrapped) > room:
            shown = wrapped[:room - 1] + ['…']
        else:
            shown = wrapped[:room]
        for line in shown:
            if y > last:
                break
            self.put(y, x, line[:inner], self.colors.normal)
            y += 1

    def render_actions(self, actions: list[tuple[str, str]], height: int, x: int, width: int):
        title_style = self.colors.selected if self.focus == 'actions' else self.colors.blue
        title = 'ACTIONS' if self.tab == TAB_SERVICES else 'SITE'
        self.put(4, x, title, title_style, True)
        if self.focus != 'actions':
            self.put(4, x + len(title) + 1, '⏎', self.colors.dim)
        if not actions:
            return
        start = 6 if height >= 20 else 5
        step = 2 if start + (len(actions) - 1) * 2 < height - 1 else 1
        for index, (label, key) in enumerate(actions):
            y = start + index * step
            if y >= height - 1:
                break
            if key == 'auto':
                default_color = self.colors.good if label == 'ACTIVÉ AU BOOT' else self.colors.bad
            else:
                default_color = self.colors.normal
            if self.focus == 'actions' and index == self.action_index:
                self.put(y, x, f'{label:<{width - 1}}'[:width - 1], self.colors.selected, True)
            else:
                self.put(y, x, label[:width - 1], default_color, True)

    def render_vps(self, height: int, width: int):
        m = stats()
        active = sum(s.active == 'active' for s in self.services)
        failed = sum(s.active == 'failed' for s in self.services)
        apps = sum(s.managed for s in self.services)
        https = sum(s.https for s in self.sites)
        apache = next((s for s in self.services if s.name == 'apache2.service'), None)
        crons = len(self.jobs)
        managed = sum(1 for job in self.jobs if job.managed)
        # Disposition haute (>= 21 lignes) : fastfetch sous le titre, puis les
        # jauges, puis le résumé. En dessous : disposition compacte sans block.
        compact = height < 21
        self.put(3, 2, 'SYSTÈME', self.colors.blue, True)
        if compact:
            gauge_y = 6
            summary_y = 12
            show_uptime = True
        else:
            self.render_fastfetch(height, width, m, start_y=4)
            gauge_y = 10
            summary_y = 16
            show_uptime = False
        cores = m['cores'] or 1
        load = m['load']
        cpu = min(100, round(load / cores * 100)) if load is not None else None
        rows = [
            ('CPU', f'load {load:.2f} / {cores}' if load is not None else 'load ?', cpu),
            ('RAM', self.usage(m['ram_used'], m['ram_total'], m['ram']), m['ram']),
            ('DISK', self.usage(m['disk_used'], m['disk_total'], m['disk']), m['disk']),
        ]
        bar_x = 34
        bar_width = max(8, min(36, width - bar_x - 8))
        y = gauge_y
        for label, value, percent in rows:
            self.put(y, 2, label, self.colors.blue, True)
            self.put(y, 8, value[:24], self.colors.normal)
            self.gauge(y, bar_x, bar_width, percent)
            y += 2
        y = summary_y
        if show_uptime:
            self.put(y, 2, 'UPTIME', self.colors.blue, True)
            self.put(y, 10, m['uptime'], self.colors.normal)
            y += 1
        self.put(y, 2, 'SERVICES', self.colors.blue, True)
        services_color = self.colors.bad if failed else self.colors.good
        x = 11
        first = f'{active} actifs'
        self.put(y, x, first, services_color, True)
        x += len(first) + 2
        second = f'· {failed} échec'
        self.put(y, x, second, self.colors.bad if failed else self.colors.dim)
        x += len(second) + 2
        if apps:
            self.put(y, x, f'· {apps} apps', self.colors.dim)
        y += 1
        self.put(y, 2, 'APACHE', self.colors.blue, True)
        if apache is None:
            self.put(y, 10, 'absent', self.colors.dim)
        else:
            color = self.colors.good if apache.active == 'active' else self.colors.bad
            self.put(y, 10, 'actif' if apache.active == 'active' else 'arrêté', color, True)
        y += 1
        self.put(y, 2, 'SITES', self.colors.blue, True)
        self.put(y, 10, f'{len(self.sites)} · {https} HTTPS', self.colors.normal)
        y += 1
        # CRON : total, gérés, état du démon. La ligne doit rester au-dessus du
        # message (height - 1), d'où la borne height - 2 : en 17 lignes la ligne
        # 16 est le message, le CRON s'efface.
        if y <= height - 2:
            self.put(y, 2, 'CRON', self.colors.blue, True)
            summary = f'{crons} · {managed} gérés'
            avail = max(0, width - 12)
            self.put(y, 11, shorten(summary, avail), self.colors.normal)
            state, state_color = self.cron_state_label()
            rest = avail - len(summary) - 3
            if len(summary) <= avail and rest >= len(state):
                self.put(y, 11 + len(summary), ' · ', self.colors.dim)
                self.put(y, 11 + len(summary) + 3, state, state_color, True)
        if self.service_error:
            if compact and height >= 20:
                self.put(17, 2, 'systemd indisponible', self.colors.bad, True)
            elif not compact and 20 < height - 1:
                self.put(20, 2, 'systemd indisponible', self.colors.bad, True)

    def cron_state_label(self) -> tuple[str, int]:
        if self.cron_service == 'active':
            return 'actif', self.colors.good
        if self.cron_service == 'inactive':
            return 'arrêté', self.colors.bad
        if self.cron_service == 'absent':
            return 'absent', self.colors.bad
        return '?', self.colors.dim

    def render_fastfetch(self, height: int, width: int, m: dict, start_y: int = 4):
        """Bloc « fastfetch » intégré : OS, HOST, KERNEL, UPTIME, IP seulement."""
        now = time.monotonic()
        # `not self._fastfetch_at` : sur un hôte démarré depuis moins de 30 s,
        # monotonic() < 30 et le premier chargement serait sinon reporté.
        if not self._fastfetch_at or now - self._fastfetch_at >= 30:
            self._fastfetch = fastfetch_info()
            self._fastfetch_at = now
        info = self._fastfetch
        last = height - 2
        if last < start_y:
            return
        # Jamais plus de height - 2 : le bloc ne touche pas la ligne de message.
        lines = last - start_y + 1
        logo = ('  ┌────────┐',
                '  │ ▪  ▪  ▪│',
                '  │ ▪  ▪  ▪│',
                '  └────────┘',
                '    DEBIAN')
        show_logo = width >= 72 and lines >= 5
        if show_logo:
            for offset, row in enumerate(logo):
                self.put(start_y + offset, 4, row, self.colors.blue, True)
        x = 20 if show_logo else 4
        label_width = 11
        value_width = max(1, width - 1 - (x + label_width))
        entries = [
            ('OS', info.get('os') or os_name()),
            ('HOST', info.get('host') or m['host']),
            ('KERNEL', info.get('kernel') or m['kernel'] or '—'),
            ('UPTIME', info.get('uptime') or m['uptime'] or '—'),
            ('IP', info.get('local ip') or info.get('localip') or local_ip() or '—'),
        ]
        for index, (label, value) in enumerate(entries[:min(lines, len(entries))]):
            y = start_y + index
            self.put(y, x, label[:label_width], self.colors.blue, True)
            self.put(y, x + label_width, shorten(str(value), value_width), self.colors.normal)

    def usage(self, used, total, percent) -> str:
        if percent is None or used is None or total is None:
            return '?'
        return f'{percent}% · {used:g} / {total:g} Go'

    def gauge(self, y: int, x: int, width: int, percent):
        percent = 0 if percent is None else max(0, min(100, int(percent)))
        filled = round(width * percent / 100)
        color = self.colors.bad if percent >= 90 else self.colors.warning if percent >= 75 else self.colors.good
        self.put(y, x, '█' * filled, color)
        self.put(y, x + filled, '░' * (width - filled), self.colors.dim)
        self.put(y, x + width + 1, f'{percent:>3}%', color, True)

    def render_message(self, height: int, width: int):
        if not self.message:
            return
        prefix = '! ' if self.message_error else '· '
        self.put(height - 1, 2, (prefix + self.message)[:width - 4],
                 self.colors.bad if self.message_error else self.colors.dim)

    # ----------------------------------------------------------------- modals

    def menu(self, title: str, options: list[str], default: int = 0,
             *, danger: bool = False) -> str | None:
        if not options:
            return None
        cursor = max(0, min(default, len(options) - 1))
        labels = [f'[{name}]' for name in options]
        joined = '  '.join(labels)
        self.blocking()
        try:
            while True:
                self.render()
                height, width = self.win.getmaxyx()
                if height < MIN_H or width < MIN_W:
                    return None
                needed = max(len(title) + 8, max(len(x) for x in labels) + 10, 46)
                box_width = min(width - 4, max(46, needed))
                # Comparer à la largeur réelle de la boîte (souvent plus étroite
                # que le terminal), sinon la ligne horizontale déborde du cadre.
                vertical = len(options) > 4 or len(joined) + 6 > box_width
                if not vertical:
                    # La ligne tient déjà dans box_width : élargir au besoin en
                    # restant borné à la largeur du terminal.
                    box_width = min(width - 4, max(box_width, len(joined) + 6))
                box_height = len(options) + 4 if vertical else 6
                y = max(1, (height - box_height) // 2)
                x = max(2, (width - box_width) // 2)
                tone = self.colors.bad if danger else self.colors.blue
                self.box(y, x, box_height, box_width, color=tone)
                self.put(y + 1, x + 3, title[:box_width - 6], tone, True)
                if vertical:
                    for index, label in enumerate(labels):
                        line_y = y + 2 + index
                        if index == cursor:
                            self.put(line_y, x + 2, f'{label:<{box_width - 4}}'[:box_width - 4],
                                     self.colors.selected, True)
                        else:
                            self.put(line_y, x + 3, label[:box_width - 6], self.colors.blue, True)
                    self.put(y + box_height - 2, x + 3, '↑ ↓  ENTRÉE  ESC', self.colors.dim)
                else:
                    offset = x + (box_width - len(joined)) // 2
                    for index, label in enumerate(labels):
                        self.put(y + 3, offset, label,
                                 self.colors.selected if index == cursor else self.colors.blue, True)
                        offset += len(label) + 2
                    self.put(y + 4, x + 3, '← →  ENTRÉE  ESC', self.colors.dim)
                self.win.refresh()
                key = self.win.getch()
                wheel = self.mouse_wheel_delta(key)
                if wheel:
                    cursor = max(0, min(len(options) - 1, cursor + wheel))
                elif key in (curses.KEY_LEFT, ord('h')):
                    cursor = (cursor - 1) % len(options)
                elif key in (curses.KEY_RIGHT, ord('l'), 9):
                    cursor = (cursor + 1) % len(options)
                elif key in (curses.KEY_UP, ord('k')):
                    cursor = (cursor - 1) % len(options)
                elif key in (curses.KEY_DOWN, ord('j')):
                    cursor = (cursor + 1) % len(options)
                elif key in KEY_ENTER:
                    return options[cursor]
                elif key in (27, ord('q'), ord('Q')):
                    return None
        finally:
            self.idle()

    def multi(self, title: str, options: list[str], default=()) -> list[str] | None:
        """Sélection multiple : ESPACE coche, ENTRÉE valide, ESC annule."""
        if not options:
            return None
        chosen = {option for option in default if option in options}
        cursor = 0
        self.blocking()
        try:
            while True:
                self.render()
                height, width = self.win.getmaxyx()
                if height < MIN_H or width < MIN_W:
                    return None
                box_height = len(options) + 5
                box_width = min(width - 4, max(52, len(title) + 8,
                                                max(len(option) for option in options) + 12))
                y = max(1, (height - box_height) // 2)
                x = max(2, (width - box_width) // 2)
                self.box(y, x, box_height, box_width, color=self.colors.blue)
                self.put(y + 1, x + 3, title[:box_width - 6], self.colors.blue, True)
                for index, option in enumerate(options):
                    line_y = y + 2 + index
                    mark = '◉' if option in chosen else '◯'
                    text = f'{mark} {option}'
                    if index == cursor:
                        self.put(line_y, x + 3, f'{text:<{box_width - 5}}'[:box_width - 5],
                                 self.colors.selected, True)
                    else:
                        color = self.colors.good if option in chosen else self.colors.normal
                        self.put(line_y, x + 3, text[:box_width - 6], color, option in chosen)
                hint = ('ESPACE Cocher   ENTRÉE Valider   ESC Annuler' if chosen
                        else 'Sélectionnez au moins un élément   ESC Annuler')
                self.put(y + box_height - 2, x + 3, hint[:box_width - 6], self.colors.dim)
                self.win.refresh()
                key = self.win.getch()
                wheel = self.mouse_wheel_delta(key)
                if wheel:
                    cursor = max(0, min(len(options) - 1, cursor + wheel))
                elif key in (curses.KEY_UP, ord('k')):
                    cursor = (cursor - 1) % len(options)
                elif key in (curses.KEY_DOWN, ord('j')):
                    cursor = (cursor + 1) % len(options)
                elif key == ord(' '):
                    chosen.symmetric_difference_update({options[cursor]})
                elif key in KEY_ENTER:
                    if chosen:
                        return [option for option in options if option in chosen]
                elif key in (27, ord('q'), ord('Q')):
                    return None
        finally:
            self.idle()

    def prompt(self, label: str, default: str = '', hint: str = '') -> str | None:
        buffer: list[str] = []
        pos = 0
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self.blocking()
        try:
            while True:
                self.render()
                height, width = self.win.getmaxyx()
                if height < MIN_H or width < MIN_W:
                    return None
                box_width = min(width - 4, max(48, len(label) + 12))
                box_height = 7
                y = max(1, (height - box_height) // 2)
                x = max(2, (width - box_width) // 2)
                self.box(y, x, box_height, box_width, color=self.colors.blue)
                self.put(y + 1, x + 3, label[:box_width - 6], self.colors.blue, True)
                self.put(y + 2, x + 3, '> ', self.colors.blue, True)
                field = box_width - 7
                start = max(0, pos - field)
                self.put(y + 2, x + 5, ''.join(buffer[start:start + field])[:field], self.colors.normal)
                detail = []
                if default:
                    detail.append(f'DÉFAUT {default}')
                if hint:
                    detail.append(hint)
                if detail:
                    self.put(y + 3, x + 3, ' · '.join(detail)[:box_width - 6], self.colors.dim)
                self.put(y + 4, x + 3, 'ENTRÉE Valider   ESC Annuler'[:box_width - 6], self.colors.dim)
                try:
                    self.win.move(y + 2, min(x + box_width - 3, x + 5 + pos - start))
                except curses.error:
                    pass
                self.win.refresh()
                try:
                    key = self.win.get_wch()
                except (curses.error, KeyboardInterrupt):
                    return None
                if key in ('\n', '\r', curses.KEY_ENTER):
                    result = ''.join(buffer).strip()
                    return result or default or None
                if key == '\x1b':
                    return None
                if key in ('\x7f', '\b', curses.KEY_BACKSPACE):
                    if pos:
                        buffer.pop(pos - 1)
                        pos -= 1
                elif key == curses.KEY_DC:
                    if pos < len(buffer):
                        buffer.pop(pos)
                elif key == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif key == curses.KEY_RIGHT:
                    pos = min(len(buffer), pos + 1)
                elif key == curses.KEY_HOME:
                    pos = 0
                elif key == curses.KEY_END:
                    pos = len(buffer)
                elif key == curses.KEY_MOUSE:
                    self.mouse_wheel_delta(key)
                elif isinstance(key, str) and key.isprintable() and len(buffer) < 200:
                    buffer.insert(pos, key)
                    pos += 1
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.idle()

    def confirm(self, title: str, *, danger: bool = True) -> bool:
        return self.menu(title, ['ANNULER', 'CONFIRMER'], danger=danger) == 'CONFIRMER'

    def operation(self, func: Callable, *args, success: str = '', **kwargs) -> bool:
        try:
            result = func(*args, **kwargs)
            self.refresh_data()
            self.flash(success or (str(result) if result else 'Terminé'))
            return True
        except (PanelError, OSError, ValueError) as exc:
            self.flash(str(exc), error=True)
            return False

    def viewer(self, title: str, loader: Callable[[], list[str]]):
        try:
            content = loader()
        except (PanelError, OSError) as exc:
            self.flash(str(exc), error=True)
            return
        offset = max(0, len(content) - max(1, self.win.getmaxyx()[0] - 6))
        self.blocking()
        try:
            while True:
                self.win.erase()
                height, width = self.win.getmaxyx()
                if height < 10 or width < 40:
                    self.center(1, 'AGRANDIR LE TERMINAL', self.colors.bad, True)
                else:
                    self.put(0, 2, title[:width - 4], self.colors.blue, True)
                    self.horizontal(1, 2, width - 4, self.colors.dim)
                    visible = max(1, height - 4)
                    offset = max(0, min(offset, max(0, len(content) - visible)))
                    for index, line in enumerate(content[offset:offset + visible]):
                        lowered = line.lower()
                        color = (self.colors.bad if any(t in lowered for t in ('error', 'failed', 'fatal', 'traceback'))
                                 else self.colors.warning if any(t in lowered for t in ('warning', 'warn', 'deprecated'))
                                 else self.colors.normal)
                        self.put(2 + index, 2, line.rstrip()[:width - 4], color)
                    self.put(height - 1, 2, '↑ ↓  PGUP/PGDN  R ACTUALISER  ESC RETOUR'[:width - 4],
                             self.colors.dim)
                self.win.refresh()
                key = self.win.getch()
                wheel = self.mouse_wheel_delta(key)
                if key in (27, ord('q'), ord('Q')):
                    return
                if wheel:
                    offset += wheel
                elif key in (curses.KEY_UP, ord('k')):
                    offset -= 1
                elif key in (curses.KEY_DOWN, ord('j')):
                    offset += 1
                elif key == curses.KEY_PPAGE:
                    offset -= max(1, height - 4)
                elif key == curses.KEY_NPAGE:
                    offset += max(1, height - 4)
                elif key == curses.KEY_HOME:
                    offset = 0
                elif key == curses.KEY_END:
                    offset = len(content)
                elif key in (ord('r'), ord('R')):
                    try:
                        content = loader()
                        offset = len(content)
                    except PanelError as exc:
                        self.flash(str(exc), error=True)
                elif key == curses.KEY_RESIZE:
                    continue
        finally:
            self.idle()

    # ---------------------------------------------------------------- actions

    def execute(self, action: str):
        rows = self.rows()
        if action == 'new':
            if self.tab == TAB_SERVICES:
                self.new_service()
            elif self.tab == TAB_SITES:
                self.new_site()
            elif self.tab == TAB_CRON:
                self.new_cron()
            return
        if not rows:
            return
        item = rows[min(self.index, len(rows) - 1)]
        if self.tab == TAB_SERVICES:
            self.service_action(item, action)
        elif self.tab == TAB_SITES:
            self.site_action(item, action)
        elif self.tab == TAB_CRON:
            self.cron_action(item, action)

    def service_action(self, item: services.Service, action: str):
        short = item.name.removesuffix('.service')
        system = item.origin != 'manual'
        if action in {'start', 'stop', 'restart'}:
            if system and not self.confirm(f'{action.upper()} {short} ?', danger=action != 'start'):
                return
            self.operation(services.action, item.name, action)
        elif action == 'logs':
            self.viewer(f'LOGS · {short}', lambda: services.logs(item.name))
        elif action == 'auto':
            enable = item.enabled != 'enabled'
            label = 'ACTIVER AU BOOT' if enable else 'DÉSACTIVER AU BOOT'
            if system and not self.confirm(f'{label} {short} ?', danger=not enable):
                return
            verb = 'enable' if enable else 'disable'

            def apply():
                # Config first: it is the source of truth, the next sync would revert us.
                services.set_autostart(item.name, enable)
                services.action(item.name, verb)

            self.operation(apply,
                           success=f'{short} · démarrage auto {"activé" if enable else "désactivé"}')
        elif action == 'pin':
            try:
                toggle_pin(item.name)
            except PanelError as exc:
                self.flash(str(exc), error=True)
            else:
                state = 'ajouté aux favoris' if short in pins() else 'retiré des favoris'
                self.flash(f'{short} · {state}')
        elif action == 'infos':
            self.service_infos(item)
        elif action == 'remove':
            if not item.managed:
                self.flash('Service système : suppression manuelle uniquement', error=True)
                return
            if self.confirm(f'SUPPRIMER {short} ?'):
                self.operation(services.remove, item.name, success=f'{short} supprimé')

    def site_action(self, item: sites.Site, action: str):
        if not item.managed:
            self.flash('Site externe : lecture seule', error=True)
            return
        if action == 'toggle':
            enabled = not item.enabled
            if not enabled and not self.confirm(f'DÉSACTIVER {item.domain} ?'):
                return
            self.operation(sites.toggle, item, enabled,
                           success=f'{item.domain} · {"activé" if enabled else "désactivé"}')
        elif action == 'https':
            if item.https:
                self.flash('HTTPS déjà actif')
                return
            email = self.prompt('E-MAIL LET’S ENCRYPT', hint='ex. admin@exemple.be')
            if email:
                self.operation(sites.certificate, item, email,
                               success=f'HTTPS activé · {item.domain}')
        elif action == 'logs':
            kind = self.menu('JOURNAL APACHE', ['ERREURS', 'ACCÈS'])
            if kind:
                name = 'error' if kind == 'ERREURS' else 'access'
                self.viewer(f'{item.domain} · {kind}',
                            lambda: sites.file_logs(item, name))
        elif action == 'remove':
            if self.confirm(f'SUPPRIMER {item.domain} ?'):
                self.operation(sites.remove, item, success=f'{item.domain} supprimé')

    def new_service(self):
        slug = self.prompt('NOUVEAU SERVICE', hint='identifiant · a-z 0-9 -')
        if not slug:
            return
        folder = self.prompt('DOSSIER DU CODE', f'/srv/apps/{slug}', 'ex. /srv/apps/mon-bot')
        if not folder:
            return
        command = self.prompt('COMMANDE', hint='ex. npm run start · python3 bot.py')
        if not command:
            return
        boot = self.menu('DÉMARRAGE AUTOMATIQUE ?', ['NON', 'OUI'], default=1)
        if boot is None:
            return
        try:
            services.create(slug, command, folder, autostart=boot == 'OUI')
            self.refresh_data()
            self.flash(f'OK! Service créé · panel-{slug}.service')
        except PanelError as exc:
            msg = str(exc)
            self.refresh_data()
            if msg.startswith('Service créé'):
                self.flash(msg)  # start failed but service exists — warning not error
            else:
                self.flash(msg, error=True)

    def service_infos(self, item: services.Service):
        """Show a popup with saved infos; allow editing command and folder."""
        short = item.name.removesuffix('.service')
        slug = short.removeprefix('panel-')
        fields = [
            ('NOM',         short,               False),
            ('COMMANDE',    item.custom_command,  True),
            ('DOSSIER',     item.custom_folder,   True),
            ('UTILISATEUR', item.custom_user or '-', False),
        ]
        cursor = 0
        self.blocking()
        try:
            while True:
                self.render()
                height, width = self.win.getmaxyx()
                if height < MIN_H or width < MIN_W:
                    return
                box_h = len(fields) + 4
                box_w = min(width - 4, max(52, 10 + max(len(v) for _, v, _ in fields) + 4))
                by = max(1, (height - box_h) // 2)
                bx = max(2, (width - box_w) // 2)
                self.box(by, bx, box_h, box_w, title=f'INFOS · {short}', color=self.colors.blue)
                for i, (label, value, editable) in enumerate(fields):
                    fy = by + 2 + i
                    lbl = f'{label}: '
                    val = shorten(value or '(vide)', box_w - len(lbl) - 6)
                    if i == cursor:
                        self.put(fy, bx + 2, f'{lbl}{val}', self.colors.selected, True)
                    else:
                        self.put(fy, bx + 2, lbl, self.colors.blue, True)
                        self.put(fy, bx + 2 + len(lbl), val,
                                 self.colors.normal if editable else self.colors.dim)
                hint = 'ENTRÉE Modifier   ↑↓ Naviguer   ESC Fermer'
                self.put(by + box_h - 2, bx + 2, hint[:box_w - 4], self.colors.dim)
                self.win.refresh()
                key = self.win.getch()
                if key in (27, ord('q'), ord('Q')):
                    return
                if key in (curses.KEY_UP, ord('k')):
                    cursor = (cursor - 1) % len(fields)
                elif key in (curses.KEY_DOWN, ord('j')):
                    cursor = (cursor + 1) % len(fields)
                elif key in KEY_ENTER:
                    label, value, editable = fields[cursor]
                    if not editable:
                        continue
                    new_val = self.prompt(f'MODIFIER {label}', default=value)
                    if new_val is None:
                        continue
                    fields[cursor] = (label, new_val, editable)
                    cmd = fields[1][1]
                    fld = fields[2][1]
                    try:
                        services.update_custom_service(slug, cmd, fld)
                        self.refresh_data()
                        rows = self.rows()
                        updated = next((s for s in rows if s.name == item.name), None)
                        if updated:
                            fields[1] = ('COMMANDE', updated.custom_command, True)
                            fields[2] = ('DOSSIER',  updated.custom_folder,  True)
                        self.flash(f'{label.lower()} mis à jour · .service rechargé')
                        if self.menu(f'REDÉMARRER {short} maintenant ?', ['NON', 'OUI'], default=1) == 'OUI':
                            self.operation(services.action, item.name, 'restart',
                                           success=f'{short} redémarré')
                    except PanelError as exc:
                        self.flash(str(exc), error=True)
        finally:
            self.idle()

    def new_site(self):
        domain = self.prompt('NOUVEAU SITE', hint='ex. exemple.be')
        if not domain:
            return
        kind = self.menu('TYPE DE SITE', ['STATIQUE', 'PROXY'], default=0)
        if kind is None:
            return
        options: dict = {}
        if kind == 'STATIQUE':
            path = self.prompt('DOSSIER WEB', f'/var/www/{domain}/', 'sous /var/www/')
            if not path:
                return
            options['path'] = path
            options['htaccess'] = True
        else:
            port = self.prompt('PORT LOCAL', '3000', 'backend sur 127.0.0.1')
            if not port:
                return
            try:
                options['proxy_port'] = int(port)
            except ValueError:
                self.flash('Port numérique requis', error=True)
                return
        www = self.menu('ALIAS WWW ?', ['NON', 'OUI'], default=0)
        if www is None:
            return
        options['www'] = www == 'OUI'
        if not self.operation(sites.create, domain, **options, success=f'Site créé · {domain}'):
            return
        https = self.menu(f'HTTPS POUR {domain} ?', ['NON', 'OUI'], default=1)
        if https != 'OUI':
            return
        email = self.prompt('E-MAIL LET’S ENCRYPT', hint='ex. admin@exemple.be')
        if not email:
            return
        created = next((site for site in self.sites
                        if site.domain == domain.lower().rstrip('.')), None)
        if created:
            self.operation(sites.certificate, created, email,
                           success=f'HTTPS activé · {created.domain}')

    # ------------------------------------------------------------------- cron

    def new_cron(self):
        """Assistant de création : commande, nom, planification, utilisateur."""
        if not cron.available():
            self.flash('Cron non installé · relance : sudo bash install.sh', error=True)
            return
        command = self.prompt('COMMANDE', hint='/usr/local/bin/backup.sh')
        if not command:
            return
        label = self.prompt('NOM DU CRON', cron.default_label(command),
                            'Entrée accepte le défaut')
        if not label:
            return
        schedule = self.cron_schedule_wizard()
        if not schedule:
            return
        user = self.prompt('UTILISATEUR', 'root', 'compte système')
        if not user:
            return
        when = cron.describe(schedule)
        if self.menu(f'CRÉER · {when}', ['ANNULER', 'CONFIRMER'], danger=False) != 'CONFIRMER':
            return
        try:
            slug = cron.add(label, command, schedule, user)
        except PanelError as exc:
            self.flash(str(exc), error=True)
            return
        self.refresh_data()
        self.focus = 'list'
        rows = self.rows()
        position = next((index for index, job in enumerate(rows) if job.slug == slug), None)
        if position is not None:
            self.index = position
            self.clamp_list_scroll(len(rows), self.list_visible())
        self.flash(f'Cron créé · {when}')

    def cron_schedule_wizard(self, current: str | None = None) -> str | None:
        """Assistant de planification ; préremplit tout depuis `current`."""
        parsed = cron.parse_schedule(current or '')
        kind = parsed['kind'] if parsed['kind'] in CRON_KIND_KEYS else 'custom'
        choice = self.menu('QUAND EXÉCUTER ?', list(CRON_KINDS),
                           default=CRON_KIND_KEYS.index(kind))
        if choice is None:
            return None
        kind = CRON_KIND_KEYS[CRON_KINDS.index(choice)]
        hour, minute = parsed['hour'], parsed['minute']
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            hour, minute = 3, 0
        clock = f'{hour:02d}:{minute:02d}'
        if kind == 'daily':
            if parsed['kind'] != 'daily':
                clock = '03:00'
            time = self.prompt_time(clock)
            return cron.schedule_daily(*time) if time else None
        if kind == 'weekly':
            picked = self.multi(
                'JOURS DE LA SEMAINE', list(WEEKDAY_LABELS),
                default=[WEEKDAY_LABELS[index] for index, value in enumerate(WEEKDAY_VALUES)
                         if parsed['kind'] == 'weekly' and value in parsed['days']])
            if picked is None:
                return None
            days = [WEEKDAY_VALUES[WEEKDAY_LABELS.index(name)] for name in picked]
            time = self.prompt_time(clock if parsed['kind'] == 'weekly' else '03:00')
            if not time:
                return None
            return cron.schedule_weekly(days, time[0], time[1])
        if kind == 'monthly':
            days = parsed['days'] if parsed['kind'] == 'monthly' else []
            chosen = self.prompt_month_days(','.join(str(day) for day in days) or '1')
            if not chosen:
                return None
            time = self.prompt_time(clock if parsed['kind'] == 'monthly' else '03:00')
            if not time:
                return None
            return cron.schedule_monthly(chosen, time[0], time[1])
        if kind == 'hours':
            interval = parsed['interval'] if parsed['kind'] == 'hours' else 6
            step = self.prompt_int('HEURES', interval, 1, 23, 'toutes les N heures')
            if not step:
                return None
            minute_value = self.prompt_int('MINUTE', parsed['minute'], 0, 59, 'minutes 0-59')
            if minute_value is None:
                return None
            return cron.schedule_every_hours(step, minute_value)
        if kind == 'minutes':
            interval = parsed['interval'] if parsed['kind'] == 'minutes' else 15
            step = self.prompt_int('MINUTES', interval, 1, 59, 'toutes les N minutes')
            return cron.schedule_every_minutes(step) if step else None
        fields = parsed['fields']
        while True:
            values = []
            for label, key, hint in CRON_FIELDS:
                text = self.prompt(label, fields.get(key, '*'), hint)
                if not text:
                    return None
                values.append(text)
            try:
                return cron.schedule_custom(*values)
            except PanelError as exc:
                self.flash(str(exc), error=True)

    def prompt_time(self, default: str = '03:00') -> tuple[int, int] | None:
        while True:
            text = self.prompt('HEURE (HH:MM)', default, 'ex. 03:30')
            if not text:
                return None
            try:
                return cron.parse_hhmm(text)
            except PanelError as exc:
                self.flash(str(exc), error=True)

    def prompt_int(self, label: str, default: int, low: int, high: int, hint: str = '') -> int | None:
        while True:
            text = self.prompt(f'{label} ({low}-{high})', str(default), hint)
            if not text:
                return None
            value = str(text).strip()
            if not value.isdigit():
                self.flash(f'{label} : nombre attendu ({low}-{high})', error=True)
                continue
            number = int(value)
            if not low <= number <= high:
                self.flash(f'{label} hors bornes ({low}-{high})', error=True)
                continue
            return number

    def prompt_month_days(self, default: str = '1') -> list[int] | None:
        while True:
            text = self.prompt('JOUR DU MOIS', default, 'ex. 1 ou 1,15')
            if not text:
                return None
            days = []
            try:
                for item in str(text).split(','):
                    item = item.strip()
                    if not item.isdigit():
                        raise PanelError('Jour du mois invalide (ex. 1 ou 1,15)')
                    number = int(item)
                    if not 1 <= number <= 31:
                        raise PanelError('Jour du mois hors bornes (1-31)')
                    days.append(number)
            except PanelError as exc:
                self.flash(str(exc), error=True)
                continue
            if days:
                return sorted(set(days))

    def edit_cron(self, item: cron.CronJob):
        if not item.managed:
            self.flash('Cron externe : lecture seule', error=True)
            return
        choice = self.menu(f'MODIFIER · {item.label}', ['COMMANDE', 'MOMENT', 'UTILISATEUR', 'NOM'])
        if choice is None:
            return
        if choice == 'COMMANDE':
            command = self.prompt('COMMANDE', item.command)
            if command:
                self.cron_update(item, command=command)
        elif choice == 'MOMENT':
            schedule = self.cron_schedule_wizard(item.schedule)
            if schedule:
                self.cron_update(item, schedule=schedule)
        elif choice == 'UTILISATEUR':
            user = self.prompt('UTILISATEUR', item.user or 'root', 'compte système')
            if user:
                self.cron_update(item, user=user)
        else:
            label = self.prompt('NOM DU CRON', item.label)
            if label:
                self.cron_update(item, label=label)

    def cron_update(self, item: cron.CronJob, **fields) -> bool:
        try:
            cron.update(item.slug, **fields)
        except PanelError as exc:
            self.flash(str(exc), error=True)
            return False
        self.refresh_data()
        self.flash(f'{item.label} · mis à jour')
        return True

    def cron_action(self, item: cron.CronJob, action: str):
        if action == 'infos':
            self.viewer(f'CRON · {item.label}', lambda: cron.detail_lines(item))
            return
        if action == 'logs':
            self.viewer('JOURNAL CRON', cron.logs)
            return
        if action == 'pin':
            # Épinglable quel que soit le cron : les externes restent en lecture seule.
            try:
                pinned = cron.toggle_pin(item)
            except PanelError as exc:
                self.flash(str(exc), error=True)
                return
            self.refresh_data()
            self.flash(f'{item.label} · '
                       f'{"ajouté aux favoris" if pinned else "retiré des favoris"}')
            return
        if not item.managed:
            self.flash('Cron externe : lecture seule', error=True)
            return
        if action == 'edit':
            self.edit_cron(item)
        elif action == 'run':
            if not self.confirm(f'EXÉCUTER {item.label} ?'):
                return
            self.viewer(f'EXÉCUTION · {item.label}', lambda: cron.run_now(item))
        elif action == 'toggle':
            enabled = not item.enabled
            try:
                cron.set_enabled(item.slug, enabled)
            except PanelError as exc:
                self.flash(str(exc), error=True)
                return
            self.refresh_data()
            self.flash(f'{item.label} · {"activé" if enabled else "désactivé"}')
        elif action == 'remove':
            if not self.confirm(f'SUPPRIMER {item.label} ?'):
                return
            try:
                cron.remove(item.slug)
            except PanelError as exc:
                self.flash(str(exc), error=True)
                return
            self.refresh_data()
            self.index = max(0, min(self.index, len(self.rows()) - 1))
            self.flash(f'{item.label} supprimé')

    # ------------------------------------------------------------------- loop

    def move_horizontal(self, delta: int):
        if delta < 0:
            if self.focus == 'actions':
                if self.tab == TAB_SITES and self.action_index > 0:
                    self.action_index -= 1
                else:
                    self.focus = 'list'
            elif self.focus == 'tabs':
                self.change_tab(-1)
            return
        if self.focus == 'tabs':
            self.change_tab(1)
            return
        if self.focus == 'list' and self.tab != TAB_VPS:
            # SERVICES, SITES et CRON : la liste ouvre les actions en horizontal.
            actions = self.actions()
            if actions:
                self.focus = 'actions'
                self.action_index = 0
            return
        if self.focus == 'actions' and self.tab == TAB_SITES:
            # Le cycle horizontal des actions reste réservé aux SITES.
            count = len(self.actions())
            if count:
                self.action_index = min(count - 1, self.action_index + 1)

    def move_vertical(self, delta: int):
        if self.focus == 'actions':
            if self.tab == TAB_SITES:
                return
            count = len(self.actions())
            if count:
                self.action_index = (self.action_index + delta) % count
            return
        if self.focus == 'tabs':
            if delta > 0 and self.tab != TAB_VPS:
                self.focus = 'list'
            return
        if delta < 0:
            if self.index > 0:
                self.index -= 1
            else:
                self.focus = 'tabs'
            return
        self.index = min(max(0, len(self.rows()) - 1), self.index + 1)

    def handle_enter(self):
        if self.focus == 'tabs':
            if self.tab != TAB_VPS:
                self.focus = 'list'
            return
        if self.tab == TAB_VPS:
            return
        actions = self.actions()
        if not actions:
            return
        rows = self.rows()
        if not rows:
            self.execute(actions[0][1])
            return
        if self.tab == TAB_SITES and not rows[min(self.index, len(rows) - 1)].managed:
            self.flash('Site externe : lecture seule', error=True)
            return
        if self.focus == 'list':
            self.focus = 'actions'
            self.action_index = 0
        else:
            self.execute(actions[min(self.action_index, len(actions) - 1)][1])

    def run(self):
        self.setup()
        self.idle()
        self.refresh_data()
        while True:
            self.render()
            key = self.win.getch()
            if key == -1:
                self.refresh_data()
                continue
            wheel = self.mouse_wheel_delta(key)
            if wheel:
                self.scroll_list(wheel)
                continue
            if key in (ord('q'), ord('Q')):
                return
            if key in (ord('1'), ord('2'), ord('3'), ord('4')):
                self.set_tab(key - ord('1'))
            elif key in (curses.KEY_LEFT, ord('h')):
                self.move_horizontal(-1)
            elif key in (curses.KEY_RIGHT, ord('l')):
                self.move_horizontal(1)
            elif key in (curses.KEY_UP, ord('k')):
                self.move_vertical(-1)
            elif key in (curses.KEY_DOWN, ord('j')):
                self.move_vertical(1)
            elif key in KEY_ENTER:
                self.handle_enter()
            elif key == 9:
                if self.focus == 'tabs':
                    if self.tab != TAB_VPS:
                        self.focus = 'list'
                elif self.tab in (TAB_SERVICES, TAB_SITES, TAB_CRON) and self.actions():
                    self.focus = 'actions' if self.focus == 'list' else 'list'
            elif key in (ord('r'), ord('R')):
                self.refresh_data()
                self.flash('Actualisé')
            elif key in (ord('n'), ord('N')):
                if self.tab == TAB_SERVICES:
                    self.new_service()
                elif self.tab == TAB_SITES:
                    self.new_site()
                elif self.tab == TAB_CRON:
                    self.new_cron()
            elif key in (ord('a'), ord('A')) and self.tab in (TAB_SERVICES, TAB_CRON):
                if self.tab == TAB_SERVICES:
                    self.all_units = not self.all_units
                else:
                    self.cron_all = not self.cron_all
                self.index = self.scroll = 0
                self.focus = 'list'
            elif key in (ord('f'), ord('F')) and self.tab in (TAB_SERVICES, TAB_CRON):
                rows = self.rows()
                if rows:
                    item = rows[min(self.index, len(rows) - 1)]
                    if self.tab == TAB_SERVICES:
                        self.service_action(item, 'pin')
                    else:
                        self.cron_action(item, 'pin')
            elif key == 27:
                if self.focus == 'actions':
                    self.focus = 'list'
                elif self.focus == 'list':
                    self.focus = 'tabs'
                elif self.focus == 'tabs' and self.tab != TAB_VPS:
                    self.set_tab(TAB_VPS)
            elif key == curses.KEY_RESIZE:
                continue



def launch():
    curses.wrapper(lambda stdscr: Panel(stdscr).run())
