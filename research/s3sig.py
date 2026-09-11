"""Minimal S3 client with AWS SigV4, written against the standard library.

boto3 is not installed and pip cannot reach PyPI on this machine, so the
signing is done here by hand. Only the two verbs we need are implemented:
ListObjectsV2 and GetObject.

Credentials come from research/../.secrets/massive.env, which is gitignored.
Nothing in this file prints or logs a secret.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import hmac
import io
import os
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENVFILE = os.path.join(ROOT, ".secrets", "massive.env")
REGION = os.environ.get("MASSIVE_REGION", "us-east-1")
SERVICE = "s3"
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

# This machine reaches the internet through a TLS-terminating proxy whose CA
# lives in the system bundle, not in certifi. Point Python at it explicitly or
# every request dies with CERTIFICATE_VERIFY_FAILED.
_CA = next((p for p in ("/etc/ssl/cert.pem", "/etc/pki/tls/certs/ca-bundle.crt")
            if os.path.exists(p)), None)
_CTX = ssl.create_default_context(cafile=_CA) if _CA else None


def creds() -> dict:
    c = {}
    if os.path.exists(ENVFILE):
        for line in open(ENVFILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip()
    for k in ("MASSIVE_ACCESS_KEY_ID", "MASSIVE_SECRET_KEY",
              "MASSIVE_ENDPOINT", "MASSIVE_BUCKET"):
        c.setdefault(k, os.environ.get(k, ""))
    if not c["MASSIVE_ACCESS_KEY_ID"] or not c["MASSIVE_SECRET_KEY"]:
        raise RuntimeError(f"credentials missing; expected {ENVFILE}")
    return c


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str) -> bytes:
    k = _sign(("AWS4" + secret).encode(), datestamp)
    k = _sign(k, REGION)
    k = _sign(k, SERVICE)
    return _sign(k, "aws4_request")


def request(path: str, query: dict | None = None, method: str = "GET",
            timeout: int = 120) -> bytes:
    """Signed request. `path` is the object path INCLUDING the bucket."""
    c = creds()
    host = urllib.parse.urlparse(c["MASSIVE_ENDPOINT"]).netloc
    query = query or {}
    # canonical query: sorted, RFC3986-encoded
    cq = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}="
        f"{urllib.parse.quote(str(v), safe='-_.~')}"
        for k, v in sorted(query.items()))
    canon_path = urllib.parse.quote(path, safe="/-_.~")

    now = _dt.datetime.now(_dt.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()

    canon_headers = (f"host:{host}\n"
                     f"x-amz-content-sha256:{payload_hash}\n"
                     f"x-amz-date:{amzdate}\n")
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canon_req = "\n".join([method, canon_path, cq, canon_headers,
                           signed_headers, payload_hash])
    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                         hashlib.sha256(canon_req.encode()).hexdigest()])
    sig = hmac.new(_signing_key(c["MASSIVE_SECRET_KEY"], datestamp),
                   to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={c['MASSIVE_ACCESS_KEY_ID']}/{scope},"
            f" SignedHeaders={signed_headers}, Signature={sig}")

    url = c["MASSIVE_ENDPOINT"].rstrip("/") + canon_path + (f"?{cq}" if cq else "")
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", auth)
    req.add_header("x-amz-content-sha256", payload_hash)
    req.add_header("x-amz-date", amzdate)
    with urllib.request.urlopen(req, timeout=timeout,
                                context=_CTX) as r:
        return r.read()


def list_objects(prefix: str = "", delimiter: str = "/", max_keys: int = 200,
                 bucket: str | None = None) -> tuple[list, list]:
    """Return (common_prefixes, keys). Keys are (name, size) tuples."""
    b = bucket or creds()["MASSIVE_BUCKET"]
    q = {"list-type": "2", "max-keys": str(max_keys)}
    if prefix:
        q["prefix"] = prefix
    if delimiter:
        q["delimiter"] = delimiter
    body = request(f"/{b}", q)
    root = ET.fromstring(body)
    pres = [e.text for e in root.iter(f"{_NS}CommonPrefixes")
            for e in e.iter(f"{_NS}Prefix")]
    keys = [(e.findtext(f"{_NS}Key"), int(e.findtext(f"{_NS}Size") or 0))
            for e in root.iter(f"{_NS}Contents")]
    return pres, keys


def list_all(prefix: str, bucket: str | None = None) -> list:
    """Paginate every key under a prefix."""
    b = bucket or creds()["MASSIVE_BUCKET"]
    out, token = [], None
    while True:
        q = {"list-type": "2", "max-keys": "1000", "prefix": prefix}
        if token:
            q["continuation-token"] = token
        root = ET.fromstring(request(f"/{b}", q))
        out += [(e.findtext(f"{_NS}Key"), int(e.findtext(f"{_NS}Size") or 0))
                for e in root.iter(f"{_NS}Contents")]
        if (root.findtext(f"{_NS}IsTruncated") or "false") != "true":
            break
        token = root.findtext(f"{_NS}NextContinuationToken")
        if not token:
            break
    return out


def get(key: str, bucket: str | None = None) -> bytes:
    b = bucket or creds()["MASSIVE_BUCKET"]
    raw = request(f"/{b}/{key}")
    if key.endswith(".gz"):
        return gzip.decompress(raw)
    return raw


def get_csv(key: str, bucket: str | None = None):
    import pandas as pd
    return pd.read_csv(io.BytesIO(get(key, bucket)))
