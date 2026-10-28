import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from serverpanel import common, services, sites


class Validation(unittest.TestCase):
    def test_domain(self):
        self.assertEqual(common.domain_name('WWW.Example.BE.'), 'www.example.be')
        for value in ('localhost', '-foo.org', 'bad..net', 'a_b.net', 'a.net\nServerAlias b.net', '../etc/passwd', 'a.com; rm'):
            with self.subTest(value=value), self.assertRaises(common.PanelError):
                common.domain_name(value)

    def test_app_name(self):
        self.assertEqual(common.app_name('my-app'), 'my-app')
        for name in ('x', '-hey', 'bad-', '../fail', 'a' * 25, 'foo_bar'):
            with self.subTest(name=name), self.assertRaises(common.PanelError):
                common.app_name(name)

    def test_path_traversal(self):
        self.assertEqual(sites.validate_webroot('/var/www/example/public'), Path('/var/www/example/public'))
        for value in ('/etc/passwd', '/var/www/../../etc', 'relative', '/var/www', '/var/www/foo"bar'):
            with self.subTest(value=value), self.assertRaises(common.PanelError):
                sites.validate_webroot(value)

    def test_config_text_rejects_bad_filename(self):
        site = sites.Site('example.be', '../evil.conf', '/var/www/example', True, False, True, True)
        with self.assertRaises(common.PanelError):
            sites.config_text(site)

    def test_config_text_appends_ssl_companion(self):
        with tempfile.TemporaryDirectory() as dirname:
            av = Path(dirname) / 'available'
            av.mkdir()
            site = sites.Site('example.be', 'panel-example.be.conf', '/var/www/example', True, True, True, False)
            (av / site.filename).write_text('<VirtualHost *:80>\n    ServerName example.be\n</VirtualHost>\n')
            (av / 'panel-example.be-le-ssl.conf').write_text(
                '<VirtualHost *:443>\n    ServerName example.be\n    SSLEngine on\n</VirtualHost>\n')
            with mock.patch.object(sites, 'AVAILABLE', av):
                text = sites.config_text(site)
            self.assertIn('VirtualHost *:80', text)
            self.assertIn('panel-example.be-le-ssl.conf', text)
            self.assertIn('VirtualHost *:443', text)
            self.assertIn('SSLEngine on', text)

    def test_vhost_static(self):
        value = sites.vhost('example.be', root=Path('/var/www/example.be/public'), www=True)
        for needle in ('ServerName example.be', 'ServerAlias www.example.be', 'AllowOverride All', 'Options -Indexes'):
            self.assertIn(needle, value)
        none = sites.vhost('example.be', root=Path('/var/www/example.be/public'), htaccess=False)
        self.assertIn('AllowOverride None', none)

    def test_vhost_proxy(self):
        value = sites.vhost('api.example.be', proxy_port=3100)
        self.assertIn('ProxyPass "/" "http://127.0.0.1:3100/"', value)
        self.assertNotIn('DocumentRoot', value)
        with self.assertRaises(common.PanelError):
            sites.vhost('api.example.be', proxy_port=80)

    def test_command_and_unit(self):
        with mock.patch.object(services.shutil, 'which', return_value='/usr/bin/python3'):
            argv = services.parse_command('python3 "app name.py" --token=abcd')
        self.assertEqual(argv[1:], ['app name.py', '--token=abcd'])
        unit = services.unit_content('discord', argv, Path('/srv/apps/discord'), 'svc-discord')
        self.assertIn('User=svc-discord', unit)
        self.assertIn('Restart=on-failure', unit)
        self.assertIn('StateDirectory=serverpanel/discord', unit)
        self.assertRegex(unit, r'ExecStart="/usr/bin/python3(?:\.\d+)?" "app name.py" "--token=abcd"')
        self.assertNotIn('/bin/sh -c', unit)

    def test_argv_literal_escaping(self):
        value = services._escape_systemd_arg('X%Y$Z"a\\b')
        self.assertEqual(value, '"X%%Y$$Z\\"a\\\\b"')

    def test_shell_operators(self):
        with mock.patch.object(services.shutil, 'which', return_value='/usr/bin/python3'):
            for val in ('python3 x.py && rm -rf /', 'python3 x.py | cat', 'bash -c python', 'sudo python3 x.py'):
                with self.subTest(val=val), self.assertRaises(common.PanelError):
                    services.parse_command(val)

    def test_service_protection(self):
        with mock.patch.object(services, 'root_only'):
            for action in ('stop', 'disable', 'restart'):
                with self.assertRaises(common.PanelError):
                    services.action('ssh.service', action)
            with self.assertRaises(common.PanelError):
                services.action('discord.service;evil', 'stop')
            with mock.patch.object(services, 'run') as cmd:
                services.action('panel-bot.service', 'restart')
                cmd.assert_called_once_with('systemctl', 'restart', 'panel-bot.service', timeout=50)

    def test_services_unavailable(self):
        with mock.patch.object(services, 'run', side_effect=common.PanelError('systemd unavailable')):
            with self.assertRaises(common.PanelError):
                services.inventory()

    def test_services_parsing(self):
        unitfiles = 'panel-bot.service enabled enabled\nssh.service enabled enabled\n'
        units = 'panel-bot.service loaded failed failed Discord bot\nssh.service loaded active running SSH server\n'
        meta = ('Id=panel-bot.service\nFragmentPath=/etc/systemd/system/panel-bot.service\nMemoryCurrent=1048576\n\n'
                'Id=ssh.service\nFragmentPath=/usr/lib/systemd/system/ssh.service\nMemoryCurrent=2097152\n\n')
        with mock.patch.object(services, 'run', side_effect=[unitfiles, units, meta]):
            data = services.inventory()
        self.assertEqual(data[0].active, 'failed')
        self.assertTrue(data[0].managed)
        self.assertEqual(data[0].origin, 'manual')
        self.assertEqual(data[0].memory, 1048576)
        self.assertEqual(data[1].enabled, 'enabled')
        self.assertEqual(data[1].origin, 'system')

    def test_services_skip_aliases_and_not_found(self):
        unitfiles = ('mariadb.service enabled enabled\n'
                     'mysql.service alias -\n')
        units = ('mariadb.service loaded active running MariaDB server\n'
                 'ghost.service not-found inactive dead ghost.service\n')
        meta = ('Id=mariadb.service\nFragmentPath=/usr/lib/systemd/system/mariadb.service\n'
                'MemoryCurrent=136314880\n\n')
        with mock.patch.object(services, 'run', side_effect=[unitfiles, units, meta]):
            data = services.inventory()
        self.assertEqual([s.name for s in data], ['mariadb.service'])
        self.assertEqual(data[0].memory, 136314880)

    def test_site_host_conflicts(self):
        self.assertEqual(sites._hosts('ServerName example.be\nServerAlias www.example.be other.net'),
                         {'example.be', 'www.example.be', 'other.net'})

    def test_atomic_write_preserves_mode(self):
        with tempfile.TemporaryDirectory() as dirname:
            target = Path(dirname) / 'file'
            common.atomic_write(target, 'a')
            common.atomic_write(target, 'b')
            self.assertEqual(target.read_text(), 'b')
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_pins_keep_unknown_keys(self):
        with tempfile.TemporaryDirectory() as dirname:
            config = Path(dirname) / 'config.json'
            config.write_text('{"pinned":["ssh"], "custom": 42}')
            with mock.patch.object(common, 'CONFIG', config), mock.patch.object(common, 'root_only'):
                common.toggle_pin('panel-bot.service')
                self.assertEqual(json.loads(config.read_text()), {'pinned': ['ssh', 'panel-bot'], 'custom': 42})

    def test_site_rollback(self):
        with tempfile.TemporaryDirectory() as dirname:
            available = Path(dirname) / 'available'
            available.mkdir()
            webroot = Path(dirname) / 'www'
            webroot.mkdir()
            with mock.patch.object(sites, 'AVAILABLE', available), mock.patch.object(sites, 'WEBROOT', webroot), \
                 mock.patch.object(sites, 'root_only'), mock.patch.object(sites, 'run') as cmd:
                cmd.side_effect = ['', '', common.PanelError('apache error'), '', '', '']
                with self.assertRaises(common.PanelError):
                    sites.create('example.be', path=str(webroot / 'example'))
            self.assertFalse((available / 'panel-example.be.conf').exists())
            self.assertFalse((webroot / 'example').exists())

    def test_site_disallow_different_config_conflict(self):
        with tempfile.TemporaryDirectory() as dirname:
            available = Path(dirname)
            (available / 'other.conf').write_text('ServerName test.be\nServerAlias www.example.be\n')
            with mock.patch.object(sites, 'AVAILABLE', available), mock.patch.object(sites, 'root_only'):
                with self.assertRaisesRegex(common.PanelError, 'déjà utilisé'):
                    sites.create('example.be', proxy_port=3000, www=True)

    def test_certbot_arguments_and_alias(self):
        site = sites.Site('example.be', 'panel-example.be.conf', '/var/www/example', True, False, True, True)
        with mock.patch.object(sites, 'root_only'), mock.patch.object(sites, 'run', return_value='Success') as command:
            self.assertEqual(sites.certificate(site, 'admin@example.be'), 'Success')
        command.assert_called_once_with('certbot', '--apache', '--non-interactive', '--agree-tos',
                                        '--email', 'admin@example.be', '--redirect', '-d', 'example.be',
                                        '-d', 'www.example.be', timeout=300)

    def test_remove_unit_preserves_application(self):
        with tempfile.TemporaryDirectory() as dirname:
            units = Path(dirname) / 'units'
            units.mkdir()
            project = Path(dirname) / 'app'
            project.mkdir()
            (project / 'bot.py').write_text('print(1)')
            (units / 'panel-discord.service').write_text('UNIT')
            with mock.patch.object(services, 'root_only'), mock.patch.object(services, 'UNITS', units), \
                 mock.patch.object(services, 'run') as commands:
                services.remove('panel-discord.service')
            self.assertTrue((project / 'bot.py').exists())
            self.assertFalse((units / 'panel-discord.service').exists())
            self.assertEqual(commands.call_count, 2)
            with mock.patch.object(services, 'root_only'):
                with self.assertRaises(common.PanelError):
                    services.remove('ssh.service')

    def test_remove_site_keeps_content_and_certificates(self):
        with tempfile.TemporaryDirectory() as dirname:
            av = Path(dirname) / 'available'; en = Path(dirname) / 'enabled'; data = Path(dirname) / 'data'
            av.mkdir(); en.mkdir(); data.mkdir()
            (data / 'data.txt').write_text('NEVER DELETE')
            site = sites.Site('example.be', 'panel-example.be.conf', str(data), True, True, True, False)
            (av / site.filename).write_text('ServerName example.be')
            (en / site.filename).symlink_to(av / site.filename)
            with mock.patch.object(sites, 'root_only'), mock.patch.object(sites, 'AVAILABLE', av), \
                 mock.patch.object(sites, 'ENABLED', en):
                def fake(*args, **kwargs):
                    if args[0] == 'a2dissite':
                        (en / args[1]).unlink()
                    return ''
                with mock.patch.object(sites, 'run', side_effect=fake):
                    sites.remove(site)
            self.assertTrue((data / 'data.txt').exists())
            self.assertFalse((av / site.filename).exists())

    def test_create_service_does_not_change_existing_permissions(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname); apps = root / 'apps'; apps.mkdir()
            folder = apps / 'discord'; folder.mkdir(mode=0o750)
            units = root / 'units'; units.mkdir()
            original_mode = folder.stat().st_mode
            with mock.patch.object(services, 'root_only'), mock.patch.object(services, 'APPS', apps), \
                 mock.patch.object(services, 'UNITS', units), mock.patch.object(services.pwd, 'getpwnam') as user, \
                 mock.patch.object(services, 'run') as cmd:
                user.return_value.pw_uid = 1012
                def runner(*argv, **kw):
                    if argv[:2] == ('runuser', '-u'):
                        raise common.PanelError('denied')
                    return ''
                cmd.side_effect = runner
                with self.assertRaisesRegex(common.PanelError, 'Accès au dossier'):
                    services.create('discord', 'python3 bot.py', str(folder))
            self.assertEqual(folder.stat().st_mode, original_mode)
            self.assertFalse((units / 'panel-discord.service').exists())

    def test_create_service_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname); apps = root / 'apps'; apps.mkdir()
            folder = apps / 'newbot'  # does not exist yet
            units = root / 'units'; units.mkdir()
            with mock.patch.object(services, 'root_only'), mock.patch.object(services, 'APPS', apps), \
                 mock.patch.object(services, 'UNITS', units), mock.patch.object(services.pwd, 'getpwnam') as user, \
                 mock.patch.object(services, 'run') as cmd:
                user.return_value.pw_uid = 1012
                def runner(*argv, **kw):
                    if argv[:2] == ('runuser', '-u'):
                        raise common.PanelError('denied')
                    return ''
                cmd.side_effect = runner
                with self.assertRaisesRegex(common.PanelError, 'Accès au dossier'):
                    services.create('newbot', 'python3 bot.py', str(folder))
            self.assertTrue(folder.is_dir(), 'Le dossier doit être créé automatiquement')



    def test_site_ssl_only_managed_enabled(self):
        site = sites.Site('test.be', 'default.conf', '/var/www/html', True, False, False, False)
        with mock.patch.object(sites, 'root_only'):
            with self.assertRaises(common.PanelError):
                sites.certificate(site, 'admin@test.be')


class ConfigIsTruth(unittest.TestCase):
    """config.json → systemd provisioning (no systemd, no root needed)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.units = root / 'units'; self.units.mkdir()
        self.apps = root / 'apps'; self.apps.mkdir()
        self.folder = self.apps / 'webapp'; self.folder.mkdir()
        self.config = root / 'config.json'
        self.saved = {'pinned': ['ssh'], 'custom_services': {}}
        self.config.write_text(json.dumps(self.saved, indent=2))
        services.take_sync_messages()

    def entry(self, **overrides):
        entry = {'command': f'"{sys.executable}" app.py', 'folder': str(self.folder),
                 'user': 'www-data'}
        entry.update(overrides)
        self.saved['custom_services']['webapp'] = entry
        self.config.write_text(json.dumps(self.saved, indent=2))

    def sync(self, uid=33):
        account = types.SimpleNamespace(pw_uid=uid)
        self.enabled = getattr(self, 'enabled', False)
        state = {'calls': []}

        def runner(*argv, **_kw):
            state['calls'].append(argv)
            if argv[:2] == ('systemctl', 'is-enabled'):
                return 'enabled\n' if self.enabled else 'disabled\n'
            if argv[:2] == ('systemctl', 'enable'):
                self.enabled = True
            if argv[:2] == ('systemctl', 'disable'):
                self.enabled = False
            return ''

        with mock.patch.object(services, 'UNITS', self.units), \
             mock.patch.object(services, 'APPS', self.apps), \
             mock.patch.object(services, 'CONFIG', self.config), \
             mock.patch.object(common, 'CONFIG', self.config), \
             mock.patch.object(services.os, 'geteuid', return_value=0), \
             mock.patch.object(services, 'run', runner), \
             mock.patch.object(services.pwd, 'getpwnam', return_value=account):
            services._sync_panel_to_config()
            return state, services.take_sync_messages()

    def test_missing_unit_is_created_once(self):
        self.entry(autostart=True, pinned=True)
        state, messages = self.sync()
        unit = self.units / 'panel-webapp.service'
        self.assertTrue(unit.is_file())
        content = unit.read_text()
        self.assertIn('User=www-data', content)
        self.assertIn(f'WorkingDirectory={self.folder}', content)
        self.assertIn(('systemd-analyze', 'verify', str(unit)), state['calls'])
        self.assertIn(('systemctl', 'enable', 'panel-webapp.service'), state['calls'])
        self.assertIn(('systemctl', 'start', 'panel-webapp.service'), state['calls'])
        self.assertEqual(json.loads(self.config.read_text())['pinned'], ['ssh', 'panel-webapp'])
        self.assertTrue(any(not error and 'créé' in text for error, text in messages))

        # Deuxième passe : aucun doublon, aucune action
        state, messages = self.sync()
        self.assertEqual(messages, [])
        self.assertNotIn(('systemctl', 'start', 'panel-webapp.service'), state['calls'])
        self.assertNotIn(('systemd-analyze', 'verify', str(unit)), state['calls'])

    def test_config_edits_are_applied(self):
        self.entry(autostart=True)
        self.sync()
        unit = self.units / 'panel-webapp.service'
        self.entry(autostart=False, command=f'"{sys.executable}" main.py')
        state, _ = self.sync()
        self.assertIn('"main.py"', unit.read_text())
        self.assertIn(('systemctl', 'daemon-reload'), state['calls'])
        self.assertIn(('systemctl', 'disable', 'panel-webapp.service'), state['calls'])
        self.assertFalse(self.enabled)

    def test_unsafe_entries_are_refused(self):
        other = self.apps / 'other'; other.mkdir()
        outside = Path(self.temp.name) / 'outside'; outside.mkdir()
        unsafe = {
            'root': {'command': f'"{sys.executable}" x.py', 'folder': str(other), 'user': 'root'},
            'nouser': {'command': f'"{sys.executable}" x.py', 'folder': str(outside)},
            'ghost': {'command': f'"{sys.executable}" x.py',
                      'folder': str(Path(self.temp.name) / 'ghost'), 'user': 'www-data'},
            'Bad_Slug': {'command': f'"{sys.executable}" x.py', 'folder': str(other)},
        }
        for slug, entry in unsafe.items():
            with self.subTest(slug=slug):
                self.saved['custom_services'] = {slug: entry}
                self.config.write_text(json.dumps(self.saved, indent=2))
                _state, messages = self.sync(uid=0 if slug == 'root' else 33)
                self.assertFalse((self.units / f'panel-{slug}.service').exists())
                self.assertTrue(any(error for error, _text in messages))

    def test_discovery_registers_existing_unit(self):
        (self.units / 'panel-legacy.service').write_text(
            '[Service]\nUser=www-data\nWorkingDirectory=/var/www/app\nExecStart=/usr/bin/npm run start\n')
        self.sync()
        saved = json.loads(self.config.read_text())['custom_services']['legacy']
        self.assertEqual(saved['user'], 'www-data')
        self.assertEqual(saved['folder'], '/var/www/app')
        self.assertEqual(saved['command'], '/usr/bin/npm run start')

    def test_invalid_json_is_never_rewritten(self):
        broken = '{"custom_services": {"webapp": '
        self.config.write_text(broken)
        state, messages = self.sync()
        self.assertEqual(self.config.read_text(), broken)
        self.assertEqual(state['calls'], [])
        self.assertTrue(any(error and 'invalide' in text for error, text in messages))


class DefaultConfig(unittest.TestCase):
    """ensure_config : créé une seule fois, favoris filtrés sur l'hôte."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'etc/serverpanel/config.json'
        # Windows n'expose pas os.fchmod : neutralisé pour exercer atomic_write.
        patcher = mock.patch.object(common.os, 'fchmod', mock.Mock(), create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ensure(self, units, root=True):
        with mock.patch.object(common, 'CONFIG', self.config), \
             mock.patch.object(common, 'installed_units', return_value=set(units)), \
             mock.patch.object(common.os, 'geteuid', return_value=0 if root else 1000, create=True):
            return common.ensure_config()

    def test_creates_pins_for_installed_services_only(self):
        created = self.ensure({'apache2.service', 'ssh.service', 'mariadb.service',
                               'redis-server.service'})
        self.assertTrue(created)
        self.assertEqual(json.loads(self.config.read_text(encoding='utf-8')),
                         {'pinned': ['apache2', 'ssh', 'mariadb', 'redis-server']})

    def test_no_installed_service_yields_empty_pins(self):
        self.assertTrue(self.ensure(set()))
        self.assertEqual(json.loads(self.config.read_text(encoding='utf-8')), {'pinned': []})

    def test_existing_file_is_never_modified(self):
        self.config.parent.mkdir(parents=True)
        original = '{"pinned": ["custom"], "autre": 1}'
        self.config.write_text(original, encoding='utf-8')
        self.assertFalse(self.ensure({'apache2.service', 'ssh.service'}))
        self.assertEqual(self.config.read_text(encoding='utf-8'), original)

    def test_existing_invalid_json_is_never_modified(self):
        self.config.parent.mkdir(parents=True)
        broken = '{"pinned": ['
        self.config.write_text(broken, encoding='utf-8')
        self.assertFalse(self.ensure({'ssh.service'}))
        self.assertEqual(self.config.read_text(encoding='utf-8'), broken)

    def test_non_root_creates_nothing(self):
        self.assertFalse(self.ensure({'ssh.service'}, root=False))
        self.assertFalse(self.config.exists())


class Fastfetch(unittest.TestCase):
    """fastfetch_info : optionnel, jamais bloquant, jamais de shell."""

    OUTPUT = ('\x1b[1mOS\x1b[0m: Debian GNU/Linux 13 (trixie)\n'
              'Host: \x1b[32mleon\x1b[0m\n'
              'Kernel: 6.12.0-amd64\n'
              'Uptime: 3 days, 4 hours\n'
              'Local IP (eth0): 192.168.1.20\n')

    def test_missing_binary_returns_empty_without_subprocess(self):
        with mock.patch.object(common.shutil, 'which', return_value=None), \
             mock.patch.object(common, 'run') as runner:
            self.assertEqual(common.fastfetch_info(), {})
        runner.assert_not_called()

    def test_parses_keys_and_first_local_ip(self):
        output = self.OUTPUT + 'Local IP (wlan0): 10.0.0.5\n'
        with mock.patch.object(common.shutil, 'which', return_value='/usr/bin/fastfetch'), \
             mock.patch.object(common, 'run', return_value=output) as runner:
            info = common.fastfetch_info()
        self.assertEqual(info, {'os': 'Debian GNU/Linux 13 (trixie)', 'host': 'leon',
                                'kernel': '6.12.0-amd64', 'uptime': '3 days, 4 hours',
                                'local ip': '192.168.1.20'})
        runner.assert_called_once()

    def test_command_line_and_no_shell(self):
        calls = []

        def runner(*argv, **kwargs):
            calls.append((argv, kwargs))
            return self.OUTPUT

        with mock.patch.object(common.shutil, 'which', return_value='/usr/bin/fastfetch'), \
             mock.patch.object(common, 'run', side_effect=runner):
            common.fastfetch_info()
        argv, kwargs = calls[0]
        self.assertEqual(argv[:6], ('fastfetch', '--config', 'none', '--pipe', '--logo', 'none'))
        self.assertEqual(argv[6:], ('--structure', 'os:host:kernel:uptime:localip'))
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs.get('check'), False)

    def test_falls_back_to_next_structure(self):
        outputs = ['', self.OUTPUT]

        def runner(*_argv, **_kwargs):
            return outputs.pop(0) if outputs else ''

        with mock.patch.object(common.shutil, 'which', return_value='/usr/bin/fastfetch'), \
             mock.patch.object(common, 'run', side_effect=runner) as run_mock:
            info = common.fastfetch_info()
        self.assertEqual(info['host'], 'leon')
        self.assertEqual(run_mock.call_count, 2)

    def test_panel_error_never_propagates(self):
        with mock.patch.object(common.shutil, 'which', return_value='/usr/bin/fastfetch'), \
             mock.patch.object(common, 'run', side_effect=common.PanelError('boom')):
            self.assertEqual(common.fastfetch_info(), {})

    def test_unusable_output_returns_empty(self):
        with mock.patch.object(common.shutil, 'which', return_value='/usr/bin/fastfetch'), \
             mock.patch.object(common, 'run', return_value='pas de deux-points\n'):
            self.assertEqual(common.fastfetch_info(), {})



if __name__ == '__main__':
    unittest.main()
