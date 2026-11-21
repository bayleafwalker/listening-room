"""One way to hand work to a thread and its answer back to the GTK main loop.

``run_async(fn, on_ok, on_err, pool)`` runs ``fn()`` on ``pool`` and calls ``on_ok(result)`` or
``on_err(error)`` on the main loop. The callbacks are the only code that sees the answer, so
nothing touches GTK off the main thread. ``fn`` itself must not touch GTK.
"""
import concurrent.futures

from gi.repository import GLib


def pool(name, workers=1):
    """A named worker pool. Polls get a pool of their own, so a slow command never delays them."""
    return concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)


def error_text(error):
    """What a failure says: its message, else the name of its type."""
    return str(error) or type(error).__name__


def _deliver(callback, value):
    callback(value)
    return False        # an idle source runs once, whatever the callback returns


def call_on_main(callback, value):
    """Hand ``value`` from any thread (a subscription's, say) to ``callback`` on the main loop."""
    GLib.idle_add(_deliver, callback, value)


def run_async(fn, on_ok=None, on_err=None, pool=None):
    """Run ``fn()`` on ``pool``; then ``on_ok(result)`` or ``on_err(exception)`` on the main loop.

    A failure with no ``on_err`` is dropped. Returns the Future, or None when the pool is already
    shut down (the window closed), in which case neither callback runs."""
    def finished(future):
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            if on_err is not None:
                GLib.idle_add(_deliver, on_err, error)
        elif on_ok is not None:
            GLib.idle_add(_deliver, on_ok, future.result())
    try:
        future = pool.submit(fn)
    except RuntimeError:        # cannot schedule new futures after shutdown
        return None
    future.add_done_callback(finished)
    return future
