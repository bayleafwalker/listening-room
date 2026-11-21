"""Desktop, launcher and phone entry points for the shared room session."""
import argparse
import html
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import threading

import session_client

METADATA_MAX_BYTES = 32 * 1024 * 1024
METADATA_MAX_RECORDS = 10000
METADATA_BATCH_BYTES = 512 * 1024


def status(snapshot):
    active = snapshot.get('active') or {}
    layers = snapshot.get('layers') or {}
    music = layers.get('music') or {}
    atmosphere = layers.get('atmosphere') or {}
    playing = [item.get('name') or item.get('title') for item in (music, atmosphere)
               if item.get('status') == 'Playing' and (item.get('name') or item.get('title'))]
    station = snapshot.get('station') or {}
    name = station.get('name') or (station.get('definition') or {}).get('name') or active.get('name') or 'Listening room'
    caption = name + (' · ' + ' + '.join(playing) if playing else '')
    return {'text': html.escape(caption), 'tooltip': html.escape(caption + '\nClick for room controls; right-click to browse'),
            'class': 'playing' if playing else 'paused' if active else 'stopped', 'alt': 'room'}


def follow_status(output=None, subscription=None):
    """`status --follow`: one Waybar JSON line whenever the status changes.

    Waybar runs it once with `exec` and no `interval`. It waits for the controller
    and never starts it, like `status`; while it is unreachable the line reads stopped.
    """
    output = output or sys.stdout
    subscription = subscription or session_client.Subscription(fields=['active', 'layers', 'station'])
    last = None
    stop_watching = _close_when_reader_leaves(output, subscription)
    try:
        for update in subscription:
            line = json.dumps(status(update.snapshot or {}), ensure_ascii=False)
            if line != last:
                output.write(line + '\n')
                output.flush()
                last = line
    except BrokenPipeError:   # Waybar closed the pipe; keep shutdown from writing to it again
        if output is sys.stdout:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except KeyboardInterrupt:
        pass
    finally:
        stop_watching()
        subscription.close()
    return 0


def _close_when_reader_leaves(output, subscription):
    """Close `subscription` as soon as the reader of a pipe `output` is gone.

    Writing is the only other way to notice, and an unchanged status writes nothing,
    so an orphaned `status --follow` would keep its subscriber slot forever. Returns a
    function that stops the watch. Outputs without a descriptor, or that are not
    pipes or sockets, have no reader to lose and are not watched.
    """
    try:
        descriptor = output.fileno()
        if not stat.S_ISFIFO(os.fstat(descriptor).st_mode) and not stat.S_ISSOCK(os.fstat(descriptor).st_mode):
            return lambda: None
    except (AttributeError, OSError, TypeError, ValueError):
        return lambda: None
    wake_read, wake_write = os.pipe()
    def watch():
        poller = select.poll()
        poller.register(descriptor, 0)   # error and hang-up are always reported
        poller.register(wake_read, select.POLLIN)
        try:
            for fd, _ in poller.poll():
                if fd == descriptor:
                    subscription.close()
        finally:
            os.close(wake_read)
    thread = threading.Thread(target=watch, name='status-follow-reader', daemon=True)
    thread.start()
    def stop():
        try:
            os.write(wake_write, b'.')
        except OSError:   # the watcher already ended
            pass
        thread.join(1)
        os.close(wake_write)
    return stop


def remote_commands():
    """Importable KDE Connect runcommand definitions; installation stays explicit."""
    return {'listening-room-quiet': {'name': 'Quiet listening room', 'command': 'listening-roomctl quiet'},
            'listening-room-return': {'name': 'Return to last room', 'command': 'listening-roomctl return'},
            'listening-room-northern': {'name': 'Enter Northern Quiet', 'command': 'listening-roomctl enter northern-quiet'},
            'listening-room-library': {'name': 'Enter Midnight Library', 'command': 'listening-roomctl enter midnight-library'},
            'listening-room-stop': {'name': 'Stop listening room', 'command': 'listening-roomctl stop'}}


def menu():
    import listening_hub as hub
    snapshot = session_client.request('inspect')
    choices = [('Return to last room', ('return', {})), ('Quiet room', ('quiet', {})),
               ('Open room', ('open', {}))]
    choices += [(item['name'], ('enter', {'variant_id': item['id']})) for item in snapshot.get('variants') or []]
    choices += [('Station: ' + item['name'], ('station.start', {'id': item['id']}))
                for item in snapshot.get('stations') or []]
    import room_catalogue
    choices += [(item['name'], ('enter', {'room_id': item['id']})) for item in room_catalogue.rooms()]
    selected = hub.choose('Listening room', choices)
    if selected:
        action, args = selected
        if action == 'open':
            subprocess.Popen(['listening-room', '--inside'])
        else:
            session_client.request(action, args)


def popover():
    """The Waybar left-click controls: the mini player component in its own small window."""
    import next_mini
    return next_mini.run_popover([sys.argv[0]])


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog='''Transport: toggle, pause and play act on the whole room, as the window's Play/Pause, MPRIS and the media keys do (pause is quiet; play resumes exactly what room pause paused, or every paused layer, or enters the room again).
music and ambience take pause, play, toggle, stop or next and act on that layer alone; a layer paused on its own stays paused when the room resumes.
Metadata: metadata-list shows a bounded preview; metadata-jobs reports progress.
metadata-enrich accepts {"reference": {"provider": "jellyfin", "type": "Audio", "id": "TRACK_ID"}}.
metadata-cancel accepts {"job_id": "JOB_ID"}; metadata-revoke accepts {"origin": "ORIGIN", "item_id": "TRACK_ID"}.
metadata-review accepts those IDs plus classification (instrumental, vocal, spoken, unknown), reviewer, and acknowledged=true after listening to the whole exact recording. Enrich and review run as jobs. metadata-match and metadata-analyze take the same reference; match accepts method=musicbrainz or acoustid; metadata-confirm-identity takes job_id and provider_id.
metadata-export writes all provenance pages as one JSON document to stdout. metadata-import takes a JSON file path; imported reviews and audio hashes are never trusted. Both allow up to 10,000 records and 32 MiB.''', formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=['inspect','status','enter','return','quiet','resume','toggle','pause','play','music','ambience','leave','stop','save','menu','popover','remote-commands','link','volume','stations','tune','replay','station-save','station-update','play-next','metadata-list','metadata-jobs','metadata-enrich','metadata-match','metadata-analyze','metadata-confirm-identity','metadata-cancel','metadata-review','metadata-revoke','metadata-export','metadata-import'])
    parser.add_argument('value', nargs='?', help='ID or JSON object for the chosen action; metadata-review requires origin, item_id, classification, reviewer, and acknowledged=true')
    parser.add_argument('--follow', action='store_true', help='with status: print a new line whenever the status changes')
    args = parser.parse_args()
    if args.follow and args.action != 'status':
        parser.error('--follow works with status only')
    try:
        if args.action == 'status' and args.follow:
            return follow_status()
        if args.action == 'status':
            try: snapshot = session_client.request('inspect', timeout=.4, start=False)
            except (ValueError, OSError): snapshot = {}
            print(json.dumps(status(snapshot), ensure_ascii=False)); return 0
        if args.action == 'volume':
            # Room volume: the one session level MPRIS and the window share.
            import listening_hub as hub
            current = session_client.request('inspect').get('room_volume')
            if args.value is None:
                print(round(current if current is not None else 100)); return 0
            try: value = hub.volume_value(args.value, current)
            except hub.HubError as error: raise ValueError(str(error)) from error
            session_client.request('room_volume', {'value': value})
            return 0
        if args.action == 'remote-commands':
            print(json.dumps(remote_commands(), indent=2)); return 0
        if args.action == 'menu': menu(); return 0
        if args.action == 'popover': return popover()
        if args.action == 'link':
            if not args.value: raise ValueError('Provide a public media link to preview')
            subprocess.Popen(['listening-room', '--preview-link', args.value]); return 0
        if args.action.startswith('metadata-'):
            action, params = metadata_arguments(args.action, args.value)
            result = metadata_export() if args.action == 'metadata-export' else metadata_import(params) if args.action == 'metadata-import' else session_client.request(action, params)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.action in ('toggle', 'pause', 'play', 'music', 'ambience'):
            action, params = transport_arguments(args.action, args.value)
            print(json.dumps(session_client.request(action, params), ensure_ascii=False))
            return 0
        if args.action in ('stations', 'tune', 'replay', 'station-save', 'station-update', 'play-next'):
            action, params = station_arguments(args.action, args.value)
            print(json.dumps(session_client.request(action, params), ensure_ascii=False))
            return 0
        params = {'room_id': args.value} if args.action == 'enter' else {'name': args.value} if args.action == 'save' else {'minutes': float(args.value)} if args.action == 'quiet' and args.value else {}
        print(json.dumps(session_client.request(args.action, params), ensure_ascii=False))
    except (ValueError, OSError) as error:
        print('listening-roomctl: ' + str(error), file=sys.stderr); return 1
    return 0


LAYER_ACTIONS = ('pause', 'play', 'toggle', 'stop', 'next')


def transport_arguments(action, value=None):
    """Room transport (toggle, pause, play) and one layer's own controls (music|ambience ACTION)."""
    if action in ('toggle', 'pause', 'play'):
        if value:
            raise ValueError('This action takes no value')
        return 'transport', {'action': action}
    if value not in LAYER_ACTIONS:
        raise ValueError('Choose ' + ', '.join(LAYER_ACTIONS[:-1]) + ' or ' + LAYER_ACTIONS[-1] + ' for ' + action)
    return 'layer', {'layer': 'atmosphere' if action == 'ambience' else 'music', 'action': value}


def station_arguments(action, value=None):
    """Shell/phone commands use stable IDs or JSON, never executable snippets."""
    if action == 'stations':
        return 'station.list', {}
    if action in ('tune', 'replay'):
        if not value or len(value) > 160 or any(ord(char) < 32 for char in value):
            raise ValueError('Choose a saved station or evening ID')
        return ('station.start' if action == 'tune' else 'station.replay'), {'id': value}
    if not value:
        raise ValueError('Provide a JSON object for this station action')
    try:
        item = json.loads(value)
    except (ValueError, TypeError):
        raise ValueError('Provide a valid JSON object') from None
    if not isinstance(item, dict):
        raise ValueError('Provide a JSON object')
    if action == 'station-save':
        return 'station.save', {'station': item}
    if action == 'station-update':
        return 'station.update', item
    return 'station.play_next', {'item': item}


def metadata_arguments(action, value=None):
    """Metadata commands accept JSON objects, never paths or executable snippets.

    metadata-list exports the latest records as JSON. Review requires a human
    acknowledgement and the controller rechecks the recording before saving.
    """
    mapping = {'metadata-list': 'metadata.list', 'metadata-jobs': 'metadata.jobs',
               'metadata-enrich': 'metadata.enrich', 'metadata-cancel': 'metadata.cancel',
               'metadata-review': 'metadata.review', 'metadata-revoke': 'metadata.revoke',
               'metadata-match': 'metadata.match', 'metadata-analyze': 'metadata.analyze',
               'metadata-confirm-identity': 'metadata.confirm_identity',
               'metadata-export': 'metadata.export', 'metadata-import': 'metadata.import'}
    if action not in mapping:
        raise ValueError('Unknown metadata action')
    if action in ('metadata-list', 'metadata-jobs', 'metadata-export'):
        if value: raise ValueError('This action takes no value')
        return mapping[action], {}
    if not value:
        raise ValueError('Provide a JSON object for this metadata action')
    if action == 'metadata-import':
        path = Path(value)
        with path.open('rb') as source:
            data = source.read(METADATA_MAX_BYTES + 1)
        if len(data) > METADATA_MAX_BYTES: raise ValueError('Metadata file exceeds 32 MiB')
        try: value = data.decode('utf-8')
        except UnicodeDecodeError: raise ValueError('Metadata file must be UTF-8 JSON') from None
    try: params = json.loads(value)
    except (ValueError, TypeError): raise ValueError('Provide a valid JSON object') from None
    if not isinstance(params, dict): raise ValueError('Provide a JSON object')
    if action == 'metadata-review' and params.get('acknowledged') is not True:
        raise ValueError('Review requires acknowledged=true after listening to the whole exact recording')
    return mapping[action], params


def metadata_export():
    """Collect all bounded controller pages before writing any output."""
    records = []
    cursor = None
    seen = set()
    size = len('{"version": 1, "records": []}'.encode('utf-8'))
    while True:
        page = session_client.request('metadata.export', {'cursor': cursor})
        if not isinstance(page, dict) or page.get('version') != 1 or not isinstance(page.get('records'), list):
            raise ValueError('Controller returned an invalid metadata export')
        for record in page['records']:
            if not isinstance(record, dict):
                raise ValueError('Controller returned an invalid metadata record')
            size += len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8')) + 2
            if size > METADATA_MAX_BYTES or len(records) >= METADATA_MAX_RECORDS:
                raise ValueError('Metadata export exceeds 10,000 records or 32 MiB')
            records.append(record)
        cursor = page.get('next_cursor')
        if cursor is None:
            return {'version': 1, 'records': records}
        if not isinstance(cursor, dict) or not isinstance(cursor.get('origin'), str) or not isinstance(cursor.get('item_id'), str):
            raise ValueError('Controller returned an invalid metadata cursor')
        marker = (cursor['origin'], cursor['item_id'])
        if marker in seen or not page['records']:
            raise ValueError('Controller returned a repeating metadata cursor')
        seen.add(marker)


def metadata_import(document):
    """Send a bounded export in batches; controller validates all observations."""
    if document.get('version') != 1 or not isinstance(document.get('records'), list):
        raise ValueError('Metadata import must be a version 1 export with records')
    records = document['records']
    if len(records) > METADATA_MAX_RECORDS:
        raise ValueError('Metadata import exceeds 10,000 records')
    batches, batch, size = [], [], 100
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('Metadata import contains an invalid record')
        encoded = len(json.dumps(record, allow_nan=False).encode('utf-8')) + 2
        if encoded + 100 > METADATA_BATCH_BYTES:
            raise ValueError('A metadata record is too large to import')
        if batch and (len(batch) == 200 or size + encoded > METADATA_BATCH_BYTES):
            batches.append(batch); batch, size = [], 100
        batch.append(record); size += encoded
    if batch: batches.append(batch)
    imported = 0
    for batch in batches:
        try:
            result = session_client.request('metadata.import', {'records': batch})
            count = result.get('imported') if isinstance(result, dict) else None
            if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > len(batch):
                raise ValueError('Controller returned an invalid import result')
            imported += count
        except (ValueError, OSError) as error:
            raise ValueError('Metadata import stopped after at least ' + str(imported) + ' records; the last batch may be partially imported: ' + str(error)) from error
    return {'imported': imported}


if __name__ == '__main__':
    raise SystemExit(main())
