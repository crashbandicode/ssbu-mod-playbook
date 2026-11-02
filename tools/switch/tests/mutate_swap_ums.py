"""Mutation check of tools/switch/swap-ums.py: remove one safety step at a time in a COPY of the script and run
tests/test_swap_ums.py against that copy (SWAP_UMS_UNDER_TEST). Every mutant must make the suite fail.

Usage: python3 -I tools/switch/tests/mutate_swap_ums.py [work dir]   (default: a new temporary folder)
Exit 1 when a mutant survives, 2 when a pattern no longer matches the script (update the list with the script)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SWITCH = Path(__file__).resolve().parents[1]
SCRIPT = SWITCH / 'swap-ums.py'
TEST = SWITCH / 'tests' / 'test_swap_ums.py'
MUTANTS = [
    ('archive readback check', "require(archived['sha256'] == self.old, 'Rollback archive readback mismatch')", 'pass'),
    ('stage readback check', "require(staged['sha256'] == self.new, 'Staging readback mismatch')", 'pass'),
    ('new NRO readback check', "require(after[self.target_new]['sha256'] == self.new, f'{self.target_new}: readback does not match')", 'pass'),
    ('data file readback check', "require(result['sha256'] == entry['sha256'], f'{entry[\"path\"]}: readback does not match')", 'pass'),
    ('archive intact readback check', "require(after[self.archive]['sha256'] == self.old, f'{self.archive}: readback does not match')", 'pass'),
    ('other files readback check', "require(result['sha256'] == previous['sha256'], f'{relative}: readback does not match')", 'pass'),
    ('watched tree after-install check', "require(not changed, f'watched tree {tree} changed during the install: {changed[:5]}')", 'pass'),
    ('watched tree before-install check', "require(not moved, f'a watched tree changed since preflight: {moved[:5]}')", 'pass'),
    ('plugin names after check', "require(receipt['plugin_names_after'] == expected, f'the plugin folder lists {receipt[\"plugin_names_after\"]}, expected {expected}')", 'pass'),
    ('old NRO hash check in preflight', "require(receipt['before'][self.target_old]['sha256'] == self.old, 'Unexpected installed build; inspect before the swap')", 'pass'),
    ('fresh just-before hash check', "require(fresh['sha256'] == self.old, 'Changed since preflight')", 'pass'),
    ('receipt re-run refusal', "require(not self.receipt_path.exists(), 'Preserve completed/failed evidence; inspect before a new operation')", 'pass'),
    ('install authorization', "require(approval.get('operator_swap_authorized') is True, 'operator_swap_authorized is not true')", 'pass'),
    ('import marker never placed', "require(path.rsplit('/', 1)[1].lower() not in NEVER_PLACED, f'{path} must never be placed: the plugin writes it itself')", 'pass'),
    ('never overwrite on store', "require(overwrite or not os.path.lexists(target), f'{relative} exists; this tool never overwrites')", 'pass'),
    ('rollback foreign-file guard', "require(present['sha256'] == self.old, f'a different file is at {self.target_old}; not overwriting it')", 'pass'),
    ('rollback restore readback', "require(restored['sha256'] == self.old, 'the restored NRO does not match the old build')", 'pass'),
    ('rollback removes the data files', "                        card.unlink(path)\n                    else:", "                        pass\n                    else:"),
    ('rollback keeps a foreign file at an added path', "if path in self.stored_files or self.download(path, 'rollback-found')['sha256'] == self.local_files[path]['sha256']:", 'if True:'),
    ('rollback removes the stray temporary', "if card.exists(temporary):\n                    card.unlink(temporary)", 'pass'),
    ('rollback keeps a data folder it did not make', "if card.exists(folder) and not card.listing(folder):", 'if card.exists(folder):'),
    ('rollback removes the new NRO', "if self.swapped_in and card.exists(self.target_new):\n                card.unlink(self.target_new)", 'pass'),
    ('rollback on failure after removal', 'if self.removed:\n                self.roll_back()', 'if False:\n                self.roll_back()'),
    ('removed flag set before the removal', 'self.removed = True\n        card.unlink(self.target_old)', 'card.unlink(self.target_old)\n        self.removed = True'),
    ('read-only card refusal', "require(not read_only, 'the card is mounted read-only')", 'pass'),
    ('free space check', "require(free >= needed + self.min_free, f'{free} bytes free, {needed} needed plus {self.min_free} kept free')", 'pass'),
    ('local copy of old NRO check', "require(info(before[self.target_old]['local'])['sha256'] == self.old, 'the local copy of the old NRO changed')", 'pass'),
    ('approval unchanged since preflight', "require(preflight['approval'] == self.summary, 'the approval changed since preflight')", 'pass'),
    ('mount id unchanged in verify', "require(receipt['mount_id_after'] == receipt['mount_id_before'], 'the card was remounted during verify')", 'pass'),
    ('archive before removal', "card.store(before[self.target_old]['local'], self.archive)", 'pass'),
    ('verify compares with the install', "require(result['sha256'] == expected['sha256'], f'{relative}: readback does not match the install')", 'pass'),
    ('same name refused', "require(self.old_name != self.new_name, 'remove and add have the same name: this tool swaps two plugins, use upgrade2-ums.py')", 'pass'),
]


def main():
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='swap-ums-mutants-'))
    work.mkdir(parents=True, exist_ok=True)
    text = SCRIPT.read_text()
    survivors = 0
    for number, (label, old, new) in enumerate(MUTANTS, 1):
        if text.count(old) != 1:
            print(f'pattern of "{label}" occurs {text.count(old)} times in swap-ums.py: update the list', file=sys.stderr)
            return 2
        mutant = work / f'swap-ums-mutant-{number}.py'
        mutant.write_text(text.replace(old, new))
        done = subprocess.run([sys.executable, '-I', str(TEST)], capture_output=True, text=True,
                              env=dict(os.environ, SWAP_UMS_UNDER_TEST=str(mutant)), timeout=600)
        killed = done.returncode != 0
        survivors += not killed
        failed = [line.split(' ')[1] for line in done.stderr.splitlines() if line.startswith(('FAIL:', 'ERROR:'))]
        print(f'{"KILLED  " if killed else "SURVIVED"} {number:2d} {label}  ({len(failed)} failing: {", ".join(failed[:3])}{"…" if len(failed) > 3 else ""})', flush=True)
    print(f'{len(MUTANTS)} mutants, {survivors} survived')
    return 1 if survivors else 0


if __name__ == '__main__':
    sys.exit(main())
