"""Release assets for ssbu-desync-detector v0.2.0 and ssbu-refresh-clock v0.1.0,
in the shape of their previous releases: the NRO, an SD-card zip, manifest.json
and SHA256SUMS.txt.

make_assets.py <repo> <out_dir> <params.json>
params: {"eden": "...", "switch": "...", "installed_on_switch": bool}
"""
import hashlib
import json
import os
import struct
import subprocess
import sys
import zipfile

repo, out, params_path = sys.argv[1:4]
params = json.load(open(params_path))
root = f'/home/intpa/{repo}'
DEST = 'atmosphere/contents/01006A800016E000/romfs/skyline/plugins/'
STAMP = (2026, 10, 31, 0, 0, 0)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.run(['git', '-C', root, *args], check=True, capture_output=True, text=True).stdout.strip()


def nro_segments(data):
    text, ro, rw = (struct.unpack_from('<II', data, 0x20 + 8 * i)[1] for i in range(3))
    return {'text': text, 'ro': ro, 'data': rw, 'bss': struct.unpack_from('<I', data, 0x38)[0]}


def elf_sections(data):
    shoff, = struct.unpack_from('<Q', data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 0x3A)
    def header(i):
        return struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * shentsize)
    strtab = header(shstrndx)
    names = data[strtab[4]:strtab[4] + strtab[5]]
    sizes = {}
    for i in range(shnum):
        h = header(i)
        name = names[h[0]:names.index(b'\0', h[0])].decode()
        sizes[name] = h[5]
    return {k.lstrip('.').replace('.', '_'): sizes[k]
            for k in ('.text', '.rodata', '.rela.dyn', '.data', '.bss') if k in sizes}


def write_zip(path, members):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)


os.makedirs(out, exist_ok=False)
version = git('describe', '--tags', '--exact-match', 'HEAD').lstrip('v')
tag = f'v{version}'
commit = git('rev-parse', 'HEAD')
assert git('status', '--porcelain') == '', 'repo not clean'
name = 'lib' + repo.replace('-', '_') + '.nro'
nro_path = f'{root}/target/aarch64-skyline-switch/release/{name}'
nro = open(nro_path, 'rb').read()
elf = open(nro_path[:-4] + '.so', 'rb').read()
open(f'{out}/{name}', 'wb').write(nro)

if repo == 'ssbu-desync-detector':
    readme_name = 'SSBU-DESYNC-DETECTOR-README.txt'
    readme = f"""ssbu-desync-detector {tag}
Skyline plugin for Super Smash Bros. Ultimate 13.0.5

Records what ended an online match and keeps the evidence a vanilla replay
needs to show which console desynced. Read-only: it changes no game state.

INSTALL
  Extract this zip to the root of your SD card. It adds exactly one file:

    {DEST}{name}

  Requirements: Atmosphere (or the Eden emulator), Skyline, Smash 13.0.5.
  No game data is included or needed. The plugin checks the game version and
  every hook site at boot and installs nothing if either differs.

OUTPUT
  sd:/ultimate/ssbu-desync-detector/ holds events.log and per-session
  frames-N.bin files; match-N.bin recordings are kept only for matches that
  did not end normally, and the log is capped, so it stays small. How to read
  them, and how to play a recording back in an unmodded game and name the
  console that left vanilla, is in the README of the repository.

TEST STATUS
  0.2.0 is 0.1.0's code built with link-time optimization, which the build
  had skipped: about 430 KiB less memory on the console.
  {params['eden']}
  {params['switch']}

SOURCE AND RELEASES
  https://git.syntaxbyexample.com/intpa/ssbu-desync-detector
  License: MIT.
"""
else:
    readme_name = 'README.txt'
    readme = f"""ssbu-refresh-clock {version} - experimental release
Normal-speed gameplay at 120 Hz / 60 FPS was operator-confirmed on 2026-10-27
with the 0.0.2-dev build; 0.1.0 is that code built with LTO (about 400 KiB
less memory). {params['eden']} {params['switch']}

Fully exit Smash and back up your plugins before copying this SD layout.
The only plugin added is {DEST}{name}
Requires Skyline and the already configured 120Hz/60FPS setup.
Targets Smash 13.0.5 / Erista / SaltyNX 2.1.2 / FPSLocker 3.5.0.
No diagnostic flags are installed automatically.
Rollback: exit Smash, remove this added NRO, relaunch.
Source, diagnostics and acceptance details: https://git.syntaxbyexample.com/intpa/ssbu-refresh-clock
"""
zip_name = f'{repo}-{tag}-sd.zip'
members = [(readme_name, readme.encode()), (DEST + name, nro)]
write_zip(f'{out}/{zip_name}', members)
zip_sha = sha(open(f'{out}/{zip_name}', 'rb').read())

if repo == 'ssbu-desync-detector':
    toolchain = open(f'{root}/rust-toolchain.toml').read()
    channel = toolchain.split('channel', 1)[1].split('"')[1] if 'channel' in toolchain else 'unknown'
    manifest = {
        'tag': tag, 'commit': commit, 'crate_version': version, 'features': 'default',
        'host_toolchain': f'{channel} (rust-toolchain.toml)', 'cargo_skyline': params.get('cargo_skyline', ''),
        'skyline_crate': '0.6.0', 'built_utc': params['built_utc'],
        'nro': name, 'nro_size': len(nro), 'nro_sha256': sha(nro), 'elf_sha256': sha(elf),
        'sd_zip': zip_name, 'sd_zip_sha256': zip_sha,
        'sd_zip_members': {n: sha(d) for n, d in members},
        'nro_segments': nro_segments(nro), 'elf_sections': elf_sections(elf),
        'previous_release': {'tag': 'v0.1.0', 'nro_sha256': '95e7def98090b4368851182e5952306bd26105fd8d6d4f57da65ecece61e1db2',
                             'change': 'crate-type cdylib only, so the release profile LTO applies; code unchanged'},
        'gates': {'cargo_test': '23 passed (10 unit, 13 integration)', 'cargo_clippy_all_targets': 'clean',
                  'cargo_build_no_default_features': 'ok', 'cargo_skyline_build_release': 'ok', 'cargo_skyline_clippy': 'clean'},
        'tests': {'installed_on_switch': params['installed_on_switch'], 'eden': params['eden']},
    }
else:
    files = git('ls-files').split('\n')
    manifest = {
        'project': repo, 'version': version, 'tag': tag, 'commit': commit,
        'repository': f'https://git.syntaxbyexample.com/intpa/{repo}',
        'release': f'https://git.syntaxbyexample.com/intpa/{repo}/releases/tag/{tag}',
        'status': 'experimental release; the 0.0.2-dev code was operator-confirmed at 120 Hz / 60 FPS on 2026-10-27',
        'hardware_accepted': False, 'game_version': '13.0.5', 'hardware_target': 'Erista Switch 1',
        'saltynx_reference': '2.1.2', 'fpslocker_reference': '3.5.0',
        'nro': name, 'nro_bytes': len(nro), 'nro_sha256': sha(nro), 'elf_sha256': sha(elf),
        'nro_segments': nro_segments(nro),
        'zip': zip_name, 'zip_sha256': zip_sha, 'zip_members': [n for n, _ in members],
        'console_destination': DEST + name,
        'source_files': [{'path': f, 'bytes': os.path.getsize(f'{root}/{f}'),
                          'sha256': sha(open(f'{root}/{f}', 'rb').read())} for f in files],
        'tested_candidate': {'build': '0.0.2-dev', 'commit': '07829f2', 'nro_sha256': '80bab7c8cc4a4dfc1200dda9852c84df1ac3cef71c9af4617774396844853a20',
                             'change': 'crate-type cdylib only and the version; src/ unchanged'},
        'local_checks': {'host_tests_passed': 15, 'host_clippy_warnings': 0, 'skyline_clippy_warnings': 0,
                         'no_default_features_build': True, 'skyline_release_build': True},
        'tests': {'installed_on_switch': params['installed_on_switch'], 'eden': params['eden']},
    }
open(f'{out}/manifest.json', 'w').write(json.dumps(manifest, indent=2) + '\n')
sums = [f'{sha(nro)}  {name}', f'{zip_sha}  {zip_name}'] + [f'{sha(d)}  {zip_name}:{n}' for n, d in members]
if repo == 'ssbu-refresh-clock':
    sums = [f'{sha(nro)}  {name}', f'{zip_sha}  {zip_name}', f'{sha(open(f"{out}/manifest.json", "rb").read())}  manifest.json']
open(f'{out}/SHA256SUMS.txt', 'w').write('\n'.join(sums) + '\n')
for f in sorted(os.listdir(out)):
    print(f, os.path.getsize(f'{out}/{f}'))
