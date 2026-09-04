"""Minimal AWS Signature Version 4 signer for S3 requests.

Only the pieces needed for GET/PUT/HEAD/DELETE object requests are implemented,
which keeps the per-request cost to a handful of SHA-256/HMAC operations.
"""

import datetime
import hashlib
import hmac
from typing import Dict, Optional
from urllib.parse import quote

EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def uri_encode_path(path: str) -> str:
    """Encode an object key as a canonical URI path (each segment separately)."""
    return "/".join(quote(segment, safe="-_.~") for segment in path.split("/"))


class SigV4Signer:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        region: str,
        service: str = "s3",
    ):
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.service = service
        self._cached_date: Optional[str] = None
        self._cached_key: bytes = b""

    def _signing_key(self, date: str) -> bytes:
        if date != self._cached_date:
            key = hmac.new(
                ("AWS4" + self.secret_key).encode(), date.encode(), hashlib.sha256
            ).digest()
            for part in (self.region, self.service, "aws4_request"):
                key = hmac.new(key, part.encode(), hashlib.sha256).digest()
            self._cached_date, self._cached_key = date, key
        return self._cached_key

    def sign(
        self,
        method: str,
        host: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        payload_hash: str = EMPTY_PAYLOAD_SHA256,
        query: str = "",
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, str]:
        """Return the headers to send, including Authorization.

        ``path`` must already be URI encoded (see :func:`uri_encode_path`) and
        ``query`` must already be in canonical form (sorted, encoded).
        """
        now = now or datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = amz_date[:8]

        signed = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if headers:
            signed.update({k.lower(): v.strip() for k, v in headers.items()})
        names = sorted(signed)
        canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
        signed_header_names = ";".join(names)
        canonical_request = (
            f"{method}\n{path}\n{query}\n{canonical_headers}\n"
            f"{signed_header_names}\n{payload_hash}"
        )
        scope = f"{date}/{self.region}/{self.service}/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )
        signature = hmac.new(
            self._signing_key(date), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()

        out = dict(headers or {})
        out["x-amz-date"] = amz_date
        out["x-amz-content-sha256"] = payload_hash
        out["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_header_names}, Signature={signature}"
        )
        return out
