# Adapted from ssbu-switch-remote tools/card/tests/fake_ftp.py (MIT).
# MIT License
#
# Copyright (c) 2026 intpa
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Loopback FTP server modeling DBI's CWD/NLST and rename behavior.

It handles one serial session, with hooks for command refusals and altered
transfer data. It writes only the test's local scratch directory.
"""
import socket
import socketserver
import threading
from pathlib import Path


class FakeFtp:
    def __init__(self, root, refuse_overwrite=True, support_size=False):
        self.root = Path(root)
        self.support_size = support_size
        self.reply = lambda cmd, path: None
        self.read = lambda path: path.read_bytes()
        self.refuse_overwrite = refuse_overwrite
        self.commands = []
        self.sessions = 0
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            disable_nagle_algorithm = True
            timeout = 5

            def handle(self):
                outer._session(self)

        class Server(socketserver.TCPServer):
            allow_reuse_address = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _session(self, h):
        def say(line):
            h.wfile.write((line + '\r\n').encode())

        self.sessions += 1
        session = self.sessions
        cwd, authed, user, pasv, rnfr = '/', False, None, None, None

        def local(path):
            p = path if path.startswith('/') else (cwd.rstrip('/') + '/' + path)
            parts = []
            for seg in p.split('/'):
                if seg in ('', '.'):
                    continue
                if seg == '..':
                    parts and parts.pop()
                else:
                    parts.append(seg)
            return '/' + '/'.join(parts), self.root.joinpath(*parts) if parts else self.root

        def data_conn():
            conn, _ = pasv.accept()
            pasv.close()
            return conn

        say('220 fake DBI FTP')
        while True:
            raw = h.rfile.readline()
            if not raw:
                if pasv:
                    pasv.close()
                return
            line = raw.decode().rstrip('\r\n')
            cmd, _, arg = line.partition(' ')
            cmd = cmd.upper()
            self.commands.append((session, cmd, None if cmd == 'PASS' else arg))
            if refusal := self.reply(cmd, arg):
                say(refusal)
                continue
            if cmd == 'USER':
                user = arg
                say('331 password')
            elif cmd == 'PASS':
                authed = user == 'anonymous'
                say('230 ok' if authed else '530 login incorrect')
            elif not authed:
                say('530 not logged in')
            elif cmd in ('TYPE', 'NOOP'):
                say('200 ok')
            elif cmd == 'PWD':
                say(f'257 "{cwd}"')
            elif cmd == 'CWD':
                vpath, lp = local(arg)
                if lp.is_dir():
                    cwd = vpath
                    say('250 Directory changed.')
                elif lp.is_file():
                    say('553 Requested action not taken. Not a directory.')
                else:
                    say('550 Requested action not taken. File not available.')
            elif cmd == 'MKD':
                _, lp = local(arg)
                if lp.exists():
                    say('550 file exists')
                else:
                    lp.mkdir()
                    say(f'257 "{arg}"')
            elif cmd == 'PASV':
                pasv = socket.socket()
                pasv.settimeout(5)
                pasv.bind(('127.0.0.1', 0))
                pasv.listen(1)
                port = pasv.getsockname()[1]
                say(f'227 Entering Passive Mode (127,0,0,1,{port >> 8},{port & 255})')
            elif cmd == 'NLST':  # DBI: the argument is ignored
                _, lp = local('.')
                say('150 listing')
                c = data_conn()
                # DBI returns absolute paths (checked on the console 2026-11-16)
                base = local('.')[0].rstrip('/')
                c.sendall(''.join(f'{base}/{p.name}\r\n' for p in sorted(lp.iterdir())).encode())
                c.close()
                say('226 done')
            elif cmd == 'SIZE':
                _, lp = local(arg)
                if not self.support_size:
                    say('502 not implemented')
                elif lp.is_file():
                    say(f'213 {lp.stat().st_size}')
                else:
                    say('550 Requested action not taken. Could not get file size.')
            elif cmd == 'RETR':
                _, lp = local(arg)
                if not lp.is_file():
                    pasv.close()
                    say('550 not found')
                    continue
                say('150 sending')
                c = data_conn()
                c.sendall(self.read(lp))
                c.close()
                say('226 done')
            elif cmd == 'STOR':
                _, lp = local(arg)
                say('150 receiving')
                c = data_conn()
                chunks = []
                while True:
                    b = c.recv(65536)
                    if not b:
                        break
                    chunks.append(b)
                c.close()
                lp.write_bytes(b''.join(chunks))
                say('226 stored')
            elif cmd == 'DELE':
                _, lp = local(arg)
                if lp.is_file():
                    lp.unlink()
                    say('250 deleted')
                else:
                    say('550 not found')
            elif cmd == 'RNFR':
                _, rnfr = local(arg)
                say('350 ready' if rnfr.exists() else '550 not found')
            elif cmd == 'RNTO':
                _, lp = local(arg)
                if lp.exists() and self.refuse_overwrite:
                    say('550 file exists')
                else:
                    rnfr.replace(lp)
                    say('250 renamed')
            elif cmd == 'QUIT':
                say('221 bye')
                return
            else:
                say('502 not implemented')
