"""Recover launcher patches across interrupted launches without replacing custom patches."""
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

import certificate_patch as cert
import client_platform


class CertificatePatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'data').mkdir()
        shutil.copy2(cert.HERE/'data/client-native-patch.json', self.root/'data/client-native-patch.json')
        self.ca = self.root/'public.pem'
        self.ca.write_text('Synthetic public certificate; modulus supplied by test double.')
        self.addCleanup(patch.stopall)
        patch.object(cert, 'HERE', self.root).start()
        self.key = patch('tls_identity.public_modulus', return_value=b'\x11'*256).start()
        self.profile = self.root/'profile'
        self.profile.mkdir()
        self.settings = self.profile/'config/patch_config.yml'
        patch.object(client_platform, 'settings_directory', return_value=self.settings.parent).start()
        self.target = self.profile/'patches/BLES02258_patch.yml'
        self.target.parent.mkdir()
        self.backup = self.root/'backup'

    def generated(self, trace=True, skip=True):
        return cert.prepare_portable(trace, skip, self.ca).read_bytes().replace(b'\r\n', b'\n')

    def context(self):
        return cert.temporary_patch(self.profile, self.backup, trust_ca=self.ca)

    def test_windows_stale_patch_and_same_hash_settings_refresh_and_restore_after_failure(self):
        old_patch = self.generated(trace=False, skip=False).replace(b'\n', b'\r\n')
        self.target.write_bytes(old_patch)
        own = cert._enabled_settings(None).replace(b'Enabled: true', b'Enabled: false')
        other_fifa = b'  "Another FIFA patch":\n    "FIFA 19":\n      BLES02258: {}\n'
        other_game = b'PPU-other-game:\n  Other patch: {}\n'
        original = (own+other_fifa+other_game).replace(b'\n', b'\r\n')
        self.settings.parent.mkdir()
        self.settings.write_bytes(original)
        self.key.return_value = b'\x22'*256
        with self.assertRaisesRegex(RuntimeError, 'synthetic startup failure'):
            with self.context():
                current = self.target.read_bytes()
                self.assertNotEqual(current, old_patch)
                self.assertIn(b'0x22222222', current)
                enabled = self.settings.read_bytes()
                self.assertEqual(enabled.count(cert.PPU_HASH.encode()), 1)
                self.assertEqual(enabled.count(cert.DESCRIPTION.encode()), 1)
                self.assertIn(b'Enabled: true', enabled)
                self.assertIn(other_fifa.replace(b'\n', b'\r\n'), enabled)
                self.assertIn(other_game.replace(b'\n', b'\r\n'), enabled)
                raise RuntimeError('synthetic startup failure')
        self.assertEqual(self.target.read_bytes(), old_patch)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual((self.backup/'BLES02258_patch.yml.before').read_bytes(), old_patch)
        self.assertEqual((self.backup/'patch_config.yml.before').read_bytes(), original)

    def test_settings_hash_can_exist_without_our_entry_and_is_restored(self):
        original = f'{cert.PPU_HASH}:\n  "Custom FIFA patch": {{}}\n'.encode()
        self.settings.parent.mkdir()
        self.settings.write_bytes(original)
        for _ in range(2):
            with self.context():
                self.assertTrue(self.target.exists())
                self.assertTrue(self.settings.read_bytes().startswith(original))
                self.assertEqual(self.settings.read_bytes().count(cert.PPU_HASH.encode()), 1)
            self.assertFalse(self.target.exists())
            self.assertEqual(self.settings.read_bytes(), original)

    def test_stale_patch_with_any_generated_options_is_recognised(self):
        for trace, skip in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(trace=trace, skip=skip):
                original = self.generated(trace, skip)
                self.target.write_bytes(original)
                with self.context(): self.assertTrue(self.target.exists())
                self.assertEqual(self.target.read_bytes(), original)
                self.assertFalse(self.settings.exists())

    def test_modified_or_unrecognised_patch_is_never_overwritten(self):
        generated = self.generated()
        for original in (b'custom title patch', generated+b'PPU-other:\n',
                         generated.replace(b'0x01f1e81c', b'0x01f1e818', 1),
                         generated+b'      - [ be32, 0x00100000, 0x00000000 ]\n'):
            with self.subTest(original_size=len(original)):
                self.target.write_bytes(original)
                with self.assertRaisesRegex(RuntimeError, re.escape(str(self.target))):
                    with self.context(): self.fail('custom patch overwritten')
                self.assertEqual(self.target.read_bytes(), original)
                self.assertFalse(self.settings.exists())

    def test_failed_settings_install_restores_existing_patch(self):
        original = self.generated(False, False)
        self.target.write_bytes(original)
        real_write = cert._write_atomic
        def write(path, content):
            if path == self.settings: raise OSError('synthetic write failure')
            real_write(path, content)
        with patch.object(cert, '_write_atomic', side_effect=write):
            with self.assertRaisesRegex(OSError, 'synthetic write failure'):
                with self.context(): self.fail('settings write unexpectedly succeeded')
        self.assertEqual(self.target.read_bytes(), original)
        self.assertFalse(self.settings.exists())

    def test_settings_restore_failure_still_restores_patch_and_retains_backups(self):
        original = self.generated(False, False)
        self.target.write_bytes(original)
        self.settings.parent.mkdir()
        self.settings.write_bytes(b'{}\n')
        real_write = cert._write_atomic
        def write(path, content):
            if path == self.settings and content == b'{}\n': raise OSError('restore failure')
            real_write(path, content)
        with patch.object(cert, '_write_atomic', side_effect=write):
            with self.assertRaisesRegex(OSError, 'restore failure'):
                with self.context(): pass
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual((self.backup/'patch_config.yml.before').read_bytes(), b'{}\n')

    def test_external_edits_during_launch_are_preserved(self):
        with self.context():
            self.target.write_bytes(b'changed externally')
            self.settings.write_bytes(b'changed externally')
        self.assertEqual(self.target.read_bytes(), b'changed externally')
        self.assertEqual(self.settings.read_bytes(), b'changed externally')

    def test_ambiguous_settings_fail_before_any_patch_changes(self):
        own = cert._enabled_settings(None)
        inline = f'  "{cert.DESCRIPTION}": {{}}\n'.encode()
        for content in (own+own, own+own.split(b'\n', 1)[1], own+inline,
                        own+f'{cert.PPU_HASH}: {{}}\n'.encode(),
                        f'{cert.PPU_HASH}:\n  "{cert.DESCRIPTION}": {{}}\n'.encode(), b'---\n'+own):
            with self.subTest(content=content):
                self.settings.parent.mkdir(exist_ok=True)
                self.settings.write_bytes(content)
                with self.assertRaises(RuntimeError):
                    with self.context(): self.fail('ambiguous settings replaced')
                self.assertFalse(self.target.exists())
                self.assertEqual(self.settings.read_bytes(), content)


if __name__ == '__main__': unittest.main()
