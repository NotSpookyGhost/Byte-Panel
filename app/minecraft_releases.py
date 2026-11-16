"""Discover released Minecraft versions and resolve official engine downloads."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import re
import threading
import time
from urllib.parse import urlsplit

import requests


MOJANG_MANIFEST = 'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'
PAPER_PROJECT = 'https://fill.papermc.io/v3/projects/paper'
PURPUR_PROJECT = 'https://api.purpurmc.org/v2/purpur'
FABRIC_META = 'https://meta.fabricmc.net/v2/versions'
FORGE_PROMOTIONS = 'https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json'
DOWNLOAD_HEADERS = {'User-Agent': 'BytePanel/1.10.02 (https://bytedev.app)'}
ENGINES = ('Vanilla', 'Paper', 'Purpur', 'Fabric', 'Forge')
CACHE_SECONDS = 300


def release_key(version):
    if not isinstance(version, str) or not re.fullmatch(r'\d+(?:\.\d+){1,2}', version):
        raise ValueError('Choose a stable Minecraft release, not a snapshot or pre-release.')
    return tuple(int(part) for part in version.split('.'))


def java_path(version, minimum=None, engine=None):
    """Choose an included JRE, respecting Mojang's published runtime requirement."""
    key = release_key(version)
    if key >= (26, 1):
        major = 25
    elif key >= (1, 20, 5):
        major = 21
    elif key >= (1, 17):
        major = 17
    elif key >= (1, 16):
        major = 11
    else:
        major = 8
    if engine in ('Paper', 'Purpur'):
        # Paper's recommended Java versions also apply to its Purpur fork.
        if (1, 20) <= key < (26, 1):
            major = max(major, 21)
        elif (1, 16, 5) <= key < (1, 20):
            major = max(major, 17)
        elif (1, 12) <= key < (1, 16, 5):
            major = max(major, 11)
    required = max(major, int(minimum or major))
    selected = next((runtime for runtime in (8, 11, 17, 21, 25) if runtime >= required), None)
    if selected is None:
        raise RuntimeError(f'Minecraft {version} requires Java {required}; rebuild the panel with that runtime before installing it.')
    if selected == 8:
        return '/usr/lib/jvm/java-1.8-openjdk/jre/bin/java'
    return f'/usr/lib/jvm/java-{selected}-openjdk/bin/java'


class MinecraftReleases:
    def __init__(self):
        self._cache = {}
        self._cache_lock = threading.RLock()
        self._catalog_lock = threading.Lock()
        self._catalog = None
        self._catalog_time = 0

    def get_json(self, url):
        with self._cache_lock:
            cached = self._cache.get(url)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return deepcopy(cached[1])
        try:
            response = requests.get(url, headers=DOWNLOAD_HEADERS, timeout=(5, 15))
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as error:
            raise RuntimeError(f'Unable to load release information from {urlsplit(url).hostname}. Try again shortly.') from error
        with self._cache_lock:
            self._cache[url] = (time.monotonic(), data)
        return deepcopy(data)

    def catalog(self):
        with self._catalog_lock:
            # Cache partial failures briefly, without pretending unavailable engines are supported.
            ttl = 30 if self._catalog and self._catalog['warnings'] else CACHE_SECONDS
            if self._catalog and time.monotonic() - self._catalog_time < ttl:
                return deepcopy(self._catalog)
            sources = {'Vanilla': MOJANG_MANIFEST, 'Paper': PAPER_PROJECT, 'Purpur': PURPUR_PROJECT,
                       'Fabric': FABRIC_META + '/game', 'Forge': FORGE_PROMOTIONS}
            results, warnings = {}, []
            with ThreadPoolExecutor(max_workers=5) as pool:
                pending = {engine: pool.submit(self.get_json, url) for engine, url in sources.items()}
                for engine, future in pending.items():
                    try:
                        results[engine] = future.result()
                    except RuntimeError as error:
                        if engine == 'Vanilla':
                            raise RuntimeError('Cannot load the official Minecraft release list. Check internet access and retry.') from error
                        warnings.append(f'{engine} versions are temporarily unavailable. Retry shortly.')
            manifest = results['Vanilla']
            try:
                releases = {item['id'] for item in manifest['versions'] if item['type'] == 'release'
                            and release_key(item['id']) >= (1, 8, 8)}
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError('The Minecraft release service returned an invalid version list.') from error
            available = {'Vanilla': releases}
            for engine in ENGINES[1:]:
                if engine not in results:
                    continue
                try:
                    data = results[engine]
                    if engine == 'Paper':
                        # Fill v3 groups version strings by release family.
                        available[engine] = {version for group in data['versions'].values() for version in group}
                    elif engine == 'Purpur':
                        available[engine] = set(data['versions'])
                    elif engine == 'Fabric':
                        available[engine] = {item['version'] for item in data if item.get('stable')}
                    else:
                        available[engine] = {key.rsplit('-', 1)[0] for key in data['promos']
                                             if key.endswith(('-recommended', '-latest'))}
                except (KeyError, TypeError, AttributeError) as error:
                    warnings.append(f'{engine} returned an invalid version list and is temporarily hidden.')
            versions = [{'version': version, 'engines': [engine for engine in ENGINES if version in available.get(engine, set())]}
                        for version in sorted(releases, key=release_key, reverse=True)]
            if not versions:
                raise RuntimeError('No stable Minecraft server releases were returned.')
            latest = manifest.get('latest', {}).get('release')
            self._catalog = dict(latest=latest if latest in releases else versions[0]['version'],
                                 versions=versions, warnings=warnings)
            self._catalog_time = time.monotonic()
            return deepcopy(self._catalog)

    def forge_version(self, version):
        release_key(version)
        promos = self.get_json(FORGE_PROMOTIONS).get('promos', {})
        build = promos.get(f'{version}-recommended') or promos.get(f'{version}-latest')
        if not build:
            raise RuntimeError(f'Forge does not publish a server build for Minecraft {version}.')
        return build, f'{version}-{build}'

    def resolve_download(self, engine, version):
        release_key(version)
        if engine not in ENGINES:
            raise ValueError('Unknown server software platform.')
        manifest = self.get_json(MOJANG_MANIFEST)
        release = next((item for item in manifest['versions'] if item['id'] == version and item['type'] == 'release'), None)
        if not release:
            raise ValueError('Choose a released Minecraft version from the version list.')
        metadata = self.get_json(release['url'])
        runtime = java_path(version, metadata.get('javaVersion', {}).get('majorVersion'), engine)
        result = dict(java_path=runtime, forge_version='', forge_full_version='', checksum=None)
        if engine == 'Vanilla':
            download = metadata.get('downloads', {}).get('server', {})
            result['url'] = download.get('url')
            if download.get('sha1'):
                result['checksum'] = ('sha1', download['sha1'])
        elif engine == 'Paper':
            builds = self.get_json(f'{PAPER_PROJECT}/versions/{version}/builds')
            stable = [build for build in builds if build.get('channel') == 'STABLE'
                      and build.get('downloads', {}).get('server:default', {}).get('url')]
            if not stable:
                raise RuntimeError(f'Paper has no stable build for Minecraft {version} yet. Choose another version or engine.')
            latest = max(stable, key=lambda build: int(build['id']))
            download = latest['downloads']['server:default']
            result['url'] = download['url']
            if download.get('checksums', {}).get('sha256'):
                result['checksum'] = ('sha256', download['checksums']['sha256'])
        elif engine == 'Purpur':
            info = self.get_json(f'{PURPUR_PROJECT}/{version}')
            build = info.get('builds', {}).get('latest')
            if not build:
                raise RuntimeError(f'Purpur has no build for Minecraft {version}.')
            result['url'] = f'{PURPUR_PROJECT}/{version}/{build}/download'
        elif engine == 'Fabric':
            loaders = self.get_json(f'{FABRIC_META}/loader/{version}')
            loader = next((item['loader']['version'] for item in loaders if item.get('loader', {}).get('stable')), None)
            installers = self.get_json(FABRIC_META + '/installer')
            installer = next((item['version'] for item in installers if item.get('stable')), None)
            if not loader or not installer:
                raise RuntimeError(f'Fabric has no stable loader/installer combination for Minecraft {version}.')
            result['url'] = f'{FABRIC_META}/loader/{version}/{loader}/{installer}/server/jar'
        else:
            build, full = self.forge_version(version)
            result.update(forge_version=build, forge_full_version=full,
                          url=f'https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/forge-{full}-installer.jar')
        if not result.get('url') or urlsplit(result['url']).scheme != 'https':
            raise RuntimeError(f'{engine} did not provide a valid server download for Minecraft {version}.')
        return result
