# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Credential retries validate each entry without leaking secrets or bypassing checks."""

from contextlib import redirect_stdout
import io
import subprocess
import unittest
from unittest.mock import patch

from cisetup import config, github_api, host, install, xcode_cache
from cisetup.util import SetupError


class CredentialRetryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config(repo_root=host.ROOT, owner='example', scope='org')
        self.output = io.StringIO()
        self.capture = redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_new_pat_is_checked_and_rejected_entry_can_be_replaced(self):
        missing = subprocess.CompletedProcess([], 44, '', '')
        entered = subprocess.CompletedProcess([], 0)
        checked = []

        def validate(cfg):
            checked.append(cfg.pat)
            if cfg.pat == 'synthetic-invalid':
                raise github_api.GitHubAPIError('synthetic-private-response', 401)
            return 'synthetic-registration-token'

        with patch.object(config, 'load', return_value=self.cfg), \
                patch.object(host.subprocess, 'run', side_effect=[missing, entered, entered]) as run, \
                patch.object(host, 'command', side_effect=['synthetic-invalid', 'synthetic-valid']), \
                patch.object(github_api, 'registration_token', side_effect=validate):
            result = host.runner_credentials(interactive=True)
        self.assertEqual(checked, ['synthetic-invalid', 'synthetic-valid'])
        self.assertEqual(result.pat, 'synthetic-valid')
        self.assertEqual(run.call_count, 3)
        self.assertIn('verified', self.output.getvalue())
        self.assertNotIn('synthetic-', self.output.getvalue() + str(run.call_args_list))

    def test_rejected_saved_pat_has_bounded_retries(self):
        stored = subprocess.CompletedProcess([], 0, 'synthetic-invalid', '')
        entered = subprocess.CompletedProcess([], 0)
        with patch.object(config, 'load', return_value=self.cfg), \
                patch.object(host.subprocess, 'run', side_effect=[stored, entered, entered]) as run, \
                patch.object(host, 'command', return_value='synthetic-invalid'), \
                patch.object(github_api, 'registration_token', side_effect=github_api.GitHubAPIError('synthetic-private', 401)) as check:
            with self.assertRaisesRegex(host.HostError, 'HTTP 401'):
                host.runner_credentials(interactive=True)
        self.assertEqual(check.call_count, 3)
        self.assertEqual(run.call_count, 3)
        self.assertIsNone(self.cfg.pat)
        self.assertNotIn('synthetic-', self.output.getvalue())

    def test_permissions_network_and_noninteractive_rejection_do_not_replace_pat(self):
        stored = subprocess.CompletedProcess([], 0, 'synthetic-valid', '')
        for error, interactive in [(github_api.GitHubAPIError('synthetic-private', 403), True),
                                   (SetupError('synthetic-network-detail'), True),
                                   (github_api.GitHubAPIError('synthetic-private', 401), False)]:
            with self.subTest(error=type(error).__name__, interactive=interactive), \
                    patch.object(config, 'load', return_value=self.cfg), \
                    patch.object(host.subprocess, 'run', return_value=stored) as run, \
                    patch.object(host, 'command') as replace, \
                    patch.object(github_api, 'registration_token', side_effect=error):
                with self.assertRaises(host.HostError) as raised:
                    host.runner_credentials(interactive=interactive)
                self.assertNotIn('synthetic-', str(raised.exception))
                self.assertEqual(run.call_count, 1)
                replace.assert_not_called()

    def test_cancelled_pat_reentry_does_not_continue_verification(self):
        stored = subprocess.CompletedProcess([], 0, 'synthetic-invalid', '')
        cancelled = subprocess.CompletedProcess([], 130)
        with patch.object(config, 'load', return_value=self.cfg), \
                patch.object(host.subprocess, 'run', side_effect=[stored, cancelled]), \
                patch.object(host, 'command') as lookup, \
                patch.object(github_api, 'registration_token', side_effect=github_api.GitHubAPIError('denied', 401)) as check:
            with self.assertRaisesRegex(host.HostError, 'did not complete'):
                host.runner_credentials(interactive=True)
        self.assertEqual(check.call_count, 1)
        lookup.assert_not_called()

    def test_valid_apple_session_does_not_prompt(self):
        with patch.object(xcode_cache, 'command', return_value='catalog') as check, \
                patch.object(xcode_cache, 'login') as login, patch('builtins.input') as prompt:
            install.apple_access(interactive=True)
        check.assert_called_once_with(['update', '--data-source', 'apple'], timeout=120)
        login.assert_not_called()
        prompt.assert_not_called()

    def test_failed_apple_signin_retries_and_verifies_before_returning(self):
        events = []

        def check(*args, **kwargs):
            events.append('check')
            if len(events) == 1:
                raise host.HostError('synthetic-private-session')

        def login():
            events.append('login')
            if events.count('login') == 1:
                raise host.HostError('synthetic-private-login')

        with patch.object(xcode_cache, 'command', side_effect=check), \
                patch.object(xcode_cache, 'login', side_effect=login), \
                patch('builtins.input', side_effect=['y', 'y']):
            install.apple_access(interactive=True)
        self.assertEqual(events, ['check', 'login', 'login', 'check'])
        self.assertNotIn('synthetic-', self.output.getvalue())

    def test_successful_login_with_failed_noninteractive_check_does_not_request_password_again(self):
        with patch.object(xcode_cache, 'command', side_effect=host.HostError('synthetic-private')) as check, \
                patch.object(xcode_cache, 'login') as login, \
                patch('builtins.input', return_value='y') as prompt:
            with self.assertRaisesRegex(host.HostError, 'immediate non-interactive') as raised:
                install.apple_access(interactive=True)
        self.assertEqual(check.call_count, 2)
        login.assert_called_once()
        prompt.assert_called_once()
        self.assertNotIn('synthetic-', str(raised.exception) + self.output.getvalue())

    def test_failed_apple_signin_has_three_attempt_limit(self):
        with patch.object(xcode_cache, 'command', side_effect=host.HostError('unavailable')) as check, \
                patch.object(xcode_cache, 'login', side_effect=host.HostError('failed')) as login, \
                patch('builtins.input', return_value='y'):
            with self.assertRaisesRegex(host.HostError, 'three attempts'):
                install.apple_access(interactive=True)
        self.assertEqual(login.call_count, 3)
        self.assertEqual(check.call_count, 1)

    def test_apple_retry_can_be_declined(self):
        with patch.object(xcode_cache, 'command', side_effect=host.HostError('unavailable')), \
                patch.object(xcode_cache, 'login') as login, patch('builtins.input', return_value='n'):
            with self.assertRaises(install.SetupCancelled):
                install.apple_access(interactive=True)
        login.assert_not_called()

    def test_background_apple_check_never_prompts(self):
        with patch.object(xcode_cache, 'command', side_effect=host.HostError('unavailable')), \
                patch.object(xcode_cache, 'login') as login, patch('builtins.input') as prompt:
            with self.assertRaises(host.HostError):
                install.apple_access()
        login.assert_not_called()
        prompt.assert_not_called()

    def test_apple_service_failure_never_requests_credentials(self):
        with patch.object(xcode_cache, 'command', side_effect=xcode_cache.AppleAccessUnavailable('service unavailable')), \
                patch.object(xcode_cache, 'login') as login, patch('builtins.input') as prompt:
            with self.assertRaises(xcode_cache.AppleAccessUnavailable):
                install.apple_access(interactive=True)
        login.assert_not_called()
        prompt.assert_not_called()

    def test_captured_service_errors_export_only_fixed_diagnostics(self):
        for status in (429, 500, 503):
            raw = f'NetworkError.non200StatusCode(statusCode: {status}, data: synthetic-private-session)'
            result = subprocess.CompletedProcess([], 1, raw, 'synthetic-private-stderr')
            with self.subTest(status=status), patch.object(xcode_cache, 'cli', return_value='/fake/xcodes'), \
                    patch.object(xcode_cache.subprocess, 'run', return_value=result):
                with self.assertRaises(xcode_cache.AppleAccessUnavailable) as raised:
                    xcode_cache.command(['update', '--data-source', 'apple'])
                self.assertNotIn('synthetic-', str(raised.exception))

    def test_unknown_and_authentication_errors_are_not_misclassified_as_service_outage(self):
        for raw in ('synthetic-private-unknown', 'NetworkError.non200StatusCode(statusCode: 401, data: nil)'):
            result = subprocess.CompletedProcess([], 1, raw, '')
            with self.subTest(raw=raw), patch.object(xcode_cache, 'cli', return_value='/fake/xcodes'), \
                    patch.object(xcode_cache.subprocess, 'run', return_value=result):
                with self.assertRaises(host.HostError) as raised:
                    xcode_cache.command(['update'])
                self.assertNotIsInstance(raised.exception, xcode_cache.AppleAccessUnavailable)
                self.assertNotIn('synthetic-', str(raised.exception))

    def test_timeouts_and_missing_tool_do_not_request_credentials(self):
        for error in (subprocess.TimeoutExpired(['/fake/xcodes'], 1, output='synthetic-private'),
                      OSError('synthetic-private')):
            with self.subTest(error=type(error).__name__), patch.object(xcode_cache, 'cli', return_value='/fake/xcodes'), \
                    patch.object(xcode_cache.subprocess, 'run', side_effect=error), \
                    patch.object(xcode_cache, 'login') as login, patch('builtins.input') as prompt:
                with self.assertRaises(xcode_cache.AppleAccessUnavailable) as raised:
                    install.apple_access(interactive=True)
                self.assertNotIn('synthetic-', str(raised.exception))
                login.assert_not_called()
                prompt.assert_not_called()
