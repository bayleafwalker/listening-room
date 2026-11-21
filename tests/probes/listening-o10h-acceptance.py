"""Independent acceptance checks for O10-HYGIENE criterion 2 (an unset Familiarity shows "A mix").

Owned by the acceptance author, not the implementer. Behavioural: the real Listen window on private Xvfb/D-Bus, the real
session controller on the session test double, a fixture station. Oracles are the toggle labels the window shows and the
controller's stored station.

    python3 tests/probes/listening-o10h-acceptance.py --repo WORKTREE --output OUTDIR
"""
import copy
import json
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
import o6_env  # noqa: E402  (re-executes this file on Xvfb when there is no private display yet)

ENV = o6_env.Env(__doc__, default_output='/tmp/listening-o10h', extra_args={'--repo': {'default': None}})
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append({'criterion': name, 'pass': bool(ok), 'detail': str(detail)[:600]})
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ': ' + str(detail)[:600]), flush=True)


def shown(window):
    """What each mood row shows: the label of the active toggle, read from the widget."""
    out = {}
    for key, group in window.listen.mood_groups.items():
        index = group.get_active()
        out[key] = group.get_toggle(index).get_label() if 0 <= index < group.get_n_toggles() else None
    return out


def labels_of(window, key):
    group = window.listen.mood_groups[key]
    return [group.get_toggle(i).get_label() for i in range(group.get_n_toggles())]


def station_named(env, name):
    return next(s for s in env.controller.stations() if s['name'] == name)


def run(env):
    base = copy.deepcopy(o6_env.RADIO_STATION)
    base['preferences'] = {}
    window = env.launch(page='listen')
    check('F0 the Familiarity row offers Familiar, A mix and Discover (the labels are unchanged)',
          labels_of(window, 'familiarity') == ['Familiar', 'A mix', 'Discover'], labels_of(window, 'familiarity'))

    # A station with no stored preferences at all.
    env.service.execute('station.save', {'station': dict(base, name='Unset station')})
    station = station_named(env, 'Unset station')
    unset = (station.get('preferences') or {}).get('familiarity')
    window.listen.show_station(station)
    env.pump(1.0)
    rows = shown(window)
    check('F1 an unset Familiarity shows "A mix", not "Familiar"', rows.get('familiarity') == 'A mix', rows)
    check('F1b the other unset moods still show their neutral choice (Energy Any, Voices Any, Range Any)',
          (rows.get('energy'), rows.get('speech'), rows.get('stay_close')) == ('Any', 'Any', 'Any'), rows)
    check('F1c the precondition holds: the fixture station really has no Familiarity stored', unset in (None, 'Any'), unset)

    # Showing is inert: it must not write the station.
    before = json.dumps(station_named(env, 'Unset station'), sort_keys=True)
    env.pump(2.0)
    window.listen.show_station(station_named(env, 'Unset station'))
    env.pump(1.0)
    after = json.dumps(station_named(env, 'Unset station'), sort_keys=True)
    check('F2 merely showing an unset station writes nothing', before == after, (before[:200], after[:200]))

    # The label survives refreshes (polls re-render the page) and does not flicker back.
    seen = []
    for _ in range(4):
        env.pump(.7)
        seen.append(shown(window).get('familiarity'))
    check('F3 the unset Familiarity stays "A mix" across poll re-renders', set(seen) == {'A mix'}, seen)

    # Choosing Familiar from an unset station still stores Familiar, and the page then shows it.
    group = window.listen.mood_groups['familiarity']
    group.set_active(labels_of(window, 'familiarity').index('Familiar'))
    env.settle(1.0)
    stored = (station_named(env, 'Unset station').get('preferences') or {}).get('familiarity')
    check('F4 choosing Familiar on an unset station stores Familiar', stored == 'Familiar', stored)
    check('F4b ... and the row then shows Familiar', shown(window).get('familiarity') == 'Familiar', shown(window))
    group.set_active(labels_of(window, 'familiarity').index('Discover'))
    env.settle(1.0)
    check('F4c choosing Discover stores New and shows Discover',
          (station_named(env, 'Unset station').get('preferences') or {}).get('familiarity') == 'New'
          and shown(window).get('familiarity') == 'Discover', shown(window))
    group.set_active(labels_of(window, 'familiarity').index('A mix'))
    env.settle(1.0)
    check('F4d choosing A mix shows A mix again',
          shown(window).get('familiarity') == 'A mix'
          and (station_named(env, 'Unset station').get('preferences') or {}).get('familiarity') in (None, 'Any'), shown(window))

    # Stored values keep their own labels.
    for stored_value, label in (('Familiar', 'Familiar'), ('Any', 'A mix'), ('New', 'Discover')):
        definition = dict(base, name='Stored ' + stored_value, preferences={'familiarity': stored_value})
        env.service.execute('station.save', {'station': definition})
        window.listen.show_station(station_named(env, 'Stored ' + stored_value))
        env.pump(1.0)
        check('F5 a stored Familiarity of %r shows %r' % (stored_value, label), shown(window).get('familiarity') == label, shown(window))

    # A station with other preferences set but no Familiarity.
    env.service.execute('station.save', {'station': dict(base, name='Energy only', preferences={'energy': 'Low'})})
    window.listen.show_station(station_named(env, 'Energy only'))
    env.pump(1.0)
    rows = shown(window)
    check('F6 a station with only Energy set shows Calm and an unset Familiarity of "A mix"',
          (rows.get('energy'), rows.get('familiarity')) == ('Calm', 'A mix'), rows)


def main():
    try:
        with ENV as env:
            run(env)
    except Exception:
        traceback.print_exc()
        check('acceptance run completed', False, traceback.format_exc()[-600:])
    finally:
        out = Path(ENV.args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'acceptance-o10h.json').write_text(json.dumps({'results': RESULTS}, indent=2))
        failed = [r for r in RESULTS if not r['pass']]
        print(f'{len(RESULTS) - len(failed)} passed, {len(failed)} failed')
        os._exit(1 if failed else 0)


main()
