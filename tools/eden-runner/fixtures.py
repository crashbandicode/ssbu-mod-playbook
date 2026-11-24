"""Shared setup fixtures, inert for stage A."""
GIVES = {}

def recording_role(ctx, role, where="page", scene="training"):
    return {}

def guard_first_use(ctx, scene, game="guard_edge"):
    return {}

def mark_before_room(ctx):
    return {}

def seeded_room(ctx, behavior, stage, partners=1, mark_row=None):
    return {}

def seed_settings(base, behavior, stage, partners=1):
    return {}

def window_from_wait(samples, entry, frames):
    return []
