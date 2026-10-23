"""Apache VirtualHost lifecycle with config validation and rollback."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .common import PanelError, atomic_write, domain_name, root_only, run, EMAIL

AVAILABLE = Path('/etc/apache2/sites-available')
ENABLED = Path('/etc/apache2/sites-enabled')
WEBROOT = Path('/var/www')
SERVER = re.compile(r'^\s*ServerName\s+(\S+)', re.I | re.M)
ALIASES = re.compile(r'^\s*ServerAlias\s+([^#\r\n]+)', re.I | re.M)
DOCROOT = re.compile(r'^\s*DocumentRoot\s+"?([^"\r\n]+)"?', re.I | re.M)
PROXY = re.compile(r'^\s*ProxyPass\s+"?/"?\s+"?(http://127\.0\.0\.1:\d+)', re.I | re.M)


@dataclass(frozen=True)
class Site:
    domain: str
    filename: str
    target: str
    enabled: bool
    https: bool
    managed: bool
    www: bool


def _hosts(text: str) -> set[str]:
    matches = [m.group(1) for m in SERVER.finditer(text)]
    for match in ALIASES.finditer(text):
        matches.extend(match.group(1).split())
    return {x.lower().rstrip('.') for x in matches}


def inventory() -> list[Site]:
    sites = []
    if not AVAILABLE.is_dir():
        return sites
    for conf in sorted(AVAILABLE.glob('*.conf')):
        if conf.name.endswith('-le-ssl.conf'):
            continue
        try:
            content = conf.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        name = SERVER.search(content)
        domain = name.group(1).lower() if name else conf.stem
        doc = DOCROOT.search(content); upstream = PROXY.search(content)
        target = ('PROXY ' + upstream.group(1) if upstream else doc.group(1).strip() if doc else '?')
        tlsname = conf.stem + '-le-ssl.conf'
        tls_enabled = (ENABLED / tlsname).exists()
        if not tls_enabled and (ENABLED / conf.name).exists():
            tls_enabled = 'SSLCertificateFile' in content
        sites.append(Site(domain, conf.name, target, (ENABLED / conf.name).exists() or tls_enabled,
                          tls_enabled, conf.name.startswith('panel-'), 'www.' + domain in _hosts(content)))
    return sites


def validate_webroot(value: str) -> Path:
    path = Path(value.strip())
    if not path.is_absolute() or '..' in path.parts or any(c in str(path) for c in '\x00\r\n"\\'):
        raise PanelError('Dossier web absolu requis (sans guillemets ni traversée)')
    real = path.resolve()
    if real == WEBROOT or not real.is_relative_to(WEBROOT):
        raise PanelError('Dossier web obligatoire sous /var/www/')
    return real


def vhost(domain: str, *, root: Path | None = None, proxy_port: int | None = None,
          www: bool = False, htaccess: bool = False) -> str:
    domain = domain_name(domain)
    if (root is None) == (proxy_port is None):
        raise PanelError('Choisir un dossier web OU un proxy')
    alias = f'    ServerAlias www.{domain}\n' if www else ''
    common = (f'    ErrorLog ${{APACHE_LOG_DIR}}/panel-{domain}-error.log\n'
              f'    CustomLog ${{APACHE_LOG_DIR}}/panel-{domain}-access.log combined\n')
    if proxy_port is not None:
        if not 1024 <= proxy_port <= 65535:
            raise PanelError('Port local requis : 1024 à 65535')
        inner = ('    ProxyPreserveHost On\n'
                 f'    ProxyPass "/" "http://127.0.0.1:{proxy_port}/"\n'
                 f'    ProxyPassReverse "/" "http://127.0.0.1:{proxy_port}/"\n')
    else:
        root = validate_webroot(str(root))
        inner = (f'    DocumentRoot "{root}"\n'
                 f'    <Directory "{root}">\n'
                 f'        Options -Indexes +FollowSymLinks\n'
                 f'        AllowOverride {"All" if htaccess else "None"}\n'
                 '        Require all granted\n'
                 '    </Directory>\n')
    return (f'# Managed by ServerPanel. Edit through ServerPanel; Certbot may create a TLS companion.\n'
            f'<VirtualHost *:80>\n    ServerName {domain}\n{alias}{inner}{common}</VirtualHost>\n')


def create(domain: str, *, path: str | None = None, proxy_port: int | None = None,
           www: bool = False, htaccess: bool = False) -> str:
    root_only()
    domain = domain_name(domain)
    requested = {domain, 'www.' + domain} if www else {domain}
    conf = AVAILABLE / f'panel-{domain}.conf'
    if conf.exists() or conf.is_symlink():
        raise PanelError('Ce site existe déjà')
    for existing in AVAILABLE.glob('*.conf'):
        try:
            declared = _hosts(existing.read_text(encoding='utf-8', errors='replace'))
        except OSError as ex:
            raise PanelError(f'Impossible de vérifier {existing.name}') from ex
        if requested & declared:
            raise PanelError(f'Domaine déjà utilisé : {existing.name}')
    root = validate_webroot(path) if path is not None else None
    body = vhost(domain, root=root, proxy_port=proxy_port, www=www, htaccess=htaccess)
    created_dirs: list[Path] = []
    placeholder: Path | None = None
    if root is not None and not root.exists():
        current = root
        while not current.exists():
            created_dirs.append(current)
            current = current.parent
        root.mkdir(parents=True, mode=0o755)
        placeholder = root / 'index.html'
        # Only create a landing page in a directory created by this action.
        placeholder.write_text(
            '<!doctype html><html lang="fr"><meta charset="utf-8">'
            f'<title>{domain}</title><h1>{domain}</h1></html>\n', encoding='utf-8')
    try:
        atomic_write(conf, body)
        if proxy_port is not None:
            run('a2enmod', 'proxy', 'proxy_http')
        if htaccess:
            run('a2enmod', 'rewrite')
        run('a2ensite', conf.name)
        run('apache2ctl', '-t')
        run('systemctl', 'reload', 'apache2')
    except (PanelError, OSError):
        if (ENABLED / conf.name).exists():
            run('a2dissite', conf.name, check=False)
        conf.unlink(missing_ok=True)
        # Remove only objects created during this attempt; never customer files.
        if placeholder is not None:
            placeholder.unlink(missing_ok=True)
        for created in created_dirs:
            try:
                created.rmdir()
            except OSError:
                break
        run('apache2ctl', '-t', check=False)
        run('systemctl', 'reload', 'apache2', check=False)
        raise
    return domain


def toggle(site: Site, enabled: bool) -> None:
    root_only()
    if not site.managed or not (AVAILABLE / site.filename).is_file():
        raise PanelError('Site externe : consultation uniquement')
    ssl = site.filename.removesuffix('.conf') + '-le-ssl.conf'
    targets = [site.filename] + ([ssl] if (AVAILABLE / ssl).exists() else [])
    changed = []
    try:
        for target in targets:
            active = (ENABLED / target).exists()
            if active != enabled:
                run('a2ensite' if enabled else 'a2dissite', target)
                changed.append(target)
        run('apache2ctl', '-t')
        run('systemctl', 'reload', 'apache2')
    except PanelError:
        for target in reversed(changed):
            run('a2dissite' if enabled else 'a2ensite', target, check=False)
        run('apache2ctl', '-t', check=False)
        run('systemctl', 'reload', 'apache2', check=False)
        raise


def certificate(site: Site, email: str) -> str:
    root_only()
    if not site.managed or not site.enabled:
        raise PanelError('Activer un site géré par ServerPanel avant le HTTPS')
    if not EMAIL.fullmatch(email.strip()):
        raise PanelError('Adresse e-mail invalide')
    cmd = ['certbot', '--apache', '--non-interactive', '--agree-tos', '--email', email.strip(),
           '--redirect', '-d', domain_name(site.domain)]
    if site.www:
        cmd.extend(['-d', 'www.' + domain_name(site.domain)])
    return run(*cmd, timeout=300)[-1200:]


def certificates() -> str:
    return run('certbot', 'certificates', timeout=35)


def renew_check() -> str:
    return run('certbot', 'renew', '--dry-run', timeout=300)


def file_logs(site: Site, kind: str = 'error', count: int = 200) -> list[str]:
    if kind not in {'error', 'access'}:
        raise PanelError('Journal inconnu')
    # On external sites, avoid resolving untrusted ServerName to a filesystem path.
    if not site.managed:
        raise PanelError('Logs accessibles seulement pour les sites gérés')
    domain = domain_name(site.domain)
    path = Path('/var/log/apache2') / f'panel-{domain}-{kind}.log'
    try:
        with path.open('r', encoding='utf-8', errors='replace') as file:
            from collections import deque
            return list(deque(file, maxlen=count))
    except FileNotFoundError:
        return ['Journal vide ou pas encore créé.']
    except OSError as exc:
        raise PanelError(f'Journal inaccessible : {exc}') from exc


def remove(site: Site) -> None:
    """Unregister a managed vhost only; retain content, certificates and logs."""
    root_only()
    if not site.managed or not site.filename.startswith('panel-'):
        raise PanelError('Seuls les sites ServerPanel peuvent être supprimés')
    conf = AVAILABLE / site.filename
    if not conf.is_file() or conf.is_symlink():
        raise PanelError('Configuration gérée introuvable')
    ssl_name = conf.stem + '-le-ssl.conf'
    tls = AVAILABLE / ssl_name
    if tls.is_symlink():
        raise PanelError('Configuration TLS symbolique : suppression manuelle nécessaire')
    targets = [conf] + ([tls] if tls.is_file() else [])
    disabled = []
    try:
        for target in targets:
            if (ENABLED / target.name).exists():
                run('a2dissite', target.name)
                disabled.append(target.name)
        run('apache2ctl', '-t')
        run('systemctl', 'reload', 'apache2')
    except PanelError:
        for name in disabled:
            run('a2ensite', name, check=False)
        run('apache2ctl', '-t', check=False)
        run('systemctl', 'reload', 'apache2', check=False)
        raise
    for target in targets:
        target.unlink()
