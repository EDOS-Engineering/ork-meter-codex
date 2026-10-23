"""Setup uses a disposable user profile; no browser, credential or model calls."""
import contextlib
import io
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from datetime import datetime, timezone

from ork_codex import onboarding as setup
from ork_codex import cli
from workspace_temp import WorkspaceTemp as TemporaryDirectory


FRAGMENT = (b'[otel]\nlog_user_prompt = false\nmetrics_exporter = "none"\ntrace_exporter = "none"\n'
            b'[otel.exporter.otlp-http]\nendpoint = "http://127.0.0.1:42345/v1/logs"\n'
            b'protocol = "json"\nheaders = { Authorization = "Bearer ' + b'a' * 64 + b'" }\n')


class Setup(unittest.TestCase):
    def test_new_connection_recovers_unusable_saved_pairing_through_cli(self):
        for scenario in ('expired-approval', 'revoked-credential', 'missing-local-record'):
            with self.subTest(scenario=scenario), TemporaryDirectory() as temp:
                home = Path(temp)
                before = b'model="chosen"\n'
                (home / 'config.toml').write_bytes(before)
                base = setup.private_directory(home / 'ork-meter-codex')
                saved = base / 'pairing.json'
                saved.write_text(json.dumps({'reference': 'old-ref', 'dev': True}))
                old_cache = setup.entitlement.path(home)
                old_cache.parent.mkdir(parents=True)
                old_cache.write_text(json.dumps({'optimize': True, 'state': 'active',
                                                 'until': '2099-01-01T00:00:00Z',
                                                 'checked_at': '2026-10-14T12:00:00Z'}))
                old_cache.chmod(0o600)
                new = {'reference': 'new-ref', 'fingerprint': 'new-fingerprint',
                       'approval_url': 'https://dev.ork-meter.edos.io/pair#proposal=new'}
                calls = []
                def result(argv):
                    calls.append(argv)
                    if argv[1:3] == ['pair', 'prepare']:
                        return new
                    if argv[1:3] == ['pair', 'show']:
                        if argv[-1] == 'new-ref':
                            return new
                        if scenario == 'missing-local-record':
                            raise ValueError('Local record unavailable')
                        old = dict(new, reference='old-ref', approval_url=new['approval_url'].replace('new', 'old'))
                        if scenario == 'revoked-credential':
                            old['acknowledged_until'] = '2099-01-01T00:00:00Z'
                        return old
                    if argv[1:3] == ['pair', 'verify'] and argv[-1] == 'old-ref':
                        raise ValueError('Credential refused')
                    return {}
                interrupted = True
                def terminal(argv):
                    calls.append(argv)
                    if argv[1:3] == ['pair', 'claim']:
                        return int(argv[-1] == 'old-ref' or interrupted)
                    self.assertIn(b'Bearer', (home / 'config.toml').read_bytes())
                    return 0
                with patch.dict(os.environ, {'CODEX_HOME': str(home)}), \
                     patch.object(setup, 'sys_terminal', return_value=True), \
                     patch.object(setup, 'binary', return_value='helper'), \
                     patch('ork_codex.meter.binary', return_value=str(home / 'missing-helper')), \
                     patch.object(setup, 'native_codex', return_value='native-codex'), \
                     patch.object(setup, 'install_plugin'), \
                     patch.object(setup, 'public_result', side_effect=result), \
                     patch.object(setup.subprocess, 'call', side_effect=terminal), \
                     patch.object(setup.webbrowser, 'open') as browser, \
                     patch.object(setup, 'Capture') as capture, \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    capture.return_value.ready.return_value = FRAGMENT
                    capture.return_value.fragment = home / 'private-fragment.toml'
                    with self.assertRaises(SystemExit) as failed:
                        cli.main(['setup'])
                    self.assertEqual(failed.exception.code, 2)
                    self.assertEqual(json.loads(saved.read_text())['reference'], 'old-ref')
                    self.assertFalse(any(argv[1:3] == ['pair', 'prepare'] for argv in calls))
                    with self.assertRaises(SystemExit) as interrupted_claim:
                        cli.main(['setup', '--new-connection'])
                    self.assertEqual(interrupted_claim.exception.code, 2)
                    self.assertEqual(json.loads(saved.read_text()), {'reference': 'new-ref', 'dev': True})
                    self.assertFalse(setup.entitlement.entitled(home), 'old Plus cache must be inactive')
                    capture.assert_not_called()
                    interrupted = False
                    with self.assertRaises(SystemExit) as complete:
                        cli.main(['setup'])
                    self.assertEqual(complete.exception.code, 0)
                    self.assertEqual([argv for argv in calls if argv[1:3] == ['pair', 'prepare']],
                                     [['helper', 'pair', 'prepare', '--source', 'openai', '--client', 'codex',
                                       '--origin', 'https://dev.ork-meter.edos.io']])
                    self.assertFalse(any(argv[1:3] == ['pair', 'forget'] for argv in calls))
                    capture.assert_called_once_with('helper', 'new-ref')
                    capture.return_value.stop.assert_called_once()
                    self.assertEqual(browser.call_args.args, (new['approval_url'],))
                    self.assertTrue(old_cache.exists())
                    self.assertEqual((home / 'config.toml').read_bytes(), before)
                    self.assertFalse((base / 'setup.lock').exists())

    def test_new_connection_preserves_saved_state_when_prepare_fails_or_session_is_active(self):
        for scenario in ('prepare-failure', 'wrong-origin', 'active-session', 'conflicting-reference'):
            with self.subTest(scenario=scenario), TemporaryDirectory() as temp:
                home = Path(temp)
                base = setup.private_directory(home / 'ork-meter-codex')
                saved = base / 'pairing.json'
                before = json.dumps({'reference': 'old-ref', 'dev': False})
                saved.write_text(before)
                lock = base / 'setup.lock'
                if scenario == 'active-session':
                    lock.write_text('existing session')
                with patch.dict(os.environ, {'CODEX_HOME': str(home)}), \
                     patch.object(setup, 'sys_terminal', return_value=True), \
                     patch.object(setup, 'binary', return_value='helper'), \
                     patch.object(setup, 'native_codex', return_value='native-codex'), \
                     patch.object(setup, 'install_plugin'), \
                     patch.object(setup, 'public_result') as result, \
                     patch.object(setup.webbrowser, 'open') as browser, \
                     patch.object(setup, 'Capture') as capture, \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result.side_effect = ValueError('Synthetic prepare failure') if scenario == 'prepare-failure' else None
                    result.return_value = {'reference': 'new-ref', 'fingerprint': 'new',
                                           'approval_url': 'https://dev.ork-meter.edos.io/pair#proposal=new'}
                    argv = ['setup', '--new-connection']
                    if scenario == 'conflicting-reference':
                        argv += ['--reference', 'old-ref']
                    with self.assertRaises(SystemExit) as failure:
                        cli.main(argv)
                    self.assertEqual(failure.exception.code, 2)
                    self.assertEqual(saved.read_text(), before)
                    if scenario == 'active-session':
                        self.assertEqual(lock.read_text(), 'existing session')
                    else:
                        self.assertFalse(lock.exists())
                    self.assertEqual(result.call_count, int(scenario in ('prepare-failure', 'wrong-origin')))
                    browser.assert_not_called()
                    capture.assert_not_called()

    def test_config_lease_preserves_original_bytes_and_unrelated_edits(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.toml'
            before = b'# preserve this\r\nmodel = "chosen"\r\n[permissions.example]\r\nnetwork = "deny"\r\n'
            config.write_bytes(before)
            lease = setup.ConfigLease(config, root)
            lease.install(FRAGMENT)
            self.assertEqual(lease.backup.read_bytes(), before)
            self.assertTrue(config.read_bytes().startswith(before))
            addition = b'\n[notice]\ncustom = true\n'
            config.write_bytes(config.read_bytes() + addition)
            self.assertTrue(lease.restore())
            self.assertEqual(config.read_bytes(), before + addition)
            self.assertNotIn(b'Bearer', config.read_bytes())

    def test_existing_telemetry_invalid_fragments_and_changed_config_are_not_overwritten(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.toml'
            for before in [b'[otel]\nexporter="none"\n', b'otel={exporter="none"}\n',
                           b'[profiles.custom.otel]\nexporter="none"\n', b'invalid(:']:
                config.write_bytes(before)
                with self.assertRaises(ValueError):
                    setup.ConfigLease(config, root)
                self.assertEqual(config.read_bytes(), before)
            config.write_bytes(b'model="chosen"\n')
            for bad in [FRAGMENT.replace(b'127.0.0.1', b'example.com'),
                        FRAGMENT.replace(b'false', b'true'), FRAGMENT + b'\n[model_providers.bad]\nsecret="PRIVATE"\n']:
                lease = setup.ConfigLease(config, root)
                with self.assertRaises(ValueError):
                    lease.install(bad)
                self.assertEqual(config.read_bytes(), b'model="chosen"\n')
            lease = setup.ConfigLease(config, root)
            config.write_bytes(b'model="edited"\n')
            with self.assertRaises(ValueError):
                lease.install(FRAGMENT)
            self.assertEqual(config.read_bytes(), b'model="edited"\n')

    def test_failed_publish_and_modified_owned_block_never_blindly_restore(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.toml'
            config.write_bytes(b'model="chosen"\n')
            lease = setup.ConfigLease(config, root)
            with patch.object(setup.os, 'replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    lease.install(FRAGMENT)
            self.assertEqual(config.read_bytes(), b'model="chosen"\n')
            self.assertTrue(lease.restore())
            lease.backup.unlink()
            lease = setup.ConfigLease(config, root)
            lease.install(FRAGMENT)
            changed = config.read_bytes().replace(b'42345', b'42346')
            config.write_bytes(changed)
            self.assertFalse(lease.restore())
            self.assertEqual(config.read_bytes(), changed)
            self.assertEqual(lease.backup.read_bytes(), b'model="chosen"\n')

    def test_setup_automates_handoff_session_cleanup_and_resumes_same_pairing(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            before = b'model="chosen"\n'
            (home/'config.toml').write_bytes(before)
            pairing = {'reference':'public-ref','fingerprint':'public-fingerprint',
                       'approval_url':'https://orkmeter.com/pair#proposal=public'}
            calls = []
            def result(argv):
                calls.append(argv)
                if argv[1:3] == ['pair','prepare']:
                    return pairing
                if argv[1:3] == ['pair','show']:
                    return {**pairing,'acknowledged_until':'2099-01-01T00:00:00Z'}
                return {}
            def terminal(argv):
                calls.append(argv)
                if argv == ['native-codex']:
                    self.assertIn(b'Bearer', (home/'config.toml').read_bytes())
                return 0
            out = io.StringIO()
            with patch.dict(os.environ, {'CODEX_HOME':str(home)}), \
                 patch.object(setup,'sys_terminal',return_value=True), \
                 patch.object(setup,'binary',return_value='helper'), \
                 patch('ork_codex.meter.binary', return_value=str(home/'missing-helper')), \
                 patch.object(setup,'native_codex',return_value='native-codex'), \
                 patch.object(setup,'install_plugin') as install, \
                 patch.object(setup,'public_result',side_effect=result), \
                 patch.object(setup.subprocess,'call',side_effect=terminal), \
                 patch.object(setup.webbrowser,'open') as browser, \
                 patch.object(setup,'Capture') as capture, contextlib.redirect_stdout(out):
                capture.return_value.ready.return_value = FRAGMENT
                capture.return_value.fragment = home/'private-fragment.toml'
                self.assertEqual(setup.setup(),0)
                self.assertEqual((home/'config.toml').read_bytes(),before)
                self.assertFalse((home/'ork-meter-codex/setup.lock').exists())
                self.assertEqual(json.loads((home/'ork-meter-codex/pairing.json').read_text()),
                                 {'reference':'public-ref','dev':False})
                browser.assert_called_once_with(pairing['approval_url'])
                self.assertIn(['helper','pair','claim','--reference','public-ref'],calls)
                self.assertNotIn('aaaaaa',out.getvalue())
                self.assertIn('could not verify current Plus access', out.getvalue())
                self.assertNotIn('Upgrade at', out.getvalue())
                cache = setup.entitlement.path(home)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({'optimize': False, 'state': 'free', 'until': None,
                                             'checked_at': datetime.now(timezone.utc).isoformat()}))
                cache.chmod(0o600)
                out.seek(0)
                out.truncate()
                self.assertEqual(setup.setup(),0)
                self.assertIn('Upgrade at', out.getvalue())
                self.assertEqual(len(json.loads((home/'hooks.json').read_text())['hooks']['PostToolUse']),1)
                self.assertEqual(browser.call_count,1)
                self.assertEqual(sum(x[1:3]==['pair','prepare'] for x in calls),1)
                self.assertEqual(capture.return_value.stop.call_count,2)
                self.assertEqual(install.call_count,2)

    def test_expired_saved_pairing_restarts_approval_and_resumes_interrupted_claim(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            before = b'model="chosen"\n'
            (home / 'config.toml').write_bytes(before)
            base = setup.private_directory(home / 'ork-meter-codex')
            saved = base / 'pairing.json'
            saved.write_text(json.dumps({'reference': 'expired-ref', 'dev': False}))
            old_cache = setup.entitlement.path(home)
            old_cache.parent.mkdir(parents=True)
            old_cache.write_text(json.dumps({'optimize': True, 'state': 'active',
                                             'until': '2099-01-01T00:00:00Z',
                                             'checked_at': '2026-10-14T12:00:00Z'}))
            old_cache.chmod(0o600)
            old = {'reference': 'expired-ref', 'acknowledged_until': '2020-01-01T00:00:00Z',
                   'approval_url': 'https://orkmeter.com/pair#proposal=expired', 'fingerprint': 'old'}
            new = {'reference': 'renewed-ref', 'approval_url': 'https://orkmeter.com/pair#proposal=new',
                   'fingerprint': 'new'}
            calls = []
            def result(argv):
                calls.append(argv)
                if argv[1:3] == ['pair', 'prepare']:
                    return new
                if argv[1:3] == ['pair', 'show']:
                    return old if argv[-1] == 'expired-ref' else new
                if argv[1:3] == ['pair', 'verify'] and argv[-1] == 'expired-ref':
                    raise ValueError('Expired credential refused')
                return {}
            fail_claim = True
            def terminal(argv):
                calls.append(argv)
                return 1 if argv[1:3] == ['pair', 'claim'] and fail_claim else 0
            with patch.dict(os.environ, {'CODEX_HOME': str(home)}), \
                 patch.object(setup, 'sys_terminal', return_value=True), \
                 patch.object(setup, 'binary', return_value='helper'), \
                 patch('ork_codex.meter.binary', return_value=str(home / 'missing-helper')), \
                 patch.object(setup, 'native_codex', return_value='native-codex'), \
                 patch.object(setup, 'install_plugin'), \
                 patch.object(setup, 'public_result', side_effect=result), \
                 patch.object(setup.subprocess, 'call', side_effect=terminal), \
                 patch.object(setup.webbrowser, 'open') as browser, \
                 patch.object(setup, 'Capture') as capture, contextlib.redirect_stdout(io.StringIO()):
                capture.return_value.ready.return_value = FRAGMENT
                capture.return_value.fragment = home / 'private-fragment.toml'
                with self.assertRaisesRegex(ValueError, 'resume reference renewed-ref'):
                    setup.setup()
                self.assertEqual(json.loads(saved.read_text())['reference'], 'renewed-ref')
                self.assertFalse(setup.entitlement.entitled(home), 'old Plus cache must be inactive')
                capture.assert_not_called()
                self.assertEqual((home / 'config.toml').read_bytes(), before)
                self.assertFalse((base / 'setup.lock').exists())
                fail_claim = False
                self.assertEqual(setup.setup(), 0)
                self.assertEqual(sum(argv[1:3] == ['pair', 'prepare'] for argv in calls), 1)
                self.assertNotIn(['helper', 'pair', 'verify', '--reference', 'expired-ref'], calls)
                self.assertNotIn(['helper', 'pair', 'forget', '--reference', 'expired-ref'], calls)
                capture.assert_called_once_with('helper', 'renewed-ref')
                self.assertEqual(browser.call_count, 2)
                self.assertTrue(old_cache.exists(), 'old local evidence is preserved')
                self.assertEqual((home / 'config.toml').read_bytes(), before)

    def test_expired_pairing_recovery_preserves_state_on_uncertain_or_explicit_input(self):
        for scenario in ('invalid-expiry', 'prepare-failure', 'explicit-reference'):
            with self.subTest(scenario=scenario), TemporaryDirectory() as temp:
                home = Path(temp)
                base = setup.private_directory(home / 'ork-meter-codex')
                saved = base / 'pairing.json'
                before = json.dumps({'reference': 'old-ref', 'dev': False})
                saved.write_text(before)
                calls = []
                def result(argv):
                    calls.append(argv)
                    if argv[1:3] == ['pair', 'prepare']:
                        raise ValueError('Synthetic prepare failure')
                    return {'reference': 'old-ref', 'approval_url': 'https://orkmeter.com/pair#proposal=old',
                            'acknowledged_until': 'invalid' if scenario == 'invalid-expiry' else '2020-01-01T00:00:00Z'}
                with patch.dict(os.environ, {'CODEX_HOME': str(home)}), \
                     patch.object(setup, 'sys_terminal', return_value=True), \
                     patch.object(setup, 'binary', return_value='helper'), \
                     patch.object(setup, 'native_codex', return_value='native-codex'), \
                     patch.object(setup, 'install_plugin'), \
                     patch.object(setup, 'public_result', side_effect=result), \
                     patch.object(setup.webbrowser, 'open') as browser, \
                     patch.object(setup, 'Capture') as capture, contextlib.redirect_stdout(io.StringIO()):
                    message = {'invalid-expiry': 'expiry could not be verified',
                               'prepare-failure': 'Synthetic prepare failure',
                               'explicit-reference': 'without --reference'}[scenario]
                    with self.assertRaisesRegex(ValueError, message):
                        setup.setup(ref='old-ref' if scenario == 'explicit-reference' else None)
                    self.assertEqual(saved.read_text(), before)
                    self.assertFalse((base / 'setup.lock').exists())
                    self.assertEqual(sum(argv[1:3] == ['pair', 'prepare'] for argv in calls),
                                     1 if scenario == 'prepare-failure' else 0)
                    browser.assert_not_called()
                    capture.assert_not_called()

    def test_session_launch_failure_restores_config_stops_capture_and_releases_lock(self):
        with TemporaryDirectory() as temp:
            home=Path(temp)
            (home/'config.toml').write_bytes(b'model="chosen"\n')
            def result(argv):
                if argv[1:3]==['pair','show']:
                    return {'acknowledged_until':'2099-01-01T00:00:00Z',
                            'approval_url':'https://orkmeter.com/pair#proposal=public'}
                return {}
            with patch.dict(os.environ,{'CODEX_HOME':str(home)}), \
                 patch.object(setup,'sys_terminal',return_value=True), \
                 patch.object(setup,'binary',return_value='helper'), \
                 patch.object(setup,'native_codex',return_value='native-codex'), \
                 patch.object(setup,'install_plugin'), \
                 patch.object(setup,'public_result',side_effect=result), \
                 patch.object(setup.subprocess,'call',side_effect=OSError('launch failed')), \
                 patch.object(setup,'Capture') as capture, contextlib.redirect_stdout(io.StringIO()):
                capture.return_value.ready.return_value=FRAGMENT
                capture.return_value.fragment=home/'private-fragment.toml'
                with self.assertRaises(OSError):
                    setup.setup(ref='public-ref')
                self.assertEqual((home/'config.toml').read_bytes(),b'model="chosen"\n')
                capture.return_value.stop.assert_called_once()
                self.assertFalse((home/'ork-meter-codex/setup.lock').exists())

    def test_noninteractive_setup_stops_before_credential_or_profile_operations(self):
        with patch.object(setup,'sys_terminal',return_value=False), patch.object(setup,'binary') as helper:
            with self.assertRaisesRegex(ValueError,'interactive terminal'):
                setup.setup()
            helper.assert_not_called()

    def test_saved_dev_pairing_rejects_production_destination(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / 'config.toml').write_text('model="chosen"\n')
            state = home / 'ork-meter-codex'
            setup.private_directory(state)
            (state / 'pairing.json').write_text(json.dumps({'reference': 'dev-ref', 'dev': True}))
            with patch.dict(os.environ, {'CODEX_HOME': str(home)}), \
                 patch.object(setup, 'sys_terminal', return_value=True), \
                 patch.object(setup, 'binary', return_value='helper'), \
                 patch.object(setup, 'native_codex', return_value='native-codex'), \
                 patch.object(setup, 'install_plugin'), \
                 patch.object(setup, 'public_result', return_value={
                     'acknowledged_until': '2099-01-01T00:00:00Z',
                     'approval_url': 'https://orkmeter.com/pair#proposal=wrong'}), \
                 patch.object(setup, 'Capture') as capture, \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'destination'):
                    setup.setup()
                capture.assert_not_called()
                self.assertFalse((state / 'setup.lock').exists())

    def test_real_native_install_bundles_helper_and_can_repeat_in_isolated_profile(self):
        try:
            codex = setup.native_codex()
            helper = setup.binary()
        except ValueError:
            self.skipTest('requires locally installed Codex CLI and Ork Meter helper')
        with TemporaryDirectory() as temp:
            root=Path(temp)
            profile=root/'profile'
            profile.mkdir()
            with patch.dict(os.environ,{'CODEX_HOME':str(profile)}):
                for number in range(2):
                    state=root/f'state-{number}'
                    state.mkdir()
                    setup.install_plugin(codex,state,helper)
                    self.assertTrue((root/'package/.local'/Path(helper).name).is_file())

    def test_checkout_cache_reuse_requires_complete_current_runtime(self):
        for damage in [None, 'stale', 'missing']:
            with self.subTest(damage=damage), TemporaryDirectory() as temp:
                root = Path(temp)
                version = json.loads((setup.ROOT / 'plugin.json').read_text())['version']
                cached = root / 'plugins/cache/edos-codex/ork-meter-codex' / version
                (cached / '.local').mkdir(parents=True)
                shutil.copytree(setup.ROOT / 'ork_codex', cached / 'ork_codex',
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                name = 'ork-meter.exe' if os.name == 'nt' else 'ork-meter'
                helper = root / name
                helper.write_bytes(b'synthetic helper; never executed')
                shutil.copy2(helper, cached / '.local' / name)
                target = cached / 'ork_codex/entitlement.py'
                if damage == 'stale':
                    target.write_text('# Synthetic obsolete module\n', encoding='utf-8')
                elif damage == 'missing':
                    target.unlink()
                config = ('[marketplaces.edos-codex]\nsource_type="local"\nsource=' +
                          json.dumps(str(setup.ROOT)) +
                          '\n[plugins."ork-meter-codex@edos-codex"]\nenabled=true\n').encode()
                (root / 'config.toml').write_bytes(config)
                state = root / 'session'
                state.mkdir()
                with patch.dict(os.environ, {'CODEX_HOME': str(root)}), \
                     patch.object(setup, 'public_result', return_value={'installedPath': str(cached)}) as command, \
                     contextlib.redirect_stdout(io.StringIO()):
                    setup.install_plugin('native-codex', state, str(helper))
                    if damage is None:
                        command.assert_not_called()
                        self.assertFalse((root / 'package').exists())
                    else:
                        command.assert_called_once()
                        self.assertIn('ork-meter-codex@edos-codex', command.call_args.args[0])
                        self.assertEqual((root / 'package/ork_codex/entitlement.py').read_bytes(),
                                         (setup.ROOT / 'ork_codex/entitlement.py').read_bytes())
                self.assertEqual((root / 'config.toml').read_bytes(), config)

    def test_real_native_upgrade_removes_legacy_skills_without_rebinding_checkout(self):
        try:
            codex,helper=setup.native_codex(),setup.binary()
        except ValueError:
            self.skipTest('requires locally installed Codex CLI and helper')
        sys.path.insert(0, str(setup.ROOT / "scripts"))
        from native_codex import snapshot
        with TemporaryDirectory() as temp:
            root=Path(temp)
            profile=root/'profile';profile.mkdir()
            legacy=root/'legacy';snapshot(legacy)
            for name in ['plugin.json','.codex-plugin/plugin.json']:
                path=legacy/name
                manifest=json.loads(path.read_text());manifest['version']='0.1.0'
                path.write_text(json.dumps(manifest),encoding='utf-8')
            skill=legacy/'skills/legacy/SKILL.md';skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: legacy\ndescription: Synthetic legacy metadata.\n---\nSynthetic only.\n',encoding='utf-8')
            with patch.dict(os.environ,{'CODEX_HOME':str(profile)}):
                setup.public_result([codex,'plugin','marketplace','add',str(legacy),'--json'])
                old=setup.public_result([codex,'plugin','add','ork-meter-codex@edos-codex','--json'])
                self.assertTrue((Path(old['installedPath'])/'skills/legacy/SKILL.md').is_file())
                config=('''[marketplaces.edos-codex]\nsource_type="local"\nsource='''+
                        json.dumps(str(setup.ROOT))+'''\n[plugins."ork-meter-codex@edos-codex"]\nenabled=true\n''')
                (profile/'config.toml').write_text(config,encoding='utf-8')
                state=root/'state';state.mkdir()
                setup.install_plugin(codex,state,helper)
                cached=profile/'plugins/cache/edos-codex/ork-meter-codex/0.1.2'
                self.assertTrue((cached/'ork_codex/mcp.py').is_file())
                self.assertFalse((cached/'skills').exists())
                self.assertFalse((cached/'hooks/hooks.json').exists())
                after=tomllib.loads((profile/'config.toml').read_text())
                self.assertEqual(after['marketplaces']['edos-codex']['source'],str(setup.ROOT))

    def test_real_native_reuse_repairs_stale_or_missing_runtime_modules(self):
        try:
            codex, helper = setup.native_codex(), setup.binary()
        except ValueError:
            self.skipTest('requires locally installed Codex CLI and helper')
        for module, damage in [('entitlement.py', 'stale'), ('tracking.py', 'missing')]:
            with self.subTest(module=module), TemporaryDirectory() as temp:
                root = Path(temp)
                profile = root / 'profile'
                profile.mkdir()
                state = root / 'state'
                state.mkdir()
                with patch.dict(os.environ, {'CODEX_HOME': str(profile)}):
                    setup.install_plugin(codex, state, helper)
                    version = json.loads((setup.ROOT / 'plugin.json').read_text())['version']
                    cached = profile / 'plugins/cache/edos-codex/ork-meter-codex' / version
                    target = cached / 'ork_codex' / module
                    if damage == 'missing':
                        target.unlink()
                    else:
                        target.write_text('# Synthetic obsolete runtime module\n', encoding='utf-8')
                    config = ('[marketplaces.edos-codex]\nsource_type="local"\nsource=' +
                              json.dumps(str(setup.ROOT)) +
                              '\n[plugins."ork-meter-codex@edos-codex"]\nenabled=true\n')
                    (profile / 'config.toml').write_text(config, encoding='utf-8')
                    setup.install_plugin(codex, state, helper)
                    self.assertTrue(target.is_file(), 'setup accepted a cache missing runtime code')
                    self.assertEqual(target.read_bytes(), (setup.ROOT / 'ork_codex' / module).read_bytes())
                    after = tomllib.loads((profile / 'config.toml').read_text())
                    self.assertEqual(after['marketplaces']['edos-codex']['source'], str(setup.ROOT))
                    self.assertTrue(after['plugins']['ork-meter-codex@edos-codex']['enabled'])

    def test_owned_capture_process_drains_output_and_stops_cleanly(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            fragment,marker=root/'fragment.toml',root/'stopped.txt'
            fixture=Path(__file__).parent/'fixtures/setup_capture.py'
            original=setup.subprocess.Popen
            def spawn(argv,**kwargs):
                return original([sys.executable,str(fixture),str(fragment),str(marker)],**kwargs)
            with patch.object(setup.subprocess,'Popen',side_effect=spawn):
                capture=setup.Capture('synthetic-helper','public-ref')
                try:
                    self.assertEqual(capture.ready(),b'synthetic only')
                finally:
                    capture.stop()
                self.assertEqual(capture.process.returncode,0)
                self.assertEqual(marker.read_text(), 'stopped')
                self.assertFalse(fragment.exists())


if __name__=='__main__':
    unittest.main()
