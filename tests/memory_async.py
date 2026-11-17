"""Single-threaded, no-I/O event loop for deterministic ASGI unit exchanges.

Windows asyncio normally creates a loopback socket pair even when the application
uses only in-memory queues. These tests need timers/tasks, not network access. File
registration is explicitly rejected. Never use this loop to serve real sockets.
"""
import asyncio
import os
import selectors
import time


class NoIOSelector(selectors.BaseSelector):
    def register(self, *args, **kwargs):
        raise AssertionError('Socket/file I/O is forbidden in in-memory host tests.')

    def unregister(self, *args, **kwargs):
        raise AssertionError('No I/O registrations exist.')

    def select(self, timeout=None):
        if timeout:
            time.sleep(min(timeout, 0.01))
        return []

    def get_map(self):
        return {}


class MemoryLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__(selector=NoIOSelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise AssertionError('Cross-thread scheduling is not supported by this test loop.')


def run(coroutine):
    # Linux CI exercises the normal event loop. Windows can explicitly opt into
    # real local socket initialization when its execution environment permits it.
    if os.name != 'nt' or os.environ.get('MCA_TEST_REAL_ASYNCIO') == 'true':
        return asyncio.run(coroutine)
    with asyncio.Runner(loop_factory=MemoryLoop) as runner:
        return runner.run(coroutine)
