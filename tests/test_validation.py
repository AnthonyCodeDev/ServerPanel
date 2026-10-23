import json
import os
import tempfile
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

    def test_vhost_static(self):
        value = sites.vhost('example.be', root=Path('/var/www/example.be/public'), www=True, htaccess=True)
        for needle in ('ServerName example.be', 'ServerAlias www.example.be', 'AllowOverride All', 'Options -Indexes'):
            self.assertIn(needle, value)

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


if __name__ == '__main__':
    unittest.main()
