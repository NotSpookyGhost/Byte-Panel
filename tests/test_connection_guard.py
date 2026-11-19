"""Byte Guard fixtures never contact Minecraft, SMTP, MaxMind or production files."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from connection_guard import ConnectionGuard, GuardConflict, identity_key, nearby


UUID = '10528edc-5bd6-4e36-80f8-d59ebbb99b71'


def location(country='US', lat=40, lon=-74, accuracy=10):
    return dict(usable=True, country=country, country_name=country, city='Test City',
                latitude=lat, longitude=lon, accuracy_km=accuracy, database_build=1700000000)


class FakeGeo:
    def __init__(self):
        self.ready = True
        self.points = {'8.8.8.8': location(), '8.8.4.4': location(lat=40.2),
                       '1.1.1.1': location(country='AU', lat=-33, lon=151),
                       '9.9.9.9': location(lat=46), '172.100.1.1': location(),
                       '2001:4860:4860::8888': location()}
        self.secret, self.account = 'secret-license', '123'

    def lookup(self, ip):
        return dict(deepcopy(self.points.get(ip, dict(usable=False, reason='unmapped'))), ip=ip)

    def health(self):
        return dict(ready=self.ready, available=self.ready, stale=not self.ready)

    def credentials(self):
        return dict(account_id=self.account, has_license_key=bool(self.secret), credentials_validated=self.ready)

    def set_credentials(self, payload):
        if payload.get('clear_license_key'):
            self.secret, self.ready = '', False
        elif payload.get('license_key'):
            self.secret, self.ready = payload['license_key'], False

    def update(self):
        self.ready = True
        return self.health()

    maintenance = health


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.geo, self.now, self.scope = FakeGeo(), 1800000000., 'server-a'
        self.server = dict(identity_mode='online')
        self.guard = self.new_service()
        self.guard.save_settings(dict(mode='enforce'))

    def new_service(self):
        return ConnectionGuard(self.temp.name, lambda: self.scope, lambda: self.server,
                               self.geo, clock=lambda: self.now)

    def request(self, ip='8.8.8.8', username='Steve', player_uuid=UUID, **extra):
        policy = self.guard.config()
        return dict(scope=self.scope, protocol_version=1, policy_revision=policy['policy_revision'],
                    identity_mode=policy['identity_mode'], mode=policy['mode'],
                    username=username, uuid=player_uuid, ip=ip, attempt_id=uuid.uuid4().hex, **extra)

    def finish(self, request, stages=('joined',)):
        result = None
        for stage in stages:
            result = self.guard.outcome(dict(request, stage=stage))
        return result

    def enroll(self, **kwargs):
        request = self.request(**kwargs)
        self.assertEqual('allow', self.guard.prelogin(request)['action'])
        self.finish(request, ('joined', 'authenticated') if self.server['identity_mode'] == 'offline' else ('joined',))
        return request

    def offline(self):
        self.guard.save_settings(dict(mode='off'))
        self.server['identity_mode'] = 'offline'
        self.guard.save_settings(dict(mode='enforce', identity_mode='offline'))
        self.guard.heartbeat(dict(self.guard.config(), ready=True, auth_ready=True))

    def test_defaults_disabled_and_secret_never_returned(self):
        self.guard.reset()
        data = json.dumps(self.guard.snapshot())
        self.assertEqual('off', self.guard.config()['mode'])
        self.assertEqual(250, self.guard.config()['radius_km'])
        self.assertNotIn('secret-license', data)
        self.assertNotIn('"license_key":', data)

    def test_activation_requires_valid_database_and_credentials(self):
        self.guard.save_settings(dict(mode='off'))
        self.geo.ready = False
        with self.assertRaises(ValueError):
            self.guard.save_settings(dict(mode='enforce'))
        self.assertEqual('off', self.guard.config()['mode'])

    def test_clear_key_requires_off_and_alias_supported(self):
        with self.assertRaises(ValueError):
            self.guard.save_settings(dict(remove_license_key=True))
        self.guard.save_settings(dict(mode='off', remove_license_key=True))
        self.assertFalse(self.guard.config()['has_license_key'])

    def test_save_validate_new_credentials_before_activation(self):
        self.guard.save_settings(dict(mode='off'))
        with patch.object(self.geo, 'update', wraps=self.geo.update) as update:
            self.guard.save_settings(dict(mode='enforce', license_key='replacement-key'),
                                     validate_credentials=True)
            update.assert_called_once()
        self.assertEqual('enforce', self.guard.config()['mode'])
        with patch.object(self.geo, 'update') as update:
            self.guard.save_settings(dict(mode='enforce', license_key=''), validate_credentials=True)
            update.assert_not_called()

    def test_failed_validation_does_not_activate_or_change_old_policy(self):
        self.guard.save_settings(dict(mode='off'))
        with patch.object(self.geo, 'update', side_effect=ValueError('Download failed')):
            with self.assertRaisesRegex(ValueError, 'Download failed'):
                self.guard.save_settings(dict(mode='enforce', license_key='replacement-key'),
                                         validate_credentials=True)
        self.assertEqual('off', self.guard.config()['mode'])

    def test_first_identity_not_committed_until_success(self):
        request = self.request()
        decision = self.guard.prelogin(request)
        self.assertTrue(decision['trust_on_success'])
        self.assertEqual([], self.guard.roster()['identities'])
        result = self.finish(request)
        self.assertTrue(result['committed'])
        self.assertFalse(self.finish(request)['committed'])
        profile = self.guard.profile('online:' + UUID)
        self.assertEqual(['8.8.8.8'], profile['accepted_ips'])
        self.assertEqual('8.8.8.8', profile['original_ip'])

    def test_concurrent_first_attempt_rejected_no_overwrite(self):
        first, second = self.request(), self.request(ip='1.1.1.1')
        self.guard.prelogin(first)
        decision = self.guard.prelogin(second)
        self.assertEqual(('deny', 'identity_busy'), (decision['action'], decision['reason']))
        with self.assertRaises(GuardConflict):
            self.finish(second)
        self.finish(first)
        self.assertEqual('8.8.8.8', self.guard.profile('online:' + UUID)['original_ip'])

    def test_failed_join_does_not_enroll_and_releases_reservation(self):
        request = self.request()
        self.guard.prelogin(request)
        self.finish(request, ('disconnected',))
        self.assertEqual([], self.guard.roster()['identities'])
        self.enroll(ip='1.1.1.1')
        with self.assertRaises(GuardConflict):
            self.finish(request)

    def test_nearby_allowed_without_reference_drift(self):
        self.enroll()
        reference = self.guard.profile('online:' + UUID)['references'][0]
        nearby_request = self.enroll(ip='8.8.4.4')
        self.assertEqual(reference, self.guard.profile('online:' + UUID)['references'][0])
        self.assertEqual('nearby', self.guard.prelogin(nearby_request)['reason'])
        self.geo.points['9.9.9.9'] = location(lat=42.15)
        # Within 250 km from the last login, but beyond original including uncertainty.
        self.assertEqual('deny', self.guard.prelogin(self.request(ip='9.9.9.9'))['action'])

    def test_country_distant_unknown_rejected_and_pending(self):
        self.enroll()
        for ip, reason in [('1.1.1.1', 'country_changed'), ('9.9.9.9', 'outside_trusted_area'),
                           ('192.168.1.1', 'location_unavailable')]:
            with self.subTest(ip=ip):
                result = self.guard.prelogin(self.request(ip=ip))
                self.assertEqual(('deny', reason), (result['action'], result['reason']))
        self.assertEqual(3, self.guard.snapshot()['pending_reviews'])

    def test_uncertainty_is_conservative(self):
        self.assertTrue(nearby(location(), location(lat=41), 250))
        self.assertFalse(nearby(location(accuracy=200), location(accuracy=100), 250))
        self.assertFalse(nearby(location(), location(country='CA'), 20000))
        self.assertFalse(nearby(location(), location(lat=float('nan')), 250))

    def test_monitor_flags_but_never_learns_suspicious_ip(self):
        self.enroll()
        self.guard.save_settings(dict(mode='monitor'))
        request = self.request(ip='1.1.1.1')
        decision = self.guard.prelogin(request)
        self.assertEqual('allow', decision['action'])
        self.assertFalse(decision['trust_on_success'])
        self.assertTrue(self.finish(request)['committed'])
        self.assertEqual(['8.8.8.8'], self.guard.profile('online:' + UUID)['accepted_ips'])
        self.assertEqual(1, self.guard.snapshot()['pending_reviews'])

    def test_exact_ip_reused_even_when_location_changes(self):
        self.enroll()
        self.geo.points['8.8.8.8'] = location(country='AU', lat=-33, lon=151)
        self.assertEqual('accepted_ip', self.guard.prelogin(self.request())['reason'])

    def test_unknown_first_location_enrolls_exact_ip_only(self):
        self.enroll(ip='192.168.1.1')
        self.assertEqual([], self.guard.profile('online:' + UUID)['references'])
        self.assertEqual('allow', self.guard.prelogin(self.request(ip='192.168.1.1'))['action'])
        self.assertEqual('deny', self.guard.prelogin(self.request())['action'])

    def test_uuid_identity_survives_name_change(self):
        self.enroll()
        self.enroll(username='Alex')
        self.assertEqual('Alex', self.guard.profile('online:' + UUID)['username'])
        self.assertEqual(1, len(self.guard.roster()['identities']))
        with self.assertRaises(ValueError):
            identity_key('online', 'Steve', 'unknown')

    def test_offline_enrollment_requires_both_session_stages(self):
        self.offline()
        request = self.request(username='StEvE')
        self.guard.prelogin(request)
        self.assertFalse(self.finish(request)['committed'])
        self.assertEqual([], self.guard.roster()['identities'])
        self.assertTrue(self.finish(request, ('authenticated',))['committed'])
        self.assertEqual('offline:steve', self.guard.roster()['identities'][0]['identity'])
        self.assertEqual('accepted_ip', self.guard.prelogin(self.request(username='steve'))['reason'])

    def test_auth_before_join_and_wrong_password_disconnect(self):
        self.offline()
        first = self.request()
        self.guard.prelogin(first)
        self.assertFalse(self.finish(first, ('authenticated',))['committed'])
        self.assertTrue(self.finish(first)['committed'])
        second = self.request(username='Alex', player_uuid=str(uuid.uuid4()))
        self.guard.prelogin(second)
        self.finish(second, ('joined', 'disconnected'))
        with self.assertRaises(GuardConflict):
            self.finish(second, ('authenticated',))
        self.assertEqual(1, len(self.guard.roster()['identities']))

    def test_missing_or_stale_auth_provider_denies_offline(self):
        self.offline()
        self.now += 46
        decision = self.guard.prelogin(self.request())
        self.assertEqual(('deny', 'authentication_unavailable'), (decision['action'], decision['reason']))
        self.guard.heartbeat(dict(self.guard.config(), ready=True, auth_ready=False))
        self.assertEqual('deny', self.guard.prelogin(self.request())['action'])

    def test_expired_heartbeat_no_longer_reports_ready(self):
        self.guard.heartbeat(dict(self.guard.config(), ready=True, auth_ready=True, status='ready'))
        self.assertTrue(self.guard.snapshot()['bridge']['healthy'])
        self.now += 46
        health = self.guard.snapshot()['bridge']
        self.assertFalse(health['healthy'])
        self.assertEqual(health['status'], 'Heartbeat expired')

    def test_heartbeat_health_requires_ready_and_offline_auth(self):
        self.offline()
        self.guard.heartbeat(dict(self.guard.config(), ready=True, auth_ready=False,
                                  status='AuthMe unavailable', pending_events=4))
        bridge = self.guard.health()['bridge']
        self.assertFalse(bridge['healthy'])
        self.assertEqual('AuthMe unavailable', bridge['status'])
        self.assertEqual(4, bridge['pending_events'])
        self.guard.heartbeat(dict(self.guard.config(), ready=False, auth_ready=True))
        self.assertFalse(self.guard.health()['bridge']['healthy'])
        self.guard.heartbeat(dict(self.guard.config(), ready=True, auth_ready=True))
        self.assertTrue(self.guard.health()['bridge']['healthy'])

    def test_allow_once_consumed_only_on_success_and_not_remembered(self):
        self.enroll()
        rejected = self.request(ip='1.1.1.1')
        self.guard.prelogin(rejected)
        self.guard.review(rejected['attempt_id'], 'allow_once', 'admin')
        allowed = self.request(ip='1.1.1.1')
        decision = self.guard.prelogin(allowed)
        self.assertEqual('approved_once', decision['reason'])
        self.assertFalse(decision['trust_on_success'])
        self.finish(allowed, ('disconnected',))
        retry = self.request(ip='1.1.1.1')
        self.assertEqual('allow', self.guard.prelogin(retry)['action'])
        self.finish(retry)
        self.assertEqual('deny', self.guard.prelogin(self.request(ip='1.1.1.1'))['action'])
        self.assertNotIn('1.1.1.1', self.guard.profile('online:' + UUID)['accepted_ips'])

    def test_allow_once_expires_after_fifteen_minutes(self):
        self.enroll()
        request = self.request(ip='1.1.1.1')
        self.guard.prelogin(request)
        self.guard.review(request['attempt_id'], 'allow_once')
        self.now += 901
        self.assertEqual('deny', self.guard.prelogin(self.request(ip='1.1.1.1'))['action'])

    def test_trust_area_preserves_original_and_requires_usable_geo(self):
        self.enroll()
        request = self.request(ip='1.1.1.1')
        self.guard.prelogin(request)
        self.guard.review(request['attempt_id'], 'trust_area')
        self.assertEqual('allow', self.guard.prelogin(self.request(ip='1.1.1.1'))['action'])
        profile = self.guard.profile('online:' + UUID)
        self.assertEqual(['US', 'AU'], [ref['country'] for ref in profile['references']])
        unknown = self.request(ip='192.168.1.1')
        self.guard.prelogin(unknown)
        with self.assertRaises(ValueError):
            self.guard.review(unknown['attempt_id'], 'trust_area')

    def test_outage_journal_new_identity_replay_idempotent(self):
        request = self.request(fallback=True, fallback_decision='new_identity', occurred_at=self.now * 1000)
        self.assertEqual('allow', self.guard.prelogin(request)['action'])
        self.assertEqual('allow', self.guard.prelogin(request)['action'])
        self.finish(request)
        self.finish(request)
        self.assertEqual(1, len(self.guard.get_events()['events']))
        replay = self.request(fallback=True, fallback_decision='accepted_ip', occurred_at=self.now * 1000)
        self.assertEqual('allow', self.guard.prelogin(replay)['action'])

    def test_outage_enrollment_reservation_survives_slow_journal_replay(self):
        request = self.request(fallback=True, fallback_decision='new_identity', occurred_at=self.now * 1000)
        self.guard.prelogin(request)
        self.now += 1000
        self.guard.maintenance()
        conflict = self.guard.prelogin(self.request(ip='1.1.1.1'))
        self.assertEqual('identity_busy', conflict['reason'])
        self.assertTrue(self.finish(request)['committed'])
        self.assertEqual(['8.8.8.8'], self.guard.profile('online:' + UUID)['accepted_ips'])

    def test_live_first_enrollment_reservation_still_expires(self):
        request = self.request()
        self.guard.prelogin(request)
        self.now += 121
        result = self.finish(request)
        self.assertTrue(result['revoked'])
        self.assertTrue(result['accepted'])
        self.assertEqual([], self.guard.profile('online:' + UUID)['accepted_ips'])

    def test_expired_approval_outcome_is_acknowledged_without_learning_ip(self):
        self.offline()
        self.enroll()
        rejected = self.request(ip='1.1.1.1')
        self.guard.prelogin(rejected)
        self.guard.review(rejected['attempt_id'], 'allow_once')
        request = self.request(ip='1.1.1.1')
        self.guard.prelogin(request)
        self.finish(request, ('joined',))
        self.now += 901
        result = self.finish(request, ('authenticated',))
        self.assertTrue(result['revoked'])
        self.assertTrue(result['accepted'])
        self.assertEqual(['8.8.8.8'], self.guard.profile('offline:steve')['accepted_ips'])

    def test_outage_journal_cannot_reclassify_existing_or_unknown_ip(self):
        self.enroll()
        for choice in ('new_identity', 'accepted_ip'):
            request = self.request(ip='1.1.1.1', fallback=True, fallback_decision=choice, occurred_at=self.now * 1000)
            with self.assertRaises(GuardConflict):
                self.guard.prelogin(request)

    def test_stale_disconnect_acknowledged_without_journal_deadlock(self):
        request = self.enroll()
        self.guard.save_settings(dict(radius_km=200))
        result = self.finish(request, ('disconnected',))
        self.assertTrue(result['accepted'])
        self.assertTrue(result['revoked'])
        self.assertEqual(['8.8.8.8'], self.guard.profile('online:' + UUID)['accepted_ips'])

    def test_stale_success_retains_existence_but_not_permission(self):
        request = self.request()
        self.guard.prelogin(request)
        self.guard.save_settings(dict(radius_km=200))
        result = self.finish(request)
        self.assertTrue(result['revoked'])
        self.assertTrue(result['committed'])
        profile = self.guard.profile('online:' + UUID)
        self.assertEqual([], profile['accepted_ips'])
        self.assertEqual([], profile['references'])
        self.assertEqual('deny', self.guard.prelogin(self.request())['action'])

    def test_stale_fallback_replay_is_audit_only_never_live_allow(self):
        request = self.request(fallback=True, fallback_decision='new_identity', occurred_at=self.now * 1000)
        self.guard.save_settings(dict(radius_km=200))
        decision = self.guard.prelogin(request)
        self.assertEqual('deny', decision['action'])
        self.assertTrue(decision['replay_accepted'])
        self.assertFalse(decision['trust_on_success'])
        self.finish(request)
        self.assertEqual([], self.guard.profile('online:' + UUID)['accepted_ips'])
        live = dict(request, attempt_id=uuid.uuid4().hex, fallback=False)
        with self.assertRaises(GuardConflict):
            self.guard.prelogin(live)

    def test_stale_offline_outcome_still_requires_authentication(self):
        self.offline()
        request = self.request()
        self.guard.prelogin(request)
        self.guard.save_settings(dict(radius_km=200))
        self.assertFalse(self.finish(request)['committed'])
        self.assertEqual([], self.guard.roster()['identities'])
        self.assertTrue(self.finish(request, ('authenticated',))['committed'])
        self.assertEqual([], self.guard.profile('offline:steve')['accepted_ips'])

    def test_scope_policy_identity_mismatch_never_fallback(self):
        for field, value in [('scope', 'wrong'), ('policy_revision', 0), ('identity_mode', 'offline')]:
            request = self.request()
            request[field] = value
            with self.assertRaises(GuardConflict):
                self.guard.prelogin(request)
        self.server['identity_mode'] = 'offline'
        with self.assertRaises(GuardConflict):
            self.guard.prelogin(self.request())

    def test_outcome_identity_and_ip_bound_and_no_secret_input(self):
        request = self.request()
        self.guard.prelogin(request)
        for change in ({'ip': '1.1.1.1'}, {'uuid': str(uuid.uuid4())}, {'password': 'secret'}):
            with self.assertRaises(ValueError):
                self.guard.outcome(dict(request, stage='joined', **change))
        self.assertNotIn('secret', json.dumps(self.guard.get_events()))

    def test_persistence_and_scope_reset_preserve_geoip_credentials(self):
        self.enroll()
        self.guard = self.new_service()
        self.assertEqual(1, len(self.guard.roster()['identities']))
        self.scope = 'server-b'
        self.assertEqual([], self.guard.roster()['identities'])
        self.assertEqual('off', self.guard.config()['mode'])
        self.assertTrue(self.guard.config()['has_license_key'])

    def test_retention_keeps_identity_roster_and_pagination_is_bounded(self):
        self.enroll()
        self.guard.prelogin(self.request(ip='1.1.1.1'))
        page = self.guard.get_events(limit=1)
        self.assertIsNotNone(page['next_cursor'])
        self.assertEqual(1, len(self.guard.get_events(limit=1, cursor=page['next_cursor'])['events']))
        self.now += 91 * 86400
        self.guard.maintenance()
        self.assertEqual([], self.guard.get_events()['events'])
        self.assertEqual(1, len(self.guard.roster()['identities']))

    def test_retention_cap_does_not_delete_profiles(self):
        self.enroll()
        for _ in range(4):
            self.guard.prelogin(self.request())
        with patch('connection_guard.EVENT_LIMIT', 2):
            self.guard.maintenance()
        self.assertEqual(2, len(self.guard.get_events()['events']))
        self.assertEqual(1, len(self.guard.roster()['identities']))

    def test_retention_cap_preserves_inflight_authentication_state(self):
        self.offline()
        request = self.request()
        self.guard.prelogin(request)
        self.finish(request, ('joined',))
        self.guard.prelogin(self.request(ip='1.1.1.1'))
        with patch('connection_guard.EVENT_LIMIT', 1):
            self.guard.maintenance()
        self.assertNotIn(request['attempt_id'], [event['attempt_id'] for event in self.guard.get_events()['events']])
        result = self.finish(request, ('authenticated',))
        self.assertTrue(result['committed'])
        self.assertEqual(['8.8.8.8'], self.guard.profile('offline:steve')['accepted_ips'])

    def test_pruned_completed_attempt_disconnect_and_replay_are_safe_acks(self):
        request = self.enroll()
        self.now += 91 * 86400
        self.guard.maintenance()
        for stage in ('joined', 'disconnected'):
            result = self.finish(request, (stage,))
            self.assertTrue(result['accepted'])
            self.assertTrue(result['revoked'])
            self.assertFalse(result['committed'])
        self.assertEqual(['8.8.8.8'], self.guard.profile('online:' + UUID)['accepted_ips'])

    def test_inflight_capacity_is_bounded_and_denies_without_enrollment(self):
        with patch('connection_guard.MAX_INFLIGHT', 1):
            self.guard.prelogin(self.request())
            request = self.request(username='Alex', player_uuid=str(uuid.uuid4()))
            result = self.guard.prelogin(request)
        self.assertEqual(('deny', 'capacity_exhausted'), (result['action'], result['reason']))
        self.assertEqual([], self.guard.roster()['identities'])

    def test_initialized_missing_database_refuses_silent_reset(self):
        self.enroll()
        saved = self.guard.database_path.with_suffix('.saved')
        self.guard.database_path.rename(saved)
        try:
            with self.assertRaisesRegex(GuardConflict, 'missing or invalid'):
                self.new_service()
            self.assertFalse(self.guard.database_path.exists())
        finally:
            saved.rename(self.guard.database_path)
        self.assertEqual(1, len(self.new_service().roster()['identities']))

    def test_initialized_missing_identity_table_or_policy_is_not_recreated(self):
        with sqlite3.connect(self.guard.database_path) as conn:
            conn.execute('DROP TABLE identities')
        with self.assertRaisesRegex(GuardConflict, 'incomplete'):
            self.new_service()
        with sqlite3.connect(self.guard.database_path) as conn:
            self.assertEqual([], conn.execute("SELECT name FROM sqlite_master WHERE name='identities'").fetchall())

    def test_initialized_missing_policy_is_not_defaulted_off(self):
        with sqlite3.connect(self.guard.database_path) as conn:
            conn.execute("DELETE FROM metadata WHERE key='mode'")
        with self.assertRaisesRegex(GuardConflict, 'incomplete'):
            self.new_service()

    def test_ipv6_mapped_ipv4_normalized(self):
        self.enroll()
        self.assertEqual('accepted_ip', self.guard.prelogin(self.request(ip='::ffff:8.8.8.8'))['reason'])
        self.assertEqual('allow', self.guard.prelogin(self.request(ip='2001:4860:4860::8888'))['action'])

    def make_history(self):
        path = Path(self.temp.name) / 'players.db'
        with sqlite3.connect(path) as conn:
            conn.executescript('CREATE TABLE users(uuid TEXT,username TEXT); CREATE TABLE login_events(event_id INTEGER PRIMARY KEY,uuid TEXT,ip TEXT,timestamp TEXT);')
            conn.executemany('INSERT INTO users VALUES (?,?)', [(UUID, 'Steve'), ('unknown', 'Bad')])
            conn.executemany('INSERT INTO login_events(uuid,ip,timestamp) VALUES (?,?,?)',
                             [(UUID, '8.8.8.8', '2025-01-01'), (UUID, '1.1.1.1', '2025-02-01'),
                              ('unknown', '9.9.9.9', '2025-01-01')])
        return path

    def test_history_explicit_import_earliest_ip_and_unknown_skipped(self):
        path = self.make_history()
        preview = self.guard.history_preview(path)
        self.assertEqual(1, len(preview['candidates']))
        self.assertEqual('8.8.8.8', preview['candidates'][0]['ip'])
        self.assertEqual([], self.guard.roster()['identities'])
        self.assertEqual(1, self.guard.history_import(path)['imported'])
        with self.assertRaises(GuardConflict):
            self.guard.history_import(path)

    def test_offline_history_not_password_verified_or_outage_authority(self):
        self.offline()
        self.guard.history_import(self.make_history())
        roster = self.guard.roster()['identities']
        self.assertEqual([dict(identity='offline:steve', accepted_ips=[])], roster)
        self.assertFalse(self.guard.profile('offline:steve')['authenticated'])
        self.guard.heartbeat(dict(self.guard.config(), ready=True, auth_ready=True))
        self.enroll()
        self.assertTrue(self.guard.profile('offline:steve')['authenticated'])


if __name__ == '__main__':
    unittest.main()
