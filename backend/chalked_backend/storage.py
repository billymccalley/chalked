from __future__ import annotations

import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .security import new_id


def s3_upload_configured() -> bool:
    return bool(
        os.environ.get("CHALKED_UPLOAD_BUCKET")
        and os.environ.get("CHALKED_UPLOAD_ENDPOINT")
        and os.environ.get("CHALKED_UPLOAD_ACCESS_KEY_ID")
        and os.environ.get("CHALKED_UPLOAD_SECRET_ACCESS_KEY")
    )


def production_uploads_require_object_storage() -> bool:
    explicit = os.environ.get("CHALKED_REQUIRE_OBJECT_STORAGE")
    if explicit is not None:
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get("CHALKED_ENV", "").strip().lower() in {"prod", "production"}


def upload_storage_status() -> dict:
    configured = s3_upload_configured()
    required = production_uploads_require_object_storage()
    return {
        "configured": configured,
        "required": required,
        "mode": "s3" if configured else "local",
        "local_allowed": configured or not required,
        "bucket": os.environ.get("CHALKED_UPLOAD_BUCKET") if configured else None,
        "public_url": os.environ.get("CHALKED_UPLOAD_PUBLIC_URL", "").rstrip("/") or None,
        "prefix": os.environ.get("CHALKED_UPLOAD_PREFIX", "uploads").strip("/") or "uploads",
    }


def upload_image(raw: bytes, mime: str, ext: str, upload_root: Path) -> dict:
    filename = f"{new_id('img')}{ext}"
    if s3_upload_configured():
        key = f"{os.environ.get('CHALKED_UPLOAD_PREFIX', 'uploads/').strip('/')}/{filename}"
        return upload_s3_compatible(raw, mime, key)
    if production_uploads_require_object_storage():
        raise RuntimeError("Object storage is required for production uploads")
    upload_root.mkdir(parents=True, exist_ok=True)
    target = (upload_root / filename).resolve()
    if not str(target).startswith(str(upload_root.resolve())):
        raise ValueError("Invalid upload path")
    target.write_bytes(raw)
    return {"url": f"/uploads/{filename}", "storage": "local"}


def upload_s3_compatible(raw: bytes, mime: str, key: str) -> dict:
    bucket = os.environ["CHALKED_UPLOAD_BUCKET"].strip()
    endpoint = os.environ["CHALKED_UPLOAD_ENDPOINT"].rstrip("/")
    access_key = os.environ["CHALKED_UPLOAD_ACCESS_KEY_ID"]
    secret_key = os.environ["CHALKED_UPLOAD_SECRET_ACCESS_KEY"]
    region = os.environ.get("CHALKED_UPLOAD_REGION", "auto")
    public_url = os.environ.get("CHALKED_UPLOAD_PUBLIC_URL", "").rstrip("/")
    quoted_key = "/".join(urllib.parse.quote(part, safe="") for part in key.split("/"))
    parsed = urllib.parse.urlparse(endpoint)
    host = parsed.netloc
    path = f"/{bucket}/{quoted_key}"
    url = f"{endpoint}{path}"
    payload_hash = hashlib.sha256(raw).hexdigest()
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"content-type:{mime}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    canonical_request = "\n".join(
        ["PUT", path, "", canonical_headers, signed_headers, payload_hash]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = signature_key(secret_key, date_stamp, region, "s3")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    request = urllib.request.Request(
        url,
        method="PUT",
        data=raw,
        headers={
            "Authorization": authorization,
            "Content-Type": mime,
            "Host": host,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": amz_date,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status not in (200, 201, 204):
                raise RuntimeError(f"Upload failed with status {response.status}")
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Upload failed: {exc}") from exc
    if public_url:
        url = f"{public_url}/{quoted_key}"
    return {"url": url, "storage": "s3", "key": key}


def signature_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    key = ("AWS4" + secret_key).encode("utf-8")
    date_key = hmac.new(key, date_stamp.encode("utf-8"), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
