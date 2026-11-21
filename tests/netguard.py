"""Block network access to anything but this machine while a suite runs.

``tests/run_suites.py`` installs this in every suite it starts, so a test that forgets to fake
the network fails with a clear error instead of reaching a live service. Loopback addresses,
``localhost`` and Unix sockets stay allowed (tests use local fixture servers and fake mpv sockets).
Probes run through ``scripts/listening-dev.sh`` are not guarded: they re-execute themselves
under Xvfb, so the guard would not follow.

Child processes: ``install()`` also puts ``tests/netguard_child`` (a ``sitecustomize``) first on ``PYTHONPATH``
and sets ``LISTENING_NETGUARD=1``, in this process's environment and in any explicit ``env=`` a suite hands to
``subprocess``, so a child *Python* guards itself the same way. Limits: a child that is not Python (mpv, curl)
or that runs ``python -I``/``-S`` or replaces ``PYTHONPATH`` after start is not covered.
"""
import ipaddress
import os
import socket
import subprocess

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'netguard_child')


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


def child_environment(environment):
    """`environment` (a mapping) with the child guard first on PYTHONPATH and the switch set."""
    result = dict(environment)
    parts = [part for part in result.get('PYTHONPATH', '').split(os.pathsep) if part and part != CHILD]
    result['PYTHONPATH'] = os.pathsep.join([CHILD] + parts)
    result['LISTENING_NETGUARD'] = '1'
    return result


def install():
    if getattr(socket, '_listening_netguard', False):
        return
    socket._listening_netguard = True
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

    real_popen_init = subprocess.Popen.__init__

    def popen_init(self, *args, **kwargs):
        if kwargs.get('env') is not None:
            kwargs['env'] = child_environment(kwargs['env'])
        return real_popen_init(self, *args, **kwargs)

    os.environ.update(child_environment(os.environ))
    subprocess.Popen.__init__ = popen_init
    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
