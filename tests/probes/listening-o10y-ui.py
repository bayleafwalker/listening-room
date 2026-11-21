#!/usr/bin/env python3
"""O10 YouTube implementer probe: Watch (with sound), Show as scene (silent) and the shelves.

Real GTK on a private Xvfb and D-Bus (o6_env), the real session controller on the session test
double with a fake Watch window, and a fake yt-dlp runner. Nothing is downloaded or played.
Writes screenshots and o10y-ui.json (check name, pass, detail) to --output.
"""
import json
import time

import o6_env

env = o6_env.Env(__doc__, default_output='/tmp/listening-o10y-review')
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append({'criterion': name, 'pass': bool(ok), 'detail': str(detail)[:500]})
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ': ' + str(detail)[:500]), flush=True)


def mutating(since):
    return [entry for entry in env.commands(since) if entry[0] not in ('inspect', 'get', 'station.list', 'station.recent',
                                                                         'variants', 'metadata.list')]


with env:
    Gtk = env.Gtk
    video = {'state': None}
    calls = []

    def start_video(item, placement='here', display=None, cancel=None):
        calls.append(('start_video', item['url']))
        video['state'] = {'status': 'Playing', 'title': item['name'], 'ended': False}

    def stop_video():
        calls.append(('stop_video',))
        video['state'] = None

    env.backend.start_video = start_video
    env.backend.stop_video = stop_video
    env.backend.video_state = lambda: dict(video['state']) if video['state'] else None

    def scope_to(page, key):
        keys = [k for k, _t in page.scope_list]
        page.scopes.set_active(keys.index(key))
        env.pump(.6)

    def search(page, text, wait=1.2):
        page.search.set_text(text)
        page.query_submitted()
        env.pump(wait)

    def menu_words(root, index=0):
        menus = [w for w in env.widgets(root) if isinstance(w, Gtk.MenuButton) and w.get_mapped()]
        if len(menus) <= index:
            return []
        menus[index].popup()
        env.pump(.3)
        words = [b.get_label() for b in env.widgets(menus[index].get_popover()) if isinstance(b, Gtk.Button)]
        menus[index].popdown()
        return words

    def toasts(window):
        said = []
        original = window.toast

        def record(title, undo_label=None, undo=None, urgent=False):
            said.append((title, undo_label, undo))
            return original(title, undo_label, undo, urgent)
        window.toast = record
        return said

    def watch_buttons(root):
        return [w for w in env.widgets(root) if isinstance(w, Gtk.Button) and w.get_mapped()
                and w.get_tooltip_text() == 'Watch with sound']

    # ---- shelves before typing, with a catalogue room ------------------------------------
    env.service.execute('enter', {'room_id': 'midnight-library'})
    env.settle(1.2)
    check('0 a catalogue room is playing', env.controller.active and env.backend.state['music']['status'] == 'Playing',
          env.controller.active)
    window = env.launch()
    said = toasts(window)
    page = window.listen
    mark, queries = len(env.log), len(env.yt_queries)
    scope_to(page, 'youtube')
    env.pump(1.2)
    texts = set(env.texts(page.index_body))
    check('S1 the YouTube scope shows For Midnight library and Live now before typing',
          {'For Midnight library', 'Live now'} <= texts, sorted(texts)[:30])
    check('S2 it keeps the how-it-works line', any('Nothing plays until' in t for t in texts))
    check('S3 the shelves were fetched off the GTK thread through yt-dlp (room queries and the live filter)',
          any('ytsearch' in q and 'dark academia' in q for q in env.yt_queries[queries:])
          and any('sp=EgJAAQ' in q for q in env.yt_queries[queries:]), env.yt_queries[queries:])
    check('S4 browsing the shelves sent no command', not mutating(mark), mutating(mark))
    check('S5 shelf rows say Play, never Tune in, and each has Watch',
          env.buttons(page.index_body, 'Play') and not env.buttons(page.index_body, 'Tune in')
          and len(watch_buttons(page.index_body)) == len(env.buttons(page.index_body, 'Play')))
    check('S6 a shelf says when it was updated', any(t.startswith('Updated') for t in texts), sorted(texts)[:30])
    cache = __import__('youtube_shelves').cache_path()
    check('S7 the shelves are cached on disk', cache.is_file() and 'live' in json.loads(cache.read_text()))
    env.shot(window, 'o10y-01-youtube-shelves')
    window.close()
    env.pump(.4)
    queries = len(env.yt_queries)
    window = env.launch()
    said = toasts(window)
    page = window.listen
    scope_to(page, 'youtube')
    env.pump(.3)
    texts = set(env.texts(page.index_body))
    check('S8 a new window shows the cached shelves at once, without a refresh',
          'Live now' in texts and env.buttons(page.index_body, 'Play') and len(env.yt_queries) == queries,
          (sorted(texts)[:20], env.yt_queries[queries:]))

    # ---- search rows ----------------------------------------------------------------------
    mark = len(env.log)
    search(page, 'rain')
    check('R1 every result has Play and Watch, none says Tune in',
          len(env.buttons(page.index_body, 'Play')) == 3 and len(watch_buttons(page.index_body)) == 3
          and not env.buttons(page.index_body, 'Tune in'))
    words = menu_words(page.index_body)
    check('R2 the row menu: Show as scene, Play as ambience, Save as station, Open elsewhere',
          words == ['Show as scene', 'Play as ambience', 'Save as station', 'Open elsewhere'], words)
    check('R3 searching is inert', not mutating(mark), mutating(mark))
    env.shot(window, 'o10y-02-youtube-results')

    # ---- Watch with a room ----------------------------------------------------------------
    mark = len(env.log)
    watch_buttons(page.index_body)[0].emit('clicked')
    env.settle(1.0)
    sent = mutating(mark)
    check('W1 Watch sends one video start with the video URL',
          [c[0] for c in sent] == ['video'] and sent[0][1].get('action') == 'start'
          and 'watch?v=aaaaaaaaaaa' in sent[0][1].get('url', ''), sent)
    check('W2 the Watch window opened (fake mpv) and the room paused like Quiet, the scene kept',
          calls[-1:] == [('start_video', 'https://www.youtube.com/watch?v=aaaaaaaaaaa')]
          and env.backend.state['music']['status'] == 'Paused' and ('picture', 'pause', None) not in env.backend.calls,
          (calls, env.backend.state['music']))
    check('W3 Now shows Watching · title with Stop watching',
          window.current_page() == 'now' and window.watching.chip.get_mapped()
          and env.labels(window, 'Watching · Rain on a tin roof, 3 hours') and env.buttons(window, 'Stop watching'),
          (window.current_page(), sorted(t for t in env.texts(window) if 'atch' in t)))
    env.shot(window, 'o10y-03-watching')
    mark = len(env.log)
    env.press(window, 'Stop watching')
    env.settle(1.0)
    sent = mutating(mark)
    check('W4 Stop watching sends video stop', [c[0] for c in sent] == ['video'] and sent[0][1] == {'action': 'stop'}, sent)
    check('W5 stopping offers Resume and never resumes on its own',
          any(t.startswith('Resume') and u == 'Resume' for t, u, _ in said)
          and env.backend.state['music']['status'] == 'Paused' and not window.watching.chip.get_visible(),
          (said[-3:], env.backend.state['music']))
    resume = next(cb for t, u, cb in reversed(said) if u == 'Resume')
    resume()
    env.settle(1.0)
    check('W6 Resume plays the room again', env.backend.state['music']['status'] == 'Playing')
    window.show_listen()
    env.pump(.5)
    watch_buttons(page.index_body)[1].emit('clicked')
    env.settle(.8)
    video['state'] = None          # the listener closes the window
    env.pump(1.0)
    check('W7 closing the window ends watching and offers Resume',
          not window.watching.chip.get_visible() and said and said[-1][1] == 'Resume'
          and env.backend.state['music']['status'] == 'Paused', said[-2:])
    env.service.execute('resume', {})
    env.settle(.5)

    # ---- Show as scene ------------------------------------------------------------------
    window.show_listen()
    env.pump(.5)
    mark, opened = len(env.log), len(calls)
    menus = [w for w in env.widgets(page.index_body) if isinstance(w, Gtk.MenuButton) and w.get_mapped()]
    menus[0].popup()
    env.pump(.3)
    next(b for b in env.widgets(menus[0].get_popover()) if isinstance(b, Gtk.Button) and b.get_label() == 'Show as scene').emit('clicked')
    env.settle(1.0)
    sent = mutating(mark)
    check('C1 Show as scene sends one link command to the picture layer',
          [c[0] for c in sent] == ['link'] and sent[0][1].get('destination') == 'picture', sent)
    check('C2 only the picture changed; music still plays and no video window opened',
          env.backend.state['music']['status'] == 'Playing' and env.backend.state['picture'].get('url', '').startswith('https://www.youtube.com/')
          and len(calls) == opened,
          (env.backend.state, calls))
    check('C3 the room is marked Changed, with Save as my room',
          window.changed_chip.get_visible() and any(u == 'Save as my room' for _t, u, _cb in said[-2:]), said[-2:])
    window.show_now()
    env.shot(window, 'o10y-04-scene-changed')

    # ---- the link sheet -------------------------------------------------------------------
    window.show_listen()
    env.pump(.3)
    page.pasted('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    env.pump(.7)
    sheet = window.get_visible_dialog()
    offered = [b.get_label() for b in env.widgets(sheet) if isinstance(b, Gtk.Button) and b.get_mapped()
               and b.get_label() and b.get_label() != 'Cancel'] if sheet else []
    check('L1 the YouTube link sheet: Play, Watch, Show as scene, Play as ambience, Open elsewhere',
          offered == ['Play', 'Watch', 'Show as scene', 'Play as ambience', 'Open elsewhere'], offered)
    env.shot(window, 'o10y-05-link-sheet')
    mark = len(env.log)
    if sheet:
        next(b for b in env.widgets(sheet) if isinstance(b, Gtk.Button) and b.get_label() == 'Watch').emit('clicked')
    env.settle(.8)
    sent = mutating(mark)
    check('L2 Watch in the sheet starts the video', [c[0] for c in sent] == ['video'] and 'dQw4w9WgXcQ' in sent[0][1]['url'], sent)
    env.service.execute('video', {'action': 'stop'})
    env.settle(.4)

    # ---- ambience mode ----------------------------------------------------------------
    env.service.execute('resume', {})
    env.settle(.4)
    window.show_now()
    window.open_mix()
    env.pump(.5)
    window.mix.rows['atmosphere'].change.emit('clicked')
    env.pump(.6)
    scope_to(page, 'youtube')
    search(page, 'rain')
    words = menu_words(page.index_body)
    check('A1 choosing an ambience: the primary is the ambience action, Watch and Play move into the menu',
          env.buttons(page.index_body, 'Play as ambience') and not watch_buttons(page.index_body)
          and not env.buttons(page.index_body, 'Play') and words[:3] == ['Play as music', 'Watch', 'Show as scene'], words)
    env.shot(window, 'o10y-06-ambience-mode')
    mark = len(env.log)
    menus = [w for w in env.widgets(page.index_body) if isinstance(w, Gtk.MenuButton) and w.get_mapped()]
    menus[0].popup()
    env.pump(.3)
    next(b for b in env.widgets(menus[0].get_popover()) if isinstance(b, Gtk.Button) and b.get_label() == 'Watch').emit('clicked')
    env.settle(.8)
    check('A2 Watch from ambience mode leaves the mode and starts the video',
          page.mode == 'music' and [c[0] for c in mutating(mark)] == ['video'], (page.mode, mutating(mark)))
    env.service.execute('video', {'action': 'stop'})
    env.service.execute('stop', {})
    env.settle(.5)

    # ---- no room ------------------------------------------------------------------------
    window.show_listen()
    env.pump(.4)
    scope_to(page, 'youtube')
    search(page, 'fire')
    before = list(env.backend.calls)
    mark = len(env.log)
    watch_buttons(page.index_body)[0].emit('clicked')
    env.settle(.8)
    check('N1 with no room, Watch just plays: one video command, nothing else touched',
          [c[0] for c in mutating(mark)] == ['video'] and env.backend.calls == before and video['state']
          and window.current_page() == 'browse', (mutating(mark), env.backend.calls[len(before):], window.current_page()))

    # ---- saved YouTube -------------------------------------------------------------------
    env.service.execute('station.save', {'station': {'name': 'Rain video', 'sources': [
        {'source_id': 'youtube-video', 'id': 'youtube-video', 'name': 'Rain video', 'kind': 'radio', 'resolver': True,
         'url': 'https://www.youtube.com/watch?v=aaaaaaaaaaa'}]}})
    env.pump(1.0)
    page.search.set_text('')
    env.pump(.8)
    texts = set(env.texts(page.index_body))
    check('Y1 saved YouTube stations show first, with no network', 'Your saved YouTube' in texts and 'Rain video' in texts,
          sorted(texts)[:20])
    env.shot(window, 'o10y-07-saved-youtube')

(env.output / 'o10y-ui.json').write_text(json.dumps(RESULTS, indent=1))
failed = [r for r in RESULTS if not r['pass']]
print('%d checks, %d failed' % (len(RESULTS), len(failed)))
raise SystemExit(1 if failed else 0)
