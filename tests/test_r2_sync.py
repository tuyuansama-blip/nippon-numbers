"""R2 storage layer (`footy/odds/r2.py`) and `footy odds sync`
(`footy/odds/sync.py`) -- docs/DESIGN_ACTIONS.md 2.

Nothing here reaches the network (DESIGN.md 4, enforced by
`tests/conftest.py`). The signer is checked against AWS's own published
SigV4 test-suite vector, which is a harder target than any mock; the client
is driven against `FakeBucket`, an in-memory transport that speaks exactly
the four S3 calls this project makes.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from footy.odds.r2 import (
    SNAPSHOT_PREFIX,
    STATE_KEY,
    R2Client,
    R2Config,
    authorization_header,
    canonical_query_string,
    canonical_request,
    credential_scope,
    missing_env_names,
    parse_list_response,
    sha256_hex,
    signing_key,
    string_to_sign,
)
from footy.odds.sync import (
    STATE_FILENAME,
    local_snapshot_names,
    merge_state,
    plan_pull,
    plan_push,
    pull,
    push,
    snapshot_names,
)

# AWS SigV4 test suite, case `get-vanilla`. The credentials are AWS's own
# documented examples and grant nothing.
VECTOR_ACCESS_KEY = "AKIDEXAMPLE"
VECTOR_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
VECTOR_DATE = "20150830T123600Z"
VECTOR_HEADERS = {"host": "example.amazonaws.com", "x-amz-date": VECTOR_DATE}
VECTOR_STS_HASH = "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63"
VECTOR_SIGNATURE = "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
EMPTY = sha256_hex(b"")


# --- SigV4 -------------------------------------------------------------------
def test_canonical_request_matches_the_aws_test_vector():
    request, signed = canonical_request("GET", "/", {}, VECTOR_HEADERS, EMPTY)
    assert signed == "host;x-amz-date"
    assert request == (
        "GET\n/\n\n"
        "host:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\n"
        "host;x-amz-date\n" + EMPTY
    )


def test_string_to_sign_matches_the_aws_test_vector():
    request, _ = canonical_request("GET", "/", {}, VECTOR_HEADERS, EMPTY)
    scope = credential_scope(VECTOR_DATE, "us-east-1", "service")
    assert string_to_sign(VECTOR_DATE, scope, request).splitlines()[-1] == VECTOR_STS_HASH


def test_authorization_header_matches_the_aws_test_vector():
    """The whole point of hand-rolling the signer instead of pulling in
    botocore: it is pinned to the spec's own answer, not to a mock."""
    header = authorization_header(
        method="GET", path="/", query={}, headers=VECTOR_HEADERS, payload_hash=EMPTY,
        access_key=VECTOR_ACCESS_KEY, secret_key=VECTOR_SECRET,
        amz_date=VECTOR_DATE, region="us-east-1", service="service",
    )
    assert header == (
        f"AWS4-HMAC-SHA256 Credential={VECTOR_ACCESS_KEY}/20150830/us-east-1/service/aws4_request, "
        f"SignedHeaders=host;x-amz-date, Signature={VECTOR_SIGNATURE}"
    )


def test_signing_key_is_derived_from_the_date_not_the_full_timestamp():
    assert signing_key(VECTOR_SECRET, "20150830", "us-east-1", "service") == signing_key(
        VECTOR_SECRET, VECTOR_DATE[:8], "us-east-1", "service"
    )


def test_canonical_query_string_sorts_and_percent_encodes_reserved_characters():
    """A continuation token is base64: `+`, `/` and `=` all have to survive
    identically into the signature and onto the wire, or R2 answers
    `SignatureDoesNotMatch` with no hint as to which side drifted."""
    encoded = canonical_query_string({"list-type": "2", "continuation-token": "a+b/c="})
    assert encoded == "continuation-token=a%2Bb%2Fc%3D&list-type=2"


def test_canonical_request_does_not_double_encode_the_path_separator():
    request, _ = canonical_request(
        "GET", "/bucket/odds_snapshots/j1_h2h_eu_20260820T072241Z.json", {},
        VECTOR_HEADERS, EMPTY,
    )
    assert "/bucket/odds_snapshots/j1_h2h_eu_20260820T072241Z.json" in request
    assert "%2F" not in request


# --- config ------------------------------------------------------------------
def test_config_derives_the_endpoint_from_the_account_id():
    config = R2Config.from_env({
        "CLOUDFLARE_ACCOUNT_ID": "acc123", "R2_BUCKET": "odds",
        "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
    })
    assert config.endpoint == "https://acc123.r2.cloudflarestorage.com"
    assert config.host == "acc123.r2.cloudflarestorage.com"
    assert config.path_for("state/x.json") == "/odds/state/x.json"
    assert config.region == "auto"


def test_config_is_none_when_anything_is_missing():
    assert R2Config.from_env({}) is None
    assert R2Config.from_env({"R2_BUCKET": "odds"}) is None
    assert "R2_ACCESS_KEY_ID" in missing_env_names({"R2_BUCKET": "odds"})


# --- ListObjectsV2 parsing ----------------------------------------------------
LIST_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>odds</Name>
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>tok2</NextContinuationToken>
  <Contents><Key>odds_snapshots/a.json</Key><Size>10</Size></Contents>
  <Contents><Key>odds_snapshots/b.json</Key><Size>10</Size></Contents>
</ListBucketResult>"""


def test_parse_list_response_reads_keys_and_the_continuation_token():
    keys, token = parse_list_response(LIST_XML)
    assert keys == ["odds_snapshots/a.json", "odds_snapshots/b.json"]
    assert token == "tok2"


def test_parse_list_response_is_namespace_agnostic():
    keys, token = parse_list_response(
        b"<ListBucketResult><Contents><Key>k</Key></Contents></ListBucketResult>"
    )
    assert (keys, token) == (["k"], None)


# --- the pure sync planner ----------------------------------------------------
def test_snapshot_names_strips_the_prefix_and_ignores_foreign_keys():
    assert snapshot_names([
        f"{SNAPSHOT_PREFIX}b.json", f"{SNAPSHOT_PREFIX}a.json", STATE_KEY, SNAPSHOT_PREFIX,
    ]) == ["a.json", "b.json"]


def test_plan_pull_and_push_are_pure_set_differences():
    remote = ["a.json", "b.json"]
    local = ["b.json", "c.json"]
    assert plan_pull(remote, local) == ["a.json"]
    assert plan_push(local, remote) == ["c.json"]


def test_plan_push_is_empty_when_everything_is_already_uploaded():
    assert plan_push(["a.json"], ["a.json", "b.json"]) == []


def test_merge_state_keeps_every_point_either_side_believes_is_done():
    """A point one side has a snapshot for must never be re-fetched by the
    other -- that would spend a credit for a byte-identical duplicate."""
    merged = merge_state(
        {"2026-08-22T09:00:00+00:00|t72h": True},
        {"2026-08-22T09:00:00+00:00|t24h": True},
    )
    assert merged == {
        "2026-08-22T09:00:00+00:00|t72h": True,
        "2026-08-22T09:00:00+00:00|t24h": True,
    }


def test_merge_state_takes_the_pessimistic_remaining_credit_count():
    merged = merge_state({"_last_remaining": 300}, {"_last_remaining": 120})
    assert merged["_last_remaining"] == 120


def test_merge_state_never_resurrects_a_false_over_a_true():
    assert merge_state({"k": False}, {"k": True}) == {"k": True}


# --- an in-memory bucket ------------------------------------------------------
class _Response:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class FakeBucket:
    """The four S3 calls, in memory. Also asserts every request arrives
    signed, so a code path that forgets `Authorization` fails here rather
    than against the real bucket."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts: list[str] = []

    def request(self, method, url, *, data=None, headers=None, timeout=None):
        assert headers and headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256")
        assert headers["x-amz-content-sha256"] == sha256_hex(data or b"")
        split = urlsplit(url)
        key = split.path.split("/", 2)[2] if split.path.count("/") >= 2 else ""

        if split.query:
            query = parse_qs(split.query)
            prefix = query.get("prefix", [""])[0]
            keys = sorted(k for k in self.objects if k.startswith(prefix))
            body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
            return _Response(200, f"<ListBucketResult>{body}</ListBucketResult>".encode())
        if method == "PUT":
            self.objects[key] = data
            self.puts.append(key)
            return _Response(200)
        if key not in self.objects:
            return _Response(404)
        return _Response(200, self.objects[key])


@pytest.fixture
def client():
    config = R2Config.from_env({
        "R2_ACCOUNT_ID": "acc", "R2_BUCKET": "odds",
        "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
    })
    bucket = FakeBucket()
    return R2Client(config, session=bucket), bucket


def test_client_round_trips_an_object(client):
    r2, bucket = client
    assert r2.get("state/nope.json") is None
    r2.put("state/x.json", b'{"a": 1}')
    assert r2.get("state/x.json") == b'{"a": 1}'
    assert bucket.puts == ["state/x.json"]


def test_client_list_filters_by_prefix(client):
    r2, bucket = client
    bucket.objects.update({
        f"{SNAPSHOT_PREFIX}a.json": b"[]", f"{SNAPSHOT_PREFIX}b.json": b"[]",
        STATE_KEY: b"{}",
    })
    assert r2.list(SNAPSHOT_PREFIX) == [f"{SNAPSHOT_PREFIX}a.json", f"{SNAPSHOT_PREFIX}b.json"]


def test_client_raises_on_an_unexpected_status(client):
    r2, bucket = client

    def refuse(*args, **kwargs):
        return _Response(403, b"AccessDenied")

    bucket.request = refuse
    with pytest.raises(RuntimeError, match="403"):
        r2.get("state/x.json")


# --- pull / push --------------------------------------------------------------
def test_push_uploads_only_the_snapshots_r2_has_never_seen(client, tmp_path):
    r2, bucket = client
    bucket.objects[f"{SNAPSHOT_PREFIX}j1_h2h_eu_20260820T072241Z.json"] = b"[]"
    for name in ("j1_h2h_eu_20260820T072241Z.json", "j1_h2h_eu_20260821T040006Z.json"):
        (tmp_path / name).write_bytes(b"[]")

    result = push(r2, snapshot_dir=tmp_path, include="snapshots", log=lambda *_: None)
    assert result["pushed"] == ["j1_h2h_eu_20260821T040006Z.json"]
    assert bucket.puts == [f"{SNAPSHOT_PREFIX}j1_h2h_eu_20260821T040006Z.json"]


def test_push_writes_the_state_last_and_merges_it(client, tmp_path):
    r2, bucket = client
    bucket.objects[STATE_KEY] = json.dumps({"anchor|t72h": True}).encode()
    (tmp_path / STATE_FILENAME).write_text(json.dumps({"anchor|t25min": True}), encoding="utf-8")

    result = push(r2, snapshot_dir=tmp_path, log=lambda *_: None)
    assert result["state"] == "written"
    assert bucket.puts[-1] == STATE_KEY
    assert json.loads(bucket.objects[STATE_KEY]) == {"anchor|t72h": True, "anchor|t25min": True}


def test_push_state_is_a_noop_when_there_is_nothing_local(client, tmp_path):
    r2, bucket = client
    result = push(r2, snapshot_dir=tmp_path, include="state", log=lambda *_: None)
    assert result["state"] == "absent"
    assert bucket.puts == []


def test_pull_downloads_missing_snapshots_and_merges_the_state(client, tmp_path):
    r2, bucket = client
    bucket.objects[f"{SNAPSHOT_PREFIX}j1_h2h_eu_20260820T072241Z.json"] = b'[{"id": "e1"}]'
    bucket.objects[STATE_KEY] = json.dumps({"anchor|t72h": True}).encode()
    (tmp_path / STATE_FILENAME).write_text(json.dumps({"anchor|t2h": True}), encoding="utf-8")

    result = pull(r2, snapshot_dir=tmp_path, log=lambda *_: None)
    assert result["pulled"] == ["j1_h2h_eu_20260820T072241Z.json"]
    assert (tmp_path / "j1_h2h_eu_20260820T072241Z.json").read_bytes() == b'[{"id": "e1"}]'
    state = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
    assert state == {"anchor|t72h": True, "anchor|t2h": True}


def test_pull_reports_an_absent_state_instead_of_writing_an_empty_one(client, tmp_path):
    r2, _ = client
    result = pull(r2, snapshot_dir=tmp_path, include="state", log=lambda *_: None)
    assert result["state"] == "absent"
    assert not (tmp_path / STATE_FILENAME).exists()


def test_pull_dry_run_writes_nothing(client, tmp_path):
    r2, bucket = client
    bucket.objects[f"{SNAPSHOT_PREFIX}j1_h2h_eu_20260820T072241Z.json"] = b"[]"
    result = pull(r2, snapshot_dir=tmp_path, dry_run=True, log=lambda *_: None)
    assert result["pulled"] == ["j1_h2h_eu_20260820T072241Z.json"]
    assert local_snapshot_names(tmp_path) == []


def test_sync_never_deletes_anything(client, tmp_path):
    """There is deliberately no mirror mode: the raw snapshots are the one
    artefact in this project that cannot be re-created (`odds/ingest.py`)."""
    r2, bucket = client
    (tmp_path / "j1_h2h_eu_20260820T072241Z.json").write_bytes(b"[]")
    bucket.objects[f"{SNAPSHOT_PREFIX}j1_h2h_eu_20260821T040006Z.json"] = b"[]"

    push(r2, snapshot_dir=tmp_path, include="snapshots", log=lambda *_: None)
    pull(r2, snapshot_dir=tmp_path, include="snapshots", log=lambda *_: None)
    assert len(local_snapshot_names(tmp_path)) == 2
    assert len([k for k in bucket.objects if k.startswith(SNAPSHOT_PREFIX)]) == 2
