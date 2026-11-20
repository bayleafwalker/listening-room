"""Block network access to anything but this machine while a suite runs.

``tests/run_suites.py`` installs this in every suite it starts, so a test that forgets to fake
the network fails with a clear error instead of reaching a live service. Loopback addresses,
``localhost`` and Unix sockets stay allowed (tests use local fixture servers and fake mpv sockets).
Probes run through ``scripts/listening-dev.sh`` are not guarded: they re-execute themselves
under Xvfb, so the guard would not follow.
"""
import ipaddress
import socket


class NetworkBlocked(OSError):
    """A test tried to reach a non-loopback address."""


def _local(host):
    if host is None or isinstance(host, bytes) and not host:
        return True
    if isinstance(host, bytes):
        host = host.decode('ascii', 'ignore')
    host = str(host)
    if host in ('', 'localhost', 'localhost.localdomain') or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host.split('%')[0]).is_loopback
    except ValueError:
        return False


def _check(address):
    if isinstance(address, tuple) and address and not _local(address[0]):
        raise NetworkBlocked('test network access blocked (fake it): %r' % (address[0],))


def install():
    real_getaddrinfo = socket.getaddrinfo
    real_connect, real_connect_ex, real_sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto

    def getaddrinfo(host, *args, **kwargs):
        if not _local(host):
            raise NetworkBlocked('test network lookup blocked (fake it): %r' % (host,))
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        _check(args[-1] if args else None)
        return real_sendto(self, data, *args)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
