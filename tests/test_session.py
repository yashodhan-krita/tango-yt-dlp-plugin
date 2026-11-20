"""Offline tests for the tango extractor's visitor-session handling.

Run with the interpreter that ships with yt-dlp so ``yt_dlp`` is importable::

    PYTHONPATH=. /opt/homebrew/Cellar/yt-dlp/*/libexec/bin/python \
        -m unittest discover -s tests
"""

import base64
import http.cookiejar
import json
import time
import types
import unittest
from unittest import mock

from yt_dlp_plugins.extractor.tango import TangoIE, TangoTokenRefresher


def _cookie(domain, name, value='x'):
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=domain.startswith('.'),
        domain_initial_dot=domain.startswith('.'), path='/',
        path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False)


def _jwt(exp):
    def enc(part):
        raw = json.dumps(part).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return f'{enc({"typ": "JWT"})}.{enc({"exp": exp})}.sig'


def _extractor_with(*cookies):
    jar = http.cookiejar.CookieJar()
    for cookie in cookies:
        jar.set_cookie(cookie)
    ie = TangoIE.__new__(TangoIE)          # no downloader needed
    ie._downloader = types.SimpleNamespace(cookiejar=jar)
    return ie, jar


class VisitorResetTest(unittest.TestCase):
    def test_reset_forgets_the_visitor_but_keeps_the_account(self):
        ie, jar = _extractor_with(
            _cookie('www.tango.me', 'Tango-VT'),
            _cookie('www.tango.me', 'Tango-SD'),
            _cookie('www.tango.me', 'Tango-DeviceId'),
            _cookie('.www.tango.me', 'Tango-SD'),    # account session
            _cookie('.www.tango.me', 'Tango-ST'),
            _cookie('www.tango.me', 'datadome'),
        )
        ie._reset_visitor()
        self.assertEqual(sorted((c.domain, c.name) for c in jar), [
            ('.www.tango.me', 'Tango-SD'),
            ('.www.tango.me', 'Tango-ST'),
            ('www.tango.me', 'datadome'),
        ])


class HasVisitorTokenTest(unittest.TestCase):
    def test_expired_token_needs_a_check_in(self):
        ie, _ = _extractor_with(
            _cookie('www.tango.me', 'Tango-VT', _jwt(time.time() - 60)))
        self.assertFalse(ie._has_visitor_token())

    def test_valid_token_skips_the_check_in(self):
        ie, _ = _extractor_with(
            _cookie('www.tango.me', 'Tango-VT', _jwt(time.time() + 3600)))
        self.assertTrue(ie._has_visitor_token())

    def test_missing_token_needs_a_check_in(self):
        ie, _ = _extractor_with()
        self.assertFalse(ie._has_visitor_token())

    def test_unparseable_token_is_treated_as_usable(self):
        ie, _ = _extractor_with(_cookie('www.tango.me', 'Tango-VT', 'opaque'))
        self.assertTrue(ie._has_visitor_token())


class TokenRefreshTest(unittest.TestCase):
    def test_a_refresh_does_not_print_a_progress_line(self):
        ie = mock.Mock()
        ie._issue_token.return_value = {'token': 't', 'username': 'u', 'expireAt': 2 ** 31}
        TangoTokenRefresher(ie, 'v').refresh()
        self.assertIsNone(ie._issue_token.call_args.kwargs['note'])
        ie.write_debug.assert_called_once()


if __name__ == '__main__':
    unittest.main()
