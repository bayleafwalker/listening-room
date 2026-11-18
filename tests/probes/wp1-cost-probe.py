"""Print controller hot-path costs per 60 fake seconds (one refresh + one policy poll per second).

    WP1_SRC=<tree>/src python3 tests/probes/wp1-cost-probe.py   # prints JSON; default tree is this checkout

Run it once against the baseline source and once against the change to compare. Counts come from the
rig in tests/wp1_harness.py (fake pactl, open/replace hooks, fake mpv sockets), not from the product.
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wp1_harness as h

SECONDS = 60
PAUSE_AND_DUCK = {'preferred_output': 'sink-a', 'pause_on_disconnect': True, 'duck_on_call': True}
SCENARIOS = {
    'A idle, policies off': dict(),
    'B idle, pause-on-disconnect on': dict(audio_settings={'preferred_output': 'sink-a', 'pause_on_disconnect': True}),
    'C idle, call ducking on': dict(audio_settings={'duck_on_call': True}, controller_settings={'duck_on_call': True}),
    'D playing, policies off': dict(playing=True),
    'E playing, policies off, station active': dict(playing=True, active=True),
    'F playing, pause+duck on, station active': dict(playing=True, active=True, audio_settings=PAUSE_AND_DUCK,
                                                      controller_settings={'duck_on_call': True}),
}


def measure(name, volume_change=False, **kwargs):
    with h.rig(**kwargs) as rig:
        def between(second):
            if volume_change and second == 30:
                rig.players['radio'].properties['volume'] = 61.0
        rig.tick()  # warm-up refresh outside the window (first-run caches)
        for player in rig.players.values():
            player.connections = 0
            player.commands.clear()
        rig.pactl.calls.clear()
        with h.Meter(rig.pactl) as meter:
            rig.run_seconds(SECONDS, between)
        row = {'pactl': dict(rig.pactl.calls), 'pactl_total': sum(rig.pactl.calls.values()),
               'files': dict(sorted(meter.counts.items())),
               'state_json_writes': meter.counts['write state.json'], 'state_json_reads': meter.counts['read state.json'],
               'mpv_connections': rig.connections(), 'mpv_connections_total': sum(rig.connections().values()),
               'mpv_commands_total': sum(rig.commands().values())}
        rig.controller.snapshot()
        return row


if __name__ == '__main__':
    results = {name: measure(name, **kwargs) for name, kwargs in SCENARIOS.items()}
    results['G playing, volume changes once at t=30'] = measure('G', volume_change=True, playing=True)
    print(json.dumps({'source': str(h.SRC), 'seconds': SECONDS, 'scenarios': results}, indent=1))
