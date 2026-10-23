import unittest
from unittest import mock

from serverpanel import ui, services, sites


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
        return panel

    def text(self, panel):
        return ' '.join(x[2] for x in panel.win.chunks)

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

    def test_enter_moves_focus_then_executes(self):
        panel = self.get_panel()
        panel.focus = 'list'
        panel.index = 0
        panel.handle_enter()
        self.assertEqual(panel.focus, 'actions')
        self.assertEqual(panel.action_index, 0)
        with mock.patch.object(ui.services, 'action') as action, \
             mock.patch.object(ui.Panel, 'refresh_data'):
            panel.handle_enter()
        action.assert_called_once_with('panel-bot.service', 'start')

    def test_system_service_requires_confirmation(self):
        panel = self.get_panel()
        panel.index = 1  # apache2.service
        panel.focus = 'actions'
        panel.action_index = 1  # STOPPER
        with mock.patch.object(panel, 'confirm', return_value=False) as confirm, \
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
        self.assertEqual(names, ['manual.service', 'heavy.service', 'boot.service', 'failed.service'])

    def test_pin_action(self):
        panel = self.get_panel()
        item = panel.services[1]
        with mock.patch.object(ui, 'toggle_pin') as pin, \
             mock.patch.object(ui, 'pins', return_value=[]):
            panel.service_action(item, 'pin')
        pin.assert_called_once_with('apache2.service')

    def test_small_service_action_has_no_confirmation(self):
        panel = self.get_panel()
        panel.index = 0  # panel-bot.service
        panel.focus = 'actions'
        panel.action_index = 1  # STOPPER
        with mock.patch.object(panel, 'confirm') as confirm, \
             mock.patch.object(ui.services, 'action') as action, \
             mock.patch.object(ui.Panel, 'refresh_data'):
            panel.handle_enter()
        confirm.assert_not_called()
        action.assert_called_once_with('panel-bot.service', 'stop')

    def test_tabs_vps_and_sites(self):
        panel = self.get_panel()
        panel.set_tab(1)
        panel.render()
        joined = self.text(panel)
        for label in ('SYSTÈME', 'RAM', 'DISK', 'UPTIME', 'SERVICES'):
            self.assertIn(label, joined)
        panel.set_tab(2)
        panel.render()
        joined = self.text(panel)
        self.assertIn('example.be', joined)
        self.assertIn('HTTPS', joined)
        self.assertIn('NOUVEAU', joined)

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


if __name__ == '__main__':
    unittest.main()
