#!/usr/bin/env python3
"""Create an immutable, timestamped snapshot of the mounted card's Eden lineup.

The card root is read only. Plugins are copied from Atmosphere, every regular file
under ultimate/mods is included, along with ARCropolis configuration (not logs
or caches). Identical payload files from earlier stamps are hard-linked.
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


def arc_config(relative):
    parts = Path(relative).parts
    return bool(parts and parts[0].lower() not in ('cache', 'logs', 'resources')
                and not parts[-1].lower().endswith('.log')
                and (parts[0].lower() == 'config' or parts[-1].lower().endswith('.toml')))


def arc_resource(relative):
    parts = Path(relative).parts
    return bool(parts and all(part.lower() not in ('cache', 'logs') for part in parts)
                and not parts[-1].lower().endswith('.log'))


def _prior_content(output):
    by_digest = {}
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
                by_digest.setdefault(digest(path), path)
    return by_digest


def _stage_file(source, target, by_digest):
    target.parent.mkdir(parents=True, exist_ok=True)
    value = digest(source)
    previous = by_digest.get(value)
    if previous is not None:
        try:
            os.link(previous, target)
            return value
        except FileExistsError:
            raise
        except OSError:
            pass  # Separate volume or filesystem without hard links.
    shutil.copyfile(source, target)
    if digest(target) != value:
        raise ValueError(f'card file changed during copy: {source}')
    target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    by_digest[value] = target
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
    arc_configs = [p for p in _regular_files(arc_src) if arc_config(p.relative_to(arc_src))]
    resources_src = arc_src / 'resources'
    arc_resources = [p for p in _regular_files(resources_src)
                     if arc_resource(p.relative_to(resources_src))]
    output_root.mkdir(parents=True, exist_ok=True)
    by_digest = _prior_content(output_root)
    destination.mkdir(parents=True)
    try:
        plugin_dir = destination / 'plugins'
        plugin_dir.mkdir()
        (destination / 'ultimate/mods').mkdir(parents=True)
        (destination / 'ultimate/arcropolis').mkdir(parents=True)
        plugin_sums, asset_sums = [], []
        for source in plugin_files:
            target = plugin_dir / source.name
            value = _stage_file(source, target, by_digest)
            plugin_sums.append(f'{value}  {target.name}\n')
        (plugin_dir / 'SHA256SUMS.txt').write_text(''.join(plugin_sums), encoding='utf-8')
        for source, base in ((path, mods_src) for path in mods_files):
            relative = Path('ultimate/mods') / source.relative_to(base)
            target = destination / relative
            value = _stage_file(source, target, by_digest)
            asset_sums.append(f'{value}  {relative.as_posix()}\n')
        for source in arc_configs:
            relative = Path('ultimate/arcropolis') / source.relative_to(arc_src)
            target = destination / relative
            value = _stage_file(source, target, by_digest)
            asset_sums.append(f'{value}  {relative.as_posix()}\n')
        for source in arc_resources:
            relative = Path('ultimate/arcropolis/resources') / source.relative_to(resources_src)
            target = destination / relative
            value = _stage_file(source, target, by_digest)
            asset_sums.append(f'{value}  {relative.as_posix()}\n')
        (destination / 'CARD-SHA256SUMS.txt').write_text(''.join(sorted(asset_sums)), encoding='utf-8')
        (destination / 'README.md').write_text(
            f'# Card lineup {stamp}\n\n'
            f'Source root: `{card_root}` (read-only snapshot).\n\n'
            'Includes card plugins, all regular `ultimate/mods/` files, ARCropolis '
            'configuration, and `ultimate/arcropolis/resources/` files. '
            'ARCropolis logs and cache files are excluded. '
            '`CARD-SHA256SUMS.txt` covers all staged mods and ARCropolis files.\n',
            encoding='utf-8')
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
