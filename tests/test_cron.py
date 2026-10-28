"""Tests du module cron : aucun accès réel à /etc, aucun besoin de root ni de cron."""
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from serverpanel import common, cron

POSIX = os.name == 'posix'


class CronBase(unittest.TestCase):
    """Environnement jetable : config.json, /etc/cron.d et spool simulés."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cron_d = root / 'cron.d'
        self.cron_d.mkdir()
        self.managed = self.cron_d / 'serverpanel'
        self.config = root / 'config.json'
        self.crontab = root / 'crontab'
        self.spool = root / 'spool'
        self.spool.mkdir()
        for patch in (
            mock.patch.object(cron, 'CONFIG', self.config),
            mock.patch.object(cron, 'CRON_D', self.cron_d),
            mock.patch.object(cron, 'MANAGED', self.managed),
            mock.patch.object(cron, 'CRONTAB', self.crontab),
            mock.patch.object(cron, 'SPOOL', self.spool),
            mock.patch.object(cron, 'root_only'),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        if not POSIX:
            # os.fchmod n'existe que sous Unix : common.atomic_write reste testable ici.
            fchmod = mock.patch.object(common.os, 'fchmod', lambda fd, mode: None, create=True)
            fchmod.start()
            self.addCleanup(fchmod.stop)
        cron._CACHE.clear()
        self.saved = {'pinned': ['ssh'], 'custom_services': {'bot': {'folder': '/srv/apps/bot'}},
                      'future': {'nested': True}}
        self.write_config(self.saved)

    def write_config(self, data) -> None:
        text = data if isinstance(data, str) else json.dumps(data, indent=2)
        self.config.write_text(text, encoding='utf-8')

    def read_config(self) -> dict:
        return json.loads(self.config.read_text(encoding='utf-8'))

    def managed_text(self) -> str:
        return self.managed.read_text(encoding='utf-8')


class Validation(unittest.TestCase):
    def test_validate_accepts_normal_forms(self):
        cases = {
            '* * * * *': '* * * * *',
            '  */15   *  * * *  ': '*/15 * * * *',
            '0 3 * * *': '0 3 * * *',
            '0 0 1,15 * *': '0 0 1,15 * *',
            '0 8 * * 1-5': '0 8 * * 1-5',
            '5,10-20 * * * *': '5,10-20 * * * *',
            '30 2 * * 0-7': '30 2 * * 0-7',
            '@daily': '@daily',
            '@REBOOT': '@reboot',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(cron.validate_schedule(value), expected)

    def test_validate_rejects_invalid_forms(self):
        values = ('', '   ', '0 3 * *', '0 3 * * * *', '60 * * * *', '* 24 * * *',
                  '0 0 0 * *', '0 0 32 * *', '0 0 * 13 *', '0 0 * * 8', '*/0 * * * *',
                  '0 3 * * *\n', '0 3 * * %', 'a b c d e', '@sometimes', '0 3 1-0 * *',
                  '-1 * * * *', '0 3 * * 1,,2', '0 3 * * 1-')
        for value in values:
            with self.subTest(value=value), self.assertRaises(cron.PanelError):
                cron.validate_schedule(value)

    def test_parse_hhmm(self):
        self.assertEqual(cron.parse_hhmm('3'), (3, 0))
        self.assertEqual(cron.parse_hhmm('3:30'), (3, 30))
        self.assertEqual(cron.parse_hhmm('03:30'), (3, 30))
        self.assertEqual(cron.parse_hhmm('3h30'), (3, 30))
        self.assertEqual(cron.parse_hhmm('23:59'), (23, 59))
        for value in ('24:00', '3:60', 'abc', '', '3:3:3', '-1'):
            with self.subTest(value=value), self.assertRaises(cron.PanelError):
                cron.parse_hhmm(value)


class Builders(unittest.TestCase):
    def test_daily(self):
        self.assertEqual(cron.schedule_daily(3), '0 3 * * *')
        self.assertEqual(cron.schedule_daily(3, 30), '30 3 * * *')
        for hour, minute in ((24, 0), (-1, 0), (1, 60)):
            with self.subTest(hour=hour, minute=minute), self.assertRaises(cron.PanelError):
                cron.schedule_daily(hour, minute)

    def test_weekly(self):
        self.assertEqual(cron.schedule_weekly([1, 3, 5], 8), '0 8 * * 1,3,5')
        self.assertEqual(cron.schedule_weekly([7], 0), '0 0 * * 0')
        self.assertEqual(cron.schedule_weekly([5, 1, 1], 6, 30), '30 6 * * 1,5')
        for days in ([], [8], [-1]):
            with self.subTest(days=days), self.assertRaises(cron.PanelError):
                cron.schedule_weekly(days, 8)

    def test_monthly(self):
        self.assertEqual(cron.schedule_monthly([1, 15], 0), '0 0 1,15 * *')
        self.assertEqual(cron.schedule_monthly([31, 1, 31], 23, 5), '5 23 1,31 * *')
        for days in ([], [0], [32]):
            with self.subTest(days=days), self.assertRaises(cron.PanelError):
                cron.schedule_monthly(days, 0)

    def test_every_minutes(self):
        self.assertEqual(cron.schedule_every_minutes(15), '*/15 * * * *')
        self.assertEqual(cron.schedule_every_minutes(1), '*/1 * * * *')
        for value in (0, 60):
            with self.subTest(value=value), self.assertRaises(cron.PanelError):
                cron.schedule_every_minutes(value)

    def test_every_hours(self):
        self.assertEqual(cron.schedule_every_hours(6), '0 */6 * * *')
        self.assertEqual(cron.schedule_every_hours(2, 30), '30 */2 * * *')
        for value in (0, 24):
            with self.subTest(value=value), self.assertRaises(cron.PanelError):
                cron.schedule_every_hours(value)
        with self.assertRaises(cron.PanelError):
            cron.schedule_every_hours(2, 60)

    def test_custom(self):
        self.assertEqual(cron.schedule_custom('0', '3', '*', '*', '*'), '0 3 * * *')
        self.assertEqual(cron.schedule_custom('*/5', '9-17', '1,15', '*', '1-5'),
                         '*/5 9-17 1,15 * 1-5')
        for fields in (('60', '*', '*', '*', '*'), ('*', '24', '*', '*', '*'),
                       ('*', '*', '0', '*', '*'), ('*', '*', '*', '*', '8'),
                       ('*/0', '*', '*', '*', '*')):
            with self.subTest(fields=fields), self.assertRaises(cron.PanelError):
                cron.schedule_custom(*fields)


class Description(unittest.TestCase):
    LONG = {
        '* * * * *': 'Chaque minute',
        '*/15 * * * *': 'Toutes les 15 minutes',
        '0 * * * *': 'Chaque heure à la minute 0',
        '0 3 * * *': 'Chaque jour à 03:00',
        '0 9 * * 1': 'Chaque lundi à 09:00',
        '0 8 * * 1,3,5': 'Lundi, mercredi et vendredi à 08:00',
        '0 8 * * 1-5': 'Du lundi au vendredi à 08:00',
        '0 0 1 * *': 'Le 1er de chaque mois à 00:00',
        '0 0 1,15 * *': 'Les 1 et 15 de chaque mois à 00:00',
        '0 */6 * * *': 'Toutes les 6 heures, à la minute 0',
        '0 9-17 * * *': 'Chaque jour de 09:00 à 17:00',
        '0 0 1 1 *': 'Le 1er janvier à 00:00',
        '0 0 * * 7': 'Chaque dimanche à 00:00',
        '@reboot': 'Au démarrage',
        '@hourly': 'Chaque heure',
        '@daily': 'Chaque jour à 00:00',
        '@midnight': 'Chaque jour à 00:00',
        '@weekly': 'Chaque semaine (dimanche à 00:00)',
        '@monthly': 'Le 1er de chaque mois à 00:00',
        '@yearly': 'Le 1er janvier à 00:00',
        '@annually': 'Le 1er janvier à 00:00',
        '5 4 3 2 1': 'Personnalisé · 5 4 3 2 1',
        'nope': 'Personnalisé · nope',
    }
    SHORT = {
        '* * * * *': 'Chaque minute',
        '*/15 * * * *': 'Toutes les 15 min',
        '0 * * * *': 'Chaque heure',
        '0 3 * * *': 'Chaque jour 03:00',
        '0 9 * * 1': 'Lundi 09:00',
        '0 8 * * 1,3,5': 'Lundi, mercredi et vendredi 08:00',
        '0 8 * * 1-5': 'Du lundi au vendredi 08:00',
        '0 0 1 * *': 'Le 1er du mois 00:00',
        '0 0 1,15 * *': 'Les 1 et 15 du mois 00:00',
        '0 */6 * * *': 'Toutes les 6 h',
        '0 9-17 * * *': 'Chaque jour de 09:00 à 17:00',
        '0 0 1 1 *': 'Le 1er janvier à 00:00',
        '@daily': 'Chaque jour 00:00',
        '@weekly': 'Chaque semaine (dimanche à 00:00)',
        '@monthly': 'Le 1er du mois 00:00',
        'nope': 'Personnalisé · nope',
    }

    def test_long_descriptions(self):
        for schedule, expected in self.LONG.items():
            with self.subTest(schedule=schedule):
                self.assertEqual(cron.describe(schedule), expected)

    def test_short_descriptions(self):
        for schedule, expected in self.SHORT.items():
            with self.subTest(schedule=schedule):
                self.assertEqual(cron.describe(schedule, short=True), expected)

    def test_job_when_uses_describe(self):
        job = cron.CronJob('Test', '/bin/true', 'root', '0 3 * * *', '/etc/crontab')
        self.assertEqual(job.when, 'Chaque jour à 03:00')


class ScheduleParsing(unittest.TestCase):
    def test_daily(self):
        parsed = cron.parse_schedule('0 3 * * *')
        self.assertEqual(parsed['kind'], 'daily')
        self.assertEqual((parsed['minute'], parsed['hour']), (0, 3))
        self.assertEqual(parsed['days'], [])
        self.assertEqual(parsed['fields']['weekday'], '*')

    def test_weekly(self):
        parsed = cron.parse_schedule('0 8 * * 1,3,5')
        self.assertEqual(parsed['kind'], 'weekly')
        self.assertEqual(parsed['days'], [1, 3, 5])
        self.assertEqual((parsed['minute'], parsed['hour']), (0, 8))

    def test_monthly(self):
        parsed = cron.parse_schedule('0 0 1,15 * *')
        self.assertEqual(parsed['kind'], 'monthly')
        self.assertEqual(parsed['days'], [1, 15])

    def test_hours_and_minutes(self):
        hours = cron.parse_schedule('0 */6 * * *')
        self.assertEqual(hours['kind'], 'hours')
        self.assertEqual(hours['interval'], 6)
        minutes = cron.parse_schedule('*/15 * * * *')
        self.assertEqual(minutes['kind'], 'minutes')
        self.assertEqual(minutes['interval'], 15)

    def test_custom_and_defaults(self):
        parsed = cron.parse_schedule('5 4 3 2 1')
        self.assertEqual(parsed['kind'], 'custom')
        self.assertEqual(parsed['fields'],
                         {'minute': '5', 'hour': '4', 'day': '3', 'month': '2', 'weekday': '1'})
        for value in ('nope', '', '@reboot', '99 * * * *'):
            with self.subTest(value=value):
                parsed = cron.parse_schedule(value)
                self.assertEqual(parsed['kind'], 'custom')
                self.assertEqual((parsed['minute'], parsed['hour'], parsed['days'],
                                  parsed['interval']), (0, 0, [], 1))
                self.assertEqual(set(parsed['fields']),
                                 {'minute', 'hour', 'day', 'month', 'weekday'})

    def test_macro_roundtrip(self):
        self.assertEqual(cron.parse_schedule('@daily')['kind'], 'daily')
        self.assertEqual(cron.parse_schedule('@weekly')['days'], [0])


class Labels(unittest.TestCase):
    def test_default_label(self):
        cases = {
            '/usr/local/bin/backup.sh': 'backup.sh',
            '/usr/local/bin/backup.sh --now': 'backup.sh',
            'python3 /srv/apps/bot/main.py -v': 'main.py',
            '/usr/bin/env python3 /srv/apps/bot/job.py': 'job.py',
            'systemctl restart nginx': 'systemctl',
            # Bruit shell : le vrai programme est plus loin dans la ligne.
            ('test -x /usr/bin/certbot -a \\! -d /run/systemd/system '
             '&& perl -e 1 && certbot -q renew'): 'certbot',
            'cd / && run-parts --report /etc/cron.daily': 'cron.daily',
            'test -x /usr/sbin/anacron || ( cd / && run-parts --report /etc/cron.weekly )':
                'anacron',
            '[ -x /usr/bin/foo ]': 'foo',
            'true &&  /usr/local/bin/backup.sh': 'backup.sh',
            '/': 'Cron',
            'echo hello': 'Cron',
            'true && cd /': 'Cron',
            '': 'Cron',
            '   ': 'Cron',
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(cron.default_label(command), expected)

    def test_run_parts_target(self):
        self.assertEqual(cron._run_parts_target('cd / && run-parts --report /etc/cron.daily'),
                         'cron.daily')
        self.assertEqual(cron._run_parts_target('run-parts /etc/cron.weekly'), 'cron.weekly')
        self.assertEqual(cron._run_parts_target('run-parts /etc/cron.monthly'), 'cron.monthly')
        self.assertEqual(cron._run_parts_target('/usr/bin/uptime'), '')
        self.assertEqual(cron._run_parts_target(''), '')

    def test_external_label(self):
        self.assertEqual(cron._external_label(str(cron.CRONTAB),
                                             'test -x /usr/sbin/anacron || ( cd / && '
                                             'run-parts --report /etc/cron.weekly )', 'root'),
                         'cron.weekly')
        self.assertEqual(cron._external_label(str(cron.CRONTAB), '/usr/bin/uptime', 'root'),
                         'uptime')
        self.assertEqual(cron._external_label(str(cron.CRON_D / 'certbot'),
                                             'test -x /usr/bin/certbot && certbot renew', 'root'),
                         'certbot')
        self.assertEqual(cron._external_label(str(cron.SPOOL / 'alice'),
                                             '/home/alice/job.sh', 'alice'), 'alice · job.sh')
        self.assertEqual(cron._external_label(str(cron.SPOOL / 'alice'), 'true', 'alice'),
                         'alice')
        self.assertEqual(cron._external_label('/etc/cron.d/serverpanel', '/bin/true', 'root'),
                         'serverpanel')
        self.assertEqual(cron._external_label('/autre/fichier', 'systemctl restart nginx',
                                             'root'), 'systemctl')

    def test_slugify(self):
        self.assertEqual(cron._slugify('Sauvegarde été'), 'sauvegarde-ete')
        self.assertEqual(cron._slugify('***'), 'cron')
        self.assertEqual(len(cron._slugify('a' * 80)), 40)


class Storage(CronBase):
    def test_add_writes_config_and_managed_file(self):
        slug = cron.add('Sauvegarde', '/usr/local/bin/backup.sh', '0 3 * * *')
        self.assertEqual(slug, 'sauvegarde')
        data = self.read_config()
        self.assertEqual(data['crons']['sauvegarde'], {
            'label': 'Sauvegarde', 'command': '/usr/local/bin/backup.sh',
            'schedule': '0 3 * * *', 'user': 'root', 'enabled': True})
        # Toutes les autres clés sont préservées
        self.assertEqual(data['pinned'], ['ssh'])
        self.assertEqual(data['custom_services'], {'bot': {'folder': '/srv/apps/bot'}})
        self.assertEqual(data['future'], {'nested': True})
        text = self.managed_text()
        self.assertTrue(text.startswith('# ServerPanel'))
        self.assertIn('SHELL=/bin/sh', text)
        self.assertIn('MAILTO=""', text)
        self.assertIn('# Sauvegarde — Chaque jour à 03:00', text)
        self.assertIn('0 3 * * * root /usr/local/bin/backup.sh', text)
        self.assertTrue(text.endswith('\n'))

    def test_managed_file_mode(self):
        with mock.patch.object(cron, 'atomic_write') as write:
            cron.add('Test', '/bin/true', '* * * * *')
        calls = write.call_args_list
        self.assertEqual(calls[0][0][0], self.config)
        self.assertEqual(calls[1][0][0], self.managed)
        self.assertEqual(calls[1][0][2], 0o644)
        if POSIX:
            cron.add('Autre', '/bin/false', '@daily')
            self.assertEqual(self.managed.stat().st_mode & 0o777, 0o644)

    def test_percent_is_escaped_in_managed_file(self):
        cron.add('Date', 'date +%Y-%m-%d', '@daily')
        self.assertIn('@daily root date +\\%Y-\\%m-\\%d', self.managed_text())
        self.assertEqual(self.read_config()['crons']['date']['command'], 'date +%Y-%m-%d')

    def test_command_spacing_is_preserved(self):
        cron.add('Espaces', 'printf "a  b"', '@daily')
        self.assertIn('@daily root printf "a  b"', self.managed_text())

    def test_disabled_job_only_leaves_a_comment(self):
        slug = cron.add('Veille', '/usr/local/bin/nightly.sh', '30 23 * * *')
        cron.set_enabled(slug, False)
        self.assertFalse(self.read_config()['crons'][slug]['enabled'])
        text = self.managed_text()
        self.assertIn('# DÉSACTIVÉ — Veille · 30 23 * * *', text)
        self.assertNotIn('30 23 * * * root', text)

    def test_add_uses_default_label_and_unique_slug(self):
        first = cron.add('', 'python3 /srv/apps/bot/main.py --verbose', '* * * * *')
        second = cron.add('', 'python3 /srv/apps/bot/main.py --verbose', '* * * * *')
        self.assertEqual((first, second), ('main-py', 'main-py-2'))
        crons = self.read_config()['crons']
        self.assertEqual(crons[first]['label'], 'main.py')
        self.assertEqual([entry['label'] for entry in crons.values()], ['main.py', 'main.py'])

    def test_add_refusals(self):
        for label, command, schedule, user in (
            ('X', '', '* * * * *', 'root'),
            ('X', '/bin/true\nrm -rf /', '* * * * *', 'root'),
            ('X', 'a' * 901, '* * * * *', 'root'),
            ('X', '/bin/true', 'bogus', 'root'),
            ('X', '/bin/true', '* * * * *', 'Root'),
            ('Libellé\ncassé', '/bin/true', '* * * * *', 'root'),
            ('L' * 61, '/bin/true', '* * * * *', 'root'),
        ):
            with self.subTest(label=label, command=command[:20], schedule=schedule, user=user):
                with self.assertRaises(cron.PanelError):
                    cron.add(label, command, schedule, user)
        self.assertEqual(self.read_config(), self.saved)
        self.assertFalse(self.managed.exists())

    def test_update(self):
        slug = cron.add('Sauvegarde', '/bin/true', '0 3 * * *')
        cron.update(slug, label='Backup', command='/bin/false', schedule='*/5 * * * *',
                    user='www-data')
        self.assertEqual(self.read_config()['crons'][slug], {
            'label': 'Backup', 'command': '/bin/false', 'schedule': '*/5 * * * *',
            'user': 'www-data', 'enabled': True})
        text = self.managed_text()
        self.assertIn('# Backup — Toutes les 5 minutes', text)
        self.assertIn('*/5 * * * * www-data /bin/false', text)
        with self.assertRaises(cron.PanelError):
            cron.update('inconnu', label='X')
        with self.assertRaises(cron.PanelError):
            cron.update(slug, folder='/tmp')
        with self.assertRaises(cron.PanelError):
            cron.update(slug, enabled='oui')
        self.assertEqual(self.read_config()['pinned'], ['ssh'])

    def test_remove(self):
        slug = cron.add('Unique', '/bin/true', '@daily')
        cron.remove(slug)
        self.assertEqual(self.read_config()['crons'], {})
        self.assertIn('# Aucun cron géré.', self.managed_text())
        with self.assertRaises(cron.PanelError):
            cron.remove('disparu')

    def test_missing_config_is_created(self):
        self.config.unlink()
        slug = cron.add('Neuf', '/bin/true', '@daily')
        self.assertEqual(self.read_config(), {'crons': {slug: {
            'label': 'Neuf', 'command': '/bin/true', 'schedule': '@daily',
            'user': 'root', 'enabled': True}}})

    def test_broken_config_is_never_overwritten(self):
        broken = '{"crons": {"x": '
        self.write_config(broken)
        with self.assertRaisesRegex(cron.PanelError, 'invalide'):
            cron.add('Test', '/bin/true', '* * * * *')
        self.assertEqual(self.config.read_text(encoding='utf-8'), broken)
        self.assertFalse(self.managed.exists())
        self.write_config('[]')
        with self.assertRaisesRegex(cron.PanelError, 'invalide'):
            cron.remove('x')
        self.assertEqual(self.config.read_text(encoding='utf-8'), '[]')

    def test_sync_managed_requires_root(self):
        with mock.patch.object(cron.os, 'geteuid', return_value=1000, create=True):
            cron._sync_managed()
        self.assertFalse(self.managed.exists())

    def test_sync_managed_writes_once_and_never_raises(self):
        with mock.patch.object(cron.os, 'geteuid', return_value=0, create=True):
            cron._sync_managed()
            self.assertIn('# Aucun cron géré.', self.managed_text())
            with mock.patch.object(cron, 'atomic_write') as write:
                cron._sync_managed()
            write.assert_not_called()
            self.write_config('{cassé')
            self.managed.unlink()
            cron._sync_managed()
            self.assertFalse(self.managed.exists())


class Inventory(CronBase):
    def prepare(self):
        self.write_config({'crons': {
            'sauvegarde': {'label': 'Sauvegarde', 'command': '/usr/local/bin/backup.sh',
                           'schedule': '0 3 * * *', 'user': 'root', 'enabled': True},
            'veille': {'label': 'veille', 'command': '/usr/local/bin/nightly.sh',
                       'schedule': "n'importe quoi", 'user': 'root', 'enabled': False},
        }})
        self.crontab.write_text(
            'SHELL=/bin/sh\n'
            '# commentaire ignoré\n'
            '17 * * * * root /usr/bin/uptime\n'
            '@daily root /usr/local/bin/cleanup.sh --fast\n'
            'ligne mal formée\n', encoding='utf-8')
        (self.cron_d / 'backup').write_text('30 2 * * * www-data /usr/local/bin/backup.sh\n')
        (self.cron_d / 'nightly').write_text('@reboot root /usr/local/bin/nightly\n')
        (self.cron_d / 'readme.conf').write_text('0 0 * * * root /ignore/conf\n')
        (self.spool / 'alice').write_text('0 5 * * * /home/alice/job.sh\n'
                                          '@reboot /home/alice/boot.sh\n')

    def test_inventory_merges_managed_and_external(self):
        self.prepare()
        with mock.patch.object(cron.os, 'geteuid', return_value=0, create=True):
            jobs = cron.inventory()
        managed = [job for job in jobs if job.managed]
        self.assertEqual([job.slug for job in managed], ['sauvegarde', 'veille'])
        self.assertEqual(managed[0].source, str(self.managed))
        self.assertEqual(managed[0].line, 0)
        self.assertTrue(managed[0].enabled)
        self.assertFalse(managed[1].enabled)
        self.assertTrue(managed[1].when.startswith('Personnalisé'))
        text = self.managed_text()
        self.assertIn('0 3 * * * root /usr/local/bin/backup.sh', text)
        self.assertFalse(any(line.startswith("n'importe") for line in text.splitlines()))

        external = [job for job in jobs if not job.managed]
        self.assertEqual(external, sorted(external, key=lambda job: (job.source, job.line)))
        self.assertFalse(any(job.source == str(self.managed) for job in external))
        labels = {job.label: job for job in external}
        self.assertEqual(set(labels),
                         {'uptime', 'cleanup.sh', 'backup', 'nightly',
                          'alice · job.sh', 'alice · boot.sh'})
        self.assertEqual((labels['uptime'].schedule, labels['uptime'].user,
                          labels['uptime'].line, labels['uptime'].source),
                         ('17 * * * *', 'root', 3, str(self.crontab)))
        self.assertEqual((labels['cleanup.sh'].schedule, labels['cleanup.sh'].line),
                         ('@daily', 4))
        self.assertEqual((labels['backup'].schedule, labels['backup'].user,
                          labels['backup'].line, labels['backup'].source),
                         ('30 2 * * *', 'www-data', 1, str(self.cron_d / 'backup')))
        self.assertEqual((labels['nightly'].schedule, labels['nightly'].command,
                          labels['nightly'].source),
                         ('@reboot', '/usr/local/bin/nightly', str(self.cron_d / 'nightly')))
        self.assertEqual((labels['alice · job.sh'].user, labels['alice · job.sh'].line,
                          labels['alice · job.sh'].source),
                         ('alice', 1, str(self.spool / 'alice')))
        self.assertEqual((labels['alice · boot.sh'].schedule, labels['alice · boot.sh'].user),
                         ('@reboot', 'alice'))
        self.assertFalse(any('/ignore/conf' in job.command for job in external))

    def test_system_cron_labels_are_meaningful(self):
        """Les lignes Debian standard ne doivent plus s'appeler « test », « cd » ou « [ »."""
        self.write_config({})
        self.crontab.write_text(
            'SHELL=/bin/sh\n'
            '25 6 * * * root test -x /usr/sbin/anacron || ( cd / && '
            'run-parts --report /etc/cron.daily )\n'
            '47 6 * * 7 root test -x /usr/sbin/anacron || ( cd / && '
            'run-parts --report /etc/cron.weekly )\n'
            '52 6 1 * * root test -x /usr/sbin/anacron || ( cd / && '
            'run-parts --report /etc/cron.monthly )\n', encoding='utf-8')
        (self.cron_d / 'certbot').write_text(
            '0 */12 * * * root test -x /usr/bin/certbot -a \\! -d /run/systemd/system '
            '&& perl -e 1 && certbot -q renew --no-random-sleep-on-renew\n')
        (self.cron_d / 'e2scrub_all').write_text(
            '*/10 * * * * root test -e /run/systemd/system || '
            'cd / && /sbin/e2scrub_all -A -r\n')
        with mock.patch.object(cron.os, 'geteuid', return_value=0, create=True):
            jobs = cron.inventory()
        labels = {job.label: job for job in jobs}
        self.assertEqual(set(labels), {'cron.daily', 'cron.weekly', 'cron.monthly',
                                       'certbot', 'e2scrub_all'})
        for period in ('cron.daily', 'cron.weekly', 'cron.monthly'):
            self.assertEqual(labels[period].source, str(self.crontab))
            self.assertEqual(labels[period].user, 'root')
        self.assertEqual(labels['certbot'].source, str(self.cron_d / 'certbot'))
        self.assertEqual(labels['e2scrub_all'].source, str(self.cron_d / 'e2scrub_all'))
        for noise in ('test', 'cd', '['):
            self.assertNotIn(noise, labels)

    def test_inventory_without_root_does_not_write(self):
        self.prepare()
        with mock.patch.object(cron.os, 'geteuid', return_value=1000, create=True):
            jobs = cron.inventory()
        self.assertFalse(self.managed.exists())
        self.assertEqual(len([job for job in jobs if job.managed]), 2)

    def test_inventory_ignores_unreadable_sources(self):
        self.config.unlink()
        with mock.patch.object(cron, 'CRONTAB', self.cron_d), \
             mock.patch.object(cron, 'CRON_D', Path(self.temp.name) / 'absent'), \
             mock.patch.object(cron, 'SPOOL', Path(self.temp.name) / 'absent'):
            self.assertEqual(cron.inventory(), [])

    def test_inventory_never_raises_on_broken_config(self):
        self.write_config('{"crons": ')
        with mock.patch.object(cron.os, 'geteuid', return_value=0, create=True):
            self.assertEqual(cron.inventory(), [])
        self.assertFalse(self.managed.exists())


class Pinned(CronBase):
    """Favoris cron : clés stables, config.json préservé, plafond respecté."""

    def external(self, **overrides):
        fields = dict(label='Uptime', command='/usr/bin/uptime', user='root',
                      schedule='17 * * * *', source='/etc/crontab')
        fields.update(overrides)
        return cron.CronJob(**fields)

    def managed_job(self, **overrides):
        fields = dict(label='Sauvegarde', command='/usr/local/bin/backup.sh', user='root',
                      schedule='0 3 * * *', source=str(self.managed),
                      managed=True, slug='sauvegarde')
        fields.update(overrides)
        return cron.CronJob(**fields)

    def test_key_is_stable_for_external_jobs(self):
        first = self.external()
        self.assertTrue(first.key.startswith('ext:'))
        self.assertTrue(cron.PIN_RX.fullmatch(first.key))
        self.assertEqual(first.key, self.external().key)
        # Le numéro de ligne ne compte pas : le favori suit la ligne, pas le fichier.
        self.assertEqual(first.key, self.external(line=42).key)
        self.assertEqual(first.key, self.external(label='Autre libellé').key)
        self.assertNotEqual(first.key, self.external(command='/bin/true').key)
        self.assertNotEqual(first.key, self.external(schedule='0 4 * * *').key)
        self.assertNotEqual(first.key, self.external(user='www-data').key)
        self.assertNotEqual(first.key, self.external(source='/etc/cron.d/autre').key)

    def test_key_prefers_managed_slug(self):
        job = self.managed_job()
        self.assertEqual(job.key, 'panel:sauvegarde')
        self.assertTrue(cron.PIN_RX.fullmatch(job.key))
        # Géré sans slug : repli sur l'empreinte externe (même identité).
        nameless = self.managed_job(slug='')
        self.assertEqual(nameless.key,
                         self.external(label=nameless.label, command=nameless.command,
                                       schedule=nameless.schedule, user=nameless.user,
                                       source=nameless.source).key)

    def test_pinned_crons_filters_invalid_entries(self):
        self.write_config({'pinned_crons': ['panel:sauvegarde', 'ext:abc123',
                                            'invalide', 42, None, 'panel:', 'ext:a b',
                                            'x' * 80, 'panel:sauvegarde'],
                           'pinned': ['ssh']})
        self.assertEqual(cron.pinned_crons(), ['panel:sauvegarde', 'ext:abc123'])

    def test_pinned_crons_tolerates_bad_config(self):
        self.write_config({'pinned_crons': 'pas une liste'})
        self.assertEqual(cron.pinned_crons(), [])
        self.write_config('{cassé')
        self.assertEqual(cron.pinned_crons(), [])

    def test_toggle_pin_adds_then_removes(self):
        job = self.managed_job()
        self.assertTrue(cron.toggle_pin(job))
        data = self.read_config()
        self.assertEqual(data['pinned_crons'], ['panel:sauvegarde'])
        # Toutes les autres clés restent intactes.
        self.assertEqual(data['pinned'], ['ssh'])
        self.assertEqual(data['custom_services'], {'bot': {'folder': '/srv/apps/bot'}})
        self.assertEqual(data['future'], {'nested': True})
        self.assertIn('# Aucun cron géré.', self.managed_text())
        self.assertFalse(cron.toggle_pin(job))
        data = self.read_config()
        self.assertEqual(data['pinned_crons'], [])
        self.assertEqual(data['pinned'], ['ssh'])

    def test_toggle_pin_keeps_order_and_accepts_externals(self):
        jobs = [self.external(command='/bin/true'), self.external(), self.managed_job()]
        for job in jobs:
            self.assertTrue(cron.toggle_pin(job))
        self.assertEqual(self.read_config()['pinned_crons'],
                         [jobs[0].key, jobs[1].key, 'panel:sauvegarde'])

    def test_toggle_pin_refuses_invalid_key(self):
        with self.assertRaises(cron.PanelError):
            cron.toggle_pin(self.managed_job(slug='nom invalide'))
        self.assertNotIn('pinned_crons', self.read_config())

    def test_toggle_pin_never_overwrites_broken_config(self):
        broken = '{"crons": {"x": '
        self.write_config(broken)
        with self.assertRaises(cron.PanelError):
            cron.toggle_pin(self.managed_job())
        self.assertEqual(self.config.read_text(encoding='utf-8'), broken)
        self.assertFalse(self.managed.exists())

    def test_toggle_pin_minimum_when_config_missing(self):
        self.config.unlink()
        self.assertTrue(cron.toggle_pin(self.managed_job()))
        self.assertEqual(self.read_config(), {'pinned_crons': ['panel:sauvegarde']})

    def test_toggle_pin_is_capped_at_fifty(self):
        keys = [f'ext:{index:016x}' for index in range(50)]
        self.write_config({'pinned_crons': keys, 'pinned': ['ssh']})
        self.assertEqual(cron.pinned_crons(), keys)
        with self.assertRaises(cron.PanelError):
            cron.toggle_pin(self.managed_job())
        self.assertEqual(self.read_config()['pinned_crons'], keys)
        self.assertEqual(self.read_config()['pinned'], ['ssh'])
        # À 49 favoris, l'ajout passe et complète la liste.
        self.write_config({'pinned_crons': keys[:49]})
        self.assertTrue(cron.toggle_pin(self.managed_job()))
        pinned = self.read_config()['pinned_crons']
        self.assertEqual(len(pinned), 50)
        self.assertEqual(pinned[-1], 'panel:sauvegarde')


class NextRun(unittest.TestCase):
    def setUp(self):
        cron._CACHE.clear()

    def test_daily(self):
        self.assertEqual(cron.next_run('0 3 * * *', datetime(2026, 10, 28, 12, 0)),
                         datetime(2026, 10, 29, 3, 0))
        self.assertEqual(cron.next_run('0 3 * * *', datetime(2026, 10, 28, 2, 59)),
                         datetime(2026, 10, 28, 3, 0))
        self.assertEqual(cron.next_run('0 3 * * *', datetime(2026, 10, 28, 3, 0)),
                         datetime(2026, 10, 29, 3, 0))

    def test_weekly_and_monthly(self):
        now = datetime(2026, 10, 28, 12, 0)  # mercredi
        self.assertEqual(cron.next_run('0 9 * * 1', now), datetime(2026, 11, 2, 9, 0))
        self.assertEqual(cron.next_run('0 0 1 * *', now), datetime(2026, 11, 1, 0, 0))

    def test_intervals(self):
        self.assertEqual(cron.next_run('*/15 * * * *', datetime(2026, 10, 28, 10, 7)),
                         datetime(2026, 10, 28, 10, 15))
        self.assertEqual(cron.next_run('0 */6 * * *', datetime(2026, 10, 28, 10, 7)),
                         datetime(2026, 10, 28, 12, 0))

    def test_day_of_month_or_weekday(self):
        # dimanche 1er novembre (jour du mois) passe avant le lundi 2 novembre.
        self.assertEqual(cron.next_run('0 0 1 * 1', datetime(2026, 10, 28, 12, 0)),
                         datetime(2026, 11, 1, 0, 0))

    def test_leap_day(self):
        self.assertEqual(cron.next_run('0 0 29 2 *', datetime(2026, 3, 1)),
                         datetime(2028, 2, 29, 0, 0))

    def test_macros_and_invalid(self):
        self.assertIsNone(cron.next_run('@reboot'))
        self.assertIsNone(cron.next_run(''))
        self.assertIsNone(cron.next_run('pas une planification'))
        self.assertEqual(cron.next_run('@daily', datetime(2026, 10, 28, 12, 0)),
                         datetime(2026, 10, 29, 0, 0))
        self.assertEqual(cron.next_run('@hourly', datetime(2026, 10, 28, 12, 30)),
                         datetime(2026, 10, 28, 13, 0))

    def test_cache_is_bounded(self):
        for minute in range(200):
            cron.next_run('0 3 * * *', datetime(2026, 1, 1) + timedelta(minutes=minute))
        self.assertLessEqual(len(cron._CACHE), cron._CACHE_MAX)


class RunNow(CronBase):
    def test_root_command(self):
        job = cron.CronJob('Test', 'echo hi', 'root', '* * * * *', '/etc/crontab')
        done = mock.Mock(stdout='ligne1\nligne2\n', returncode=0)
        with mock.patch.object(cron.subprocess, 'run', return_value=done) as runner:
            lines = cron.run_now(job)
        self.assertEqual(lines, ['$ echo hi', 'ligne1', 'ligne2', 'Code 0'])
        self.assertEqual(runner.call_args[0][0], ['/bin/sh', '-c', 'echo hi'])
        options = runner.call_args[1]
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['stdout'], subprocess.PIPE)
        self.assertEqual(options['stderr'], subprocess.STDOUT)
        self.assertTrue(options['text'])
        self.assertEqual(options['timeout'], 60)
        self.assertFalse(options['check'])
        self.assertNotIn('shell', options)

    def test_other_user_uses_runuser(self):
        job = cron.CronJob('Test', 'id', 'www-data', '* * * * *', '/etc/crontab')
        done = mock.Mock(stdout='', returncode=3)
        with mock.patch.object(cron.subprocess, 'run', return_value=done) as runner:
            lines = cron.run_now(job, timeout=5)
        self.assertEqual(runner.call_args[0][0],
                         ['runuser', '-u', 'www-data', '--', '/bin/sh', '-c', 'id'])
        self.assertEqual(runner.call_args[1]['timeout'], 5)
        self.assertEqual(lines, ['$ id', 'Code 3'])

    def test_errors_become_panel_errors(self):
        job = cron.CronJob('Test', 'true', 'root', '* * * * *', '/etc/crontab')
        with mock.patch.object(cron.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('true', 1)):
            with self.assertRaises(cron.PanelError):
                cron.run_now(job, timeout=1)
        with mock.patch.object(cron.subprocess, 'run', side_effect=OSError('boom')):
            with self.assertRaises(cron.PanelError):
                cron.run_now(job)


class Status(CronBase):
    def test_available(self):
        with mock.patch.object(cron.shutil, 'which', return_value='/usr/bin/crontab'):
            self.assertTrue(cron.available())
        with mock.patch.object(cron.shutil, 'which', return_value=None):
            self.assertFalse(cron.available())

    def test_service_state(self):
        with mock.patch.object(cron, 'available', return_value=False):
            self.assertEqual(cron.service_state(), 'absent')
        with mock.patch.object(cron, 'available', return_value=True), \
             mock.patch.object(cron, 'run', return_value='active\n') as command:
            self.assertEqual(cron.service_state(), 'active')
            command.assert_called_once_with('systemctl', 'is-active', 'cron',
                                            timeout=5, check=False)
        with mock.patch.object(cron, 'available', return_value=True), \
             mock.patch.object(cron, 'run', return_value='inactive\n'):
            self.assertEqual(cron.service_state(), 'inactive')
        with mock.patch.object(cron, 'available', return_value=True), \
             mock.patch.object(cron, 'run', return_value='failed\n'):
            self.assertEqual(cron.service_state(), 'unknown')
        with mock.patch.object(cron, 'available', return_value=True), \
             mock.patch.object(cron, 'run', side_effect=cron.PanelError('absent')):
            self.assertEqual(cron.service_state(), 'unknown')

    def test_logs_from_journal(self):
        entry = '2026-10-28T10:00:00 CRON[42]: (root) CMD (true)'
        with mock.patch.object(cron, 'run', return_value=f'{entry}\n') as command:
            self.assertEqual(cron.logs(10), [entry])
        command.assert_called_once_with('journalctl', '-t', 'CRON', '-n', '10', '--no-pager',
                                        '--output=short-iso', timeout=15)

    def test_logs_fallback_to_syslog(self):
        journal = Path(self.temp.name) / 'syslog'
        journal.write_text('ligne banale\nOct 28 CRON[42]: (root) CMD (true)\n',
                           encoding='utf-8')
        with mock.patch.object(cron, 'run', side_effect=cron.PanelError('absent')), \
             mock.patch.object(cron, '_SYSLOG', journal):
            self.assertEqual(cron.logs(5), ['Oct 28 CRON[42]: (root) CMD (true)'])

    def test_logs_unavailable(self):
        with mock.patch.object(cron, 'run', side_effect=cron.PanelError('absent')), \
             mock.patch.object(cron, '_SYSLOG', Path(self.temp.name) / 'absent'):
            self.assertIn('indisponible', cron.logs()[0])

    def test_detail_lines(self):
        job = cron.CronJob('Sauvegarde', '/usr/local/bin/backup.sh', 'root', '0 3 * * *',
                           str(self.managed), managed=True, enabled=True, slug='sauvegarde')
        lines = cron.detail_lines(job)
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].startswith('QUAND'))
        self.assertTrue(lines[0].endswith('Chaque jour à 03:00'))
        self.assertRegex(lines[1], r'^PROCHAINE\s+\d{2}/\d{2}/\d{4} \d{2}:\d{2}$')
        self.assertIn('ACTIF', lines[2])
        self.assertIn('UTILISATEUR', lines[3])
        self.assertIn(str(self.managed), lines[4])
        self.assertTrue(lines[5].startswith('COMMANDE'))
        self.assertTrue(lines[5].endswith('/usr/local/bin/backup.sh'))

        disabled = cron.CronJob('Veille', '/bin/true', 'root', '0 3 * * *', str(self.managed),
                                managed=True, enabled=False)
        self.assertIn('—', cron.detail_lines(disabled)[1])
        self.assertIn('DÉSACTIVÉ', cron.detail_lines(disabled)[2])

        external = cron.CronJob('Uptime', '/usr/bin/uptime', 'root', '17 * * * *', '/etc/crontab')
        self.assertIn('EXTERNE', cron.detail_lines(external)[2])


if __name__ == '__main__':
    unittest.main()
