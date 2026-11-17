"""Opt-in ASGI application, isolated from Flask's production entry point.

No listener, credential loading, or provider client is started on import. Session
factory is trusted configuration, never selected from setup/customParameters.
Only bounded local interpreter work is supported, not remote model calls.
"""
import asyncio
from contextlib import nullcontext
import json

from voice_relay import authenticate_handshake


class RelayHost:
    def __init__(self, public_url, auth_token, session_factory, *, context_factory=nullcontext,
                 io_timeout=5, tick_seconds=1, max_connections=4, max_frames=128):
        if io_timeout <= 0 or tick_seconds <= 0 or max_connections < 1 or max_frames < 1:
            raise ValueError('Positive host limits required.')
        self.public_url, self.auth_token = public_url, auth_token
        self.session_factory, self.context_factory = session_factory, context_factory
        self.io_timeout, self.tick_seconds = io_timeout, tick_seconds
        self.max_connections, self.max_frames = max_connections, max_frames
        self.active = 0  # per ASGI process/event loop, not a deployment-wide quota

    async def __call__(self, scope, receive, send):
        async def emit(message):
            await asyncio.wait_for(send(message), self.io_timeout)

        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        if scope['type'] == 'http':
            await emit({'type': 'http.response.start', 'status': 404,
                        'headers': [(b'cache-control', b'no-store')]})
            await emit({'type': 'http.response.body', 'body': b'Not found.'})
            return
        if scope['type'] != 'websocket':
            return
        session = None
        accepted = disconnected = counted = False
        try:
            first = await asyncio.wait_for(receive(), self.io_timeout)
            if first['type'] != 'websocket.connect':
                return
            try:
                target = scope['raw_path'].decode('ascii')
                query = scope.get('query_string', b'').decode('ascii')
                if query:
                    target += '?' + query
                signatures = [v.decode('ascii') for k, v in scope.get('headers', [])
                              if k.lower() == b'x-twilio-signature']
                valid = len(signatures) == 1 and authenticate_handshake(
                    self.public_url, target, signatures[0], self.auth_token)
            except (KeyError, AttributeError, UnicodeError, TypeError, ValueError):
                valid = False
            if not valid:
                await emit({'type': 'websocket.close', 'code': 1008})
                return
            if self.active >= self.max_connections:
                await emit({'type': 'websocket.close', 'code': 1013})
                return
            self.active += 1
            counted = True
            with self.context_factory():
                session = self.session_factory()
            await emit({'type': 'websocket.accept'})
            accepted = True
            frames = 0
            while not session.closed:
                try:
                    event = await asyncio.wait_for(receive(), self.tick_seconds)
                except asyncio.TimeoutError:
                    with self.context_factory():
                        outgoing = session.tick()
                else:
                    if event['type'] == 'websocket.disconnect':
                        disconnected = True
                        break
                    frames += 1
                    if event['type'] != 'websocket.receive' or frames > self.max_frames:
                        with self.context_factory():
                            outgoing = session.end('frame_limit_or_invalid_event')
                    else:
                        with self.context_factory():
                            outgoing = session.receive(event.get('text'))
                for message in outgoing:
                    await emit({'type': 'websocket.send', 'text': json.dumps(message)})
            if not disconnected:
                await emit({'type': 'websocket.close', 'code': 1000})
        except (Exception, asyncio.CancelledError) as error:
            # Never send/log exception bodies; they may contain credentials or PII.
            if accepted and not disconnected:
                try:
                    await emit({'type': 'websocket.send', 'text': json.dumps({
                        'type': 'end', 'handoffData': json.dumps({
                            'disposition': 'needs_human', 'reason': 'host_unavailable'})})})
                except Exception:
                    pass  # no claim the fallback was delivered on a broken socket
            try:
                await emit({'type': 'websocket.close', 'code': 1011})
            except Exception:
                pass
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            if session is not None:
                try:
                    with self.context_factory():
                        session.disconnect()
                except Exception:
                    pass  # storage outage needs reconciliation, not a false success
            if counted:
                self.active -= 1
