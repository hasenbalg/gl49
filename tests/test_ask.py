"""_ask(): stdin prompt that survives aborts — answers parse, cancels
cleanly, and a stuck prompt cannot keep the process alive (daemon thread)."""
import asyncio
import threading

from glucobridge.pairing import _ask


async def test_ask_parses(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda p: "  123456 ")
    assert await _ask("code: ") == 123456


async def test_ask_rejects_garbage(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda p: "abc")
    try:
        await _ask("code: ")
        assert False, "expected ValueError"
    except ValueError:
        pass


async def test_ask_forwards_eof(monkeypatch):
    def eof(prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    try:
        await _ask("code: ")
        assert False, "expected EOFError"
    except EOFError:
        pass


async def test_ask_cancellable_with_stuck_input():
    release = threading.Event()
    started = threading.Event()

    def blocking_input(prompt):
        started.set()
        release.wait(5)
        return "0"

    import builtins
    real = builtins.input
    builtins.input = blocking_input
    try:
        pending: set = set()
        task = asyncio.ensure_future(_ask("code: ", pending))
        await asyncio.to_thread(started.wait, 2)   # prompt is open
        assert pending                              # registered
        task.cancel()
        try:
            await task
            assert False, "expected CancelledError"
        except asyncio.CancelledError:
            pass
        assert not pending                          # deregistered
        # the daemon worker is still stuck in input(); releasing it
        # after the loop is gone must not explode either
        release.set()
    finally:
        builtins.input = real
        release.set()
