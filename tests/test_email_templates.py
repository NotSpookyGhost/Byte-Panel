"""Invitation text templates; no external mail or production data."""
from email.message import EmailMessage
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from panel_mail import (DEFAULT_SMTP, PanelMailer, PasswordEmailDeliveryError,
                        PASSWORD_EMAIL_DELAY_SECONDS, render_email_text)


class EmailTemplateTests(unittest.TestCase):
    def setUp(self):
        self.smtp = {**DEFAULT_SMTP, 'enabled': True, 'panel_url': 'https://panel.example.com/'}
        self.store = Mock()
        self.store.get_setting.side_effect = lambda key, default: self.smtp.copy()
        self.store.invitation_is_current.return_value = True
        delay = patch('panel_mail.sleep')
        self.delay = delay.start()
        self.addCleanup(delay.stop)
        self.config = {'panel_name': 'Legacy Panel Branding', 'server_name': 'Byte Community'}
        self.mailer = PanelMailer(self.store, lambda: self.config)
        self.user = {'username': 'player', 'email': 'player@example.com',
                     'temporary_password_expires': 2000000000}

    def test_named_tokens_whitespace_and_unknown_tokens(self):
        self.assertEqual(
            render_email_text('{{servername}} / {{ servername }} / {{unknown}}',
                              {'servername': 'Byte Community'}),
            'Byte Community / Byte Community / {{unknown}}')

    def test_values_are_not_rendered_again(self):
        self.assertEqual(
            render_email_text('{{servername}} / {{username}}',
                              {'servername': '{{username}}', 'username': 'player'}),
            '{{username}} / player')

    def test_template_expressions_are_not_evaluated(self):
        text = '{{7 * 7}} {% include "private" %} {{config.secret}}'
        self.assertEqual(render_email_text(text, {}), text)

    def test_invitation_uses_server_name_and_preserves_sign_in_details(self):
        with patch.object(self.mailer, 'send') as send:
            self.assertEqual(self.mailer.welcome(self.user, 'temporary-test-password'), 'sent')
        self.assertEqual(send.call_count, 2)
        self.delay.assert_called_once_with(12)
        recipient, subject, body = send.call_args_list[0].args
        self.assertEqual(recipient, 'player@example.com')
        self.assertEqual(subject, 'Welcome to Byte Community - your panel sign-in details')
        self.assertIn('Hello player,', body)
        self.assertIn('Welcome to Byte Community!', body)
        self.assertIn('Panel: https://panel.example.com/login', body)
        self.assertIn('Username: player', body)
        self.assertNotIn('temporary-test-password', body)
        self.assertNotIn('Temporary password:', body)
        self.assertIn('second email', body)
        self.assertIn('Panel access stays locked until you change', body)
        self.assertNotIn('Legacy Panel Branding', body)
        self.assertNotIn('{{', body)
        self.assertNotIn('temporary-test-password', subject)
        recipient, subject, password_body = send.call_args_list[1].args
        self.assertEqual(recipient, self.user['email'])
        self.assertEqual(subject, 'Your temporary password for Byte Community')
        self.assertIn('Temporary password: temporary-test-password', password_body)
        self.assertIn('24 hours', password_body)
        self.assertNotIn('temporary-test-password', subject)
        self.assertNotIn('{{', password_body)

    def test_name_is_loaded_again_for_each_invitation(self):
        with patch.object(self.mailer, 'send') as send:
            self.mailer.welcome(self.user, 'first-password')
            self.config['server_name'] = 'Renamed Panel'
            self.mailer.welcome(self.user, 'second-password')
        self.assertIn('Byte Community', send.call_args_list[0].args[1])
        self.assertIn('Byte Community', send.call_args_list[1].args[1])
        self.assertIn('Renamed Panel', send.call_args_list[2].args[1])
        self.assertIn('Welcome to Renamed Panel!', send.call_args_list[2].args[2])
        self.assertIn('Renamed Panel', send.call_args_list[3].args[1])

    def test_default_name_for_missing_or_invalid_names(self):
        for name in (None, '', ' \r\n\t ', 123):
            with self.subTest(name=name):
                self.config['server_name'] = name
                self.assertEqual(self.mailer.server_name(), 'Byte Panel')
        self.assertEqual(PanelMailer(self.store).server_name(), 'Byte Panel')

    def test_control_characters_cannot_create_email_headers(self):
        self.config['server_name'] = 'Byte\r\nBcc: other@example.com\x00\x7f'
        with patch.object(self.mailer, 'send') as send:
            self.mailer.welcome(self.user, 'temporary-test-password')
        subject = send.call_args.args[1]
        self.assertFalse(any(ord(char) < 32 or ord(char) == 127 for char in subject))
        message = EmailMessage()
        message['Subject'] = subject
        self.assertIsNone(message['Bcc'])
        message.as_bytes()

    def test_unicode_and_markup_remain_plain_text(self):
        self.config['server_name'] = 'Caf\u00e9 <Builders> & Friends'
        with patch.object(self.mailer, 'send') as send:
            self.mailer.welcome(self.user, 'temporary-test-password')
        _, subject, body = send.call_args_list[0].args
        message = EmailMessage()
        message['Subject'] = subject
        message.set_content(body)
        self.assertEqual(message.get_content_type(), 'text/plain')
        self.assertIn('Welcome to Caf\u00e9 <Builders> & Friends!', message.get_content())
        message.as_bytes()

    def test_disabled_notifications_do_not_render_or_send(self):
        self.smtp['notify_new_users'] = False
        with patch.object(self.mailer, 'load_panel_config') as load, \
             patch.object(self.mailer, 'send') as send:
            self.assertEqual(self.mailer.welcome(self.user, 'private-password'), 'not_requested')
        load.assert_not_called()
        send.assert_not_called()
        self.delay.assert_not_called()

    def test_send_pause_send_order(self):
        events = []
        self.delay.side_effect = lambda seconds: events.append(('pause', seconds))
        with patch.object(self.mailer, 'send', side_effect=lambda *args, **kwargs: events.append(('send', args[1]))):
            self.assertEqual(self.mailer.welcome(self.user, 'private-password'), 'sent')
        self.assertEqual(PASSWORD_EMAIL_DELAY_SECONDS, 12)
        self.assertEqual(events, [('send', 'Welcome to Byte Community - your panel sign-in details'),
                                  ('pause', 12), ('send', 'Your temporary password for Byte Community')])

    def test_first_email_failure_never_sends_password_or_waits(self):
        with patch.object(self.mailer, 'send', side_effect=OSError('failed')) as send:
            with self.assertRaises(OSError):
                self.mailer.welcome(self.user, 'private-password')
        self.assertEqual(send.call_count, 1)
        self.assertNotIn('private-password', send.call_args.args[2])
        self.delay.assert_not_called()

    def test_second_email_failure_is_distinct_and_does_not_retry(self):
        failure = OSError('private-password')
        with patch.object(self.mailer, 'send', side_effect=[None, failure]) as send:
            with self.assertRaises(PasswordEmailDeliveryError) as caught:
                self.mailer.welcome(self.user, 'private-password')
        self.assertIs(caught.exception.error, failure)
        self.assertNotIn('private-password', str(caught.exception))
        self.assertEqual(send.call_count, 2)
        self.delay.assert_called_once_with(12)

    def test_replaced_invitation_is_cancelled_before_first_or_second_send(self):
        for current, count in (([False], 0), ([True, False], 1)):
            with self.subTest(current=current):
                self.store.invitation_is_current.side_effect = current
                with patch.object(self.mailer, 'send') as send:
                    self.assertEqual(self.mailer.welcome(self.user, 'private-password'), 'cancelled')
                self.assertEqual(send.call_count, count)

    def test_disabling_smtp_or_notifications_during_delay_stops_second_email(self):
        for setting in ('enabled', 'notify_new_users'):
            with self.subTest(setting=setting):
                self.smtp.update(enabled=True, notify_new_users=True)
                self.delay.side_effect = lambda seconds: self.smtp.update({setting: False})
                with patch.object(self.mailer, 'send') as send:
                    self.assertEqual(self.mailer.welcome(self.user, 'private-password'), 'cancelled')
                self.assertEqual(send.call_count, 1)

    def test_password_token_is_blank_even_in_customized_welcome(self):
        with patch('panel_mail.WELCOME_TEMPLATE') as template, patch.object(self.mailer, 'send') as send:
            template.read_text.return_value = 'Hello {{username}}, password={{ password }}, legacy={{temporary_password}}.'
            self.mailer.welcome(self.user, 'private-password')
        self.assertEqual(send.call_args_list[0].args[2], 'Hello player, password=, legacy=.')
        self.assertIn('private-password', send.call_args_list[1].args[2])

    def test_password_alias_matches_legacy_token_in_second_email(self):
        with patch('panel_mail.PASSWORD_TEMPLATE') as template, patch.object(self.mailer, 'send') as send:
            template.read_text.return_value = '{{password}} / {{ password }} / {{temporary_password}}'
            self.assertEqual(self.mailer.welcome(self.user, 'Generated-password-123!'), 'sent')
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[1].args[2],
                         'Generated-password-123! / Generated-password-123! / Generated-password-123!')
        self.assertNotIn('Generated-password-123!', send.call_args_list[0].args[2])
        for call in send.call_args_list:
            self.assertNotIn('Generated-password-123!', call.args[1])
        self.delay.assert_called_once_with(12)

    def test_missing_password_template_fails_before_sending_welcome(self):
        with patch('panel_mail.PASSWORD_TEMPLATE') as template, patch.object(self.mailer, 'send') as send:
            template.read_text.side_effect = FileNotFoundError()
            with self.assertRaises(FileNotFoundError):
                self.mailer.welcome(self.user, 'private-password')
        send.assert_not_called()
        self.delay.assert_not_called()

    def test_server_name_and_product_brand_are_independent(self):
        with patch('panel_mail.WELCOME_TEMPLATE') as template, patch.object(self.mailer, 'send') as send:
            template.read_text.return_value = '{{servername}} / {{panel_name}}'
            self.mailer.welcome(self.user, 'Generated-password-123!')
        self.assertEqual(send.call_args_list[0].args[2], 'Byte Community / Byte Panel')


if __name__ == '__main__':
    unittest.main()
