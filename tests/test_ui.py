import unittest
from unittest import mock

from serverpanel import ui, services, sites, cron


class DummyWindow:
    def __init__(self, h=24, w=100, inputs=()):
        self.h, self.w = h, w
        self.inputs = iter(inputs)
        self.chunks = []
    def getmaxyx(self):
        return self.h, self.w
    def addnstr(self, y, x, text, limit, attr):
        self.chunks.append((y, x, str(text)[:limit], attr))
    def erase(self):
        self.chunks.clear()
    def refresh(self):
        pass
    def getch(self):
        return next(self.inputs)
    def keypad(self, value):
        pass
    def move(self, y, x):
        pass
    def get_wch(self):
        return next(self.inputs)
    def timeout(self, value):
        pass


class Tests(unittest.TestCase):
    def get_panel(self, width=100):
        panel = ui.Panel(DummyWindow(w=width))
        panel.services = [services.Service('panel-bot.service', 'active', 'enabled',
                                           fragment='/etc/systemd/system/panel-bot.service', memory=1),
                          services.Service('apache2.service', 'failed', 'enabled',
                                           fragment='/usr/lib/systemd/system/apache2.service')]
        panel.sites = [sites.Site('example.be', 'panel-example.be.conf', '/var/www/example', True, True, True, True)]
        panel.set_tab(ui.TAB_SERVICES)
        return panel

    def text(self, panel):
        return ' '.join(x[2] for x in panel.win.chunks)

    def service_index(self, panel, name):
        # Le tri des services bouge : on retrouve l'index réel au lieu de le coder en dur.
        with mock.patch.object(ui, 'pins', return_value=[]):
            names = [item.name for item in panel.rows()]
        return names.index(name)

    def test_dashboard_sparse(self):
        panel = self.get_panel()
        with mock.patch.object(ui, 'pins', return_value=['apache2']):
            panel.render()
        joined = self.text(panel)
        self.assertIn('PANEL', joined)
        self.assertIn('panel-bot.service', joined)
        self.assertIn('[SERVICES]', joined)
        self.assertNotIn('DÉMONSTRATION', joined)

    def test_services_actions_sidebar(self):
        panel = self.get_panel()
        with mock.patch.object(ui, 'pins', return_value=['apache2']):
            panel.render()
        joined = self.text(panel)
        self.assertIn('ACTIONS', joined)
        for label in ('DÉMARRER', 'STOPPER', 'RESTART', 'LOGS', 'ACTIVÉ AU BOOT'):
            self.assertIn(label, joined)
        self.assertIn('ACTIF', joined)
        self.assertIn('ÉCHEC', joined)

    def test_site_actions_navigate_with_arrows(self):
        panel = self.get_panel()
        panel.sites = [sites.Site('anthonyuno.be', 'panel-anthonyuno.be.conf',
                                  '/var/www/anthonyuno', True, False, True, True)]
        panel.set_tab(ui.TAB_SITES)
        panel.focus = 'list'
        panel.move_horizontal(1)
        self.assertEqual(panel.focus, 'actions')
        self.assertEqual(panel.actions()[panel.action_index][1], 'toggle')
        panel.move_horizontal(1)
        self.assertEqual(panel.actions()[panel.action_index][1], 'https')
        panel.move_horizontal(1)
        self.assertEqual(panel.actions()[panel.action_index][1], 'logs')
        panel.move_horizontal(-1)
        self.assertEqual(panel.actions()[panel.action_index][1], 'https')
        panel.move_horizontal(-1)
        panel.move_horizontal(-1)
        self.assertEqual(panel.focus, 'list')
        panel.move_horizontal(1)
        panel.move_horizontal(1)
        self.assertEqual(panel.actions()[panel.action_index][1], 'https')
        panel.move_vertical(-1)
        panel.move_vertical(1)
        self.assertEqual(panel.focus, 'actions')
        self.assertEqual(panel.actions()[panel.action_index][1], 'https')
        panel.render()
        chips = [(y, x, text) for y, x, text, _style in panel.win.chunks
                 if text.startswith('[') and any(label in text for label in
                                                ('DÉSACTIVER', 'HTTPS', 'LOGS', 'SUPPRIMER'))]
        xs = [x for _y, x, _text in chips]
        self.assertGreaterEqual(len(chips), 4)
        self.assertEqual(len(set(xs)), len(chips))

    def test_enter_moves_focus_then_executes(self):
        panel = self.get_panel()
        panel.focus = 'list'
        panel.index = self.service_index(panel, 'panel-bot.service')
        with mock.patch.object(ui, 'pins', return_value=[]), \
             mock.patch.object(ui.services, 'action') as action, \
             mock.patch.object(ui.Panel, 'refresh_data'):
            panel.handle_enter()
            self.assertEqual(panel.focus, 'actions')
            self.assertEqual(panel.action_index, 0)
            panel.handle_enter()
        action.assert_called_once_with('panel-bot.service', 'start')

    def test_system_service_requires_confirmation(self):
        panel = self.get_panel()
        panel.index = self.service_index(panel, 'apache2.service')
        panel.focus = 'actions'
        panel.action_index = 1  # STOPPER
        with mock.patch.object(ui, 'pins', return_value=[]), \
             mock.patch.object(panel, 'confirm', return_value=False) as confirm, \
             mock.patch.object(ui.services, 'action') as action:
            panel.handle_enter()
        confirm.assert_called_once()
        action.assert_not_called()

    def test_service_rows_auto_selection(self):
        panel = self.get_panel()
        panel.services = [
            services.Service('manual.service', 'inactive', 'disabled',
                             fragment='/etc/systemd/system/manual.service'),
            services.Service('heavy.service', 'active', 'disabled',
                             fragment='/usr/lib/systemd/system/heavy.service', memory=900),
            services.Service('idle.service', 'inactive', 'disabled',
                             fragment='/usr/lib/systemd/system/idle.service'),
            services.Service('boot.service', 'active', 'enabled',
                             fragment='/usr/lib/systemd/system/boot.service', memory=1),
            services.Service('failed.service', 'failed', 'disabled',
                             fragment='/usr/lib/systemd/system/failed.service'),
        ]
        with mock.patch.object(ui, 'pins', return_value=[]):
            names = [s.name for s in panel.service_rows()]
        # Actifs/échec triés par nom, puis les arrêtés (manual.service).
        self.assertEqual(names, ['boot.service', 'failed.service', 'heavy.service', 'manual.service'])
        # Sélection verrouillée : les 4 visibles, jamais idle.service.
        self.assertEqual(set(names), {'boot.service', 'failed.service', 'heavy.service', 'manual.service'})
        self.assertNotIn('idle.service', names)

    def test_pin_action(self):
        panel = self.get_panel()
        item = panel.services[1]
        with mock.patch.object(ui, 'toggle_pin') as pin, \
             mock.patch.object(ui, 'pins', return_value=[]):
            panel.service_action(item, 'pin')
        pin.assert_called_once_with('apache2.service')

    def test_small_service_action_has_no_confirmation(self):
        panel = self.get_panel()
        panel.index = self.service_index(panel, 'panel-bot.service')
        panel.focus = 'actions'
        panel.action_index = 1  # STOPPER
        with mock.patch.object(ui, 'pins', return_value=[]), \
             mock.patch.object(panel, 'confirm') as confirm, \
             mock.patch.object(ui.services, 'action') as action, \
             mock.patch.object(ui.Panel, 'refresh_data'):
            panel.handle_enter()
        confirm.assert_not_called()
        action.assert_called_once_with('panel-bot.service', 'stop')

    def test_tabs_order_and_default(self):
        self.assertEqual(ui.TABS, ('VPS', 'SERVICES', 'SITES', 'CRON'))
        self.assertEqual((ui.TAB_VPS, ui.TAB_SERVICES, ui.TAB_SITES, ui.TAB_CRON),
                         (0, 1, 2, 3))
        self.assertEqual(ui.Panel(DummyWindow()).tab, ui.TAB_VPS)

    def test_startup_focuses_vps_tab(self):
        panel = ui.Panel(DummyWindow())
        self.assertEqual((panel.tab, panel.focus), (ui.TAB_VPS, 'tabs'))
        # En-tête : [VPS] porte le style sélectionné, les autres sont atténués.
        panel.colors.selected, panel.colors.dim = 1, 2
        with mock.patch.object(ui, 'fastfetch_info', return_value={}):
            panel.render()
        tabs = {text: attr for y, _x, text, attr in panel.win.chunks if y == 1}
        self.assertIn('[VPS]', tabs)
        self.assertEqual(tabs['[VPS]'], panel.colors.selected | ui.curses.A_BOLD)
        self.assertEqual(tabs['[SERVICES]'], panel.colors.dim | ui.curses.A_BOLD)

    def test_set_tab_focus_follows_vps_rule(self):
        panel = ui.Panel(DummyWindow())
        panel.set_tab(ui.TAB_SERVICES)
        self.assertEqual((panel.tab, panel.focus), (ui.TAB_SERVICES, 'list'))
        panel.set_tab(ui.TAB_VPS)
        self.assertEqual((panel.tab, panel.focus), (ui.TAB_VPS, 'tabs'))
        # ESC depuis les onglets : retour sur VPS avec le focus sur les onglets.
        panel.set_tab(ui.TAB_CRON)
        panel.focus = 'tabs'
        panel.win.inputs = iter([27, ord('q')])
        with mock.patch.object(ui.Panel, 'setup'), \
             mock.patch.object(ui.Panel, 'refresh_data'), \
             mock.patch.object(ui, 'fastfetch_info', return_value={}):
            panel.run()
        self.assertEqual((panel.tab, panel.focus), (ui.TAB_VPS, 'tabs'))
        # Réaffecter l'onglet courant ne casse pas le focus.
        panel.set_tab(ui.TAB_VPS)
        self.assertEqual(panel.focus, 'tabs')

    def test_move_vertical_from_tabs(self):
        panel = ui.Panel(DummyWindow())
        self.assertEqual(panel.focus, 'tabs')
        panel.move_vertical(1)
        self.assertEqual(panel.focus, 'tabs', 'VPS : ↓ ne quitte pas les onglets')
        panel.move_vertical(-1)
        self.assertEqual(panel.focus, 'tabs')
        panel.handle_enter()
        self.assertEqual(panel.focus, 'tabs', 'VPS : Entrée ne quitte pas les onglets')
        panel.set_tab(ui.TAB_SERVICES)
        panel.focus = 'tabs'
        panel.move_vertical(1)
        self.assertEqual(panel.focus, 'list', 'SERVICES : ↓ entre dans la liste')
        panel.focus = 'tabs'
        panel.handle_enter()
        self.assertEqual(panel.focus, 'list', 'SERVICES : Entrée entre dans la liste')

    def test_tabs_vps_and_sites(self):
        panel = self.get_panel()
        panel.set_tab(ui.TAB_VPS)
        self.assertEqual(panel.tab, ui.TAB_VPS)
        panel.render()
        joined = self.text(panel)
        for label in ('SYSTÈME', 'RAM', 'DISK', 'UPTIME', 'SERVICES', 'CRON', 'OS', 'KERNEL'):
            self.assertIn(label, joined)
        panel.set_tab(ui.TAB_SITES)
        vhost = '<VirtualHost *:80>\n    ServerName example.be\n    DocumentRoot "/var/www/example/"\n</VirtualHost>\n'
        with mock.patch.object(ui.sites, 'config_text', return_value=vhost):
            panel.render()
        joined = self.text(panel)
        self.assertIn('example.be', joined)
        self.assertIn('HTTPS', joined)
        self.assertIn('NOUVEAU', joined)
        self.assertIn('APACHE', joined)
        self.assertIn('panel-example.be.conf', joined)
        self.assertIn('DocumentRoot', joined)
        self.assertIn('DÉSACTIVER', joined)
        self.assertIn('HTTPS', joined)
        self.assertIn('LOGS', joined)
        self.assertIn('SUPPRIMER', joined)

    def test_sites_sidebar_is_proportional_on_wide_terminal(self):
        # Terminal large : le panneau latéral (ACTIONS + APACHE) fait 40 % de la
        # largeur, sans plafond à 84 colonnes, comme SERVICES et CRON.
        width = 194
        panel = self.get_panel(width=width)  # hauteur par défaut : 24 lignes
        self.assertEqual(panel.win.getmaxyx(), (24, width))
        panel.set_tab(ui.TAB_SITES)
        vhost = '<VirtualHost *:80>\n    ServerName example.be\n    DocumentRoot "/var/www/example/"\n</VirtualHost>\n'
        with mock.patch.object(ui.sites, 'config_text', return_value=vhost):
            panel.render()

        separators = [(y, x) for y, x, text, _style in panel.win.chunks if text == '│']
        self.assertTrue(separators, 'le séparateur vertical doit être dessiné')
        self.assertEqual(len({x for _y, x in separators}), 1, 'un seul séparateur vertical')
        separator = separators[0][1]
        side_width = width - separator - 4  # side_x = separator + 2, marge droite = 2
        self.assertEqual(side_width, panel.sidebar_width(width))
        self.assertEqual(side_width, round(width * 0.40))
        self.assertEqual((separator, side_width), (112, 78))
        list_width = separator - 2
        self.assertGreaterEqual(list_width, 108, 'la liste garde ~112 colonnes')
        self.assertEqual((side_width, list_width), (78, 110))

        joined = self.text(panel)
        for label in ('ACTIONS', 'APACHE', 'example.be', 'panel-example.be.conf'):
            self.assertIn(label, joined)

        # Chips d'actions : tous dans la colonne du panneau, jamais au-delà.
        side_x = separator + 2
        chips = [(y, x, text) for y, x, text, _style in panel.win.chunks
                 if text.startswith('[') and y >= 5 and x >= side_x]
        self.assertEqual(len(chips), 4, 'ACTIVER / HTTPS / LOGS / SUPPRIMER')
        for _y, x, text in chips:
            self.assertLessEqual(x + len(text), side_x + side_width)

        # La configuration Apache est rendue dans le panneau (x = side_x).
        config = [(y, x, text) for y, x, text, _style in panel.win.chunks
                  if x == side_x and 'ServerName' in text]
        self.assertTrue(config, 'la config Apache doit remplir le panneau')

        # La colonne cible et le domaine restent dans la zone de la liste.
        for y, x, text, _style in panel.win.chunks:
            if text.startswith('example.be'):
                self.assertEqual(x, 4)
                self.assertLessEqual(x + len(text), separator - 1)
            if text.startswith('/var/www/example'):
                self.assertLessEqual(x + len(text), separator - 1)
        for _y, x, text, _style in panel.win.chunks:
            self.assertLessEqual(x + len(text), width - 1)

    def test_sites_sidebar_narrower_gains_list_space(self):
        # À 100 colonnes, l'ancienne formule donnait 68 colonnes au panneau
        # (liste = 26). Les 40 % laissent le panneau à 40 (liste = 54).
        width = 100
        panel = self.get_panel(width=width)
        panel.set_tab(ui.TAB_SITES)
        vhost = '<VirtualHost *:80>\n    ServerName example.be\n</VirtualHost>\n'
        with mock.patch.object(ui.sites, 'config_text', return_value=vhost):
            panel.render()
        separators = [x for _y, x, text, _style in panel.win.chunks if text == '│']
        self.assertTrue(separators)
        separator = separators[0]
        side_width = width - separator - 4
        list_width = separator - 2
        old_side = max(36, width - 28 - 4)  # ancienne formule (list_min = 28)
        old_list = width - old_side - 6     # old_separator - 2
        self.assertEqual(old_side, 68)
        self.assertEqual((side_width, list_width), (40, 54))
        self.assertEqual(old_list, 26)
        self.assertGreater(list_width, old_list, 'la liste gagne de la place')
        self.assertIn('APACHE', self.text(panel))
        for _y, x, text, _style in panel.win.chunks:
            self.assertLessEqual(x + len(text), width - 1)

    def test_menu_keyboard(self):
        panel = self.get_panel()
        panel.win.inputs = iter((ui.curses.KEY_RIGHT, ui.curses.KEY_RIGHT, ui.curses.KEY_LEFT, 10))
        self.assertEqual(panel.menu('TEST', ['UN', 'DEUX', 'TROIS']), 'DEUX')

    def test_menu_escape(self):
        panel = self.get_panel()
        panel.win.inputs = iter([27])
        self.assertIsNone(panel.menu('TEST', ['UN']))

    def test_prompt_editor_and_escape(self):
        panel = self.get_panel()
        with mock.patch.object(ui.curses, 'curs_set'):
            panel.win.inputs = iter(['n', 'o', 'd', 'x', ui.curses.KEY_BACKSPACE, 'e', '\n'])
            self.assertEqual(panel.prompt('COMMANDE'), 'node')
            panel.win.inputs = iter(['x', '\x1b'])
            self.assertIsNone(panel.prompt('COMMANDE'))

    def test_menu_focus_visible_narrow_terminal(self):
        panel = self.get_panel(56)
        panel.win.inputs = iter([ui.curses.KEY_RIGHT] * 3 + [10])
        self.assertEqual(panel.menu('ACTIONS', ['DÉMARRER', 'STOPPER', 'RESTART', 'SUPPRIMER']), 'SUPPRIMER')
        self.assertTrue(any('[SUPPRIMER]' in text for _y, _x, text, _style in panel.win.chunks))

    def test_schedule_menu_six_options_stays_inside_box(self):
        # Menu « QUAND EXÉCUTER ? » : 6 libellés longs qui ne tiennent pas sur une
        # ligne dans la boîte (46 colonnes) : le rendu doit passer en vertical et
        # ne jamais dessiner d'option hors du cadre.
        options = ['CHAQUE JOUR', 'CHAQUE SEMAINE', 'CHAQUE MOIS', 'TOUTES LES X HEURES',
                   'TOUTES LES X MINUTES', 'PERSONNALISÉ']
        panel = ui.Panel(DummyWindow(h=24, w=100))
        panel.win.inputs = iter([ui.curses.KEY_DOWN, 10])
        self.assertEqual(panel.menu('QUAND EXÉCUTER ?', options, default=0), 'CHAQUE SEMAINE')

        labels = [f'[{name}]' for name in options]
        # La ligne sélectionnée est dessinée complétée d'espaces : on compare le
        # libellé détouré pour retrouver les 6 options.
        rows = [(y, x, text.strip()) for y, x, text, _style in panel.win.chunks
                if text.strip() in labels]
        self.assertEqual(len(rows), 6)
        self.assertEqual(sorted(text for _y, _x, text in rows), sorted(labels))
        self.assertEqual(len({y for y, _x, _text in rows}), 6, 'les 6 options doivent être sur des lignes distinctes')
        self.assertEqual([y for y, _x, _text in rows], sorted(y for y, _x, _text in rows))
        for _y, x, text in rows:
            self.assertGreaterEqual(x, 2)
            self.assertLessEqual(x + len(text), 99)

        # Cadre : une seule ligne de bordure supérieure dans la frame.
        frames = [(y, x, text) for y, x, text, _style in panel.win.chunks if text.startswith('┌')]
        self.assertEqual(len(frames), 1)
        _fy, box_x, top = frames[0]
        box_width = len(top)
        self.assertLessEqual(box_width, 96)  # width - 4
        for _y, x, text in rows:
            self.assertGreaterEqual(x, box_x + 1)
            self.assertLessEqual(x + len(text), box_x + box_width - 1)

    def test_schedule_menu_short_stays_horizontal(self):
        panel = ui.Panel(DummyWindow(h=24, w=100))
        panel.win.inputs = iter([ui.curses.KEY_RIGHT, 10])
        self.assertEqual(panel.menu('QUAND EXÉCUTER ?', ['CHAQUE JOUR', 'CHAQUE MOIS']), 'CHAQUE MOIS')
        labels = ['[CHAQUE JOUR]', '[CHAQUE MOIS]']
        rows = [(y, x, text.strip()) for y, x, text, _style in panel.win.chunks
                if text.strip() in labels]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({y for y, _x, _text in rows}), 1, 'un menu court reste horizontal')
        frames = [(y, x, text) for y, x, text, _style in panel.win.chunks if text.startswith('┌')]
        self.assertEqual(len(frames), 1)
        _fy, box_x, top = frames[0]
        box_width = len(top)
        for _y, x, text in rows:
            self.assertGreaterEqual(x, box_x + 1)
            self.assertLessEqual(x + len(text), box_x + box_width - 1)

    def test_logs_viewer(self):
        panel = self.get_panel()
        panel.win.inputs = iter((ui.curses.KEY_PPAGE, ui.curses.KEY_END, ord('r'), 27))
        calls = []
        def loader():
            calls.append(1)
            return [str(x) for x in range(100)]
        panel.viewer('TEST', loader)
        self.assertEqual(len(calls), 2)

    def test_min_size_does_not_crash(self):
        panel = self.get_panel(45)
        panel.render()
        self.assertIn('TERMINAL TROP PETIT', self.text(panel))

    def test_wheel_scroll_is_vertical_and_clamped(self):
        panel = self.get_panel()
        panel.win.h = 17
        panel.services = [
            services.Service(f'panel-svc{i}.service', 'active', 'enabled',
                             fragment='/etc/systemd/system/panel-svc.service')
            for i in range(40)
        ]
        visible = panel.list_visible()
        max_scroll = max(0, 40 - visible)
        self.assertGreater(max_scroll, 0)
        with mock.patch.object(ui, 'pins', return_value=[]):
            panel.scroll_list(-5)
            self.assertEqual(panel.scroll, 0)
            self.assertEqual(panel.index, 0)
            panel.scroll_list(1)
            self.assertEqual(panel.index, 1)
            self.assertEqual(panel.scroll, 0)
            self.assertGreaterEqual(panel.index, panel.scroll)
            self.assertLess(panel.index, panel.scroll + visible)
            panel.scroll_list(visible)
            self.assertEqual(panel.index, 1 + visible)
            self.assertEqual(panel.scroll, panel.index - visible + 1)
            panel.scroll_list(1000)
            self.assertEqual(panel.index, 39)
            self.assertEqual(panel.scroll, max_scroll)
            panel.scroll_list(-1000)
            self.assertEqual(panel.scroll, 0)
            self.assertEqual(panel.index, 0)
        panel.set_tab(ui.TAB_SITES)
        panel.sites = [
            sites.Site(f'site{i}.be', f'panel-site{i}.be.conf', f'/var/www/site{i}',
                       True, False, True, True)
            for i in range(20)
        ]
        panel.scroll_list(3)
        self.assertEqual(panel.index, 3)
        panel.scroll_list(-1)
        self.assertEqual(panel.index, 2)
        self.assertEqual(panel.mouse_wheel_delta(ui.curses.KEY_SR), -1)
        self.assertEqual(panel.mouse_wheel_delta(ui.curses.KEY_SF), 1)
        self.assertEqual(panel.mouse_wheel_delta(ord('j')), 0)


class DisplayTests(unittest.TestCase):
    """Mémoire dans SERVICES et bloc VPS (CRON + fastfetch intégré)."""

    def services_panel(self, width=100, height=24):
        panel = ui.Panel(DummyWindow(h=height, w=width))
        panel.services = [
            services.Service('light.service', 'active', 'enabled',
                             fragment='/usr/lib/systemd/system/light.service',
                             memory=56 * 1024 ** 2),
            services.Service('heavy.service', 'failed', 'enabled',
                             fragment='/usr/lib/systemd/system/heavy.service',
                             memory=int(1.15 * 1024 ** 3)),
        ]
        panel.sites = [sites.Site('example.be', 'panel-example.be.conf', '/var/www/example',
                                  True, True, True, True)]
        panel.set_tab(ui.TAB_SERVICES)
        return panel

    def vps_panel(self, width=100, height=24):
        panel = ui.Panel(DummyWindow(h=height, w=width))
        panel.services = [
            services.Service('panel-bot.service', 'active', 'enabled',
                             fragment='/etc/systemd/system/panel-bot.service', memory=1),
            services.Service('apache2.service', 'failed', 'enabled',
                             fragment='/usr/lib/systemd/system/apache2.service')]
        panel.sites = [sites.Site('example.be', 'panel-example.be.conf', '/var/www/example',
                                  True, True, True, True)]
        panel.jobs = [
            cron.CronJob('Sauvegarde', '/usr/local/bin/backup.sh', 'root', '0 3 * * *',
                         '/etc/cron.d/serverpanel', managed=True, slug='sauvegarde'),
            cron.CronJob('Purge', '/usr/local/bin/purge.sh', 'root', '0 4 * * *',
                         '/etc/cron.d/serverpanel', managed=True, slug='purge'),
            cron.CronJob('Uptime', '/usr/bin/uptime', 'root', '17 * * * *', '/etc/crontab'),
        ]
        panel.cron_service = 'active'
        panel.set_tab(ui.TAB_VPS)
        return panel

    def text(self, panel):
        return ' '.join(x[2] for x in panel.win.chunks)

    def test_memory_label(self):
        self.assertEqual(ui.memory_label(56 * 1024 ** 2), '56Mo')
        self.assertEqual(ui.memory_label(int(1.15 * 1024 ** 3)), '1.15Go')
        self.assertEqual(ui.memory_label(1024 ** 3), '1Go')
        self.assertEqual(ui.memory_label(int(12.5 * 1024 ** 3)), '12.5Go')
        self.assertEqual(ui.memory_label(0), '')
        self.assertEqual(ui.memory_label(-42), '')
        self.assertEqual(ui.memory_label(5 * 1024 ** 2), '5.0Mo')

    def test_services_memory_column_never_overlaps(self):
        for width in (56, 80, 100, 160):
            with self.subTest(width=width):
                panel = self.services_panel(width=width)
                panel.render()
                chunks = panel.win.chunks
                for label in ('56Mo', '1.15Go'):
                    mem = [(y, x, text) for y, x, text, _style in chunks
                           if text.strip() == label]
                    self.assertEqual(len(mem), 1, f'{width} : {label} doit apparaître')
                    y, mem_x, mem_text = mem[0]
                    self.assertEqual(len(mem_text), 7, 'colonne mémoire de largeur fixe')
                    name = next((x, text) for row, x, text, _style in chunks
                                if row == y and x == 4)
                    status = next(((x, text) for row, x, text, _style in chunks
                                   if row == y and text.strip() in ('ACTIF', 'ÉCHEC')), None)
                    self.assertIsNotNone(status, f'{width} : badge ACTIF/ÉCHEC présent')
                    status_x, _status_text = status
                    self.assertLessEqual(4 + len(name[1]), mem_x,
                                         f'{width} : {label} chevauche le nom')
                    self.assertLessEqual(mem_x + 7, status_x,
                                         f'{width} : {label} chevauche le badge')
                self.assertIn('ACTIF', self.text(panel))
                self.assertIn('ÉCHEC', self.text(panel))

    def test_services_memory_column_absent_when_narrow(self):
        # mem_x - 4 < 8 : la mémoire s'efface et le nom reprend la place.
        panel = ui.Panel(DummyWindow(w=56, h=24))
        panel.services = [services.Service('panel-bot.service', 'active', 'enabled',
                                           fragment='/etc/systemd/system/panel-bot.service',
                                           memory=56 * 1024 ** 2)]
        panel.set_tab(ui.TAB_SERVICES)
        separator = 26  # place insuffisante : mem_x - 4 = 7 < 8
        panel.render_service(panel.services[0], 4, False, separator)
        self.assertNotIn('56Mo', self.text(panel))
        self.assertIn('ACTIF', self.text(panel))

    def vps_rows(self, panel, start=0, stop=99):
        return [text for y, _x, text, _style in panel.win.chunks
                if start <= y <= stop and text.strip()]

    def test_vps_cron_line_and_fastfetch(self):
        panel = self.vps_panel(width=100, height=24)
        with mock.patch.object(ui, 'os_name', return_value='Debian GNU/Linux 13 (trixie)'), \
             mock.patch.object(ui, 'local_ip', return_value='192.168.1.20'):
            panel.render()
        chunks = panel.win.chunks
        self.assertIn(('CRON', 19, 2),
                      [(text, y, x) for y, x, text, _style in chunks])
        cron_row = ' '.join(text for y, _x, text, _style in chunks
                            if y == 19 and text.strip())
        self.assertIn('3 · 2 gérés', cron_row)
        self.assertIn('actif', cron_row)
        labels = {text for y, x, text, _style in chunks if 4 <= y <= 8 and x == 20}
        for label in ('OS', 'HOST', 'KERNEL', 'UPTIME', 'IP'):
            self.assertIn(label, labels, f'fastfetch : {label} manquant')
        self.assertIn('Debian GNU/Linux 13 (trixie)', self.text(panel))
        self.assertIn('192.168.1.20', self.text(panel))
        logo = [text for y, x, text, _style in chunks
                if 4 <= y <= 8 and text.strip().startswith('┌')]
        self.assertTrue(logo, 'le logo doit être dessiné à width 100')
        self.assertIn('DEBIAN', self.text(panel))
        # Jamais sur la dernière ligne (message).
        self.assertTrue(all(y <= 22 for y, _x, _text, _style in chunks if y >= 4))

    def test_vps_layout_order_without_duplicates(self):
        # Nouvelle disposition : SYSTÈME, fastfetch, jauges, puis le résumé.
        panel = self.vps_panel(width=100, height=24)
        panel.render()
        chunks = panel.win.chunks

        def row_of(label):
            rows = [y for y, _x, text, _style in chunks if text == label]
            self.assertEqual(len(rows), 1, f'{label} doit apparaître une seule fois')
            return rows[0]

        order = [row_of(label) for label in
                 ('OS', 'CPU', 'RAM', 'DISK', 'SERVICES', 'CRON')]
        self.assertEqual(order, sorted(order))
        self.assertEqual(order, [4, 10, 12, 14, 16, 19])
        # Chaque libellé du résumé est unique (les onglets sont entre crochets).
        for label in ('UPTIME', 'CPU', 'RAM', 'DISK', 'SERVICES', 'SITES', 'CRON'):
            self.assertEqual(sum(1 for _y, _x, text, _style in chunks if text == label),
                             1, f'{label} : doublon affiché')
        self.assertEqual(row_of('SYSTÈME'), 3)
        self.assertIn('SYSTÈME', self.vps_rows(panel, 3, 3))
        # Plus de label « Debian 13 · <kernel> » à droite du titre.
        self.assertFalse([text for y, _x, text, _style in chunks
                          if y == 3 and text.startswith('Debian 13 ·')])

    def test_vps_fastfetch_block_has_only_five_fields(self):
        panel = self.vps_panel(width=100, height=24)
        panel._fastfetch_at = 0.0
        panel._fastfetch = {}
        info = {'os': 'Debian 13 test', 'host': 'vps-test', 'kernel': '6.12-test',
                'uptime': '3j', 'local ip': '10.1.2.3'}
        with mock.patch.object(ui, 'fastfetch_info', return_value=info):
            panel.render()
        chunks = panel.win.chunks
        block = [text for y, _x, text, _style in chunks if 4 <= y <= 8]
        for value in ('Debian 13 test', 'vps-test', '6.12-test', '3j', '10.1.2.3'):
            self.assertIn(value, block, f'fastfetch : {value} manquant')
        for label in ('CPU', 'RAM', 'DISK', 'SERVICES', 'SITES', 'CRON'):
            self.assertNotIn(label, block, f'{label} ne doit pas être dans le bloc')
        headers = {text for y, x, text, _style in chunks if 4 <= y <= 8 and x == 20}
        self.assertEqual(headers, {'OS', 'HOST', 'KERNEL', 'UPTIME', 'IP'})
        # Ces libellés existent bien une fois ailleurs (jauges + résumé).
        for label in ('CPU', 'RAM', 'DISK', 'SERVICES', 'SITES', 'CRON'):
            self.assertEqual(sum(1 for _y, _x, text, _style in chunks if text == label), 1)

    def test_vps_21_lines_keeps_message_row(self):
        panel = self.vps_panel(width=100, height=21)
        panel.message = 'Prêt'
        panel.message_until = ui.time.monotonic() + 60
        panel.render()
        rows = [y for y, _x, text, _style in panel.win.chunks if text.strip()]
        self.assertLessEqual(max(rows), 20, 'la ligne de message reste libre')
        chunks = panel.win.chunks
        self.assertEqual([y for y, _x, text, _style in chunks if text == 'CRON'], [19])
        self.assertIn('· Prêt', [text for y, _x, text, _style in chunks if y == 20])

    def test_vps_fastfetch_values_are_used(self):
        panel = self.vps_panel(width=100, height=24)
        panel._fastfetch_at = 0.0
        panel._fastfetch = {}
        info = {'os': 'Debian 13 test', 'host': 'vps-test', 'kernel': '6.12-test',
                'uptime': '3j', 'local ip': '10.1.2.3'}
        with mock.patch.object(ui, 'fastfetch_info', return_value=info):
            panel.render()
            joined = self.text(panel)
            for value in ('Debian 13 test', 'vps-test', '6.12-test', '3j', '10.1.2.3'):
                self.assertIn(value, joined, f'fastfetch : {value} manquant')
            # Cache 30 s : un seul appel pour deux rendus consécutifs.
            panel.render()
        self.assertIn('vps-test', self.text(panel))

    def test_vps_fastfetch_cache_limits_calls(self):
        panel = self.vps_panel(width=100, height=24)
        panel._fastfetch_at = 0.0
        panel._fastfetch = {}
        with mock.patch.object(ui, 'fastfetch_info', return_value={}) as fetch:
            panel.render()
            panel.render()
        self.assertEqual(fetch.call_count, 1)

    def test_vps_fastfetch_fallback_without_binary(self):
        panel = self.vps_panel(width=100, height=24)
        panel._fastfetch_at = 0.0
        panel._fastfetch = {}
        metrics = {'host': 'hôte-repli', 'kernel': '6.1-repli', 'uptime': '2j',
                   'cores': 2, 'load': 0.5, 'ram': None, 'ram_used': None,
                   'ram_total': None, 'disk': None, 'disk_used': None, 'disk_total': None}
        with mock.patch.object(ui, 'fastfetch_info', return_value={}), \
             mock.patch.object(ui, 'stats', return_value=metrics), \
             mock.patch.object(ui, 'os_name', return_value='Debian 13 repli'):
            panel.render()
        joined = self.text(panel)
        for value in ('Debian 13 repli', 'hôte-repli', '6.1-repli', '2j'):
            self.assertIn(value, joined, f'repli : {value} manquant')
        labels = {text for y, x, text, _style in panel.win.chunks if 4 <= y <= 8 and x == 20}
        for label in ('OS', 'HOST', 'KERNEL', 'UPTIME'):
            self.assertIn(label, labels, f'repli : {label} manquant')

    def test_vps_narrow_terminal_keeps_message(self):
        panel = self.vps_panel(width=56, height=17)
        panel.message = 'Cron créé'
        panel.message_until = ui.time.monotonic() + 60
        panel.render()
        joined = self.text(panel)
        self.assertIn('Cron créé', joined)
        self.assertNotIn('KERNEL', joined)
        self.assertNotIn('DEBIAN', joined)
        # Compact : ni fastfetch ni ligne CRON, le message garde la ligne 16.
        exact = [text for _y, _x, text, _style in panel.win.chunks]
        self.assertNotIn('CRON', exact)
        self.assertIn('UPTIME', exact)
        self.assertEqual(exact.count('SERVICES'), 1)
        rows = [(y, x, text) for y, x, text, _style in panel.win.chunks if y >= 16]
        self.assertEqual(rows, [(16, 2, '· Cron créé')])

    def test_vps_cron_counts_external_and_managed(self):
        panel = self.vps_panel(width=56, height=24)
        panel.cron_service = 'inactive'
        with mock.patch.object(ui, 'os_name', return_value='Debian 13'), \
             mock.patch.object(ui, 'local_ip', return_value=''):
            panel.render()
        cron_row = ' '.join(text for y, _x, text, _style in panel.win.chunks
                            if y == 19 and text.strip())
        self.assertIn('3 · 2 gérés', cron_row)
        self.assertIn('arrêté', cron_row)


class SidebarTests(unittest.TestCase):
    """Panneaux latéraux : même largeur (40 %) pour SERVICES, SITES et CRON."""

    VHOST = ('<VirtualHost *:80>\n    ServerName example.be\n'
             '    DocumentRoot "/var/www/example/"\n</VirtualHost>\n')

    def get_panel(self, width=100, height=24):
        panel = ui.Panel(DummyWindow(h=height, w=width))
        panel.services = [
            services.Service('panel-bot.service', 'active', 'enabled',
                             fragment='/etc/systemd/system/panel-bot.service',
                             memory=56 * 1024 ** 2),
            services.Service('apache2.service', 'failed', 'enabled',
                             fragment='/usr/lib/systemd/system/apache2.service',
                             memory=int(1.15 * 1024 ** 3))]
        panel.sites = [sites.Site('example.be', 'panel-example.be.conf',
                                  '/var/www/example', True, True, True, True)]
        panel.jobs = [cron.CronJob('Sauvegarde', '/usr/local/bin/backup.sh', 'root',
                                   '0 3 * * *', '/etc/cron.d/serverpanel',
                                   managed=True, enabled=True, slug='sauvegarde')]
        panel.cron_all = True
        panel.cron_service = 'active'
        return panel

    def separator(self, panel):
        xs = {x for _y, x, text, _style in panel.win.chunks if text == '│'}
        self.assertEqual(len(xs), 1, 'un seul séparateur vertical')
        return xs.pop()

    def render(self, panel):
        with mock.patch.object(ui.sites, 'config_text', return_value=self.VHOST), \
             mock.patch.object(ui, 'pins', return_value=[]):
            panel.render()

    def test_sidebar_width_is_40_percent_for_all_tabs(self):
        panel = self.get_panel()
        for width in (56, 100, 160, 194):
            with self.subTest(width=width):
                expected = max(22, round(width * 0.40))
                self.assertEqual(panel.sidebar_width(width), expected)
                panel.win.w = width
                widths = set()
                for tab in (ui.TAB_SERVICES, ui.TAB_SITES, ui.TAB_CRON):
                    panel.set_tab(tab)
                    self.render(panel)
                    separator = self.separator(panel)
                    self.assertEqual(separator, width - expected - 4)
                    widths.add(width - separator - 4)
                self.assertEqual(widths, {expected},
                                 'même largeur pour SERVICES, SITES et CRON')

    def test_sidebar_minimum_keeps_list_usable(self):
        panel = self.get_panel(width=56, height=17)
        self.assertEqual(panel.sidebar_width(56), 22)
        panel.set_tab(ui.TAB_SERVICES)
        self.render(panel)
        separator = self.separator(panel)
        self.assertEqual((separator, 56 - separator - 4), (30, 22))
        self.assertGreaterEqual(separator - 2, 28, 'la liste garde ~28 colonnes')

    def test_sidebar_widths_per_terminal(self):
        panel = self.get_panel()
        # round(width * 0.40) avec plancher à 22 : la liste garde la place utile.
        self.assertEqual(
            [(width, panel.sidebar_width(width), width - panel.sidebar_width(width) - 6)
             for width in (56, 80, 100, 160, 194)],
            [(56, 22, 28), (80, 32, 42), (100, 40, 54), (160, 64, 90), (194, 78, 110)])

    def test_render_has_no_overflow_for_three_tabs(self):
        for width, height in ((56, 17), (80, 24), (100, 24), (194, 24)):
            for tab in (ui.TAB_SERVICES, ui.TAB_SITES, ui.TAB_CRON):
                with self.subTest(width=width, height=height, tab=ui.TABS[tab]):
                    panel = self.get_panel(width=width, height=height)
                    panel.set_tab(tab)
                    panel.focus = 'list'
                    self.render(panel)
                    separator = self.separator(panel)
                    self.assertEqual(separator, width - panel.sidebar_width(width) - 4)
                    for y, x, text, _style in panel.win.chunks:
                        if y < 4 or not text.strip():
                            continue
                        self.assertLessEqual(x + len(text), width - 1,
                                             f'{width}×{height} : {text!r} sort de l’écran')
                        if x < separator:
                            self.assertLessEqual(
                                x + len(text), separator - 1,
                                f'{width}×{height} : {text!r} déborde dans la sidebar')
                        elif x > separator:
                            self.assertGreaterEqual(
                                x, separator + 2,
                                f'{width}×{height} : {text!r} mord sur le séparateur')


class CronTests(unittest.TestCase):
    """Onglet CRON : rendu, navigation et assistants (aucun accès à /etc)."""

    def get_panel(self, width=100, height=24):
        panel = ui.Panel(DummyWindow(h=height, w=width))
        panel.services = [
            services.Service('panel-bot.service', 'active', 'enabled',
                             fragment='/etc/systemd/system/panel-bot.service', memory=1),
            services.Service('apache2.service', 'failed', 'enabled',
                             fragment='/usr/lib/systemd/system/apache2.service')]
        panel.sites = [sites.Site('example.be', 'panel-example.be.conf', '/var/www/example',
                                  True, True, True, True)]
        panel.jobs = []
        return panel

    def job(self, **overrides):
        fields = dict(label='Sauvegarde', command='/usr/local/bin/backup.sh', user='root',
                      schedule='0 3 * * *', source='/etc/cron.d/serverpanel', managed=True,
                      enabled=True, slug='sauvegarde', line=0)
        fields.update(overrides)
        return cron.CronJob(**fields)

    def external(self):
        return cron.CronJob('Uptime', '/usr/bin/uptime', 'root', '17 * * * *', '/etc/crontab')

    def text(self, panel):
        return ' '.join(x[2] for x in panel.win.chunks)

    def render_text(self, panel):
        panel.render()
        return self.text(panel)

    # ------------------------------------------------------------------ rendu

    def test_cron_tab_render_managed_job(self):
        panel = self.get_panel()
        panel.jobs = [self.job()]
        panel.cron_service = 'active'
        panel.set_tab(ui.TAB_CRON)
        panel.render()
        joined = self.text(panel)
        for label in ('[CRON]', 'CRON  1', 'Sauvegarde', 'ACTIONS', 'NOUVEAU', 'QUAND',
                      'PROCHAINE', 'COMMANDE', 'MODIFIER', 'EXÉCUTER', 'JOURNAL',
                      'SUPPRIMER', 'SERVICE ACTIF'):
            self.assertIn(label, joined)
        self.assertIn('Chaque jour 03:00', joined)

    def test_cron_actions_render_vertically(self):
        panel = self.get_panel()
        panel.jobs = [self.job()]
        panel.set_tab(ui.TAB_CRON)
        panel.focus = 'actions'
        panel.render()
        chips = [(y, text) for y, _x, text, _style in panel.win.chunks
                 if text.startswith('[') and any(name in text for name in
                                                 ('MODIFIER', 'EXÉCUTER', 'DÉSACTIVER',
                                                  'JOURNAL', 'ÉPINGLER', 'SUPPRIMER'))]
        self.assertEqual(len(chips), 6)
        self.assertEqual([y for y, _text in chips], sorted({y for y, _text in chips}))
        self.assertEqual(len({y for y, _text in chips}), len(chips))

    def test_cron_tab_empty_and_absent(self):
        panel = self.get_panel()
        panel.set_tab(ui.TAB_CRON)
        panel.cron_service = 'absent'
        with mock.patch.object(ui.cron, 'available', return_value=False):
            panel.render()
        joined = self.text(panel)
        self.assertIn('CRON NON INSTALLÉ', joined)
        self.assertIn('CRON ABSENT', joined)
        self.assertIn('Aucun cron', joined)
        self.assertIn('AJOUTER', joined)
        with mock.patch.object(ui.cron, 'available', return_value=True):
            panel.render()
        self.assertIn('AUCUN CRON FAVORI', self.text(panel))
        self.assertIn('[N] NOUVEAU', self.text(panel))

    def test_cron_render_narrow_terminal_is_safe(self):
        panel = self.get_panel(width=56, height=17)
        panel.jobs = [self.job(), self.external(),
                      self.job(label='', command='/bin/true', slug='muet', enabled=False)]
        panel.cron_all = True
        panel.cron_service = 'inactive'
        panel.set_tab(ui.TAB_CRON)
        for index in range(3):
            panel.index = index
            panel.focus = 'list'
            panel.render()
            panel.focus = 'actions'
            panel.action_index = index % 6
            panel.render()
        self.assertIn('CRON  3', self.text(panel))

    def test_cron_header_never_overlaps(self):
        def check(panel, width, height, expected, missing=()):
            panel.render()
            spans = sorted((x, x + len(text), text) for y, x, text, _style in panel.win.chunks
                           if y == 3 and text.strip())
            for (start, end, text), (following, _end, next_text) in zip(spans, spans[1:]):
                self.assertLessEqual(end, following,
                                     f'{width}×{height} : {text!r} chevauche {next_text!r}')
            rendered = ' '.join(text for _start, _end, text in spans)
            for text in expected:
                self.assertIn(text, rendered, f'{width}×{height}')
            for text in missing:
                self.assertNotIn(text, rendered, f'{width}×{height}')

        for width, height in ((100, 24), (56, 17)):
            panel = self.get_panel(width=width, height=height)
            panel.jobs = [self.job(), self.external()]
            panel.cron_service = 'active'
            panel.set_tab(ui.TAB_CRON)
            # Favoris par défaut : un seul cron visible, A propose TOUS.
            if width >= 100:
                check(panel, width, height, ('CRON  1', '[A] TOUS'))
            else:
                # Sidebar à 40 % : en 56 colonnes, [A] s'efface proprement.
                check(panel, width, height, ('CRON  1',), missing=('[A]',))
            panel.cron_all = True
            if width >= 100:
                check(panel, width, height, ('CRON  2', '[A] FAVORIS'))
            else:
                check(panel, width, height, ('CRON  2',), missing=('[A]',))
        # Peu de place : l'indicateur [A] disparaît proprement (aucune superposition).
        panel = self.get_panel(width=56, height=17)
        panel.jobs = [self.external() for _ in range(11)] + [self.job()]
        panel.cron_all = True
        panel.cron_service = 'active'
        panel.set_tab(ui.TAB_CRON)
        panel.render()
        row = ' '.join(text for y, _x, text, _style in panel.win.chunks
                       if y == 3 and text.strip())
        self.assertIn('CRON  12', row)
        self.assertNotIn('[A]', row)

    def test_cron_external_job_is_read_only(self):
        panel = self.get_panel()
        panel.jobs = [self.external()]
        panel.cron_all = True
        panel.set_tab(ui.TAB_CRON)
        panel.focus = 'actions'
        panel.render()
        joined = self.text(panel)
        self.assertIn('SYST', joined)
        self.assertIn('LECTURE SEULE', joined)
        self.assertIn('EXTERNE', joined)
        self.assertIn('INFOS', joined)
        self.assertIn('ÉPINGLER', joined)
        self.assertNotIn('SUPPRIMER', joined)
        self.assertNotIn('MODIFIER', joined)

    # ----------------------------------------------------------------- filtre A

    def test_cron_filter_defaults_to_favorites(self):
        panel = self.get_panel()
        panel.jobs = [self.job(), self.external()]
        panel.set_tab(ui.TAB_CRON)
        self.assertFalse(panel.cron_all)
        self.assertEqual([job.label for job in panel.rows()], ['Sauvegarde'])
        self.assertIn('[A] TOUS', self.render_text(panel))
        self.assertNotIn('Uptime', self.render_text(panel))
        # Un cron externe épinglé devient visible dans les favoris.
        panel.cron_pins = {self.external().key}
        self.assertEqual([job.label for job in panel.rows()], ['Uptime', 'Sauvegarde'])
        self.assertIn('Uptime', self.render_text(panel))
        panel.cron_all = True
        self.assertEqual(len(panel.rows()), 2)
        self.assertIn('[A] FAVORIS', self.render_text(panel))

    def test_cron_key_a_switches_filter(self):
        panel = self.get_panel()
        panel.jobs = [self.job(), self.external()]
        panel.set_tab(ui.TAB_CRON)
        panel.index = panel.scroll = 1
        panel.focus = 'actions'
        self.assertFalse(panel.cron_all)
        panel.win.inputs = iter([ord('a'), ord('q')])
        # setup() touche le terminal (initscr/noecho/cbreak) : neutralisé sous test.
        with mock.patch.object(ui.Panel, 'setup'), \
             mock.patch.object(ui.Panel, 'refresh_data'):
            panel.run()
        self.assertTrue(panel.cron_all)
        self.assertEqual((panel.index, panel.scroll, panel.focus), (0, 0, 'list'))
        self.assertEqual(len(panel.rows()), 2)
        panel.win.inputs = iter([ord('A'), ord('q')])
        with mock.patch.object(ui.Panel, 'setup'), \
             mock.patch.object(ui.Panel, 'refresh_data'):
            panel.run()
        self.assertFalse(panel.cron_all)
        self.assertEqual(len(panel.rows()), 1)

    def test_cron_filtered_empty_label(self):
        panel = self.get_panel()
        panel.jobs = [self.external()]
        panel.set_tab(ui.TAB_CRON)
        self.assertFalse(panel.cron_all)
        with mock.patch.object(ui.cron, 'available', return_value=True):
            self.assertEqual(panel.empty_label(), 'AUCUN CRON FAVORI')
            self.assertEqual(panel.rows(), [])
            panel.render()
            joined = self.text(panel)
        self.assertIn('AUCUN CRON FAVORI', joined)
        self.assertNotIn('Uptime', joined)
        panel.cron_all = True
        with mock.patch.object(ui.cron, 'available', return_value=True):
            self.assertEqual(panel.empty_label(), 'AUCUN CRON')
            self.assertEqual(len(panel.rows()), 1)
        with mock.patch.object(ui.cron, 'available', return_value=False):
            self.assertEqual(panel.empty_label(), 'CRON NON INSTALLÉ · sudo bash install.sh')

    # ---------------------------------------------------------------- actions

    def test_cron_actions_managed_external_empty(self):
        panel = self.get_panel()
        panel.set_tab(ui.TAB_CRON)
        self.assertEqual(panel.actions(), [('NOUVEAU', 'new')])
        panel.jobs = [self.external()]
        # Favoris par défaut : l'externe non épinglé n'est pas listé.
        self.assertEqual(panel.actions(), [('NOUVEAU', 'new')])
        panel.cron_all = True
        self.assertEqual([key for _label, key in panel.actions()], ['infos', 'logs', 'pin'])
        self.assertEqual(panel.actions()[2], ('ÉPINGLER', 'pin'))
        panel.cron_pins = {self.external().key}
        self.assertEqual(panel.actions()[2], ('RETIRER ÉPINGLE', 'pin'))
        panel.jobs = [self.job()]
        self.assertEqual([key for _label, key in panel.actions()],
                         ['edit', 'run', 'toggle', 'logs', 'pin', 'remove'])
        self.assertEqual(panel.actions()[0], ('MODIFIER', 'edit'))
        self.assertEqual(panel.actions()[4], ('ÉPINGLER', 'pin'))
        self.assertEqual(panel.actions()[2][0], 'DÉSACTIVER')
        panel.cron_pins = {self.job().key}
        self.assertEqual(panel.actions()[4], ('RETIRER ÉPINGLE', 'pin'))
        panel.jobs = [self.job(enabled=False)]
        self.assertEqual(panel.actions()[2], ('ACTIVER', 'toggle'))


    def test_execute_new_routes_to_new_cron(self):
        panel = self.get_panel()
        panel.set_tab(ui.TAB_CRON)
        with mock.patch.object(panel, 'new_cron') as new:
            panel.execute('new')
        new.assert_called_once_with()

    def test_cron_navigation_and_enter(self):
        panel = self.get_panel()
        panel.jobs = [self.job()]
        panel.set_tab(ui.TAB_CRON)
        self.assertEqual(panel.focus, 'list')
        panel.move_horizontal(1)
        self.assertEqual(panel.focus, 'actions')
        self.assertEqual(panel.actions()[panel.action_index][1], 'edit')
        panel.move_vertical(1)
        self.assertEqual(panel.actions()[panel.action_index][1], 'run')
        panel.move_vertical(-1)
        self.assertEqual(panel.actions()[panel.action_index][1], 'edit')
        panel.move_horizontal(1)  # cycle horizontal réservé aux SITES
        self.assertEqual(panel.actions()[panel.action_index][1], 'edit')
        panel.move_horizontal(-1)
        self.assertEqual(panel.focus, 'list')
        panel.handle_enter()
        self.assertEqual(panel.focus, 'actions')
        self.assertEqual(panel.action_index, 0)
        with mock.patch.object(panel, 'cron_action') as action:
            panel.handle_enter()
        action.assert_called_once_with(panel.jobs[0], 'edit')

    def test_cron_enter_without_jobs_creates(self):
        panel = self.get_panel()
        panel.set_tab(ui.TAB_CRON)
        with mock.patch.object(panel, 'new_cron') as new:
            panel.handle_enter()
        new.assert_called_once_with()

    def test_cron_action_toggle_remove_and_external_refusal(self):
        panel = self.get_panel()
        job = self.job()
        with mock.patch.object(ui.cron, 'set_enabled') as enabled, \
             mock.patch.object(panel, 'refresh_data'):
            panel.cron_action(job, 'toggle')
        enabled.assert_called_once_with('sauvegarde', False)
        self.assertIn('désactivé', panel.message)
        with mock.patch.object(panel, 'confirm', return_value=True) as confirm, \
             mock.patch.object(ui.cron, 'remove') as remove, \
             mock.patch.object(panel, 'refresh_data'):
            panel.cron_action(job, 'remove')
        confirm.assert_called_once()
        remove.assert_called_once_with('sauvegarde')
        self.assertIn('supprimé', panel.message)
        external = self.external()
        with mock.patch.object(ui.cron, 'set_enabled') as enabled, \
             mock.patch.object(panel, 'refresh_data'):
            panel.cron_action(external, 'toggle')
        enabled.assert_not_called()
        self.assertTrue(panel.message_error)
        self.assertIn('lecture seule', panel.message)

    def test_cron_action_run_requires_confirmation(self):
        panel = self.get_panel()
        job = self.job()
        with mock.patch.object(panel, 'confirm', return_value=False), \
             mock.patch.object(ui.cron, 'run_now') as runner:
            panel.cron_action(job, 'run')
        runner.assert_not_called()
        panel.win.inputs = iter([27])
        with mock.patch.object(panel, 'confirm', return_value=True), \
             mock.patch.object(ui.cron, 'run_now', return_value=['Code 0']) as runner:
            panel.cron_action(job, 'run')
        runner.assert_called_once_with(job)

    def test_cron_action_infos_and_logs(self):
        panel = self.get_panel()
        panel.win.inputs = iter([27, 27])
        with mock.patch.object(ui.cron, 'logs', return_value=['ligne']) as logs:
            panel.cron_action(self.external(), 'logs')
        logs.assert_called_once_with()
        with mock.patch.object(ui.cron, 'detail_lines',
                               return_value=['QUAND Chaque jour']) as detail:
            panel.cron_action(self.job(), 'infos')
        detail.assert_called_once()

    def test_edit_cron_fields(self):
        panel = self.get_panel()
        job = self.job()
        with mock.patch.object(panel, 'menu', return_value='COMMANDE'), \
             mock.patch.object(panel, 'prompt', return_value='/bin/true'), \
             mock.patch.object(ui.cron, 'update') as update, \
             mock.patch.object(panel, 'refresh_data'):
            panel.edit_cron(job)
        update.assert_called_once_with('sauvegarde', command='/bin/true')
        with mock.patch.object(panel, 'menu', return_value='MOMENT'), \
             mock.patch.object(panel, 'cron_schedule_wizard', return_value='*/5 * * * *'), \
             mock.patch.object(ui.cron, 'update') as update, \
             mock.patch.object(panel, 'refresh_data'):
            panel.edit_cron(job)
        update.assert_called_once_with('sauvegarde', schedule='*/5 * * * *')
        with mock.patch.object(panel, 'menu', return_value='UTILISATEUR'), \
             mock.patch.object(panel, 'prompt', return_value='www-data'), \
             mock.patch.object(ui.cron, 'update') as update, \
             mock.patch.object(panel, 'refresh_data'):
            panel.edit_cron(job)
        update.assert_called_once_with('sauvegarde', user='www-data')
        with mock.patch.object(panel, 'menu', return_value='NOM'), \
             mock.patch.object(panel, 'prompt', return_value='Backup'), \
             mock.patch.object(ui.cron, 'update') as update, \
             mock.patch.object(panel, 'refresh_data'):
            panel.edit_cron(job)
        update.assert_called_once_with('sauvegarde', label='Backup')
        with mock.patch.object(panel, 'menu', return_value=None), \
             mock.patch.object(ui.cron, 'update') as update:
            panel.edit_cron(job)
        update.assert_not_called()

    def test_edit_cron_refuses_external(self):
        panel = self.get_panel()
        with mock.patch.object(panel, 'menu') as menu, \
             mock.patch.object(ui.cron, 'update') as update:
            panel.edit_cron(self.external())
        menu.assert_not_called()
        update.assert_not_called()
        self.assertTrue(panel.message_error)

    # --------------------------------------------------------------- favoris

    def test_cron_action_pin_external_and_managed(self):
        panel = self.get_panel()
        external = self.external()
        with mock.patch.object(ui.cron, 'toggle_pin', return_value=True) as pin, \
             mock.patch.object(panel, 'refresh_data') as refresh:
            panel.cron_action(external, 'pin')
        pin.assert_called_once_with(external)
        refresh.assert_called_once_with()
        self.assertFalse(panel.message_error)
        self.assertIn('ajouté aux favoris', panel.message)
        with mock.patch.object(ui.cron, 'toggle_pin', return_value=False), \
             mock.patch.object(panel, 'refresh_data'):
            panel.cron_action(external, 'pin')
        self.assertIn('retiré des favoris', panel.message)
        with mock.patch.object(ui.cron, 'toggle_pin', return_value=True) as pin, \
             mock.patch.object(panel, 'refresh_data'):
            panel.cron_action(self.job(), 'pin')
        pin.assert_called_once_with(self.job())
        self.assertIn('ajouté aux favoris', panel.message)

    def test_cron_action_pin_reports_config_errors(self):
        panel = self.get_panel()
        with mock.patch.object(ui.cron, 'toggle_pin',
                               side_effect=ui.PanelError('config.json invalide (JSON)')), \
             mock.patch.object(panel, 'refresh_data') as refresh:
            panel.cron_action(self.external(), 'pin')
        refresh.assert_not_called()
        self.assertTrue(panel.message_error)
        self.assertIn('invalide', panel.message)

    def test_cron_key_f_pins_selected_job(self):
        panel = self.get_panel()
        external = self.external()
        panel.jobs = [self.job(), external]
        panel.set_tab(ui.TAB_CRON)
        panel.cron_all = True
        panel.index = next(index for index, job in enumerate(panel.rows())
                           if job.key == external.key)
        panel.win.inputs = iter([ord('f'), ord('q')])
        with mock.patch.object(ui.cron, 'toggle_pin', return_value=True) as pin, \
             mock.patch.object(ui.Panel, 'setup'), \
             mock.patch.object(ui.Panel, 'refresh_data'):
            panel.run()
        pin.assert_called_once_with(external)
        self.assertIn('ajouté aux favoris', panel.message)
        panel.win.inputs = iter([ord('F'), ord('q')])
        with mock.patch.object(ui.cron, 'toggle_pin', return_value=False) as pin, \
             mock.patch.object(ui.Panel, 'setup'), \
             mock.patch.object(ui.Panel, 'refresh_data'):
            panel.run()
        pin.assert_called_once_with(external)
        self.assertIn('retiré des favoris', panel.message)

    def test_cron_pins_reload_on_refresh(self):
        panel = self.get_panel()
        with mock.patch.object(ui.services, 'inventory', return_value=[]), \
             mock.patch.object(ui.services, 'take_sync_messages', return_value=[]), \
             mock.patch.object(ui.sites, 'inventory', return_value=[]), \
             mock.patch.object(ui.cron, 'inventory', return_value=[]), \
             mock.patch.object(ui.cron, 'service_state', return_value='unknown'), \
             mock.patch.object(ui.cron, 'pinned_crons', return_value=['ext:abc123']):
            panel.refresh_data()
        self.assertEqual(panel.cron_pins, {'ext:abc123'})
        with mock.patch.object(ui.services, 'inventory', return_value=[]), \
             mock.patch.object(ui.services, 'take_sync_messages', return_value=[]), \
             mock.patch.object(ui.sites, 'inventory', return_value=[]), \
             mock.patch.object(ui.cron, 'inventory', return_value=[]), \
             mock.patch.object(ui.cron, 'service_state', return_value='unknown'), \
             mock.patch.object(ui.cron, 'pinned_crons', side_effect=RuntimeError('boom')):
            panel.refresh_data()
        self.assertEqual(panel.cron_pins, set())

    def test_cron_pinned_rows_star_and_separator(self):
        panel = self.get_panel()
        external = self.external()
        panel.jobs = [self.job(), external]
        panel.cron_pins = {external.key}
        panel.set_tab(ui.TAB_CRON)
        panel.render()
        chunks = [(y, x, text) for y, x, text, _style in panel.win.chunks]
        # Favori d'abord, étoile, séparateur, puis les crons gérés.
        self.assertEqual([job.label for job in panel.rows()], ['Uptime', 'Sauvegarde'])
        star_y = next(y for y, x, text in chunks if y >= 4 and text.startswith('★'))
        self.assertTrue(next(text for y, x, text in chunks
                             if y == star_y and x == 4).startswith('★ Uptime'))
        sep_y = next(y for y, x, text in chunks
                     if y >= 4 and text.strip().startswith('─────'))
        self.assertEqual(sep_y, star_y + 1)
        managed_y = next(y for y, x, text in chunks
                         if x == 4 and y > sep_y and 'Sauvegarde' in text)
        self.assertGreater(managed_y, sep_y)

    def test_cron_unmanaged_actions_still_refused(self):
        panel = self.get_panel()
        external = self.external()
        for action in ('edit', 'run', 'toggle', 'remove'):
            with self.subTest(action=action), \
                 mock.patch.object(panel, 'menu') as menu, \
                 mock.patch.object(ui.cron, 'set_enabled') as enabled, \
                 mock.patch.object(ui.cron, 'update') as update, \
                 mock.patch.object(ui.cron, 'remove') as remove:
                panel.message = ''
                panel.cron_action(external, action)
            menu.assert_not_called()
            enabled.assert_not_called()
            update.assert_not_called()
            remove.assert_not_called()
            self.assertTrue(panel.message_error)
            self.assertIn('lecture seule', panel.message)

    # -------------------------------------------------------------- assistant

    def test_multi_select_space_arrows_enter_escape(self):
        panel = self.get_panel()
        panel.win.inputs = iter([ord(' '), ui.curses.KEY_DOWN, ord(' '), 10])
        self.assertEqual(panel.multi('JOURS', ['LUNDI', 'MARDI', 'MERCREDI']),
                         ['LUNDI', 'MARDI'])
        panel.win.inputs = iter([ui.curses.KEY_DOWN, ui.curses.KEY_UP, ord(' '), 10])
        self.assertEqual(panel.multi('JOURS', ['LUNDI', 'MARDI']), ['LUNDI'])
        panel.win.inputs = iter([ord(' '), 27])
        self.assertIsNone(panel.multi('JOURS', ['LUNDI', 'MARDI']))
        panel.win.inputs = iter([10])
        self.assertEqual(panel.multi('JOURS', ['LUNDI', 'MARDI'], default=['MARDI']),
                         ['MARDI'])

    def test_prompt_helpers_loop_on_errors(self):
        panel = self.get_panel()
        with mock.patch.object(ui.curses, 'curs_set'):
            panel.win.inputs = iter(['2', '4', '\n', '3', ':', '3', '0', '\n'])
            self.assertEqual(panel.prompt_time('03:00'), (3, 30))
            self.assertTrue(panel.message_error)
            panel.win.inputs = iter(['4', '0', '\n', '1', ',', '1', '5', '\n'])
            self.assertEqual(panel.prompt_month_days(), [1, 15])
        with mock.patch.object(panel, 'prompt', side_effect=['abc', '30', '5']):
            self.assertEqual(panel.prompt_int('HEURES', 6, 1, 23, 'hint'), 5)
        self.assertIn('hors bornes', panel.message)

    def test_cron_wizard_daily(self):
        panel = self.get_panel()
        with mock.patch.object(panel, 'menu', return_value='CHAQUE JOUR'), \
             mock.patch.object(panel, 'prompt_time', return_value=(4, 30)) as prompt_time:
            self.assertEqual(panel.cron_schedule_wizard(), '30 4 * * *')
        prompt_time.assert_called_once_with('03:00')

    def test_cron_wizard_weekly_prefills_days(self):
        panel = self.get_panel()
        with mock.patch.object(panel, 'menu', return_value='CHAQUE SEMAINE'), \
             mock.patch.object(panel, 'multi', return_value=['LUNDI', 'MERCREDI']) as multi, \
             mock.patch.object(panel, 'prompt_time', return_value=(8, 0)):
            self.assertEqual(panel.cron_schedule_wizard('0 8 * * 1,3'), '0 8 * * 1,3')
        self.assertEqual(multi.call_args.kwargs['default'], ['LUNDI', 'MERCREDI'])
        with mock.patch.object(panel, 'menu', return_value='CHAQUE SEMAINE'), \
             mock.patch.object(panel, 'multi', return_value=None):
            self.assertIsNone(panel.cron_schedule_wizard())
        with mock.patch.object(panel, 'menu', return_value='CHAQUE SEMAINE'), \
             mock.patch.object(panel, 'multi', return_value=['DIMANCHE']), \
             mock.patch.object(panel, 'prompt_time', return_value=(0, 0)):
            self.assertEqual(panel.cron_schedule_wizard(), '0 0 * * 0')

    def test_cron_wizard_monthly(self):
        panel = self.get_panel()
        with mock.patch.object(panel, 'menu', return_value='CHAQUE MOIS'), \
             mock.patch.object(panel, 'prompt_month_days', return_value=[1, 15]) as days, \
             mock.patch.object(panel, 'prompt_time', return_value=(0, 0)):
            self.assertEqual(panel.cron_schedule_wizard('0 0 1,15 * *'), '0 0 1,15 * *')
        days.assert_called_once_with('1,15')

    def test_cron_wizard_intervals(self):
        panel = self.get_panel()
        with mock.patch.object(panel, 'menu', return_value='TOUTES LES X HEURES'), \
             mock.patch.object(panel, 'prompt_int', side_effect=[6, 30]) as number:
            self.assertEqual(panel.cron_schedule_wizard('30 */6 * * *'), '30 */6 * * *')
        self.assertEqual(number.call_args_list[0][0][:4], ('HEURES', 6, 1, 23))
        with mock.patch.object(panel, 'menu', return_value='TOUTES LES X MINUTES'), \
             mock.patch.object(panel, 'prompt_int', return_value=15):
            self.assertEqual(panel.cron_schedule_wizard('*/15 * * * *'), '*/15 * * * *')
        with mock.patch.object(panel, 'menu', return_value='TOUTES LES X MINUTES'), \
             mock.patch.object(panel, 'prompt_int', return_value=None):
            self.assertIsNone(panel.cron_schedule_wizard())

    def test_cron_wizard_custom_and_validation(self):
        panel = self.get_panel()
        with mock.patch.object(panel, 'menu', return_value='PERSONNALISÉ'), \
             mock.patch.object(panel, 'prompt', side_effect=['5', '4', '3', '2', '1']):
            self.assertEqual(panel.cron_schedule_wizard(), '5 4 3 2 1')
        with mock.patch.object(panel, 'menu', return_value='PERSONNALISÉ'), \
             mock.patch.object(panel, 'prompt',
                               side_effect=['99', '*', '*', '*', '*',
                                            '0', '3', '*', '*', '*']):
            self.assertEqual(panel.cron_schedule_wizard(), '0 3 * * *')
        self.assertTrue(panel.message_error)
        with mock.patch.object(panel, 'menu', return_value=None):
            self.assertIsNone(panel.cron_schedule_wizard())

    def test_new_cron_end_to_end(self):
        panel = self.get_panel()
        with mock.patch.object(ui.cron, 'available', return_value=True), \
             mock.patch.object(panel, 'prompt',
                               side_effect=['/usr/local/bin/backup.sh', 'Sauvegarde',
                                            'www-data']) as prompt, \
             mock.patch.object(panel, 'cron_schedule_wizard', return_value='0 3 * * *'), \
             mock.patch.object(panel, 'menu', return_value='CONFIRMER'), \
             mock.patch.object(ui.cron, 'add', return_value='sauvegarde') as add, \
             mock.patch.object(panel, 'refresh_data') as refresh:
            panel.new_cron()
        self.assertEqual(prompt.call_args_list[1][0],
                         ('NOM DU CRON', 'backup.sh', 'Entrée accepte le défaut'))
        add.assert_called_once_with('Sauvegarde', '/usr/local/bin/backup.sh',
                                    '0 3 * * *', 'www-data')
        refresh.assert_called_once_with()
        self.assertFalse(panel.message_error)
        self.assertIn('Cron créé', panel.message)
        self.assertEqual(panel.focus, 'list')

    def test_new_cron_selects_created_job(self):
        panel = self.get_panel()
        panel.set_tab(ui.TAB_CRON)
        created = self.job()
        with mock.patch.object(ui.cron, 'available', return_value=True), \
             mock.patch.object(panel, 'prompt',
                               side_effect=['/bin/true', 'Sauvegarde', 'root']), \
             mock.patch.object(panel, 'cron_schedule_wizard', return_value='0 3 * * *'), \
             mock.patch.object(panel, 'menu', return_value='CONFIRMER'), \
             mock.patch.object(ui.cron, 'add', return_value='sauvegarde'), \
             mock.patch.object(panel, 'refresh_data') as refresh:
            refresh.side_effect = lambda: setattr(panel, 'jobs', [self.external(), created])
            panel.new_cron()
        self.assertEqual(panel.rows()[panel.index], created)

    def test_new_cron_requires_cron_and_reports_errors(self):
        panel = self.get_panel()
        with mock.patch.object(ui.cron, 'available', return_value=False), \
             mock.patch.object(panel, 'prompt') as prompt:
            panel.new_cron()
        prompt.assert_not_called()
        self.assertTrue(panel.message_error)
        self.assertIn('Cron non installé', panel.message)
        with mock.patch.object(ui.cron, 'available', return_value=True), \
             mock.patch.object(panel, 'prompt', side_effect=['/bin/true', 'Test']), \
             mock.patch.object(panel, 'cron_schedule_wizard', return_value=None):
            panel.new_cron()
        self.assertNotIn('Cron créé', panel.message)
        with mock.patch.object(ui.cron, 'available', return_value=True), \
             mock.patch.object(panel, 'prompt', side_effect=['/bin/true', 'Test', 'root']), \
             mock.patch.object(panel, 'cron_schedule_wizard', return_value='0 3 * * *'), \
             mock.patch.object(panel, 'menu', return_value='CONFIRMER'), \
             mock.patch.object(ui.cron, 'add', side_effect=ui.PanelError('Commande vide')):
            panel.new_cron()
        self.assertTrue(panel.message_error)
        self.assertIn('Commande vide', panel.message)

    def test_refresh_data_survives_cron_errors(self):
        panel = self.get_panel()
        with mock.patch.object(ui.services, 'inventory', return_value=[]), \
             mock.patch.object(ui.services, 'take_sync_messages', return_value=[]), \
             mock.patch.object(ui.sites, 'inventory', return_value=[]), \
             mock.patch.object(ui.cron, 'inventory',
                               side_effect=ui.PanelError('inventaire cassé')), \
             mock.patch.object(ui.cron, 'service_state', return_value='unknown'), \
             mock.patch.object(ui.cron, 'pinned_crons', return_value=[]):
            panel.refresh_data()
        self.assertEqual(panel.jobs, [])
        self.assertEqual(panel.cron_error, 'inventaire cassé')
        self.assertEqual(panel.cron_service, 'unknown')
        self.assertEqual(panel.cron_pins, set())
        with mock.patch.object(ui.services, 'inventory', return_value=[]), \
             mock.patch.object(ui.services, 'take_sync_messages', return_value=[]), \
             mock.patch.object(ui.sites, 'inventory', return_value=[]), \
             mock.patch.object(ui.cron, 'inventory', return_value=[self.job()]), \
             mock.patch.object(ui.cron, 'service_state', side_effect=RuntimeError('boom')), \
             mock.patch.object(ui.cron, 'pinned_crons',
                               return_value=['panel:sauvegarde']):
            panel.refresh_data()
        self.assertEqual(len(panel.jobs), 1)
        self.assertEqual(panel.cron_service, 'unknown')
        self.assertEqual(panel.cron_pins, {'panel:sauvegarde'})
        self.assertEqual(panel.message, '')


if __name__ == '__main__':
    unittest.main()
