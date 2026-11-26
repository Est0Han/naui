#!/usr/bin/env python3
"""Release hygiene, not a complete secret scanner. Never print matched values."""
import argparse
import getpass
import ipaddress
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
GENERIC_USERS = {'user', 'developer', 'test', 'remote', 'pi', 'root', 'runner', 'naui'}
GENERATED = {'__pycache__', '.venv', 'venv', 'env', 'build', 'dist', '.pytest_cache',
             '.mypy_cache', '.ruff_cache', 'htmlcov', 'node_modules'}
SECRET = re.compile(r'(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{25,}|'
                    r'sk-(?:proj-)?[A-Za-z0-9_-]{24,}|'
                    r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)')
ADDRESS = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
HOME = re.compile(r'/(?:home|Users)/([^/\s\'"<>]+)')
EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')


def identifiers():
    """Current machine's identifying strings, used only in memory."""
    values = set()
    user = getpass.getuser()
    if user.lower() not in GENERIC_USERS:
        values.add(user)
    host = socket.gethostname()
    if host.lower() not in {'localhost', 'workstation', 'example-server'}:
        values.add(host)
    for key in ('user.name', 'user.email'):
        result = subprocess.run(['git','config','--get',key], cwd=ROOT, capture_output=True, text=True)
        value = result.stdout.strip()
        if value and value.lower() not in {'naui contributors','naui test'} and not value.endswith(('@example.invalid','@example.com')):
            values.add(value)
    try:
        import pwd
        name = pwd.getpwuid(os.getuid()).pw_gecos.split(',',1)[0].strip()
        if name and name.lower() not in GENERIC_USERS:
            values.add(name)
    except (ImportError, KeyError):
        pass
    # Read alias/hostname words only; never load keys or authentication data.
    try:
        config = Path.home()/'.ssh/config'
        if config.stat().st_size <= 1024*1024:
            for line in config.read_text().splitlines():
                fields = shlex.split(line, comments=True)
                if fields and fields[0].lower() in {'host','hostname'}:
                    values.update(value for value in fields[1:] if not any(c in value for c in '*?!')
                                  and value.lower() not in {'localhost','github.com','gitlab.com','example-server'})
    except (OSError, UnicodeError, ValueError):
        pass
    return [re.compile(r'(?<![\w-])'+re.escape(value)+r'(?![\w-])', re.I) for value in values if len(value)>2]


def findings(text, personal=()):
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if SECRET.search(line): found.append((number, 'possible credential/private key'))
        if any(pattern.search(line) for pattern in personal): found.append((number, 'current machine identity'))
        if any(match.group(1).lower() not in GENERIC_USERS for match in HOME.finditer(line)):
            found.append((number, 'non-example home path'))
        for match in EMAIL.finditer(line):
            domain = match.group().split('@')[-1].lower()
            if not (domain in {'example.com','example.org','example.net','example.invalid'} or domain.endswith('.example.invalid')):
                found.append((number, 'non-example email address'))
        for match in ADDRESS.finditer(line):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            examples = ('192.0.2.0/24','198.51.100.0/24','203.0.113.0/24')
            if not address.is_loopback and not address.is_unspecified and not any(address in ipaddress.ip_network(net) for net in examples):
                # Version constants use dotted integers too; inspect these
                # manually rather than hiding potential infrastructure addresses.
                found.append((number, 'non-example IP address'))
    return found


def audit():
    result = subprocess.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],
                            cwd=ROOT, capture_output=True, check=True)
    paths = sorted(set(Path(os.fsdecode(item)) for item in result.stdout.split(b'\0') if item))
    problems, count = [], 0
    personal = identifiers()
    for relative in paths:
        path = ROOT/relative
        if path.is_symlink():
            problems.append((relative,0,'repository symlink requires review'))
            continue
        if not path.is_file(): continue
        if any(part in GENERATED or part.endswith('.egg-info') for part in relative.parts):
            problems.append((relative,0,'generated artifact eligible for commit'))
        if path.name in {'core','id_rsa','id_ed25519','known_hosts','authorized_keys'} or path.suffix in {'.log','.core','.dmp','.pyc'}:
            problems.append((relative,0,'runtime/key/crash artifact'))
        data = path.read_bytes()
        try:
            content = data.decode('utf-8')
        except UnicodeDecodeError:
            problems.append((relative,0,'binary asset requires manual privacy review'))
            continue
        count += 1
        problems.extend((relative,line,category) for line,category in findings(content,personal))
    # All ignored crash dumps remain worth detecting even though git add skips
    # them. Don't descend into environments or unrelated dependency trees.
    for directory, subdirectories, names in os.walk(ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in GENERATED|{'.git'} and not name.endswith('.egg-info')]
        for name in names:
            if name == 'core' or name.startswith('core.') or name.endswith(('.core','.dmp','.crash')):
                problems.append((Path(directory,name).relative_to(ROOT),0,'ignored crash artifact'))
    version = subprocess.run([sys.executable,'-B',str(ROOT/'naui'),'--version'], cwd=ROOT,
                             capture_output=True,text=True,check=True).stdout.strip()
    sys.path.insert(0,str(ROOT/'src'))
    from naui import __version__
    if version != 'Naui '+__version__:
        problems.append((Path('src/naui/__init__.py'),0,'version mismatch'))
    if 'dynamic = ["version"]' not in (ROOT/'pyproject.toml').read_text():
        problems.append((Path('pyproject.toml'),0,'package version is not authoritative'))
    head = subprocess.run(['git','rev-parse','--verify','HEAD'],cwd=ROOT,capture_output=True)
    print('Git history: no commits' if head.returncode else 'Git history exists: historical secret/identity review required separately')
    for relative,line,category in problems[:100]:
        print(f'{relative}:{line}: {category}')
    print(f'Audited {count} text/source/assets; {len(problems)} finding(s); {version}.')
    return 1 if problems else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tests',action='store_true',help='Also run all unit tests and validate SDK example plugins')
    args=parser.parse_args()
    status=audit()
    if status or not args.tests: return status
    environment=dict(os.environ,PYTHONPATH=str(ROOT/'src'),PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-q'],cwd=ROOT,env=environment)
    if result.returncode: return result.returncode
    for example in ('hello-world','sidebar-panel','file-action','terminal-command'):
        result=subprocess.run([sys.executable,'-B',str(ROOT/'naui'),'plugin','validate',str(ROOT/'examples/plugins'/example)],cwd=ROOT,env=environment)
        if result.returncode: return result.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
