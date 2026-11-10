"""One upstream, one request budget. Do not rotate hosts after push2 throttling."""

class ProviderCooldown(RuntimeError):
    """Stop all Eastmoney variants for this invocation; retry on a later run."""


class EastmoneyBudget:
    def __init__(self, get, max_requests=200, max_failures=4):
        self.get = get
        self.max_requests = max_requests
        self.max_failures = max_failures
        self.requests = 0
        self.failures = 0
        self.stopped = False

    def __call__(self, url, **kwargs):
        if self.stopped or self.requests >= self.max_requests:
            self.stopped = True
            raise ProviderCooldown("东财本轮请求预算已耗尽；停止所有同源重试，保留旧快照")
        self.requests += 1
        try:
            response = self.get(url, **kwargs)
            if getattr(response, "status_code", None) == 429:
                raise ProviderCooldown("东财 HTTP 429 限流；停止所有同源重试，等待后续运行")
            response.raise_for_status()
            return response
        except Exception as exc:
            self.failures += 1
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
                raise ProviderCooldown(
                    "东财疑似限流或失败预算已耗尽；本轮不再调用直连/AKShare/分市场："
                    + str(exc)[:200]
                ) from exc
            raise
