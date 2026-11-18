"""Real smtplib protocol tests with scripted sockets; no network or mail delivery."""

import base64
from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default as email_policy
import json
from pathlib import Path
import smtplib
import ssl
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from panel_mail import (DEFAULT_SMTP, PanelMailer, SMTPDeliveryError,
                        SMTP_TIMEOUT_SECONDS, _smtp_password, smtp_failure)


BANNER = [b'220 test.smtp ready\r\n']
EHLO_TLS = [b'250-test.smtp\r\n', b'250 STARTTLS\r\n']
TLS_READY = [b'220 Begin TLS\r\n']
EHLO_AUTH = [b'250-test.smtp\r\n', b'250 AUTH PLAIN LOGIN\r\n']
PREFIX = BANNER + EHLO_TLS + TLS_READY + EHLO_AUTH
AUTH_OK = [b'235 Authenticated\r\n']
ENVELOPE_OK = [b'250 Sender accepted\r\n', b'250 Recipient accepted\r\n', b'354 Send data\r\n']
DATA_OK = [b'250 Queued\r\n']
QUIT_OK = [b'221 Bye\r\n']
ACCEPTED = AUTH_OK + ENVELOPE_OK + DATA_OK


class MemoryStore:
    def __init__(self, **overrides):
        self.values = {**DEFAULT_SMTP, 'enabled': True, 'host': 'smtp.gmail.com',
                       'username': 'dummy@example.com', 'password': 'abcdefghijklmnop',
                       'from_email': 'dummy@example.com', 'panel_url': 'https://panel.example.com',
                       **overrides}

    def get_setting(self, key, default=None):
        return self.values.copy()

    def set_setting(self, key, value):
        self.values = value.copy()


class ScriptedSocket:
    """Only replace the wire: smtplib still parses replies, AUTH and MIME data."""
    def __init__(self, replies):
        self.replies = iter(replies)
        self.commands = []
        self.closed = False
        self.timeout = None

    def makefile(self, mode):
        return self

    def readline(self, limit):
        reply = next(self.replies, b'')
        if isinstance(reply, Exception):
            raise reply
        return reply

    def sendall(self, data):
        if self.closed:
            raise OSError('Test socket is closed')
        self.commands.append(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


@contextmanager
def scripted_transport(replies, tls_error=None):
    wire = ScriptedSocket(replies)
    # Keep a real secure context, but don't make an external TLS connection.
    context = ssl.create_default_context()
    with patch('panel_mail.socket.create_connection', return_value=wire) as connect, \
         patch('panel_mail.socket.getfqdn', return_value='panel.example.com'), \
         patch('panel_mail.ssl.create_default_context', return_value=context), \
         patch.object(context, 'wrap_socket', return_value=wire, side_effect=tls_error) as wrap:
        yield wire, connect, context, wrap


class SMTPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.mailer = PanelMailer(self.store)

    def send(self):
        return self.mailer.send('recipient@example.com', 'Protocol test', 'Dummy body.\n')

    def failure(self, replies, stage, code, tls_error=None):
        with scripted_transport(replies, tls_error=tls_error) as (wire, connect, context, wrap):
            with self.assertRaises(SMTPDeliveryError) as caught:
                self.send()
            connect.assert_called_once()
        diagnostic = smtp_failure(caught.exception)
        self.assertEqual(diagnostic['stage'], stage)
        self.assertEqual(diagnostic['code'], code)
        self.assertIn('Failed while', diagnostic['message'])
        self.assertFalse(any(line.lower().startswith(b'quit') for line in wire.commands))
        self.assertTrue(wire.closed)
        return wire, diagnostic, caught.exception

    def test_starttls_protocol_headers_envelope_timeout_and_certificate_policy(self):
        with scripted_transport(PREFIX + ACCEPTED + QUIT_OK) as (wire, connect, context, wrap):
            self.send()
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            connect.assert_called_once_with(('smtp.gmail.com', 587), SMTP_TIMEOUT_SECONDS, None)
            wrap.assert_called_once_with(wire, server_hostname='smtp.gmail.com')
        verbs = [line.split()[0].lower() for line in wire.commands]
        self.assertEqual(verbs[:4], [b'ehlo', b'starttls', b'ehlo', b'auth'])
        self.assertEqual(verbs[4:7], [b'mail', b'rcpt', b'data'])
        self.assertEqual(verbs[-1], b'quit')
        self.assertIn(b'from:<dummy@example.com>', wire.commands[4].lower())
        self.assertIn(b'to:<recipient@example.com>', wire.commands[5].lower())
        body = next(line for line in wire.commands if line.startswith(b'From:'))
        message = BytesParser().parsebytes(body)
        self.assertIsNotNone(message['Date'])
        self.assertIsNotNone(message['Message-ID'])
        self.assertEqual(message['To'], 'recipient@example.com')
        self.assertEqual(message.get_content_type(), 'text/plain')
        self.assertEqual(wire.timeout, 3)
        self.assertTrue(wire.closed)

    def test_html_and_plain_text_survive_real_smtp_mime_serialization(self):
        plain = 'Hello Caf\u00e9 \U0001f44b\nPassword: A9<&"sample!\n'
        html = '<p>Hello Caf\u00e9 \U0001f44b</p><h1>A9&lt;&amp;&quot;sample!</h1>\n'
        with scripted_transport(PREFIX + ACCEPTED + QUIT_OK) as (wire, *unused):
            self.mailer.send('recipient@example.com', 'HTML invitation', plain, html=html)
        payload = next(line for line in wire.commands if line.startswith(b'From:'))
        message = BytesParser(policy=email_policy).parsebytes(payload)
        self.assertEqual(message.get_content_type(), 'multipart/alternative')
        self.assertEqual([part.get_content_type() for part in message.iter_parts()],
                         ['text/plain', 'text/html'])
        self.assertEqual(message.get_body(preferencelist=('plain',)).get_content().replace('\r\n', '\n'), plain)
        self.assertEqual(message.get_body(preferencelist=('html',)).get_content().replace('\r\n', '\n'), html)
        self.assertFalse(message.defects)

    def test_inline_server_icon_survives_smtp_as_related_html_content(self):
        from test_server_icon import image_bytes
        icon = image_bytes(size=(64, 64))
        cid = 'test-server-icon@byte-panel.local'
        html = f'<h2>Welcome</h2><img src="cid:{cid}" width="64" height="64">\n'
        with scripted_transport(PREFIX + ACCEPTED + QUIT_OK) as (wire, *unused):
            self.mailer.send('recipient@example.com', 'Welcome with icon', 'Welcome\n',
                             html=html, inline_images={cid: icon})
        payload = next(line for line in wire.commands if line.startswith(b'From:'))
        message = BytesParser(policy=email_policy).parsebytes(payload)
        self.assertEqual(message.get_content_type(), 'multipart/alternative')
        self.assertEqual([part.get_content_type() for part in message.iter_parts()],
                         ['text/plain', 'multipart/related'])
        self.assertEqual(message.get_body(preferencelist=('html',)).get_content().replace('\r\n', '\n'), html)
        related = list(message.iter_parts())[1]
        self.assertEqual([part.get_content_type() for part in related.iter_parts()], ['text/html', 'image/png'])
        image = list(related.iter_parts())[1]
        self.assertEqual(image['Content-ID'], '<' + cid + '>')
        self.assertEqual(image.get_content_disposition(), 'inline')
        self.assertEqual(image.get_payload(decode=True), icon)
        self.assertFalse(any(part.defects for part in message.walk()))

    def test_invalid_inline_image_headers_are_rejected_before_smtp(self):
        from test_server_icon import image_bytes
        icon = image_bytes(size=(64, 64))
        with patch('panel_mail.smtplib.SMTP') as smtp:
            for cid in ('bad\r\nBcc: victim@example.com', '<bad>', 'bad:cid', ''):
                with self.subTest(cid=repr(cid)), self.assertRaises(ValueError):
                    self.mailer.send('recipient@example.com', 'Test', 'Text', html='<p>Text</p>',
                                     inline_images={cid: icon})
            with self.assertRaises(ValueError):
                self.mailer.send('recipient@example.com', 'Test', 'Text', inline_images={'safe': icon})
            smtp.assert_not_called()

    def test_implicit_tls_does_not_issue_starttls(self):
        self.store.values.update(security='ssl', port=465)
        with scripted_transport(BANNER + EHLO_AUTH + ACCEPTED + QUIT_OK) as (wire, connect, context, wrap):
            self.send()
            connect.assert_called_once_with(('smtp.gmail.com', 465), 30, None)
            wrap.assert_called_once_with(wire, server_hostname='smtp.gmail.com')
            self.assertTrue(context.check_hostname)
        self.assertFalse(any(line.startswith(b'STARTTLS') for line in wire.commands))
        self.assertEqual(sum(line.startswith(b'ehlo') for line in wire.commands), 1)

    def test_wrapped_read_timeouts_are_not_misreported_as_disconnects(self):
        cases = [
            ('connect', []),
            ('ehlo', BANNER),
            ('starttls', BANNER + EHLO_TLS),
            ('ehlo_tls', BANNER + EHLO_TLS + TLS_READY),
            ('authentication', PREFIX),
            ('send', PREFIX + AUTH_OK + ENVELOPE_OK),
        ]
        for stage, prefix in cases:
            with self.subTest(stage=stage):
                wire, diagnostic, error = self.failure(prefix + [TimeoutError('private timeout')],
                                                       stage, 'connection_timeout')
                self.assertIsInstance(error.error, smtplib.SMTPServerDisconnected)
                self.assertIn('30 seconds', diagnostic['message'])
                self.assertNotIn('private timeout', json.dumps(diagnostic))
                if stage == 'send':
                    self.assertIn('Delivery is unconfirmed', diagnostic['message'])
                    self.assertNotIn('No message was submitted', diagnostic['message'])
                else:
                    self.assertIn('No message was submitted', diagnostic['message'])

    def test_credential_policy_and_service_rejections_do_not_fall_through_to_login(self):
        for reply_code in (535, 534, 454, 421):
            with self.subTest(reply_code=reply_code):
                replies = PREFIX + [str(reply_code).encode() + b' private-provider-response\r\n', b'']
                wire, diagnostic, error = self.failure(replies, 'authentication', 'authentication_failed')
                self.assertEqual(diagnostic['smtp_code'], reply_code)
                self.assertEqual(sum(line.startswith(b'AUTH ') for line in wire.commands), 1)
                self.assertFalse(any(line.startswith(b'AUTH LOGIN') for line in wire.commands))
                self.assertFalse(any(line.startswith(b'mail') for line in wire.commands))
                self.assertNotIn('private-provider-response', json.dumps(diagnostic))
                self.assertNotIn('private-provider-response', str(error))

    def test_unsupported_plain_can_fall_back_to_login(self):
        replies = PREFIX + [b'504 Mechanism unavailable\r\n', b'334 UGFzc3dvcmQ6\r\n']
        with scripted_transport(replies + ACCEPTED + QUIT_OK) as (wire, connect, context, wrap):
            self.send()
        auth = [line for line in wire.commands if line.startswith(b'AUTH ')]
        self.assertTrue(auth[0].startswith(b'AUTH PLAIN '))
        self.assertTrue(auth[1].startswith(b'AUTH LOGIN '))
        self.assertEqual(len(auth), 2)
        self.assertIn(base64.b64encode(b'abcdefghijklmnop') + b'\r\n', wire.commands)

    def test_login_only_server_is_supported(self):
        prefix = BANNER + EHLO_TLS + TLS_READY + [b'250-test.smtp\r\n', b'250 AUTH LOGIN\r\n']
        with scripted_transport(prefix + [b'334 UGFzc3dvcmQ6\r\n'] + ACCEPTED + QUIT_OK) as (wire, *unused):
            self.send()
        self.assertEqual(sum(line.startswith(b'AUTH LOGIN ') for line in wire.commands), 1)
        self.assertFalse(any(line.startswith(b'AUTH PLAIN') for line in wire.commands))

    def test_cram_md5_server_is_supported(self):
        prefix = BANNER + EHLO_TLS + TLS_READY + [b'250-test.smtp\r\n', b'250 AUTH CRAM-MD5\r\n']
        challenge = b'334 ' + base64.b64encode(b'test-challenge') + b'\r\n'
        with scripted_transport(prefix + [challenge] + ACCEPTED + QUIT_OK) as (wire, *unused):
            self.send()
        self.assertIn(b'AUTH CRAM-MD5\r\n', wire.commands)

    def test_real_disconnects_keep_their_stage(self):
        for stage, prefix in (('connect', []), ('authentication', PREFIX),
                              ('send', PREFIX + AUTH_OK + ENVELOPE_OK)):
            with self.subTest(stage=stage):
                wire, diagnostic, error = self.failure(prefix + [b''], stage, 'server_disconnected')
                self.assertNotIn('Check the port', diagnostic['message'])

    def test_ehlo_rejections_are_checked_before_authentication(self):
        prefix = BANNER + EHLO_TLS + TLS_READY
        wire, diagnostic, error = self.failure(prefix + [b'550 Greeting rejected\r\n'],
                                               'ehlo_tls', 'greeting_rejected')
        self.assertEqual(diagnostic['smtp_code'], 550)
        self.assertFalse(any(line.startswith(b'AUTH ') for line in wire.commands))

    def test_starttls_failure_never_sends_credentials(self):
        wire, diagnostic, error = self.failure(BANNER + EHLO_TLS + [b'454 TLS unavailable\r\n'],
                                               'starttls', 'smtp_rejected')
        self.assertFalse(any(line.startswith(b'AUTH ') for line in wire.commands))

    def test_certificate_failure_never_sends_credentials(self):
        for security, prefix, stage in (
            ('starttls', BANNER + EHLO_TLS + TLS_READY, 'starttls'),
            ('ssl', BANNER, 'connect_tls'),
        ):
            with self.subTest(security=security):
                self.store.values['security'] = security
                with scripted_transport(prefix, tls_error=ssl.SSLCertVerificationError(1, 'private certificate')) as (wire, *unused):
                    with self.assertRaises(SMTPDeliveryError) as caught:
                        self.send()
                diagnostic = smtp_failure(caught.exception)
                self.assertEqual(diagnostic['stage'], stage)
                self.assertEqual(diagnostic['code'], 'certificate_failed')
                self.assertNotIn('private certificate', json.dumps(diagnostic))
                self.assertFalse(any(line.startswith(b'AUTH ') for line in wire.commands))

    def test_accepted_mail_stays_successful_if_quit_fails(self):
        for reply in (b'421 Session closed\r\n', b'', TimeoutError('QUIT timeout')):
            with self.subTest(reply=type(reply).__name__):
                with scripted_transport(PREFIX + ACCEPTED + [reply]) as (wire, connect, context, wrap):
                    self.send()
                    connect.assert_called_once()
                self.assertEqual(sum(line.startswith(b'data') for line in wire.commands), 1)
                self.assertEqual(sum(line.lower().startswith(b'quit') for line in wire.commands), 1)
                self.assertTrue(wire.closed)

    def test_message_rejection_stays_a_failure(self):
        replies = PREFIX + AUTH_OK + ENVELOPE_OK + [b'554 Message rejected\r\n', b'250 Reset\r\n']
        wire, diagnostic, error = self.failure(replies, 'send', 'message_rejected')
        self.assertEqual(diagnostic['smtp_code'], 554)
        self.assertEqual(sum(line.startswith(b'data') for line in wire.commands), 1)

    def test_gmail_saved_grouped_password_is_normalized_only_for_delivery(self):
        original = ' abcd efgh\u00a0ijkl\tmnop '
        self.store.values['password'] = original
        with scripted_transport(PREFIX + ACCEPTED + QUIT_OK) as (wire, *unused):
            self.send()
        auth = next(line for line in wire.commands if line.startswith(b'AUTH PLAIN '))
        password = base64.b64decode(auth.split()[2]).split(b'\0')[-1]
        self.assertEqual(password, b'abcdefghijklmnop')
        self.assertEqual(self.store.values['password'], original)

    def test_other_passwords_and_hosts_are_not_modified(self):
        for host, password in (
            ('smtp.example.com', 'abcd efgh ijkl mnop'),
            ('smtp.gmail.com.evil.example', 'abcd efgh ijkl mnop'),
            ('smtp.gmail.com', ' other Password! '),
        ):
            with self.subTest(host=host):
                self.assertEqual(_smtp_password({'host': host, 'password': password}), password)

    def test_trusted_unauthenticated_helo_relay_still_works(self):
        self.store.values.update(security='none', username='', password='')
        replies = BANNER + [b'500 EHLO unsupported\r\n', b'250 HELO accepted\r\n']
        with scripted_transport(replies + ENVELOPE_OK + DATA_OK + QUIT_OK) as (wire, *unused):
            self.send()
        self.assertTrue(any(line.startswith(b'helo') for line in wire.commands))
        self.assertFalse(any(line.startswith(b'AUTH ') for line in wire.commands))

    def test_elapsed_time_is_safe_and_visible(self):
        with scripted_transport(PREFIX + [TimeoutError('private')]), \
             patch('panel_mail.time.monotonic', side_effect=[100, 130.25]):
            with self.assertRaises(SMTPDeliveryError) as caught:
                self.send()
        diagnostic = smtp_failure(caught.exception)
        self.assertEqual(diagnostic['elapsed_seconds'], 30.2)
        self.assertIn('after 30.2s', diagnostic['message'])

    def test_timeout_cause_cycle_does_not_loop(self):
        error = smtplib.SMTPServerDisconnected('private')
        error.__context__ = error
        self.assertEqual(smtp_failure(error)['code'], 'server_disconnected')


if __name__ == '__main__':
    unittest.main()
