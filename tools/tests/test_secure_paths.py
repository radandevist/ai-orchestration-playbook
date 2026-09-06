import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pr_closure.secure_paths import SecurePathError, atomic_create, read_contained_file


@unittest.skipUnless(
    hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
    "requires no-follow directory opens",
)
class SecurePathRevalidationTests(unittest.TestCase):
    def _read_after_ancestor_move(self, *, mutate_leaf):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as temp_dir:
            base = Path(temp_dir)
            root = base / "closure"
            inside = root / "inside"
            moved = base / "moved-outside"
            target = inside / "evidence.json"
            root.mkdir()
            inside.mkdir()
            target.write_bytes(b"original-bytes")

            original_open = os.open
            swapped = False

            def open_and_replace(path, flags, *args, **kwargs):
                nonlocal swapped
                fd = original_open(path, flags, *args, **kwargs)
                if path == "inside" and kwargs.get("dir_fd") is not None and not swapped:
                    inside.rename(moved)
                    if mutate_leaf:
                        (moved / "evidence.json").write_bytes(b"moved-leaf-bytes")
                    os.symlink(moved, inside)
                    swapped = True
                return fd

            with mock.patch(
                "pr_closure.secure_paths.os.open", side_effect=open_and_replace
            ):
                with self.assertRaises(OSError):
                    read_contained_file(target, root, "authority")
            self.assertTrue(swapped)
            self.assertTrue(inside.is_symlink())
            self.assertEqual(b"moved-leaf-bytes" if mutate_leaf else b"original-bytes", target.read_bytes())

    def test_ancestor_rename_and_replacement_symlink_is_rejected_after_read(self):
        self._read_after_ancestor_move(mutate_leaf=False)

    def test_moved_directory_leaf_mutation_never_returns_moved_bytes(self):
        self._read_after_ancestor_move(mutate_leaf=True)

    def test_atomic_create_retry_repairs_target_link_published_before_temp_removal(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as temp_dir:
            target = Path(temp_dir) / "record.json"
            script = """
import os
from pr_closure import secure_paths

original_link = os.link

def publish_then_stop(*args, **kwargs):
    original_link(*args, **kwargs)
    os._exit(0)

os.link = publish_then_stop
secure_paths.atomic_create(os.environ["TARGET"], b"immutable")
"""
            env = dict(os.environ)
            env["TARGET"] = str(target)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            child = subprocess.run(
                [sys.executable, "-c", script], env=env, capture_output=True, text=True
            )
            self.assertEqual(0, child.returncode, child.stderr)
            self.assertTrue(target.exists())
            temporary = sorted(target.parent.glob(".tmp-*"))
            self.assertEqual(1, len(temporary))
            self.assertEqual(2, target.stat().st_nlink)

            self.assertTrue(atomic_create(target, b"immutable"))
            self.assertEqual([], list(target.parent.glob(".tmp-*")))
            self.assertEqual(1, target.stat().st_nlink)
            self.assertEqual(b"immutable", target.read_bytes())

    def test_atomic_create_fails_closed_on_ambiguous_publication_links(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as temp_dir:
            target = Path(temp_dir) / "record.json"
            self.assertTrue(atomic_create(target, b"immutable"))
            first = target.parent / ".tmp-11111111111111111111111111111111"
            second = target.parent / ".tmp-22222222222222222222222222222222"
            os.link(target, first)
            os.link(target, second)
            with self.assertRaises(SecurePathError):
                atomic_create(target, b"immutable")
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
