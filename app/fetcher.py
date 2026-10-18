"""HTTP fetching with conditional requests and bounded retries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from .config import Settings


class FetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class FetchResult:
    not_modified: bool
    status_code: int
    body: bytes | None
    etag: str | None
    last_modified: str | None
    content_type: str | None
    final_url: str
    attempts: int


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, min(seconds, 30.0))


async def fetch_pdf(
    url: str,
    *,
    settings: Settings,
    etag: str | None = None,
    last_modified: str | None = None,
    conditional: bool = True,
) -> FetchResult:
    """Fetch a PDF.

    A 304 response is returned directly. Network errors and 429/5xx responses
    are retried up to three times with a short exponential backoff. 4xx client
    errors are not retried.
    """
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
    }
    if conditional and etag:
        headers["If-None-Match"] = etag
    if conditional and last_modified:
        headers["If-Modified-Since"] = last_modified

    timeout = httpx.Timeout(settings.http_timeout_seconds, connect=15.0)
    last_error: FetchError | None = None

    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                headers=headers,
            ) as client:
                async with client.stream("GET", url) as response:
                    response_etag = response.headers.get("etag")
                    response_last_modified = response.headers.get("last-modified")
                    content_type = response.headers.get("content-type")
                    final_url = str(response.url)

                    if response.status_code == 304:
                        return FetchResult(
                            not_modified=True,
                            status_code=304,
                            body=None,
                            etag=response_etag,
                            last_modified=response_last_modified,
                            content_type=content_type,
                            final_url=final_url,
                            attempts=attempt,
                        )

                    if response.status_code != 200:
                        await response.aread()
                        retryable = response.status_code == 429 or response.status_code >= 500
                        raise FetchError(
                            f"Origin returned HTTP {response.status_code} for {url}",
                            status_code=response.status_code,
                            retryable=retryable,
                            retry_after=_retry_after_seconds(
                                response.headers.get("retry-after")
                            ),
                        )

                    if content_type and "text/html" in content_type.lower():
                        raise FetchError(
                            f"Expected a PDF but origin returned {content_type!r}",
                            status_code=200,
                        )

                    declared_length = response.headers.get("content-length")
                    if declared_length and declared_length.isdigit():
                        if int(declared_length) > settings.max_download_bytes:
                            raise FetchError(
                                f"PDF is larger than MAX_DOWNLOAD_BYTES "
                                f"({declared_length} > {settings.max_download_bytes})",
                                status_code=200,
                            )

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > settings.max_download_bytes:
                            raise FetchError(
                                f"PDF exceeded MAX_DOWNLOAD_BYTES "
                                f"({settings.max_download_bytes})",
                                status_code=200,
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if b"%PDF-" not in body[:1024]:
                        raise FetchError(
                            "Downloaded content does not look like a PDF "
                            "(missing %PDF- header)",
                            status_code=200,
                        )
                    return FetchResult(
                        not_modified=False,
                        status_code=200,
                        body=body,
                        etag=response_etag,
                        last_modified=response_last_modified,
                        content_type=content_type,
                        final_url=final_url,
                        attempts=attempt,
                    )

        except FetchError as exc:
            last_error = exc
            if not exc.retryable or attempt == 3:
                raise
        except httpx.TransportError as exc:
            last_error = FetchError(
                f"Network error while fetching {url}: {exc}", retryable=True
            )
            if attempt == 3:
                raise last_error from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP client error while fetching {url}: {exc}") from exc

        delay = 2 ** (attempt - 1)
        if last_error and last_error.retry_after is not None:
            delay = max(delay, last_error.retry_after)
        await asyncio.sleep(delay)

    if last_error:
        raise last_error
    raise FetchError("Unreachable fetch state")  # pragma: no cover
