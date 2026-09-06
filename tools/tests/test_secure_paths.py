import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pr_closure.secure_paths import read_contained_file


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


if __name__ == "__main__":
    unittest.main()
