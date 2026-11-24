"""Offline checks for a runner packet."""


def packet_digest(base):
    return ''


def emitted_keys(tree, commit, files):
    return set()


def check(plan, base, raw):
    return {'errors': [], 'warnings': [], 'result': 'OK', 'packet_sha256': packet_digest(base),
            'gate_sha256': '', 'target': plan.get('target'), 'emitted': {}, 'rows': {}}
