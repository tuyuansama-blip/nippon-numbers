"""Cloudflare R2 (S3-compatible) client -- the off-repo home for the raw odds
snapshots (docs/DESIGN_ACTIONS.md 2).

`docs/DESIGN_SITE.md` 2.6 forbids committing `data/odds_snapshots/*.json` to
the public repository (The Odds API's terms prohibit redistributing the raw
feed as a standalone data product) while `footy predict` cannot work without
them: the fixture list for the next round is read out of those snapshots
(`footy/pipeline/predict.py`'s module docstring). Moving the pipeline into
GitHub Actions therefore needs a private, non-repo store that a runner can
read *and* write. R2 is that store; this module is the only thing that talks
to it.

**Why a hand-rolled signer rather than boto3.** Everything here that can be
tested without the network is a pure function -- `canonical_request`,
`string_to_sign`, `signing_key`, `authorization_header`,
`parse_list_response` -- and the whole of SigV4 for the four calls this
project makes (GET / PUT / HEAD / ListObjectsV2) is about seventy lines.
`boto3` would pull ~15 MB of botocore into an environment whose entire
dependency list is eight packages, and would still need `moto` (a ninth) to
be exercised offline under DESIGN.md 4's no-network rule. The signer below
is instead pinned to AWS's own published SigV4 test-suite vector in
`tests/test_r2_sync.py`, which is a stronger check than a mock of a library.

Only `R2Client`'s methods touch HTTP, and they take an injectable session so
the test suite can drive them against a fake transport (DESIGN.md 4).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
DEFAULT_REGION = "auto"          # R2 accepts (and documents) `auto`
SERVICE = "s3"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# Where the two kinds of object live inside the bucket. Snapshots are
# immutable and append-only; the schedule state is the one mutable object.
SNAPSHOT_PREFIX = "odds_snapshots/"
STATE_KEY = "state/schedule_state.json"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uri_encode(value: str, *, encode_slash: bool) -> str:
    safe = "-._~" if encode_slash else "-._~/"
    return quote(value, safe=safe)


def canonical_query_string(query: dict[str, str]) -> str:
    """Sorted, RFC-3986-encoded `k=v&...` -- the exact bytes that go both into
    the signature and onto the wire (see `R2Client._request`)."""
    return "&".join(
        f"{_uri_encode(k, encode_slash=True)}={_uri_encode(v, encode_slash=True)}"
        for k, v in sorted(query.items())
    )


def canonical_request(
    method: str, path: str, query: dict[str, str], headers: dict[str, str], payload_hash: str
) -> tuple[str, str]:
    """`(canonical_request, signed_headers)` per SigV4.

    S3 signs the *already encoded* path once (no double encoding), which is
    why `path` is encoded here with `/` left alone.
    """
    canonical_uri = _uri_encode(path or "/", encode_slash=False) or "/"
    canonical_query = canonical_query_string(query)
    lowered = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    signed_headers = ";".join(sorted(lowered))
    canonical_headers = "".join(f"{k}:{lowered[k]}\n" for k in sorted(lowered))
    request = "\n".join([
        method.upper(), canonical_uri, canonical_query,
        canonical_headers, signed_headers, payload_hash,
    ])
    return request, signed_headers


def credential_scope(amz_date: str, region: str, service: str = SERVICE) -> str:
    return f"{amz_date[:8]}/{region}/{service}/aws4_request"


def string_to_sign(amz_date: str, scope: str, request: str) -> str:
    return "\n".join([ALGORITHM, amz_date, scope, sha256_hex(request.encode("utf-8"))])


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str = SERVICE) -> bytes:
    """`date` is the `YYYYMMDD` prefix of the x-amz-date."""
    k_date = _hmac(f"AWS4{secret}".encode("utf-8"), date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def authorization_header(
    *, method: str, path: str, query: dict[str, str], headers: dict[str, str],
    payload_hash: str, access_key: str, secret_key: str, amz_date: str,
    region: str = DEFAULT_REGION, service: str = SERVICE,
) -> str:
    request, signed_headers = canonical_request(method, path, query, headers, payload_hash)
    scope = credential_scope(amz_date, region, service)
    to_sign = string_to_sign(amz_date, scope, request)
    key = signing_key(secret_key, amz_date[:8], region, service)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def signed_headers_for(
    *, method: str, host: str, path: str, query: dict[str, str] | None,
    payload: bytes, access_key: str, secret_key: str,
    region: str = DEFAULT_REGION, now: datetime | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Every header a signed R2 request needs, including `Authorization`."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = sha256_hex(payload)
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    headers.update({k.lower(): v for k, v in (extra_headers or {}).items()})
    headers["Authorization"] = authorization_header(
        method=method, path=path, query=query or {}, headers=headers,
        payload_hash=payload_hash, access_key=access_key, secret_key=secret_key,
        amz_date=amz_date, region=region,
    )
    return headers


def parse_list_response(body: bytes) -> tuple[list[str], str | None]:
    """ListObjectsV2 XML -> `(keys, next_continuation_token)`.

    Namespace-agnostic on purpose: the response is signed by us and read only
    for its `<Key>` elements, and pinning `http://s3.amazonaws.com/doc/2006-03-01/`
    would make the parse brittle against a provider that emits a different one.
    """
    root = ET.fromstring(body)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    keys: list[str] = []
    token: str | None = None
    for child in root:
        if local(child.tag) == "Contents":
            for field in child:
                if local(field.tag) == "Key" and field.text:
                    keys.append(field.text)
        elif local(child.tag) == "NextContinuationToken":
            token = child.text
    return keys, token


# --- configuration ------------------------------------------------------------
@dataclass(frozen=True)
class R2Config:
    """Bucket coordinates, read from the environment and never from a file.

    `R2_ACCOUNT_ID` falls back to `CLOUDFLARE_ACCOUNT_ID` because they are the
    same value and docs/SETUP_ACTIONS.md asks for it only once.
    """
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    endpoint: str
    region: str = DEFAULT_REGION

    @classmethod
    def from_env(cls, env=None) -> "R2Config | None":
        env = env if env is not None else os.environ
        account = env.get("R2_ACCOUNT_ID") or env.get("CLOUDFLARE_ACCOUNT_ID") or ""
        bucket = env.get("R2_BUCKET") or ""
        access = env.get("R2_ACCESS_KEY_ID") or ""
        secret = env.get("R2_SECRET_ACCESS_KEY") or ""
        endpoint = env.get("R2_ENDPOINT") or (
            f"https://{account}.r2.cloudflarestorage.com" if account else ""
        )
        if not (bucket and access and secret and endpoint):
            return None
        return cls(
            account_id=account, bucket=bucket, access_key_id=access,
            secret_access_key=secret, endpoint=endpoint.rstrip("/"),
            region=env.get("R2_REGION") or DEFAULT_REGION,
        )

    @property
    def host(self) -> str:
        return self.endpoint.split("://", 1)[-1].split("/", 1)[0]

    def path_for(self, key: str) -> str:
        return f"/{self.bucket}/{key.lstrip('/')}"

    def url_for(self, key: str) -> str:
        return f"{self.endpoint}{self.path_for(key)}"


def missing_env_names(env=None) -> list[str]:
    """Which of the four required variables are unset -- for a CLI message
    that names them instead of printing a bare `None`."""
    env = env if env is not None else os.environ
    required = {
        "R2_BUCKET": env.get("R2_BUCKET"),
        "R2_ACCESS_KEY_ID": env.get("R2_ACCESS_KEY_ID"),
        "R2_SECRET_ACCESS_KEY": env.get("R2_SECRET_ACCESS_KEY"),
        "R2_ACCOUNT_ID (or CLOUDFLARE_ACCOUNT_ID, or R2_ENDPOINT)": (
            env.get("R2_ACCOUNT_ID") or env.get("CLOUDFLARE_ACCOUNT_ID") or env.get("R2_ENDPOINT")
        ),
    }
    return [name for name, value in required.items() if not value]


# --- network I/O ---------------------------------------------------------------
class R2Client:
    """The four S3 calls this project makes. `session` is injectable so the
    test suite can exercise every code path against a fake transport without
    reaching the network (DESIGN.md 4)."""

    def __init__(self, config: R2Config, *, session=None, timeout: int = 60):
        self.config = config
        self.timeout = timeout
        if session is not None:
            self.session = session
        else:                                          # pragma: no cover - network
            import requests

            self.session = requests.Session()

    def _request(self, method: str, key: str, *, query=None, payload: bytes = b"",
                 extra_headers=None):
        """The URL is assembled here from the *same* encoder the signature
        used, and handed to the session already complete. Letting `requests`
        build the query string from a dict instead would re-encode it with
        `quote_plus`, and any disagreement between the two encoders shows up
        only as an opaque `SignatureDoesNotMatch` from the server."""
        config = self.config
        path = config.path_for(key)
        headers = signed_headers_for(
            method=method, host=config.host, path=path, query=query,
            payload=payload, access_key=config.access_key_id,
            secret_key=config.secret_access_key, region=config.region,
            extra_headers=extra_headers,
        )
        url = f"{config.endpoint}{_uri_encode(path, encode_slash=False)}"
        if query:
            url += "?" + canonical_query_string(query)
        return self.session.request(
            method, url, data=payload if payload else None,
            headers=headers, timeout=self.timeout,
        )

    def get(self, key: str) -> bytes | None:
        """Object bytes, or `None` when it does not exist (404/403-on-missing)."""
        response = self._request("GET", key)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"R2 GET {key}: HTTP {response.status_code} {response.text[:200]}")
        return response.content

    def put(self, key: str, body: bytes, *, content_type: str = "application/json") -> None:
        response = self._request(
            "PUT", key, payload=body, extra_headers={"content-type": content_type}
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(f"R2 PUT {key}: HTTP {response.status_code} {response.text[:200]}")

    def list(self, prefix: str = "") -> list[str]:
        """Every key under `prefix`, following continuation tokens."""
        keys: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            response = self._request("GET", "", query=query)
            if response.status_code != 200:
                raise RuntimeError(
                    f"R2 LIST {prefix}: HTTP {response.status_code} {response.text[:200]}"
                )
            page, token = parse_list_response(response.content)
            keys.extend(page)
            if not token:
                return keys
