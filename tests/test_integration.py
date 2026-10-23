"""Native Debian 13 validation; never touch live Apache/systemd configuration."""
from __future__ import annotations

import os
import pty
import fcntl
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from serverpanel import sites, services, common


@unittest.skipUnless(shutil.which('apache2'), 'apache2 unavailable')
class ApacheIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.av = self.root / 'available'
        self.en = self.root / 'enabled'
        self.av.mkdir(); self.en.mkdir()
        self.doc = self.root / 'www'
        self.doc.mkdir(mode=0o755)
        self.apache_conf = self.root / 'apache2.conf'
        self.apache_conf.write_text(f'''ServerRoot "/etc/apache2"
ServerName localhost
PidFile "{self.root}/httpd.pid"
Listen 127.0.0.1:18082
LoadModule mpm_event_module /usr/lib/apache2/modules/mod_mpm_event.so
LoadModule authz_core_module /usr/lib/apache2/modules/mod_authz_core.so
LoadModule proxy_module /usr/lib/apache2/modules/mod_proxy.so
LoadModule proxy_http_module /usr/lib/apache2/modules/mod_proxy_http.so
ErrorLog "{self.root}/error.log"
LogFormat "%h %l %u %t \\"%r\\" %>s %b" combined
Define APACHE_LOG_DIR {self.root}
IncludeOptional "{self.en}/*.conf"
''')
        self.commands = []

    def command(self, *argv, **kw):
        self.commands.append(argv)
        if argv[0] == 'a2ensite':
            (self.en / argv[1]).symlink_to(self.av / argv[1])
        if argv[0] == 'a2dissite':
            (self.en / argv[1]).unlink(missing_ok=True)
        if argv[0] == 'apache2ctl':
            done = subprocess.run(['apache2', '-t', '-f', str(self.apache_conf)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if done.returncode:
                raise common.PanelError(done.stdout)
        return ''

    def patches(self):
        return (mock.patch.object(sites, 'root_only'), mock.patch.object(sites, 'AVAILABLE', self.av),
                mock.patch.object(sites, 'ENABLED', self.en), mock.patch.object(sites, 'WEBROOT', self.doc),
                mock.patch.object(sites, 'run', self.command))

    def test_real_apache_config_static_and_proxy(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self.patches(): stack.enter_context(p)
            sites.create('example.be', path=str(self.doc / 'example'), www=True)
            sites.create('api.example.be', proxy_port=3210)
        self.assertTrue((self.av / 'panel-example.be.conf').exists())
        self.assertTrue((self.en / 'panel-api.example.be.conf').exists())
        self.assertIn(('apache2ctl', '-t'), self.commands)

    def test_real_local_http_response(self):
        public = self.doc / 'example'
        public.mkdir()
        (public / 'index.html').write_text('ServerPanel E2E 200', encoding='utf-8')
        self.apache_conf.write_text(self.apache_conf.read_text().replace(
            'LoadModule authz_core_module /usr/lib/apache2/modules/mod_authz_core.so',
            'LoadModule authz_core_module /usr/lib/apache2/modules/mod_authz_core.so\n'
            'LoadModule dir_module /usr/lib/apache2/modules/mod_dir.so\n'
            'DirectoryIndex index.html\nUser www-data\nGroup www-data'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.apache_conf.write_text(self.apache_conf.read_text().replace('127.0.0.1:18082', f'127.0.0.1:{port}'))
        conf = self.en / 'site.conf'
        with mock.patch.object(sites, 'WEBROOT', self.doc):
            conf.write_text(sites.vhost('example.be', root=public).replace('<VirtualHost *:80>', f'<VirtualHost *:{port}>'))
        syntax = subprocess.run(['apache2', '-t', '-f', str(self.apache_conf)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        apache = subprocess.Popen(['apache2', '-X', '-f', str(self.apache_conf)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            end = time.monotonic() + 6
            error = ''
            while time.monotonic() < end and apache.poll() is None:
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    request = urllib.request.Request(f'http://127.0.0.1:{port}/', headers={'Host': 'example.be'})
                    with opener.open(request, timeout=1) as reply:
                        self.assertEqual(reply.status, 200)
                        self.assertIn(b'ServerPanel E2E 200', reply.read())
                    break
                except OSError as exc:
                    error = str(exc)
                    time.sleep(0.1)
            else:
                self.fail(f'Apache HTTP unavailable {error}, rc={apache.poll()}')
        finally:
            apache.terminate()
            try: apache.wait(timeout=3)
            except subprocess.TimeoutExpired:
                apache.kill(); apache.wait(timeout=3)

    def test_enabled_toggle_rollback(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self.patches(): stack.enter_context(p)
            sites.create('example.be', path=str(self.doc / 'example'))
            site = sites.inventory()[0]
            sites.toggle(site, False)
            self.assertFalse((self.en / site.filename).exists())
            sites.toggle(site, True)
            self.assertTrue((self.en / site.filename).exists())


@unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze unavailable')
class SystemdIntegration(unittest.TestCase):
    def test_unit_accepted_by_systemd_analyze(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'panel-test.service'
            path.write_text(services.unit_content('test', ['/usr/bin/python3', '/srv/apps/test/start.py'],
                                                  Path('/srv/apps/test'), 'nobody'))
            p = subprocess.run(['systemd-analyze', 'verify', str(path)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class PackageIntegration(unittest.TestCase):
    def test_package_layout_and_no_demo(self):
        deb = Path(__file__).resolve().parents[1] / 'dist/serverpanel_1.0.0~beta1-1_all.deb'
        if not deb.exists():
            self.skipTest('Build .deb before tests')
        p = subprocess.run(['dpkg-deb', '-c', str(deb)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        for file in ('./usr/bin/panel', './etc/serverpanel/config.json',
                     './etc/update-motd.d/85-serverpanel', './usr/lib/serverpanel/serverpanel/ui.py'):
            self.assertIn(file, p.stdout)
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(['dpkg-deb', '-x', str(deb), folder], check=True)
            app = Path(folder) / 'usr/lib/serverpanel/serverpanel'
            self.assertFalse(any('demo' in f.read_text().lower() for f in app.glob('*.py')))
            self.assertTrue((Path(folder) / 'usr/bin/panel').exists())


@unittest.skipUnless(os.name == 'posix', 'PTY requires Linux')
class TerminalIntegration(unittest.TestCase):
    def test_live_curses_key_navigation_without_fake_data(self):
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 98, 0, 0))
            proc = subprocess.Popen([sys.executable, '-c', 'from serverpanel.ui import launch; launch()'],
                                    stdin=slave, stdout=slave, stderr=slave,
                                    cwd=str(Path(__file__).resolve().parents[1]),
                                    env={**os.environ, 'TERM': 'xterm-256color', 'PYTHONPATH': '.'})
            os.close(slave); slave = None
            output = bytearray()
            def read_for(seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    available, _, _ = select.select([master], [], [], .04)
                    if available:
                        try: output.extend(os.read(master, 65536))
                        except OSError: break
            for key in (b'\x1bOC', b'\r', b'\x1bOD', b'\x1b', b'\x1bOC', b'\x1bOC', b'q'):
                read_for(.16)
                os.write(master, key)
            read_for(.15)
            try: rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
                self.fail('TUI did not exit after Q')
            self.assertEqual(rc, 0, output.decode('utf8', 'replace')[-800:])
            self.assertIn('PANEL', output.decode('utf8', 'replace'))
            self.assertIn('SITES', output.decode('utf8', 'replace'))
        finally:
            if slave is not None: os.close(slave)
            os.close(master)
