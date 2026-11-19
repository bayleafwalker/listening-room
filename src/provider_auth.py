"""Explicit Twitch device authorization, with no browser callback or process secrets.

https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow
Device codes live only in memory. No network activity occurs until requested.
"""
import json
import fcntl
import stat
import os
from pathlib import Path
import re
import shlex
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import http_fetch
import twitch_credentials as credentials


class AuthError(ValueError):
    pass


_NoRedirect = http_fetch.NoRedirect


def _post(path, fields):
    endpoint = 'https://id.twitch.tv/oauth2/' + path
    request = urllib.request.Request(endpoint, data=urllib.parse.urlencode(fields).encode(),
        headers={'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with http_fetch.open_url(request, 6, direct=True, refuse=_NoRedirect) as response:
            if response.geturl() != endpoint:
                raise AuthError('Twitch refused an unexpected redirect')
            body = response.read(65537)
        if len(body) > 65536:
            raise AuthError('Twitch returned an invalid authorization response')
        data = json.loads(body)
        if not isinstance(data, dict):
            raise AuthError('Twitch returned an invalid authorization response')
        return data
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            body = error.read(65537)
            data = json.loads(body) if len(body) <= 65536 else {}
        except (ValueError, OSError):
            data = {}
        finally:
            error.close()
        message = data.get('message', '') if isinstance(data, dict) else ''
        if status in (400, 401) and message in ('authorization_pending', 'slow_down'):
            return {'pending': message}
        raise AuthError('Twitch authorization failed; start again and check the client ID') from None
    except AuthError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise AuthError('Twitch authorization could not complete; retry') from None


class TwitchDeviceLogin:
    def __init__(self, client_id):
        if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9]{1,100}', client_id):
            raise AuthError('Enter a valid Twitch application client ID')
        self.client_id = client_id
        self.device_code = None
        self.deadline = 0
        self.interval = 5
        self.next_poll = 0
        self.lock = threading.Lock()
        self.commit_lock = threading.Lock()
        self.cancelled = threading.Event()

    def cancel(self):
        with self.commit_lock:
            self.cancelled.set()

    def begin(self):
        if self.cancelled.is_set():
            raise AuthError('Twitch authorization cancelled')
        result = _post('device', {'client_id': self.client_id, 'scopes': 'user:read:follows'})
        uri, code, device = result.get('verification_uri'), result.get('user_code'), result.get('device_code')
        try:
            parts = urllib.parse.urlsplit(uri)
            safe_uri = (parts.scheme == 'https' and parts.hostname == 'www.twitch.tv' and
                        parts.port in (None, 443) and not parts.username and not parts.password and
                        parts.path == '/activate' and not parts.fragment)
            lifetime, interval = int(result['expires_in']), int(result.get('interval', 5))
        except (ValueError, TypeError, KeyError):
            safe_uri = False
        if (not safe_uri or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,32}', code)
                or not credentials._valid_value(device) or not 0 < lifetime <= 3600 or not 1 <= interval <= 60):
            raise AuthError('Twitch returned an invalid authorization response')
        if self.cancelled.is_set():
            raise AuthError('Twitch authorization cancelled')
        self.device_code, self.deadline, self.interval = device, time.monotonic() + lifetime, interval
        self.next_poll = time.monotonic() + interval
        return {'url': uri, 'code': code}

    def finish(self):
        if not self.lock.acquire(blocking=False):
            return {'pending': True}
        try:
            return self._finish()
        finally:
            self.lock.release()

    def _finish(self):
        if self.cancelled.is_set():
            raise AuthError('Twitch authorization cancelled')
        if not self.device_code or time.monotonic() >= self.deadline:
            self.device_code = None
            raise AuthError('Twitch authorization expired; start again')
        if time.monotonic() < self.next_poll:
            return {'pending': True}
        result = _post('token', {'client_id': self.client_id, 'device_code': self.device_code,
                                 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                                 'scopes': 'user:read:follows'})
        if result.get('pending'):
            if result['pending'] == 'slow_down':
                self.interval += 5
            self.next_poll = time.monotonic() + self.interval
            return {'pending': True}
        access, refresh = result.get('access_token'), result.get('refresh_token')
        if (not credentials._valid_value(access) or not credentials._valid_value(refresh) or
                str(result.get('token_type', '')).lower() != 'bearer'):
            raise AuthError('Twitch returned an invalid authorization response')
        self._save(access, refresh)
        self.device_code = None
        return {'connected': True}

    def _save(self, access, refresh):
        path = credentials._paths()[0]
        lock = None
        temp = None
        try:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            meta = path.parent.stat(follow_symlinks=False)
            if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
                raise AuthError('The Twitch credentials directory must be private and owned by you')
            lock = os.open(path.parent / '.listening-room-refresh.lock',
                           os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            meta = os.fstat(lock)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_nlink != 1:
                raise AuthError('The Twitch renewal lock must be private and owned by you')
            deadline = time.monotonic() + 5
            while True:
                if self.cancelled.is_set():
                    raise AuthError('Twitch authorization cancelled')
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AuthError('Another Twitch login update is running; retry') from None
                    time.sleep(.05)
            fd, temp = tempfile.mkstemp(prefix='.twitch-', dir=path.parent)
            with os.fdopen(fd, 'w') as output:
                for key, value in (('CLIENTID', self.client_id), ('ACCESSTOKEN', access), ('REFRESHTOKEN', refresh)):
                    output.write(key + '=' + shlex.quote(value) + '\n')
                output.flush()
                os.fsync(output.fileno())
            with self.commit_lock:
                if self.cancelled.is_set():
                    raise AuthError('Twitch authorization cancelled')
                os.replace(temp, path)
                temp = None
        except OSError:
            raise AuthError('Could not save the private Twitch login') from None
        finally:
            if temp is not None:
                Path(temp).unlink(missing_ok=True)
            if lock is not None:
                os.close(lock)


def validate_saved_twitch():
    """Validate a maintained login; renew a revoked/expired saved token once.

    Twitch requires validation at startup and hourly while maintaining a
    session. Returned values deliberately exclude credentials and account data.
    """
    import twitch_channels
    try:
        saved = credentials.load()
        if not saved.access_token:
            return {'state': 'disconnected'}
        def check(value):
            data = twitch_channels._validate_token(value.access_token)
            if data.get('client_id') != value.client_id or (not isinstance(data.get('expires_in'), int) or isinstance(data.get('expires_in'), bool)) or data['expires_in'] <= 0:
                raise AuthError('Twitch returned an invalid login validation response; reconnect')
            scopes = data.get('scopes', [])
            if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
                raise AuthError('Twitch returned an invalid login validation response; reconnect')
            return 'user:read:follows' in scopes
        try:
            follows = check(saved)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code != 401:
                raise AuthError('Twitch login validation is unavailable; retry') from None
            if saved.source == 'environment':
                raise AuthError('Twitch environment login expired; update it and retry') from None
            saved = credentials.refresh(saved.access_token)
            follows = check(saved)
        return {'state': 'connected', 'follows': follows}
    except credentials.CredentialError as error:
        raise AuthError(str(error)) from None
    except AuthError:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        raise AuthError('Twitch login validation is unavailable; retry') from None
    except (OSError, ValueError):
        raise AuthError('Twitch login validation is unavailable; retry') from None
