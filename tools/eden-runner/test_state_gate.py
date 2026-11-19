"""state_gate: framed bridge state blocks and the gate's freshness/transition rules (no Eden, no console)."""
import unittest
import zlib

import state_gate as sg


def block(rev, payload, name='tr.menu', boot='0x00000000000000ab', crc=None, parts=None, filtered=0, end_rev=None):
    lines = [f'[Bridge] state begin name={name} schema={name}/1 rev={rev} boot={boot} lines={len(payload)} '
             f'filtered={filtered} frame=10 wall=20']
    for n, item in enumerate(payload):
        if parts and n in parts:
            chunks = [item[i:i + parts[n]] for i in range(0, len(item), parts[n])]
            lines += [f'[Bridge] state.{n}.part={k + 1}/{len(chunks)} {c} frame=10 wall=20' for k, c in enumerate(chunks)]
        else:
            lines.append(f'[Bridge] state.{n} {item} frame=10 wall=20')
    crc = crc if crc is not None else f'{zlib.crc32(chr(10).join(payload).encode()):08x}'
    lines.append(f'[Bridge] state end name={name} rev={end_rev or rev} crc={crc} frame=11 wall=21')
    return '\n'.join(lines) + '\n'


MENU = ['schema=tr.menu/1', 'ack=3', 'ack.result="handled"', 'focus=fighters_stages/-/24/game.1', 'open=1',
        'page="fighters_stages"']


def menu(**change):
    out = []
    for item in MENU:
        key = item.split('=')[0]
        out.append(f'{key}={change[key]}' if key in change else item)
    return out


class BlockTests(unittest.TestCase):
    def test_a_complete_block_parses_with_strings_unquoted(self):
        b = sg.latest(block(5, MENU), 'tr.menu')
        self.assertEqual((b['rev'], b['values']['page'], b['values']['open'], b['values']['ack.result']),
                         (5, 'fighters_stages', '1', 'handled'))

    def test_partial_bad_crc_wrong_count_and_torn_blocks_are_not_blocks(self):
        good = block(5, MENU)
        self.assertIsNone(sg.latest(good.rsplit('\n', 2)[0], 'tr.menu'), 'no end line')
        self.assertIsNone(sg.latest(block(5, MENU, crc='deadbeef'), 'tr.menu'))
        cut = good.replace('[Bridge] state.3 focus=fighters_stages/-/24/game.1 frame=10 wall=20\n', '')
        self.assertIsNone(sg.latest(cut, 'tr.menu'), 'a missing line')
        # A reply torn across a reconnect: begin of rev 5, then the new connection's block of rev 6 (whole) — only 6.
        torn = good.split('[Bridge] state end')[0] + block(6, menu(ack='4'))
        found = sg.blocks(torn, 'tr.menu')
        self.assertEqual([b['rev'] for b in found], [6])
        self.assertIsNone(sg.latest(block(5, MENU, end_rev=7), 'tr.menu'), 'an end of another revision')

    def test_parts_reassemble_losslessly_and_a_missing_part_rejects_the_block(self):
        row = 'rows=' + ','.join(f'"Row \\"{i}\\" \\\\ x"' for i in range(60))
        text = block(9, MENU + [row], parts={6: 100})
        self.assertEqual(sg.latest(text, 'tr.menu')['values']['rows'].count('Row "'), 60)
        broken = '\n'.join(l for l in text.splitlines() if '.part=2/' not in l)
        self.assertIsNone(sg.latest(broken, 'tr.menu'))

    def test_suffixes_with_event_seq_are_stripped(self):
        text = block(5, MENU).replace(' frame=10', ' ev=12 frame=10')
        self.assertEqual(sg.latest(text, 'tr.menu')['rev'], 5)


class GateTests(unittest.TestCase):
    base = sg.latest(block(5, MENU), 'tr.menu')

    def test_a_newer_block_with_every_predicate_passes(self):
        b = sg.latest(block(6, menu(ack='4', focus='fighters_stages/22/0/start.1')), 'tr.menu')
        self.assertTrue(sg.satisfies(b, {'page': 'fighters_stages'}, self.base, transition='focus', ack=1)[0])

    def test_the_same_or_an_older_revision_never_passes(self):
        self.assertFalse(sg.satisfies(self.base, {'page': 'fighters_stages'}, self.base)[0])

    def test_an_ignored_input_plus_an_unrelated_revision_does_not_pass_a_navigation_gate(self):
        # A notice changed (new rev) but the press was ignored: same focus, same ack.
        b = sg.latest(block(6, MENU + ['notice.body="Eggs 1"']), 'tr.menu')
        self.assertEqual(sg.satisfies(b, {}, self.base, transition='focus')[0], False)
        self.assertEqual(sg.satisfies(b, {}, self.base, ack=1)[0], False)

    def test_a_missing_predicate_key_another_boot_or_another_schema_fails(self):
        b = sg.latest(block(6, MENU), 'tr.menu')
        self.assertIn('missing', sg.satisfies(b, {'game': 'eggs'}, self.base)[1])
        other = sg.latest(block(6, MENU, boot='0x01'), 'tr.menu')
        self.assertIn('boot', sg.satisfies(other, {}, self.base)[1])

    def test_ack_must_advance_by_exactly_one_and_be_handled(self):
        two = sg.latest(block(6, menu(ack='5')), 'tr.menu')
        self.assertFalse(sg.satisfies(two, {}, self.base, ack=1)[0])
        ignored = sg.latest(block(6, menu(ack='4', **{'ack.result': '"ignored"'})), 'tr.menu')
        self.assertFalse(sg.satisfies(ignored, {}, self.base, ack=1)[0])



class LinkTests(unittest.TestCase):
    def test_the_capture_tracks_the_boot_and_the_event_cursor_across_a_reconnect(self):
        import switch_hw
        link = {'boot': None, 'cursor': 0, 'connects': 1}
        hello = (b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=x port=6970 guards=0x3fffff boot=0x00ab '
                 b'uptime_ms=5 reconnects=0 event_latest=0 state_api=1 frame=1 wall=2')
        self.assertIn(b'connected boot=0x00ab', switch_hw.link_line(link, hello))
        self.assertIsNone(switch_hw.link_line(link, b'[TrainingRooms] notice_draw header="a" body="b" ev=41 frame=3 wall=4'))
        self.assertEqual(link['cursor'], 41)
        link['connects'] = 2
        self.assertIn(b'same_boot=1', switch_hw.link_line(link, hello.replace(b'reconnects=0', b'reconnects=1')))
        self.assertEqual(link['cursor'], 41, 'the cursor survives a same-boot reconnect')
        self.assertIn(b'same_boot=0', switch_hw.link_line(link, hello.replace(b'0x00ab', b'0x00cd')))
        self.assertEqual(link['cursor'], 0, 'a new boot starts a new event sequence')

    def test_state_and_reconnect_verbs_pass_the_v2_spool(self):
        import switch_hw
        self.assertTrue({'state', 'state_watch', 'providers', 'since', 'events'} <= switch_hw.V2_VERBS)


if __name__ == '__main__':
    unittest.main()
