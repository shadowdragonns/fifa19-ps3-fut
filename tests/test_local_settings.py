"""Existing game settings survive repeated launches and interrupted startup."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import launch


class LocalSettingsTests(unittest.TestCase):
    def test_existing_cl_ini_is_backed_up_replaced_and_restored_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'cl.ini';backup=root/'logs'
            original=b'; original settings\r\nFUT_TARGET_HOSTNAME = 192.0.2.5\r\n'
            target.write_bytes(original)
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError,'startup failure'):
                    with launch.local_settings(target,b'new server settings\n',backup):
                        self.assertEqual(target.read_bytes(),b'new server settings\n')
                        raise RuntimeError('startup failure')
                self.assertEqual(target.read_bytes(),original)
                self.assertEqual((backup/'cl.ini.before').read_bytes(),original)

    def test_absent_file_is_removed_and_external_edits_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'cl.ini'
            with launch.local_settings(target,b'local\n'):self.assertTrue(target.exists())
            self.assertFalse(target.exists())
            with launch.local_settings(target,b'local\n'):target.write_bytes(b'edited externally\n')
            self.assertEqual(target.read_bytes(),b'edited externally\n')

    def test_failed_atomic_install_preserves_original_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'cl.ini';target.write_bytes(b'original')
            mode=root.stat().st_mode
            with patch.object(Path,'replace',side_effect=OSError('write failure')):
                with self.assertRaises(OSError):
                    with launch.local_settings(target,b'new'):self.fail('write should fail')
            self.assertEqual(target.read_bytes(),b'original')
            self.assertEqual(root.stat().st_mode,mode)
            self.assertFalse(list(root.glob('.fut-settings-*')))


if __name__=='__main__':unittest.main()
