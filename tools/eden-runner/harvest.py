#!/usr/bin/env python3
"""Copy what a run proved into the shared library (MACROS "Test runs", rule 7).

  harvest.py <rows.json> --label lr-03 [--out <runner out folder>] [--dry-run]

A segment or gate is harvested only if it was used in a row whose receipt says PASS. Each library segment keeps its
sha256, the gate checked before it (start) and after it (end) in that row, and the run label; each gate keeps its
definition, with an image reference or hook module copied next to it. Existing entries with the same name are left
alone unless --replace is given (a changed route needs a new name, or a deliberate replace).
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import runner  # noqa: E402


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def flatten_steps(steps):
    """A gif step's nested steps are the moment the row played. Harvest those segments and gates too."""
    flat = []
    for step in steps or []:
        gif = step.get('gif') if isinstance(step, dict) else None
        if isinstance(gif, dict):
            flat.extend(flatten_steps(gif.get('steps')))
        else:
            flat.append(step)
    return flat


def harvest(manifest, out, label, library, replace=False, dry=False):
    plan = runner.expand_library(json.loads(manifest.read_text()))
    base = manifest.parent
    index_path = library / 'index.json'
    index = json.loads(index_path.read_text()) if index_path.is_file() else {'segments': {}, 'gates': {}}
    added = {'segments': [], 'gates': [], 'skipped': []}
    for row in plan['rows']:
        receipt = out / f'{row["id"]}.json'
        if not receipt.is_file() or json.loads(receipt.read_text()).get('status') != 'PASS':
            continue
        last_gate = {}
        steps = flatten_steps(row['steps'])
        for i, step in enumerate(steps):
            on = step.get('on', '*')
            if 'gate' in step:
                last_gate[on] = step['gate']
                names = [step['gate']]
            elif 'play' in step:
                # A segment's own start gate is a dependency even when no row step names it (lr-03 harvest gap).
                names = [g for g in (plan['segments'][step['play']].get('start_gate'),) if g]
            else:
                continue
            for gate_name in names:
                gate = dict(plan['gates'][gate_name])
                if gate.get('library') or (gate_name in index['gates'] and not replace):
                    continue
                for field, folder in (('reference', 'refs'), ('module', 'checks')):
                    if field in gate:
                        src = Path(gate[field]) if Path(gate[field]).is_absolute() else base / gate[field]
                        dest = Path(folder) / f'{gate_name}{src.suffix}'
                        gate[field] = str(dest)
                        if dry:
                            continue
                        (library / folder).mkdir(parents=True, exist_ok=True)
                        if field == 'reference' and gate.get('kind') in ('image', 'change') and 'roi' in gate:
                            # Keep only the compared region: a full screenshot is ~600 KB, the region a few KB.
                            from PIL import Image
                            with Image.open(src) as im:
                                im.convert('RGB').crop(tuple(gate['roi'])).save(library / dest)
                            gate['reference_is_roi'] = True
                            gate['sha256'] = digest(library / dest)
                        else:
                            shutil.copyfile(src, library / dest)
                            if field == 'reference':
                                gate['sha256'] = digest(src)
                gate.pop('acquire', None)
                gate['from_run'] = label
                index['gates'][gate_name] = gate
                added['gates'].append(gate_name)
            if 'play' not in step:
                continue
            name = step['play']
            seg = plan['segments'][name]
            if seg.get('library'):
                continue
            if name in index['segments'] and not replace:
                if name not in added['skipped'] and name not in added['segments']:
                    added['skipped'].append(name)
                continue
            src = Path(seg['file']) if Path(seg['file']).is_absolute() else base / seg['file']
            if digest(src) != seg.get('sha256'):
                raise RuntimeError(f'segment {name}: file changed since the run recorded it')
            end_gate = next((s['gate'] for s in steps[i + 1:] if 'gate' in s and s.get('on', '*') == on), None)
            dest = Path('segments') / f'{name}.edenmacro.json'
            if not dry:
                (library / 'segments').mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, library / dest)
            index['segments'][name] = {
                'file': str(dest), 'sha256': seg['sha256'], 'cap': seg.get('cap', 60),
                'start_gate': seg.get('start_gate') or last_gate.get(on), 'end_gate': end_gate,
                'from_run': label, 'row': row['id'],
            }
            added['segments'].append(name)
    if not dry:
        index_path.write_text(json.dumps(index, indent=2) + '\n')
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--label', required=True, help='run label, e.g. lr-03')
    parser.add_argument('--out', type=Path, help='runner output folder (default: <manifest folder>/out)')
    parser.add_argument('--library', type=Path, default=runner.LIBRARY)
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    manifest = args.manifest.resolve()
    added = harvest(manifest, (args.out or manifest.parent / 'out').resolve(), args.label, args.library.resolve(),
                    args.replace, args.dry_run)
    print(json.dumps(added, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
