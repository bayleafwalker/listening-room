#!/usr/bin/env python3
"""The listening-hub command: rofi menus and CLI actions over the player core.

The player core lives in `player_core`; its public names are re-exported here
for the package, roomctl and the tests.
"""
import argparse
import html
import json
import os
import shutil
import subprocess
import sys

from player_core import (  # noqa: F401  (re-exported)
    BUILTIN_SCENES, CATALOG, COUNTRY_CODES, PREFERENCE_KEYS, RUNTIME, SOCKET, STATE, HubError,
    all_info, ambience_module, clean, ensure_baseline, favorites, ipc, load_json, mute, notify, play,
    play_scene, playback_url, player_command, player_info, player_volume, players, preferences,
    private_favorite, profiles, radio_audio_state, radio_info, radio_socket, radio_unit, recent_search,
    remove_favorite, restore_profile, run, save_favorite, save_json, save_profile, save_scene, scenes,
    search_radio, selected_target, set_volume, sources, start_radio, state, system_volume, target,
    transport, update_state, valid_url, volume_value, youtube_video_url)


def inspect_state():
    """The `inspect` snapshot, also read by the Players page."""
    ensure_baseline()
    infos = all_info()
    for entry in infos:
        entry['volume'] = player_volume(entry['id'])
        entry['can_next'] = entry['id'] not in ('radio', 'video')
        entry['can_previous'] = entry['id'] not in ('radio', 'video')
    selected = target(infos)
    try:
        ambience = ambience_module().state('ambience')
        video = ambience_module().state('video')
    except (ImportError, OSError, HubError, AttributeError):
        ambience = video = None
    return {'players': infos, 'selected': selected, 'selected_id': selected['id'] if selected else None,
            'system_volume': system_volume(), 'preferences': preferences(), 'sources': sources(),
            'favorites': favorites(), 'profiles': [{'id': k, 'name': k} for k in sorted(profiles())],
            'scenes': scenes(), 'ambience': ambience, 'video': video}

def choose(prompt, options, custom=False):
    if not options and not custom:
        raise HubError('No entries available')
    labels = [clean(label) for label, _ in options]
    args = ['rofi', '-dmenu', '-i', '-p', prompt]
    if not custom:
        # Return the original row index: directory stations can legitimately
        # share a name and country, but must remain individually selectable.
        args += ['-no-custom', '-format', 'i']
    p = run(args, check=False, timeout=None, input='\n'.join(labels) + '\n')
    if p.returncode:
        return None
    answer = p.stdout.rstrip('\n')
    if custom:
        return answer
    try:
        index = int(answer)
        return options[index][1] if 0 <= index < len(options) else None
    except ValueError:
        return None

def station_menu(entries):
    if not entries:
        notify('No stations found')
        return
    item = choose('Station', [(clean(x['name']) + (' — ' + clean(x.get('country')) if x.get('country') else ''), x) for x in entries])
    if item:
        station_actions(item)

def station_actions(item):
    action = choose(clean(item['name']), [('Play', 'play'), ('Save favorite', 'save'), ('Remove favorite', 'remove')] if item['id'].startswith('favorite-') else [('Play', 'play'), ('Save favorite', 'save')])
    if action == 'play':
        play(item)
    elif action == 'save':
        save_favorite(item)
        notify('Saved ' + item['name'])
    elif action == 'remove':
        remove_favorite(item)

def menu():
    entries = sources()
    groups = sorted(set(x['group'] for x in entries))
    action = choose('Listening Hub', [('Open listening room', 'room'), ('Current controls', 'controls'), ('Volume', 'volume'), ('Scenes', 'scenes'), ('Profiles', 'profiles'), ('Select player', 'players'), ('Search world radio', 'search'), ('Custom radio URL', 'custom'), ('Recent search results', 'recent'), ('When starting radio: ' + ('pause other players' if state().get('pause_others_on_radio_start', False) else 'keep other players playing'), 'policy')] + [('Browse: ' + g, ('group', g)) for g in groups] + [(clean(x['name']) + '  [' + clean(x['group']) + ']  ' + ' '.join(clean(t) for t in x.get('tags', []) if isinstance(t, str)), ('source', x)) for x in entries])
    if not action:
        return
    if isinstance(action, tuple) and action[0] == 'source':
        item = action[1]
        station_actions(item) if item['kind'] == 'radio' else play(item)
    elif isinstance(action, tuple):
        items = [x for x in entries if x['group'] == action[1]]
        item = choose(action[1], [(x['name'], x) for x in items])
        if item:
            station_actions(item) if item['kind'] == 'radio' else play(item)
    elif action == 'room':
        try:
            subprocess.Popen(['listening-room'], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise HubError('Could not open listening room: %s' % e) from e
    elif action == 'volume':
        option = choose('Volume', [('Selected player volume', 'player'), ('Master volume', 'master'), ('Toggle player mute', 'mute'), ('Toggle master mute', 'master-mute')])
        if option in ('player', 'master'):
            level = choose('Volume 0–100 or +5 / -5', [], custom=True)
            if level:
                set_volume(level, option == 'master')
        elif option in ('mute', 'master-mute'):
            mute(option == 'master-mute')
    elif action == 'profiles':
        ensure_baseline()
        option = choose('Profiles', [('Save current settings', 'save')] + [('Restore ' + x, ('restore', x)) for x in sorted(profiles())])
        if option == 'save':
            name = choose('Profile name', [], custom=True)
            if name:
                save_profile(name)
        elif isinstance(option, tuple):
            restore_profile(option[1])
    elif action == 'scenes':
        option = choose('Scenes', [('Save current radio scene', 'save')] + [(x['name'], ('play', x['id'])) for x in scenes()])
        if option == 'save':
            name = choose('Scene name', [], custom=True)
            if name:
                save_scene(name)
        elif isinstance(option, tuple):
            play_scene(option[1])
    elif action == 'players':
        select_player()
    elif action == 'controls':
        current = target(all_info())
        if not current:
            raise HubError('No active player')
        options = [('Play / pause', 'play-pause'), ('Stop', 'stop')]
        if current['id'] != 'radio':
            options += [('Next track', 'next'), ('Previous track', 'previous')]
        command = choose('Control ' + current['id'], options)
        if command:
            transport(current['id'], command)
    elif action == 'policy':
        update_state(pause_others_on_radio_start=not state().get('pause_others_on_radio_start', False))
        notify('Radio start: ' + ('pause other players' if state().get('pause_others_on_radio_start') else 'keep other players playing'))
    elif action == 'search':
        query = choose('Name, country:FI, tag:genre or language:name', [], custom=True)
        if query and clean(query):
            station_menu(search_radio(clean(query)))
    elif action == 'recent':
        station_menu(recent_search())
    elif action == 'custom':
        url = choose('Radio stream URL', [], custom=True)
        if not url:
            return
        if not valid_url(url):
            raise HubError('Radio URL must be HTTP or HTTPS')
        name = choose('Station name', [], custom=True)
        if name:
            station_actions({'id': 'custom', 'name': clean(name), 'kind': 'radio', 'url': url})

def select_player():
    infos = all_info()
    if not infos:
        notify('No active players found')
        return
    selected = choose('Select player', [('%s — %s — %s' % (x['id'], x['status'], x['title']), x['id']) for x in infos])
    if selected:
        update_state(selected=selected)

def status():
    current = target(all_info())
    if not current:
        return {'text': '󰎆', 'tooltip': 'Listening Hub: no active player', 'alt': 'stopped', 'class': 'stopped'}
    artist, title = current['artist'], current['title']
    label = ' — '.join(x for x in (artist, title) if x) or current['id']
    playing = current['status'].lower() == 'playing'
    return {'text': ('▶ ' if playing else 'Ⅱ ') + html.escape(label), 'tooltip': html.escape('%s: %s\n%s' % (clean(current['id']), clean(current['status']), label)), 'alt': 'playing' if playing else 'paused', 'class': 'playing' if playing else 'paused'}

def main():
    parser = argparse.ArgumentParser(description='Control radio and desktop media players')
    parser.add_argument('action', choices=['status', 'menu', 'toggle', 'next', 'previous', 'stop', 'players', 'play', 'doctor', 'config', 'select-player', 'inspect', 'volume', 'master-volume', 'mute', 'master-mute', 'profile', 'scene', 'ambience', 'video'])
    parser.add_argument('id', nargs='?')
    parser.add_argument('value', nargs='?')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'status':
            print(json.dumps(status(), ensure_ascii=False))
        elif args.action == 'menu':
            menu()
        elif args.action == 'inspect':
            print(json.dumps(inspect_state(), ensure_ascii=False))
        elif args.action in ('volume', 'master-volume'):
            if args.id is None:
                raise HubError('Volume value is required')
            set_volume(args.id, args.action == 'master-volume')
        elif args.action in ('mute', 'master-mute'):
            mute(args.action == 'master-mute')
        elif args.action == 'profile':
            if args.id == 'save' and args.value:
                if args.value != 'default':
                    ensure_baseline()
                save_profile(args.value, args.force)
            elif args.id == 'restore' and args.value:
                ensure_baseline()
                restore_profile(args.value)
            else:
                raise HubError('Usage: profile save|restore NAME [--force]')
        elif args.action == 'scene':
            if args.id == 'save' and args.value:
                save_scene(args.value)
            elif args.id == 'play' and args.value:
                play_scene(args.value)
            else:
                raise HubError('Usage: scene save NAME | scene play ID')
        elif args.action in ('ambience', 'video'):
            module = ambience_module()
            mode = args.action
            if args.id == 'start' and args.value:
                preset = next((x for x in module.presets() if x.get('id') == args.value), None)
                module.start(preset or args.value, mode)
                if mode == 'video':
                    update_state(selected='video')
            elif args.id in ('stop', 'toggle'):
                module.control('play-pause' if args.id == 'toggle' else args.id, mode)
            elif args.id in ('here', 'workspace', 'pin'):
                module.place('9' if args.id == 'workspace' else args.id, mode)
            else:
                raise HubError('Usage: %s start PRESET_OR_URL | stop | toggle | here | workspace | pin' % mode)
        elif args.action == 'select-player':
            select_player()
        elif args.action == 'players':
            print(json.dumps(all_info(), ensure_ascii=False))
        elif args.action == 'doctor':
            for name in ('playerctl', 'mpv', 'systemd-run', 'systemctl', 'rofi', 'notify-send', 'xdg-open'):
                print('%s: %s' % (name, shutil.which(name) or 'missing'))
            print('pause others on radio start: %s' % ('on' if state().get('pause_others_on_radio_start', False) else 'off'))
            print('catalogue: %s' % (CATALOG or 'not configured'))
            print('runtime directory: %s' % os.environ.get('XDG_RUNTIME_DIR', 'not configured'))
        elif args.action == 'config':
            if args.id != 'pause-others' or args.value not in ('on', 'off'):
                raise HubError('Usage: listening-hub config pause-others on|off')
            update_state(pause_others_on_radio_start=args.value == 'on')
        elif args.action == 'play':
            item = next((x for x in sources() if x['id'] == args.id), None)
            if not item:
                raise HubError('Source not found: ' + clean(args.id))
            play(item)
        else:
            current = target(all_info())
            if not current:
                if args.action == 'toggle':
                    menu()
                    return 0
                raise HubError('No active player')
            transport(current['id'], 'play-pause' if args.action == 'toggle' else args.action)
    except (HubError, OSError, ValueError, ImportError) as e:
        if args.action == 'status':
            print(json.dumps({'text': '󰎆', 'tooltip': html.escape('Listening Hub: ' + clean(e)), 'alt': 'error', 'class': 'error'}))
        else:
            print('listening-hub: %s' % e, file=sys.stderr)
            notify(str(e))
            return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
