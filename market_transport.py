"""One upstream, one request budget. Do not rotate hosts after push2 throttling."""
import time
import requests

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
            status = getattr(response, "status_code", None)
            if status in (401, 403, 429):
                reason = "限流" if status == 429 else "拒绝访问"
                raise ProviderCooldown(f"东财 HTTP {status} {reason}；停止所有同源重试")
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

    def request_page(self, url, pause=time.sleep, **kwargs):
        """Retry one transient page once; both physical calls consume this budget.

        Never retry HTTP429, disconnects, recognized push2 throttling, unknown
        errors or an exhausted shared budget. Partial pagination still fails.
        """
        for attempt in range(2):
            try:
                return self(url, **kwargs)
            except (requests.Timeout, requests.HTTPError) as exc:
                response = getattr(exc, "response", None)
                retryable = isinstance(exc, requests.Timeout) or (
                    response is not None and response.status_code in (502, 503, 504))
                if attempt or not retryable:
                    raise
                pause(2)
