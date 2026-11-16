"""Render the EmailBuilder JSON blocks used by the bundled invitation designs.

No JavaScript runtime, remote renderer, raw HTML or template evaluation is used.
"""
from html import escape
import json
import math
import re
from urllib.parse import urlsplit


class EmailTemplateError(ValueError):
    """Invalid/missing design; messages never include template content or secrets."""


FONTS = {
    'MODERN_SANS': '"Helvetica Neue", "Arial Nova", "Nimbus Sans", Arial, sans-serif',
    'BOOK_SANS': 'Optima, Candara, "Noto Sans", sans-serif',
    'ORGANIC_SANS': 'Seravek, "Gill Sans Nova", Ubuntu, Calibri, sans-serif',
    'GEOMETRIC_SANS': 'Avenir, Montserrat, Corbel, sans-serif',
    'HEAVY_SANS': 'Bahnschrift, "DIN Alternate", "Franklin Gothic Medium", sans-serif',
    'ROUNDED_SANS': 'ui-rounded, "Arial Rounded MT Bold", Calibri, sans-serif',
    'MODERN_SERIF': 'Charter, "Bitstream Charter", "Sitka Text", Cambria, serif',
    'BOOK_SERIF': '"Iowan Old Style", "Palatino Linotype", "URW Palladio L", serif',
    'MONOSPACE': '"Nimbus Mono PS", "Courier New", "Cutive Mono", monospace',
}


def render_email_text(template, values):
    """Replace named tokens once, without evaluating expressions."""
    return re.sub(r'\{\{\s*([a-z_]+)\s*\}\}',
                  lambda match: str(values[match[1]]) if match[1] in values else match[0],
                  template)


def _mapping(value):
    if not isinstance(value, dict):
        raise EmailTemplateError('Email template requires an object.')
    return value


def _number(value):
    if type(value) not in (int, float) or not 0 <= value <= 2000 or not math.isfinite(value):
        raise EmailTemplateError('Invalid email template dimension.')
    return f'{value:g}'


def _color(value):
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        raise EmailTemplateError('Invalid email template color.')
    return value


def _choice(value, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise EmailTemplateError('Unsupported email template style.')
    return value


def _style(style):
    result = {}
    for key, css in (('color', 'color'), ('backgroundColor', 'background-color')):
        if style.get(key) is not None:
            result[css] = _color(style[key])
    if style.get('borderColor') is not None:
        result['border'] = '1px solid ' + _color(style['borderColor'])
    for key, css in (('fontSize', 'font-size'), ('borderRadius', 'border-radius')):
        if style.get(key) is not None:
            result[css] = _number(style[key]) + 'px'
    if style.get('fontFamily') is not None:
        result['font-family'] = FONTS[_choice(style['fontFamily'], FONTS)]
    if style.get('fontWeight') is not None:
        result['font-weight'] = _choice(style['fontWeight'], ('normal', 'bold'))
    if style.get('textAlign') is not None:
        result['text-align'] = _choice(style['textAlign'], ('left', 'center', 'right'))
    if style.get('padding') is not None:
        padding = _mapping(style['padding'])
        result['padding'] = ' '.join(_number(padding.get(side, 0)) + 'px'
                                     for side in ('top', 'right', 'bottom', 'left'))
    return result


def _css(style):
    return escape(';'.join(f'{key}:{value}' for key, value in style.items()), quote=True)


def _text(value, values):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise EmailTemplateError('Email template text must be a string.')
    return escape(render_email_text(value, values), quote=True).replace('\n', '<br>')


def _url(value, values):
    if not isinstance(value, str) or not value.strip():
        raise EmailTemplateError('Email template link is missing.')
    # Dynamic URLs may only be the saved panel login URL. Never interpolate a
    # password (or user details) into remote image requests or tracking links.
    if '{{' in value:
        if not re.fullmatch(r'\{\{\s*login_url\s*\}\}', value.strip()):
            raise EmailTemplateError('Only the login_url token is allowed in email links.')
        value = str(values.get('login_url', ''))
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise EmailTemplateError('Invalid email template URL.')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                 and not parsed.username and not parsed.password)
        parsed.port  # Reject malformed ports as well.
    except ValueError:
        valid = False
    if not valid:
        raise EmailTemplateError('Email template links must use HTTP or HTTPS.')
    return escape(value, quote=True)


def render_email_design(document, values):
    """Return email-safe HTML for EmailLayout/Text/Heading/Image/Button/Container."""
    document = _mapping(document)
    if not 1 <= len(document) <= 1000:
        raise EmailTemplateError('Email template has too many blocks.')
    root = _mapping(document.get('root'))
    if root.get('type') != 'EmailLayout':
        raise EmailTemplateError('Email template requires an EmailLayout root.')
    layout = _mapping(root.get('data'))
    visits = 0

    def children(ids, path):
        if not isinstance(ids, list) or len(ids) > 1000:
            raise EmailTemplateError('Invalid email template children.')
        return ''.join(block(block_id, path) for block_id in ids)

    def block(block_id, path):
        nonlocal visits
        visits += 1
        if (visits > 1000 or len(path) > 30 or not isinstance(block_id, str)
                or block_id in path or block_id not in document):
            raise EmailTemplateError('Email template contains invalid block references.')
        node = _mapping(document[block_id])
        data = _mapping(node.get('data'))
        props = _mapping(data.get('props') or {})
        # Long temporary passwords must not force a wider canvas on phones.
        style = {'overflow-wrap': 'anywhere', 'word-break': 'break-word',
                 **_style(_mapping(data.get('style') or {}))}
        kind = node.get('type')
        if kind in ('Text', 'Heading'):
            if props.get('markdown'):
                raise EmailTemplateError('Markdown email blocks are not supported.')
            text = _text(props.get('text'), values)
            if kind == 'Heading':
                tag = _choice(props.get('level') or 'h2', ('h1', 'h2', 'h3'))
                style = {'margin': '0', 'font-weight': 'bold',
                         'font-size': {'h1': '32px', 'h2': '24px', 'h3': '20px'}[tag], **style}
                # Keep block padding/background on the cell for email clients.
                inner = {k: v for k, v in style.items() if k not in ('padding', 'background-color')}
                content = f'<{tag} style="{_css(inner)}">{text}</{tag}>'
            else:
                content = text
        elif kind == 'Container':
            content = children(props.get('childrenIds', []), path + (block_id,))
        elif kind == 'Image':
            src = _url(props.get('url'), values)
            alt = _text(props.get('alt', ''), values)
            image_style = {'display': 'inline-block', 'max-width': '100%', 'height': 'auto',
                           'border': '0', 'outline': 'none', 'text-decoration': 'none',
                           'vertical-align': _choice(props.get('contentAlignment') or 'middle',
                                                     ('top', 'middle', 'bottom'))}
            dimensions = ''
            for key in ('width', 'height'):
                if props.get(key) is not None:
                    number = _number(props[key])
                    dimensions += f' {key}="{number}"'
                    image_style[key] = number + 'px'
            content = f'<img src="{src}" alt="{alt}"{dimensions} style="{_css(image_style)}">'
            if props.get('linkHref'):
                content = f'<a href="{_url(props["linkHref"], values)}" target="_blank">{content}</a>'
        elif kind == 'Button':
            size = _choice(props.get('size') or 'medium', ('x-small', 'small', 'medium', 'large'))
            shape = _choice(props.get('buttonStyle') or 'rounded', ('rectangle', 'rounded', 'pill'))
            link_style = {'display': 'block' if props.get('fullWidth') else 'inline-block',
                          'box-sizing': 'border-box', 'text-decoration': 'none',
                          'font-size': style.get('font-size', '16px'),
                          'font-weight': style.get('font-weight', 'bold'),
                          'color': _color(props.get('buttonTextColor') or '#FFFFFF'),
                          'background-color': _color(props.get('buttonBackgroundColor') or '#999999'),
                          'border-radius': {'rectangle': '0', 'rounded': '4px', 'pill': '64px'}[shape],
                          'padding': {'x-small': '4px 8px', 'small': '8px 12px',
                                      'medium': '12px 20px', 'large': '16px 32px'}[size]}
            content = (f'<a href="{_url(props.get("url"), values)}" target="_blank" '
                       f'style="{_css(link_style)}">{_text(props.get("text"), values)}</a>')
        else:
            raise EmailTemplateError('Unsupported email template block type.')
        # Presentation tables and inline styles work without external stylesheets.
        return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'border="0" style="table-layout:fixed"><tr><td style="{_css(style)}">{content}</td></tr></table>')

    content = children(layout.get('childrenIds', []), ('root',))
    backdrop = _color(layout.get('backdropColor') or '#F5F5F5')
    canvas = _color(layout.get('canvasColor') or '#FFFFFF')
    base = {'background-color': canvas, 'color': _color(layout.get('textColor') or '#242424'),
            'font-family': FONTS[_choice(layout.get('fontFamily') or 'MODERN_SANS', FONTS)],
            'font-size': '16px', 'font-weight': 'normal', 'line-height': '1.5',
            'text-align': 'left', 'width': '100%', 'max-width': '600px', 'margin': '0 auto',
            'table-layout': 'fixed'}
    if layout.get('borderColor') is not None:
        base['border'] = '1px solid ' + _color(layout['borderColor'])
    if layout.get('borderRadius') is not None:
        base['border-radius'] = _number(layout['borderRadius']) + 'px'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'</head><body style="margin:0;padding:0;background-color:{backdrop}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'bgcolor="{backdrop}"><tr><td align="center" style="padding:32px 0">'
            '<!--[if mso]><table role="presentation" width="600"><tr><td><![endif]-->'
            f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
            f'bgcolor="{canvas}" style="{_css(base)}"><tr><td>{content}</td></tr></table>'
            '<!--[if mso]></td></tr></table><![endif]--></td></tr></table></body></html>')


def load_email_design(path, values):
    try:
        # Bound file size and support UTF-8 JSON exported with or without a BOM.
        with path.open('r', encoding='utf-8-sig') as stream:
            source = stream.read(1024 * 1024 + 1)
        if len(source) > 1024 * 1024:
            raise EmailTemplateError('Email template is too large.')
        return render_email_design(json.loads(source), values)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        # JSON decoding errors can contain source content; do not expose it.
        raise EmailTemplateError('Email design could not be loaded or rendered.') from None
