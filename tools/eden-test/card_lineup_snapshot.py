#!/usr/bin/env python3
"""Create an immutable, timestamped snapshot of the mounted card's Eden lineup.

The card root is read only. Plugins are copied from Atmosphere, every regular file
under ultimate/mods is copied, and only TOML files under ultimate/arcropolis are
included. Identical payload files from earlier stamps are hard-linked.
"""
import argparse
import hashlib
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

PLUGINS = Path('atmosphere/contents/01006A800016E000/romfs/skyline/plugins')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _regular_files(root, *, suffix=None):
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f'expected a plain directory: {root}')
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'symlink in card source refused: {path}')
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f'non-regular card source refused: {path}')
        if suffix is None or path.name.endswith(suffix):
            files.append(path)
    return files


def _prior_content(output, digest_value):
    if not output.is_dir():
        return None
    for stamp in sorted(output.iterdir(), reverse=True):
        if not stamp.is_dir() or not re.fullmatch(r'\d{8}-\d{4}Z', stamp.name):
            continue
        roots = [stamp / 'plugins', stamp / 'ultimate/mods', stamp / 'ultimate/arcropolis']
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob('*')):
                if path.is_symlink() or not path.is_file() or path.name.endswith('SUMS.txt'):
                    continue
                if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                    continue
                if digest(path) == digest_value:
                    return path
    return None


def _stage_file(source, target, output):
    target.parent.mkdir(parents=True, exist_ok=True)
    value = digest(source)
    previous = _prior_content(output, value)
    if previous is not None:
        os.link(previous, target)
    else:
        shutil.copyfile(source, target)
        target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return value


def snapshot(card_root, output_root, stamp=None):
    card_root = Path(card_root)
    if card_root.is_symlink() or not card_root.is_dir():
        raise ValueError(f'card root must be a plain directory: {card_root}')
    card_root, output_root = card_root.resolve(), Path(output_root).resolve()
    if output_root == card_root or card_root in output_root.parents:
        raise ValueError('snapshot output must be outside the card root')
    stamp = stamp or datetime.now(timezone.utc).strftime('%Y%m%d-%H%MZ')
    if not re.fullmatch(r'\d{8}-\d{4}Z', stamp):
        raise ValueError('stamp must be YYYYMMDD-HHMMZ')
    destination = output_root / stamp
    if destination.exists():
        raise FileExistsError(f'snapshot already exists: {destination}')
    plugins_src = card_root / PLUGINS
    plugin_files = _regular_files(plugins_src, suffix='.nro')
    if not plugin_files:
        raise ValueError(f'no plugin NROs under {plugins_src}')
    mods_src = card_root / 'ultimate/mods'
    arc_src = card_root / 'ultimate/arcropolis'
    mods_files = _regular_files(mods_src)
    arc_configs = _regular_files(arc_src, suffix='.toml')
    output_root.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True)
    try:
        plugin_dir = destination / 'plugins'
        plugin_dir.mkdir()
        (destination / 'ultimate/mods').mkdir(parents=True)
        (destination / 'ultimate/arcropolis').mkdir(parents=True)
        plugin_sums, asset_sums = [], []
        for source in plugin_files:
            target = plugin_dir / source.name
            value = _stage_file(source, target, output_root)
            plugin_sums.append(f'{value}  {target.name}\n')
        (plugin_dir / 'SHA256SUMS.txt').write_text(''.join(plugin_sums), encoding='utf-8')
        for source, base in ((path, mods_src) for path in mods_files):
            relative = Path('ultimate/mods') / source.relative_to(base)
            target = destination / relative
            value = _stage_file(source, target, output_root)
            asset_sums.append(f'{value}  {relative.as_posix()}\n')
        for source in arc_configs:
            relative = Path('ultimate/arcropolis') / source.relative_to(arc_src)
            target = destination / relative
            value = _stage_file(source, target, output_root)
            asset_sums.append(f'{value}  {relative.as_posix()}\n')
        (destination / 'CARD-SHA256SUMS.txt').write_text(''.join(sorted(asset_sums)), encoding='utf-8')
        return destination
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--card-root', required=True, help='read-only root of the mounted Switch SD card')
    parser.add_argument('--output-root', default='/home/intpa/smash-training-lobby/artifacts/phase23/card-lineup')
    parser.add_argument('--stamp', help='snapshot stamp (default: current UTC minute)')
    args = parser.parse_args(argv)
    print(snapshot(args.card_root, args.output_root, args.stamp))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
