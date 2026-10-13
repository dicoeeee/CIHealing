"""HTTP transport timing only: never inspect headers, URLs, prompts or bodies."""

from contextlib import contextmanager
import time
import threading


@contextmanager
def transport_timing(emit, before_send=None):
    import httpx
    original_async, original_sync = httpx.AsyncClient.send, httpx.Client.send
    counter = 0
    counter_lock = threading.Lock()

    def begin():
        nonlocal counter
        if before_send is not None:
            before_send()
        with counter_lock:
            counter += 1
            identity, started = counter, time.monotonic()
        emit("transport.started", {"request_seq": identity})

        def record(stage, **values):
            emit("transport." + stage, {"request_seq": identity,
                                       "elapsed_seconds": round(time.monotonic() - started, 3), **values})
        return record

    class TimedAsyncStream(httpx.AsyncByteStream):
        def __init__(self, inner, record):
            self.inner, self.record, self.total = inner, record, 0

        async def __aiter__(self):
            try:
                async for chunk in self.inner:
                    if not self.total:
                        self.record("first_body_bytes")
                    self.total += len(chunk)
                    yield chunk
                self.record("body_finished", bytes=self.total)
            except Exception as exc:
                self.record("error", error_type=type(exc).__name__, bytes=self.total)
                raise

        async def aclose(self):
            await self.inner.aclose()

    class TimedSyncStream(httpx.SyncByteStream):
        def __init__(self, inner, record):
            self.inner, self.record, self.total = inner, record, 0

        def __iter__(self):
            try:
                for chunk in self.inner:
                    if not self.total:
                        self.record("first_body_bytes")
                    self.total += len(chunk)
                    yield chunk
                self.record("body_finished", bytes=self.total)
            except Exception as exc:
                self.record("error", error_type=type(exc).__name__, bytes=self.total)
                raise

        def close(self):
            self.inner.close()

    async def send_async(client, request, **kwargs):
        record = begin()
        try:
            response = await original_async(client, request, **kwargs)
            if response.is_stream_consumed:
                record("body_already_buffered", status_code=response.status_code)
            else:
                record("headers_received", status_code=response.status_code)
                response.stream = TimedAsyncStream(response.stream, record)
            return response
        except Exception as exc:
            record("error", error_type=type(exc).__name__)
            raise

    def send_sync(client, request, **kwargs):
        record = begin()
        try:
            response = original_sync(client, request, **kwargs)
            if response.is_stream_consumed:
                record("body_already_buffered", status_code=response.status_code)
            else:
                record("headers_received", status_code=response.status_code)
                response.stream = TimedSyncStream(response.stream, record)
            return response
        except Exception as exc:
            record("error", error_type=type(exc).__name__)
            raise

    httpx.AsyncClient.send, httpx.Client.send = send_async, send_sync
    try:
        yield
    finally:
        httpx.AsyncClient.send, httpx.Client.send = original_async, original_sync
