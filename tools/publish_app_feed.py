#!/usr/bin/env python3
"""Publish a separate app feed from eggtv's already-cleaned configuration."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

BASE = 'https://raw.githubusercontent.com/1072103612/eggtv/main/app'
PROXY = 'https://gh-proxy.com/' + BASE
WELCOME = '欢迎来到蛋壳影院🎞️'


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.staging-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish(root, output, assets=None):
    config = json.loads((root / 'tvbox_config.json').read_text())
    if not config.get('sites') or any(not x.get('key') or not x.get('api') for x in config['sites']):
        raise ValueError('Refusing to publish an empty or invalid catalogue')
    # Keep this APK self-contained: it bundles Java/JAR and QuickJS, not Python/Node.
    config['sites'] = [site for site in config['sites']
                       if '.py' not in str(site['api']) and not str(site['api']).startswith('node:')]
    if not config['sites']:
        raise ValueError('No sources compatible with the bundled runtimes')
    keys = [x['key'] for x in config['sites']]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate source keys')
    # Use the exact validated JAR named by the existing configuration, not a moving pointer.
    spider = config['spider'].split(';md5;')
    jar_name = spider[0].split('/')[-1]
    jar = root / 'jar' / jar_name
    if not jar.is_file():
        raise ValueError(f'The published spider is missing: {jar_name}')
    binary = jar.read_bytes()
    md5 = hashlib.md5(binary).hexdigest()
    if len(spider) != 2 or spider[1] != md5:
        raise ValueError('Published spider checksum mismatch')
    with zipfile.ZipFile(jar) as archive:
        if 'classes.dex' not in archive.namelist() or archive.testzip():
            raise ValueError('Invalid spider archive')
    sha = hashlib.sha256(binary).hexdigest()
    filename = f'jar/{sha}.jar'
    config['spider'] = f'{PROXY}/{filename};md5;{md5}'
    config['eggSpiderSha256'] = sha
    report = root / 'sync_report.json'
    config['eggRevision'] = int(datetime.fromisoformat(json.loads(report.read_text())['sync_time']).timestamp() * 1000) if report.is_file() else 0
    config['sites'][0]['name'] = WELCOME
    config.pop('lives', None)
    config.pop('logo', None)
    payload = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode()
    # Dependencies first, then the configuration that points to them.
    atomic_write(output / filename, binary)
    atomic_write(output / 'tvbox_config.json', payload)
    update = output / 'updates/leanback.json'
    if not update.exists():
        atomic_write(update, (json.dumps({'name': '1.0.0', 'code': 1, 'desc': '蛋壳影院电视版', 'apks': {}}, ensure_ascii=False, indent=2) + '\n').encode())
    if assets is not None:
        atomic_write(assets / 'spider.jar', binary)
        atomic_write(assets / 'config.json', payload)
    return {'sites': len(config['sites']), 'spider_sha256': sha, 'homepage': WELCOME}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--apk-assets', type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.repo, args.output or args.repo / 'app', args.apk_assets), ensure_ascii=False))


if __name__ == '__main__':
    main()
