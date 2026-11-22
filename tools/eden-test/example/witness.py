import time

import routes


def end_state(ctx):
    io = routes.BridgePickerIO(ctx)
    sample = routes._tr_current(io, time.monotonic() + 6)
    keys = ('open', 'game', 'game.running')
    if any(key not in sample.values for key in keys):
        raise RuntimeError('tr.menu end-state fields are missing')
    return routes._save(ctx, 'end-state', {key: sample.values[key] for key in keys})
