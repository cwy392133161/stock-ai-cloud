"""One upstream, one request budget. Do not rotate hosts after push2 throttling."""

import time


class ProviderCooldown(RuntimeError):
    """Stop requests to this source for the current invocation."""


class CollectionDeadline:
    """Stop before the next network operation when the source budget expires."""

    def __init__(self, source, seconds, clock=time.monotonic):
        self.source = source
        self.clock = clock
        self.end = clock() + seconds

    def remaining(self):
        remaining = self.end - self.clock()
        if remaining <= 0:
            raise ProviderCooldown(f"{self.source}行情采集已达时间上限")
        return remaining

    def timeout(self, timeout):
        remaining = self.remaining()
        if isinstance(timeout, tuple):
            return tuple(min(value, remaining) for value in timeout)
        return min(timeout, remaining)

    def wait(self, pause, seconds):
        if seconds >= self.remaining():
            raise ProviderCooldown(f"{self.source}剩余采集时间不足，停止后续请求")
        pause(seconds)
        self.remaining()


_EASTMONEY_STOP_STATUS = {401, 403, 429, 502, 503, 504}


class EastmoneyBudget:
    def __init__(self, get, max_requests=200, max_failures=4, *, deadline=None):
        self.get = get
        self.max_requests = max_requests
        self.max_failures = max_failures
        self.requests = 0
        self.failures = 0
        self.stopped = False
        self.deadline = deadline

    def wait(self, pause, seconds):
        if self.deadline is None:
            pause(seconds)
        else:
            self.deadline.wait(pause, seconds)

    def __call__(self, url, **kwargs):
        if self.stopped or self.requests >= self.max_requests:
            self.stopped = True
            raise ProviderCooldown("东财本轮请求预算已耗尽；停止所有同源重试，保留旧快照")
        if self.deadline is not None:
            kwargs["timeout"] = self.deadline.timeout(kwargs.get("timeout", 20))
        self.requests += 1
        try:
            response = self.get(url, **kwargs)
            status = getattr(response, "status_code", None)
            if status in _EASTMONEY_STOP_STATUS:
                raise ProviderCooldown(f"东财 HTTP {status}，停止本轮同源请求")
            response.raise_for_status()
            if self.deadline is not None:
                self.deadline.remaining()
            return response
        except Exception as exc:
            self.failures += 1
            rejected = getattr(exc, "response", None)
            status = getattr(rejected, "status_code", None)
            if status in _EASTMONEY_STOP_STATUS:
                self.stopped = True
                raise ProviderCooldown(f"东财 HTTP {status}，停止本轮同源请求") from None
            # AKShare documents this push2 disconnect pattern as IP throttling.
            # Treat it conservatively, not as a reason to rotate more hosts.
            suspected_limit = (
                isinstance(exc, ProviderCooldown)
                or type(exc).__name__ == "Push2RateLimitError"
                or "RemoteDisconnected" in repr(exc)
                or "Remote end closed connection without response" in str(exc)
            )
            if suspected_limit or self.failures >= self.max_failures:
                self.stopped = True
                if isinstance(exc, ProviderCooldown):
                    raise
                raise ProviderCooldown(
                    "东财疑似限流或失败预算已耗尽；本轮不再调用直连/AKShare/分市场："
                    + type(exc).__name__
                ) from exc
            raise
