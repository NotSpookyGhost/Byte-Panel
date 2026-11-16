"""Offline release metadata fixtures; no Minecraft processes or live downloads."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from minecraft_releases import (MinecraftReleases, ENGINES, MOJANG_MANIFEST, PAPER_PROJECT,
                                PURPUR_PROJECT, FABRIC_META, FORGE_PROMOTIONS, java_path, release_key)


class MinecraftReleaseTests(unittest.TestCase):
    def setUp(self):
        self.service = MinecraftReleases()
        releases = ['26.4-snapshot-8', '26.3', '26.2', '26.1.2', '1.21.11', '1.21.9', '1.12.2', '1.8.8', '1.7.10']
        self.data = {
            MOJANG_MANIFEST: {'latest': {'release': '26.3', 'snapshot': '26.4-snapshot-8'},
                             'versions': [{'id': v, 'type': 'snapshot' if 'snapshot' in v else 'release',
                                           'url': 'https://metadata.example/' + v} for v in releases]},
            PAPER_PROJECT: {'versions': {'26.3': ['26.3', '26.3-rc-3'], '1.21': ['1.21.9', '1.21.11'], '1.12': ['1.12.2']}},
            PURPUR_PROJECT: {'versions': ['1.21.9', '1.21.11', '26.2', '26.3']},
            FABRIC_META + '/game': [{'version': v, 'stable': 'snapshot' not in v} for v in releases[:6]],
            FORGE_PROMOTIONS: {'promos': {'26.3-latest': '66.1.9', '26.3-recommended': '66.1.8', '26.2-latest': '65.1.6',
                                        '1.12.2-recommended': '14.23.5.2859'}},
            'https://metadata.example/26.3': {'javaVersion': {'majorVersion': 25},
                                           'downloads': {'server': {'url': 'https://downloads.example/server.jar', 'sha1': 'abc'}}},
            PAPER_PROJECT + '/versions/26.3/builds': [
                {'id': 999, 'channel': 'EXPERIMENTAL', 'downloads': {'server:default': {'url': 'https://downloads.example/unstable.jar'}}},
                {'id': 100, 'channel': 'STABLE', 'downloads': {'server:default': {'url': 'https://downloads.example/old.jar'}}},
                {'id': 253, 'channel': 'STABLE', 'downloads': {'server:default': {'url': 'https://downloads.example/new.jar', 'checksums': {'sha256': 'def'}}}},
            ],
            PURPUR_PROJECT + '/26.3': {'builds': {'latest': '2600'}},
            FABRIC_META + '/loader/26.3': [{'loader': {'version': '0.21.0-beta', 'stable': False}},
                                         {'loader': {'version': '0.20.1', 'stable': True}}],
            FABRIC_META + '/installer': [{'version': '1.2.0-beta', 'stable': False}, {'version': '1.1.2', 'stable': True}],
        }
        self.service.get_json = Mock(side_effect=self.fixture)

    def fixture(self, url):
        value = self.data[url]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def test_live_catalog_latest_sorted_and_engine_specific(self):
        result = self.service.catalog()
        self.assertEqual(result['latest'], '26.3')
        self.assertEqual(result['versions'][0], {'version': '26.3', 'engines': list(ENGINES)})
        versions = {item['version']: item['engines'] for item in result['versions']}
        self.assertNotIn('26.4-snapshot-8', versions)
        self.assertNotIn('1.7.10', versions)
        self.assertNotIn('Fabric', versions['1.12.2'])
        self.assertNotIn('Purpur', versions['1.12.2'])
        self.assertEqual(list(versions).index('1.21.11') + 1, list(versions).index('1.21.9'))

    def test_new_release_appears_without_a_panel_update(self):
        self.data[MOJANG_MANIFEST]['latest']['release'] = '26.4'
        self.data[MOJANG_MANIFEST]['versions'].insert(0, {'id': '26.4', 'type': 'release', 'url': 'https://metadata.example/26.4'})
        result = self.service.catalog()
        self.assertEqual(result['latest'], '26.4')
        self.assertEqual(result['versions'][0], {'version': '26.4', 'engines': ['Vanilla']})

    def test_provider_failure_hides_only_affected_engine(self):
        self.data[PAPER_PROJECT] = RuntimeError('offline')
        result = self.service.catalog()
        self.assertTrue(result['warnings'])
        self.assertNotIn('Paper', result['versions'][0]['engines'])
        self.assertIn('Forge', result['versions'][0]['engines'])

    def test_missing_mojang_manifest_fails_clearly(self):
        self.data[MOJANG_MANIFEST] = RuntimeError('offline')
        with self.assertRaisesRegex(RuntimeError, 'official Minecraft release list'):
            self.service.catalog()

    def test_catalog_cache_and_expiry(self):
        self.service.catalog()
        count = self.service.get_json.call_count
        self.service.catalog()
        self.assertEqual(self.service.get_json.call_count, count)
        self.service._catalog_time -= 301
        self.service.catalog()
        self.assertGreater(self.service.get_json.call_count, count)

    def test_vanilla_uses_metadata_checksum_and_java(self):
        download = self.service.resolve_download('Vanilla', '26.3')
        self.assertEqual(download['url'], 'https://downloads.example/server.jar')
        self.assertEqual(download['checksum'], ('sha1', 'abc'))
        self.assertIn('java-25-openjdk', download['java_path'])

    def test_paper_uses_newest_stable_build_and_returned_url(self):
        download = self.service.resolve_download('Paper', '26.3')
        self.assertEqual(download['url'], 'https://downloads.example/new.jar')
        self.assertEqual(download['checksum'], ('sha256', 'def'))
        self.data[PAPER_PROJECT + '/versions/26.3/builds'] = self.data[PAPER_PROJECT + '/versions/26.3/builds'][:1]
        with self.assertRaisesRegex(RuntimeError, 'no stable build'):
            self.service.resolve_download('Paper', '26.3')

    def test_fabric_resolves_stable_loader_and_installer(self):
        download = self.service.resolve_download('Fabric', '26.3')
        self.assertEqual(download['url'], FABRIC_META + '/loader/26.3/0.20.1/1.1.2/server/jar')
        self.data[FABRIC_META + '/loader/26.3'] = []
        with self.assertRaisesRegex(RuntimeError, 'no stable loader'):
            self.service.resolve_download('Fabric', '26.3')

    def test_forge_recommended_and_latest_fallback(self):
        download = self.service.resolve_download('Forge', '26.3')
        self.assertEqual(download['forge_full_version'], '26.3-66.1.8')
        self.assertIn('forge-26.3-66.1.8-installer.jar', download['url'])
        self.assertEqual(self.service.forge_version('26.2'), ('65.1.6', '26.2-65.1.6'))
        with self.assertRaisesRegex(RuntimeError, 'does not publish'):
            self.service.forge_version('26.1')

    def test_purpur_pins_a_published_build(self):
        self.assertEqual(self.service.resolve_download('Purpur', '26.3')['url'], PURPUR_PROJECT + '/26.3/2600/download')
        self.data[PURPUR_PROJECT + '/26.3'] = {'builds': {}}
        with self.assertRaisesRegex(RuntimeError, 'no build'):
            self.service.resolve_download('Purpur', '26.3')

    def test_java_selection_legacy_current_and_future_requirements(self):
        for version, expected in [('1.8.8', 'java-1.8-'), ('1.16.5', 'java-11-'), ('1.17.1', 'java-17-'),
                                  ('1.20.4', 'java-17-'), ('1.20.5', 'java-21-'), ('1.21.11', 'java-21-'), ('26.3', 'java-25-')]:
            with self.subTest(version=version):
                self.assertIn(expected, java_path(version))
        self.assertIn('java-21-', java_path('1.20.4', engine='Paper'))
        with self.assertRaisesRegex(RuntimeError, 'requires Java 26'):
            java_path('27.1', minimum=26)

    def test_invalid_unknown_and_unreleased_versions_are_rejected(self):
        for version in ('../../etc', '26.4-snapshot-8', '', None, '26.3?x=y'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.service.resolve_download('Vanilla', version)
        with self.assertRaises(ValueError):
            self.service.resolve_download('Vanilla', '99.1')
        with self.assertRaises(ValueError):
            self.service.resolve_download('Unknown', '26.3')

    def test_http_cache_timeout_headers_and_failures(self):
        service = MinecraftReleases()
        with patch('minecraft_releases.requests.get') as get:
            get.return_value.json.return_value = {'test': 'value'}
            value = service.get_json(MOJANG_MANIFEST)
            value['test'] = 'changed'
            self.assertEqual(service.get_json(MOJANG_MANIFEST)['test'], 'value')
            get.assert_called_once()
            self.assertEqual(get.call_args.kwargs['timeout'], (5, 15))
            self.assertIn('BytePanel/', get.call_args.kwargs['headers']['User-Agent'])
        with patch('minecraft_releases.requests.get', side_effect=requests.Timeout):
            with self.assertRaisesRegex(RuntimeError, 'Unable to load'):
                service.get_json(PAPER_PROJECT)


if __name__ == '__main__':
    unittest.main()
