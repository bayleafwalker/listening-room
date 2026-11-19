"""Read a private Twitch CLI login and renew it reactively after an HTTP 401.

Explicit environment credentials remain caller-owned. Only the already saved
CLI login can be renewed; its access/refresh pair is rotated atomically without
copying credentials into application settings or displaying response bodies.
"""
from dataclasses import dataclass, field
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import http_fetch

MAX_LOGIN = 128 * 1024
MAX_RESPONSE = 64 * 1024
TOKEN_ENDPOINT = 'https://id.twitch.tv/oauth2/token'
_ASSIGNMENT = re.compile(r'^([ \t]*(?:export[ \t]+)?(CLIENTID|CLIENTSECRET|ACCESSTOKEN|REFRESHTOKEN)[ \t]*=[ \t]*)(.*)$')


class CredentialError(ValueError):
    """A deliberately value-free configuration error suitable for the UI."""
    def __init__(self, message, kind='configuration'):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Credentials:
    client_id: str = field(default='', repr=False)
    access_token: str = field(default='', repr=False)
    source: str = 'none'


def _document(path, dir_fd=None):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        fd = os.open(path, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise CredentialError('Could not open the saved Twitch CLI login') from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise CredentialError('The Twitch CLI login file must be owned by you and private (chmod 600)')
        if metadata.st_size > MAX_LOGIN:
            raise CredentialError('The Twitch CLI login file is invalid')
        raw = bytearray()
        while len(raw) <= MAX_LOGIN:
            block = os.read(fd, min(8192, MAX_LOGIN + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        if len(raw) > MAX_LOGIN:
            raise CredentialError('The Twitch CLI login file is invalid')
        raw = bytes(raw)
        text = raw.decode('utf-8')
        if '\0' in text:
            raise CredentialError('The Twitch CLI login file is invalid')
        values = {}
        for line in text.splitlines():
            match = _ASSIGNMENT.match(line)
            if not match:
                continue
            try:
                parts = shlex.split(match[3], comments=True)
            except ValueError:
                raise CredentialError('The Twitch CLI login file is invalid') from None
            if match[2] in ('CLIENTSECRET', 'REFRESHTOKEN') and parts in ([], ['']):
                values[match[2]] = ''
                continue
            if len(parts) != 1 or not _valid_value(parts[0]):
                raise CredentialError('The Twitch CLI login file is invalid')
            values[match[2]] = parts[0]
        return values, raw, metadata
    except (OSError, UnicodeError):
        raise CredentialError('Could not read the saved Twitch CLI login') from None
    finally:
        if fd is not None:
            os.close(fd)


def _valid_value(value):
    return (isinstance(value, str) and 0 < len(value) <= 16384
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value))


def _read_cli(path):
    document = _document(path)
    return document[0] if document else {}


def _paths():
    config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return (config / 'listening-hub/credentials/twitch.env', config / 'twitch-cli/.twitch-cli.env', Path.home() / '.twitch-cli/.twitch-cli.env')


def _source_name(path):
    return 'Listening Room' if path == _paths()[0] else 'Twitch CLI'


def load():
    """An explicit environment pair wins; otherwise use the CLI's private file.

    Read on demand so CLI token replacement takes effect without a GUI restart.
    A rejected saved access token may be renewed through ``refresh``; loading
    alone never writes the login or makes a network request.
    """
    client_id = os.environ.get('TWITCH_CLIENT_ID', '')
    access_token = os.environ.get('TWITCH_ACCESS_TOKEN', '')
    if client_id or access_token:
        if not client_id or not access_token:
            raise CredentialError('Set both Twitch environment variables, or unset both to use the saved CLI login')
        return Credentials(client_id, access_token, 'environment')
    for path in _paths():
        values = _read_cli(path)
        if values.get('CLIENTID') and values.get('ACCESSTOKEN'):
            return Credentials(values['CLIENTID'], values['ACCESSTOKEN'], _source_name(path))
    return Credentials()


_NoRedirect = http_fetch.NoRedirect


def _renew(values):
    fields = {'grant_type': 'refresh_token', 'client_id': values['CLIENTID'], 'refresh_token': values['REFRESHTOKEN']}
    if values.get('CLIENTSECRET'):
        fields['client_secret'] = values['CLIENTSECRET']
    body = urllib.parse.urlencode(fields).encode('ascii')
    request = urllib.request.Request(TOKEN_ENDPOINT, data=body, method='POST', headers={
        'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json',
        'User-Agent': 'ListeningRoom/2'})
    try:
        with http_fetch.open_url(request, 5, direct=True, refuse=_NoRedirect) as response:
            if response.geturl() != TOKEN_ENDPOINT or response.status != 200:
                raise CredentialError('Twitch renewal returned an unexpected response; retry discovery', 'transient')
            content = bytearray()
            deadline = time.monotonic() + 5
            read = getattr(response, 'read1', response.read)
            while len(content) <= MAX_RESPONSE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CredentialError('Twitch renewal timed out; retry discovery', 'transient')
                transport = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if transport is not None:
                    transport.settimeout(min(remaining, 1))
                block = read(min(8192, MAX_RESPONSE + 1 - len(content)))
                if not block:
                    break
                content.extend(block)
            if len(content) > MAX_RESPONSE:
                raise CredentialError('Twitch renewal returned an invalid response; retry discovery', 'transient')
            data = json.loads(content)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code in (400, 401, 403):
            raise CredentialError('The saved Twitch login can no longer be renewed; reconnect in Connections or through the Twitch CLI', 'reconnect') from None
        if 300 <= code < 400:
            raise CredentialError('Twitch renewal refused a redirect; retry discovery', 'transient') from None
        raise CredentialError('Twitch renewal is temporarily unavailable; retry discovery', 'transient') from None
    except CredentialError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise CredentialError('Twitch renewal could not complete; retry discovery', 'transient') from None
    if (not isinstance(data, dict) or not _valid_value(data.get('access_token'))
            or not _valid_value(data.get('refresh_token'))
            or str(data.get('token_type', 'bearer')).lower() != 'bearer'):
        raise CredentialError('Twitch renewal returned an invalid response; retry discovery', 'transient')
    return data['access_token'], data['refresh_token']


def _rotate(raw, access, refresh_token):
    replacements = {'ACCESSTOKEN': access, 'REFRESHTOKEN': refresh_token}
    lines = []
    found = set()
    for line in raw.decode('utf-8').splitlines(keepends=True):
        ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
        text = line[:-len(ending)] if ending else line
        match = _ASSIGNMENT.match(text)
        if match and match[2] in replacements:
            line = match[1] + shlex.quote(replacements[match[2]]) + ending
            found.add(match[2])
        lines.append(line)
    if found != set(replacements):
        raise CredentialError('The saved Twitch login is incomplete; reconnect in Connections or through the Twitch CLI', 'reconnect')
    result = ''.join(lines).encode('utf-8')
    if len(result) > MAX_LOGIN:
        raise CredentialError('The saved Twitch login is too large to renew safely', 'configuration')
    return result


def refresh(failed_token):
    """Renew only the existing CLI login after its access token received a 401.

    A private advisory lock coalesces callers. The CLI may replace its own file
    without that lock; rereading inode and contents before the atomic swap keeps
    an observed concurrent replacement intact. Every returned error is safe for
    the UI, even when a remote exception includes a response body or secret URL.
    """
    if os.environ.get('TWITCH_CLIENT_ID') or os.environ.get('TWITCH_ACCESS_TOKEN'):
        raise CredentialError('Update the Twitch environment token, or unset both Twitch variables and reconnect in Connections or through the CLI', 'reconnect')
    if not _valid_value(failed_token):
        raise CredentialError('The rejected Twitch login is invalid; reconnect in Connections or through the Twitch CLI', 'reconnect')
    selected = next((path for path in _paths() if (values := _read_cli(path)).get('CLIENTID') and values.get('ACCESSTOKEN')), None)
    if selected is None:
        raise CredentialError('No saved Twitch login is available; reconnect in Connections or through the Twitch CLI', 'reconnect')
    parent, lock, temporary = None, None, None
    try:
        parent = os.open(selected.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise CredentialError('The Twitch CLI login directory must be owned by you and not writable by others')
        lock = os.open('.listening-room-refresh.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=parent)
        metadata = os.fstat(lock)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
            raise CredentialError('The Twitch renewal lock must be a private regular file owned by you')
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CredentialError('Another Twitch renewal is still running; retry discovery', 'transient') from None
                time.sleep(.05)
        document = _document(selected.name, dir_fd=parent)
        if not document or not document[0].get('CLIENTID') or not document[0].get('ACCESSTOKEN'):
            raise CredentialError('The saved Twitch login changed; retry discovery', 'transient')
        values, raw, original = document
        if values['ACCESSTOKEN'] != failed_token:
            return Credentials(values['CLIENTID'], values['ACCESSTOKEN'], _source_name(selected))
        if not values.get('REFRESHTOKEN') or (selected.name != 'twitch.env' and not values.get('CLIENTSECRET')):
            raise CredentialError('This saved Twitch login cannot be renewed; reconnect in Connections or through the Twitch CLI', 'reconnect')
        access, refresh_token = _renew(values)
        updated = _rotate(raw, access, refresh_token)
        current = _document(selected.name, dir_fd=parent)
        unchanged = (current and current[2].st_dev == original.st_dev
                     and current[2].st_ino == original.st_ino and current[1] == raw)
        if not unchanged:
            if (current and current[0].get('CLIENTID') and current[0].get('ACCESSTOKEN')
                    and current[0]['ACCESSTOKEN'] != failed_token):
                return Credentials(current[0]['CLIENTID'], current[0]['ACCESSTOKEN'], _source_name(selected))
            raise CredentialError('The Twitch CLI login changed during renewal; retry discovery', 'transient')
        path_metadata = selected.parent.stat(follow_symlinks=False)
        parent_metadata = os.fstat(parent)
        if ((path_metadata.st_dev, path_metadata.st_ino) != (parent_metadata.st_dev, parent_metadata.st_ino)
                or parent_metadata.st_mode & 0o022):
            raise CredentialError('The Twitch CLI login directory changed; retry discovery', 'transient')
        temporary = '.listening-room-token-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        # Check again after preparing the replacement, immediately before rename.
        current = _document(selected.name, dir_fd=parent)
        if not current or (current[2].st_dev, current[2].st_ino, current[1]) != (original.st_dev, original.st_ino, raw):
            raise CredentialError('The Twitch CLI login changed during renewal; retry discovery', 'transient')
        os.replace(temporary, selected.name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        try:
            os.fsync(parent)
        except OSError:
            pass  # The atomic replacement succeeded; do not report a false failure.
        return Credentials(values['CLIENTID'], access, _source_name(selected))
    except CredentialError:
        raise
    except OSError:
        raise CredentialError('Could not securely renew the saved Twitch login; check its permissions and retry discovery', 'transient') from None
    finally:
        if temporary is not None and parent is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        if lock is not None:
            os.close(lock)
        if parent is not None:
            os.close(parent)
