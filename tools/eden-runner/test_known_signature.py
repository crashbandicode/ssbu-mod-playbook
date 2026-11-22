"""judge_queue.known_issue: only an exact signature match is a KNOWN-PRODUCT-ISSUE (switch-hw-session-2)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge_queue as jq  # noqa: E402

KNOWN = {'a': {'title': 'P1 swap', 'reason': r'P1 \+ CPU.*one menu visit', 'rows': ['FX-01-for-FS-01']},
         'e': {'title': 'projectiles', 'reason': r'projectile[s]? (do not|don.t|never) break', 'rows': ['FS-57']}}


class Signature(unittest.TestCase):
    def test_no_match_stays_failed(self):
        self.assertIsNone(jq.known_issue(KNOWN, {'verdict': 'FAIL', 'reason': 'Practice menu shows Break the Targets On'}))
        self.assertIsNone(jq.known_issue(KNOWN, {'verdict': 'FAIL', 'reason': 'a menu open/close leaves the count at 4; '
                                                                             'break'}))

    def test_signature_match(self):
        got = jq.known_issue(KNOWN, {'verdict': 'FAIL', 'reason': 'Fireball projectiles do not break the targets'})
        self.assertEqual(got['ids'], ['e'])

    def test_pass_is_never_known(self):
        self.assertIsNone(jq.known_issue(KNOWN, {'verdict': 'PASS', 'reason': 'projectiles do not break'}))


if __name__ == '__main__':
    unittest.main()
