import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from compliance import guard_size


class GuardSize(unittest.TestCase):
    def test_project_repeat_initializer_has_sixty_four_bytes(self):
        source = "pub const GUARD_BYTES: usize = 64;"
        self.assertEqual(guard_size("0; GUARD_BYTES", source), 64)
        self.assertEqual(guard_size("0; 64", ""), 64)
        self.assertEqual(guard_size("0; 6_4", ""), 64)

    def test_wrong_and_unresolved_repeats_do_not_pass(self):
        self.assertEqual(guard_size("0; 63", ""), 63)
        for source in ("", "const GUARD_BYTES: usize = other;",
                       "const GUARD_BYTES: usize = 64; const GUARD_BYTES: usize = 32;",
                       "const GUARD_BYTES: usize = 64; const GUARD_BYTES: usize = other;"):
            self.assertEqual(guard_size("0; GUARD_BYTES", source), 0)

    def test_existing_literal_guard_counts_are_preserved(self):
        for count in (0, 1, 63, 64, 65):
            self.assertEqual(guard_size(", ".join(["0x12"] * count), ""), count)


if __name__ == "__main__":
    unittest.main()
