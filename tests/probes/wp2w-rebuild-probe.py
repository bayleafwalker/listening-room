"""Rebuild counts per window section, for a poll that changes nothing and for one-section changes.

Implementation-agnostic: it tags the widgets each section shows before and after
a poll (Rooms cards, each Listen shelf) and counts json.dumps calls made by the Rooms and Listen
modules, so the same probe measures the window before and after the partial-rebuild change.
Real GTK on a private Xvfb and D-Bus with the o6 fixtures; nothing reaches the network or the desktop.
Writes rebuilds.json to --output.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import o6_env  # noqa: E402

ENV = o6_env.Env(__doc__, default_output='/tmp/listening-wp2w-rebuilds')


def children(widget):
    found, child = [], widget.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def shelf_name(env, shelf):
    texts = [t for t in env.texts(shelf)]
    return texts[0] if texts else type(shelf).__name__


_MARKS = iter(range(1, 1 << 30))


def mark(widget):
    """A stable tag per widget (Python ids of GObject wrappers are not stable); a rebuilt widget gets a new one."""
    if not hasattr(widget, '_rebuild_mark'):
        widget._rebuild_mark = next(_MARKS)
    return widget._rebuild_mark


def sections(env, window):
    """{section: tuple of widget tags}; a changed tuple means that section was rebuilt."""
    shown = {'rooms.cards': tuple(mark(c) for c in children(window.rooms.cards))}
    for shelf in children(window.listen.index_body):
        shown['listen.' + shelf_name(env, shelf)] = tuple(mark(w) for w in env.widgets(shelf))
    return shown


def own_counts(window):
    """The window's own draw counters, where it has them (the partial-rebuild change adds them)."""
    found = {}
    for prefix, owner in (('window.', window), ('rooms.', window.rooms), ('listen.', window.listen)):
        for key, value in (getattr(owner, 'rebuilds', None) or {}).items():
            found[prefix + key] = value
    return found


class CountingJson:
    def __init__(self, real):
        self.real, self.calls = real, 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def dumps(self, *args, **kwargs):
        self.calls += 1
        return self.real.dumps(*args, **kwargs)


def main():
    with ENV as env:
        import next_listen
        import next_rooms
        counters = {}
        for module in (next_rooms, next_listen):
            if hasattr(module, 'json'):
                counters[module.__name__] = module.json = CountingJson(module.json)
        env.start_station_room()
        variant = env.controller.dispatch('save', {'name': 'Probe Den'})['variant']
        env.service.execute('leave', {})
        env.settle(1.0)
        window = env.launch(page='listen')
        window.show_rooms()
        env.pump(.8)
        window.show_listen()
        env.pump(.8)
        results = {}

        def measure(name, change=None, polls=5):
            env.settle(.6)
            for counter in counters.values():
                counter.calls = 0
            renders = [0]
            real_render = window.render

            def counted(snapshot):
                renders[0] += 1
                return real_render(snapshot)
            window.render = counted
            before = sections(env, window)
            rebuilt = {key: 0 for key in before}
            draws = own_counts(window)
            if change:
                change()
            for _ in range(polls):
                window.poll()
                env.pump(.35)
                now = sections(env, window)
                for key in set(now) | set(before):
                    if now.get(key) != before.get(key):
                        rebuilt[key] = rebuilt.get(key, 0) + 1
                before = now
            del window.render
            after = own_counts(window)
            results[name] = {'polls_rendered': renders[0], 'rebuilt': rebuilt,
                             'window_counters': {key: after[key] - draws.get(key, 0) for key in after
                                                 if after[key] != draws.get(key, 0)},
                             'json_dumps': {key: value.calls for key, value in counters.items()}}

        measure('no change')
        station = dict(o6_env.RADIO_STATION, name='Probe evening')
        measure('a station saved (Listen: Your stations)',
                lambda: env.service.execute('station.save', {'station': station}))
        measure('a saved room renamed (Rooms: cards)',
                lambda: env.service.execute('rename', {'id': variant['id'], 'name': 'Probe Study'}))
        env.start_station_room()
        window.poll()
        env.pump(1.0)
        measure('no change, a room playing')
        measure('room volume changed (Now, Mix, bars)',
                lambda: env.service.execute('room_volume', {'value': 55}))
        out = env.output / 'rebuilds.json'
        out.write_text(json.dumps(results, indent=2, sort_keys=True) + '\n')
        print(json.dumps(results, indent=2, sort_keys=True))
        window.close()
        env.pump(.3)


if __name__ == '__main__':
    started = time.monotonic()
    main()
    print('probe took %.1fs' % (time.monotonic() - started))
