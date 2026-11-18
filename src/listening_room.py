#!/usr/bin/env python3
"""listening-room: the entry point of the desktop listening room. The window itself is
next_window; this file only turns the command line into a launch."""
import sys


def classic_requested(arguments):
    """True when the command line asks for the retired classic window (`--ui=classic`)."""
    for index, value in enumerate(arguments):
        if value.startswith('--ui='):
            return value[len('--ui='):] != 'next'
        if value == '--ui':
            return index + 1 >= len(arguments) or arguments[index + 1] != 'next'
    return False


def main():
    if classic_requested(sys.argv[1:]):
        print('listening-room: the classic window has been removed; run listening-room without --ui.', file=sys.stderr)
        return 2
    import next_window
    return next_window.run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
