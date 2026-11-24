#!/usr/bin/env python3
import argparse
import socket
import threading


class ClearPackets:
    def __init__(self):
        self.buffer = bytearray()
        self.identification = False
        self.newkeys = False

    def feed(self, data):
        self.buffer.extend(data)
        output = bytearray()
        if not self.identification:
            while b'\n' in self.buffer:
                end = self.buffer.index(b'\n') + 1
                line = bytes(self.buffer[:end])
                del self.buffer[:end]
                output.extend(line)
                if line.startswith(b'SSH-'):
                    self.identification = True
                    break
            if not self.identification:
                return bytes(output), False
        while not self.newkeys and len(self.buffer) >= 5:
            length = int.from_bytes(self.buffer[:4], 'big')
            if length < 6 or length > 35000:
                raise ValueError('invalid SSH cleartext packet length')
            if len(self.buffer) < length + 4:
                break
            packet = bytes(self.buffer[:length + 4])
            del self.buffer[:length + 4]
            output.extend(packet)
            if packet[5] == 21:
                self.newkeys = True
        if self.newkeys:
            remaining = bytes(self.buffer)
            self.buffer.clear()
            return bytes(output), remaining
        return bytes(output), b''


def bridge(client, target_port, stall):
    server = socket.create_connection(('127.0.0.1', target_port), timeout=3)
    server.settimeout(None)
    reached = {'client': False, 'server': False}
    lock = threading.Lock()

    def copy(source, destination, side):
        parser = ClearPackets()
        try:
            while data := source.recv(65536):
                if not stall:
                    destination.sendall(data)
                    continue
                if parser.newkeys:
                    if side == 'client':
                        destination.sendall(data)
                    continue
                clear, encrypted = parser.feed(data)
                if clear:
                    destination.sendall(clear)
                if parser.newkeys:
                    with lock:
                        reached[side] = True
                        if all(reached.values()):
                            print('BOTH_NEWKEYS', flush=True)
                    if side == 'client' and encrypted:
                        destination.sendall(encrypted)
        except (OSError, ValueError):
            pass
        finally:
            for sock in (source, destination):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    threading.Thread(target=copy, args=(client, server, 'client'), daemon=True).start()
    threading.Thread(target=copy, args=(server, client, 'server'), daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--target', type=int, required=True)
    parser.add_argument('--stall', action='store_true')
    args = parser.parse_args()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', args.port))
        listener.listen()
        print('READY', flush=True)
        while True:
            client, _ = listener.accept()
            try:
                bridge(client, args.target, args.stall)
            except OSError:
                client.close()


if __name__ == '__main__':
    main()
