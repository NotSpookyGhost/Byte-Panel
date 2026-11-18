"""SMTP delivery using the Python standard library; credentials never leave the server."""

from email.message import EmailMessage
from io import BytesIO
from contextlib import suppress
from datetime import datetime, timezone
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
import re
import smtplib
import socket
import ssl
import time
from time import sleep
from urllib.parse import urlsplit

from panel_accounts import validate_email
from email_designs import EmailTemplateError, load_email_design, render_email_text
from server_icon import server_icon_path, normalize_icon, ServerIconError


DEFAULT_SMTP = dict(enabled=False, notify_new_users=True, host='', port=587,
                    security='starttls', username='', password='', from_email='',
                    from_name='Byte Panel', panel_url='')

SMTP_TIMEOUT_SECONDS = 30
WELCOME_TEMPLATE = Path(__file__).resolve().parent / 'templates' / 'emails' / 'welcome.txt'
PASSWORD_TEMPLATE = WELCOME_TEMPLATE.with_name('temporary_password.txt')
EMAIL_DESIGNS = Path(__file__).resolve().parent.parent / 'templates'
WELCOME_DESIGN = EMAIL_DESIGNS / 'welcomeTemplate.json'
PASSWORD_DESIGN = EMAIL_DESIGNS / 'passwordTemplate.json'
PASSWORD_EMAIL_DELAY_SECONDS = 12
MAX_EMAIL_ICON_BYTES = 256 * 1024
SMTP_STAGES = {
    'connect': 'connecting and waiting for the SMTP greeting',
    'connect_tls': 'connecting with SSL/TLS and waiting for the SMTP greeting',
    'ehlo': 'introducing the panel to the SMTP server (EHLO)',
    'starttls': 'negotiating STARTTLS',
    'ehlo_tls': 'introducing the panel after STARTTLS (EHLO)',
    'authentication': 'authenticating the SMTP account',
    'send': 'submitting the message',
}


class PasswordEmailDeliveryError(Exception):
    """The welcome was accepted, but delivery of the second email is unconfirmed."""
    def __init__(self, error):
        super().__init__('Temporary password email delivery failed')
        self.error = error


class SMTPDeliveryError(Exception):
    """Carry private failure details internally; never put them in the exception text."""
    def __init__(self, error, stage, elapsed_seconds):
        super().__init__('SMTP delivery failed')
        self.error = error
        self.stage = stage
        self.elapsed_seconds = round(elapsed_seconds, 1)


def _read_timed_out(error):
    # smtplib.getreply() wraps socket read timeouts in SMTPServerDisconnected.
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, TimeoutError):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def smtp_failure(error):
    """Classify delivery errors without exposing provider replies or credentials."""
    stage = None
    elapsed_seconds = None
    if isinstance(error, SMTPDeliveryError):
        stage = error.stage if error.stage in SMTP_STAGES else None
        elapsed_seconds = error.elapsed_seconds
        error = error.error
    if isinstance(error, EmailTemplateError):
        code = 'email_template_invalid'
        message = 'The invitation email design could not be rendered. Check the JSON files in the build templates folder and rebuild the image. No message was submitted.'
    elif isinstance(error, smtplib.SMTPAuthenticationError):
        code = 'authentication_failed'
        message = 'SMTP authentication was rejected. Check the saved username and password/app password, and whether SMTP access is enabled for this mailbox.'
    elif isinstance(error, ssl.SSLCertVerificationError):
        code = 'certificate_failed'
        message = 'SMTP TLS certificate verification failed. Check the SMTP hostname, server certificate chain and container clock. Certificate verification remains enabled.'
    elif isinstance(error, ssl.SSLError):
        code = 'tls_failed'
        message = 'The SMTP TLS connection failed. Check that the port matches the selected STARTTLS or SSL/TLS mode.'
    elif isinstance(error, socket.gaierror):
        code = 'dns_failed'
        message = 'The container could not resolve the SMTP hostname. Check the hostname and container DNS settings.'
    elif isinstance(error, TimeoutError) or (isinstance(error, smtplib.SMTPServerDisconnected) and _read_timed_out(error)):
        code = 'connection_timeout'
        message = f'The SMTP operation timed out. The panel allows {SMTP_TIMEOUT_SECONDS} seconds per network operation.'
    elif isinstance(error, ConnectionRefusedError):
        code = 'connection_refused'
        message = 'The SMTP connection was refused. Check the hostname, port, mail service and firewall rules.'
    elif isinstance(error, smtplib.SMTPSenderRefused):
        code = 'sender_rejected'
        message = 'The SMTP server rejected the From address. Check the sender address and whether this account is allowed to send from it.'
    elif isinstance(error, smtplib.SMTPRecipientsRefused):
        code = 'recipient_rejected'
        message = 'The SMTP server rejected the test recipient. Check the recipient address and relay permissions.'
    elif isinstance(error, smtplib.SMTPDataError):
        code = 'message_rejected'
        message = 'The SMTP server rejected the message. Check the provider sending limits, sender verification and mail policy.'
    elif isinstance(error, smtplib.SMTPNotSupportedError):
        code = 'smtp_feature_unsupported'
        message = 'The SMTP server does not support a requested feature such as STARTTLS, authentication or international addresses. Check the server and selected settings.'
    elif isinstance(error, smtplib.SMTPServerDisconnected):
        code = 'server_disconnected'
        message = 'The SMTP connection was lost before the operation completed.'
    elif isinstance(error, smtplib.SMTPHeloError):
        code = 'greeting_rejected'
        message = 'The SMTP server rejected the panel greeting (EHLO/HELO).'
    elif isinstance(error, smtplib.SMTPResponseException):
        code = 'smtp_rejected'
        message = 'The SMTP server rejected the request. Check the server settings and provider mail policy.'
    elif isinstance(error, OSError):
        code = 'connection_failed'
        message = 'Could not communicate with the SMTP server. Check the host, port and container network access.'
    else:
        code = 'delivery_failed'
        message = 'The SMTP test could not complete. Check the saved settings and panel container logs.'
    # A numeric SMTP reply code is useful; the raw reply can contain private data.
    reply = getattr(error, 'smtp_code', None)
    if not isinstance(reply, int) or not 100 <= reply <= 599:
        reply = None
    if reply is not None:
        message += f' (SMTP {reply})'
    if stage:
        message += f' Failed while {SMTP_STAGES[stage]} (after {elapsed_seconds:.1f}s).'
        if stage != 'send':
            message += ' No message was submitted.'
    if code in ('connection_timeout', 'server_disconnected', 'connection_failed', 'delivery_failed') and stage in (None, 'send'):
        message += ' Delivery is unconfirmed; check the recipient inbox before retrying.'
    return dict(code=code, message=message, smtp_code=reply, stage=stage, elapsed_seconds=elapsed_seconds)


def _smtp_password(config):
    password = config['password']
    # Only normalize recognizable Google app passwords, including already-saved
    # ones. Whitespace can be significant for other providers/password formats.
    if config['host'].lower().rstrip('.') in ('smtp.gmail.com', 'smtp.googlemail.com'):
        compact = ''.join(password.split())
        if re.fullmatch(r'[a-z]{16}', compact):
            return compact
    return password


def _smtp_login(smtp, username, password):
    """Keep credential/policy rejections instead of hiding them with a second AUTH."""
    smtp.ehlo_or_helo_if_needed()
    if not smtp.has_extn('auth'):
        raise smtplib.SMTPNotSupportedError('SMTP authentication is not advertised.')
    advertised = smtp.esmtp_features.get('auth', '').upper().split()
    smtp.user, smtp.password = username, password
    last_error = None
    for mechanism in ('CRAM-MD5', 'PLAIN', 'LOGIN'):
        if mechanism not in advertised:
            continue
        callback = getattr(smtp, 'auth_' + mechanism.lower().replace('-', '_'), None)
        if callback is None:
            continue
        try:
            return smtp.auth(mechanism, callback)
        except smtplib.SMTPAuthenticationError as error:
            # Fall back only for an unsupported AUTH command/mechanism, never
            # after bad credentials, account policy, throttling or shutdown.
            if error.smtp_code not in (500, 501, 502, 504):
                raise
            last_error = error
    if last_error is not None:
        raise last_error
    raise smtplib.SMTPNotSupportedError('No supported SMTP authentication method is advertised.')


def _smtp_ehlo(smtp):
    code, reply = smtp.ehlo()
    if code != 250:
        raise smtplib.SMTPHeloError(code, reply)


class PanelMailer:
    def __init__(self, store, load_panel_config=None, server_dir=None):
        self.store = store
        self.load_panel_config = load_panel_config or (lambda: {})
        self.server_dir = server_dir

    def server_icon_bytes(self):
        """An optional, bounded copy of the active icon, without imported metadata."""
        if self.server_dir is None:
            return None
        try:
            path = server_icon_path(self.server_dir)
            if path is None:
                return None
            with path.open('rb') as stream:
                raw = stream.read(MAX_EMAIL_ICON_BYTES + 1)
            if len(raw) > MAX_EMAIL_ICON_BYTES or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                return None
            return normalize_icon(BytesIO(raw))
        except (OSError, ServerIconError):
            # Missing, unreadable or malformed optional artwork must not block invites.
            return None

    def panel_name(self):
        return 'Byte Panel'

    def server_name(self):
        # Read for every invitation; server labels are independent of product branding.
        name = self.load_panel_config().get('server_name', '')
        if not isinstance(name, str):
            name = ''
        return re.sub(r'[\x00-\x1f\x7f]', ' ', name).strip() or 'Byte Panel'

    def config(self, public=False):
        config = {**DEFAULT_SMTP, **self.store.get_setting('smtp', {})}
        if public:
            config['password_set'] = bool(config.pop('password'))
        return config

    def save(self, data):
        config = self.config()
        for key in ('enabled', 'notify_new_users'):
            if not isinstance(data.get(key), bool):
                raise ValueError('Select whether SMTP and welcome notifications are enabled.')
            config[key] = data[key]
        for key in ('host', 'username', 'from_name', 'panel_url'):
            value = data.get(key, '')
            if not isinstance(value, str):
                raise ValueError('SMTP fields must be text.')
            value = value.strip()
            if len(value) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('SMTP fields cannot contain control characters or exceed 2048 characters.')
            config[key] = value
        config['from_email'] = validate_email(data.get('from_email', ''), required=config['enabled'])
        try:
            config['port'] = int(data.get('port', 587))
        except (TypeError, ValueError):
            raise ValueError('SMTP port must be between 1 and 65535.')
        if not 1 <= config['port'] <= 65535:
            raise ValueError('SMTP port must be between 1 and 65535.')
        config['security'] = data.get('security', 'starttls')
        if config['security'] not in ('starttls', 'ssl', 'none'):
            raise ValueError('Choose STARTTLS, SSL/TLS or a local unencrypted relay.')
        if data.get('clear_password'):
            config['password'] = ''
        password = data.get('password', '')
        if not isinstance(password, str) or len(password) > 4096:
            raise ValueError('SMTP password is too long.')
        if password:
            config['password'] = password
        if config['enabled']:
            if not config['host'] or any(c.isspace() for c in config['host']) or '://' in config['host']:
                raise ValueError('Enter an SMTP hostname, without a URL scheme.')
            if config['security'] == 'none' and (config['username'] or config['password']):
                raise ValueError('Use TLS when sending SMTP credentials. Unencrypted mode is only for a trusted relay without authentication.')
            if config['username'] and not config['password']:
                raise ValueError('Enter the SMTP password or app password.')
            if not config['panel_url']:
                raise ValueError('Enter the public panel URL for welcome emails.')
        if config['panel_url']:
            parsed = urlsplit(config['panel_url'])
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Use an http(s) panel URL without credentials, a query or a fragment.')
        self.store.set_setting('smtp', config)
        return self.config(public=True)

    def send(self, recipient, subject, text, *, html=None, inline_images=None):
        config = self.config()
        if not config['enabled']:
            raise ValueError('Enable and save SMTP settings first.')
        recipient = validate_email(recipient, required=True)
        message = EmailMessage()
        message['From'] = formataddr((config['from_name'], config['from_email']))
        message['To'] = recipient
        message['Subject'] = subject
        message['Date'] = formatdate(usegmt=True)
        message['Message-ID'] = make_msgid(domain=config['from_email'].rsplit('@', 1)[-1])
        message.set_content(text)
        if html is not None:
            message.add_alternative(html, subtype='html')
        if inline_images:
            if html is None:
                raise ValueError('Inline email images require an HTML message.')
            html_part = message.get_body(preferencelist=('html',))
            for cid, data in inline_images.items():
                if (not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9._@-]{1,200}', cid) or
                        not isinstance(data, bytes) or len(data) > MAX_EMAIL_ICON_BYTES or
                        not data.startswith(b'\x89PNG\r\n\x1a\n')):
                    raise ValueError('Invalid inline email image.')
                html_part.add_related(data, maintype='image', subtype='png', cid=f'<{cid}>',
                                      disposition='inline', filename='server-icon.png')
        smtp = None
        accepted = False
        started = time.monotonic()
        stage = 'connect_tls' if config['security'] == 'ssl' else 'connect'
        try:
            context = ssl.create_default_context()
            if config['security'] == 'ssl':
                smtp = smtplib.SMTP_SSL(config['host'], config['port'], timeout=SMTP_TIMEOUT_SECONDS, context=context)
            else:
                smtp = smtplib.SMTP(config['host'], config['port'], timeout=SMTP_TIMEOUT_SECONDS)
            stage = 'ehlo'
            if config['security'] == 'none':
                # Preserve support for trusted legacy relays that only offer HELO.
                smtp.ehlo_or_helo_if_needed()
            else:
                _smtp_ehlo(smtp)
            if config['security'] == 'starttls':
                stage = 'starttls'
                smtp.starttls(context=context)
                stage = 'ehlo_tls'
                _smtp_ehlo(smtp)
            if config['username']:
                if config['security'] == 'none':
                    raise ValueError('SMTP authentication requires TLS.')
                stage = 'authentication'
                _smtp_login(smtp, config['username'], _smtp_password(config))
            stage = 'send'
            smtp.send_message(message)
            accepted = True
        except Exception as error:
            raise SMTPDeliveryError(error, stage, time.monotonic() - started) from None
        finally:
            if smtp is not None:
                # A QUIT error cannot undo an accepted message. On failure, close
                # immediately so cleanup cannot hide the original SMTP response.
                if accepted:
                    with suppress(Exception):
                        if smtp.sock is not None:
                            smtp.sock.settimeout(3)
                        smtp.quit()
                with suppress(Exception):
                    smtp.close()

    def welcome(self, user, temporary_password):
        config = self.config()
        if not config['enabled'] or not config['notify_new_users'] or not user['email']:
            return 'not_requested'
        if not self.store.invitation_is_current(user):
            return 'cancelled'
        expires = datetime.fromtimestamp(user['temporary_password_expires'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        name = self.server_name()
        icon = self.server_icon_bytes()
        icon_cid = make_msgid(domain='byte-panel.local')[1:-1] if icon else ''
        values = dict(servername=name, panel_name=self.panel_name(), username=user['username'],
                      login_url=config['panel_url'].rstrip('/') + '/login',
                      temporary_password='', password='', expires=expires,
                      server_icon='cid:' + icon_cid if icon else '')
        subject = render_email_text('Welcome to {{servername}} - your panel sign-in details', values)
        # Never expose the password to the welcome template, including customized copies.
        # Render both templates first so a missing file cannot leave half an invitation.
        body = render_email_text(WELCOME_TEMPLATE.read_text(encoding='utf-8'), {**values, 'server_icon': ''})
        welcome_html = load_email_design(WELCOME_DESIGN, values)
        password_subject = render_email_text('Your temporary password for {{servername}}', values)
        password_values = {**values, 'temporary_password': temporary_password,
                           'password': temporary_password, 'server_icon': ''}
        password_body = render_email_text(PASSWORD_TEMPLATE.read_text(encoding='utf-8'),
                                          password_values)
        password_html = load_email_design(PASSWORD_DESIGN, password_values)
        # Do not attach unused artwork when a customized design omits the icon block.
        inline_images = {icon_cid: icon} if icon and f'src="cid:{icon_cid}"' in welcome_html else None
        self.send(user['email'], subject, body, html=welcome_html, inline_images=inline_images)
        # Keep the secret only in this request's memory, never in a persistent queue.
        # The threaded server continues serving other requests during this short wait.
        try:
            sleep(PASSWORD_EMAIL_DELAY_SECONDS)
            config = self.config()
            if not config['enabled'] or not config['notify_new_users'] or not self.store.invitation_is_current(user):
                return 'cancelled'
            self.send(user['email'], password_subject, password_body, html=password_html)
        except Exception as error:
            raise PasswordEmailDeliveryError(error) from None
        return 'sent'
