"""Signature test vectors from the AWS S3 SigV4 documentation
(https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html)."""

import datetime
import hashlib

from fast_s3._signer import SigV4Signer, uri_encode_path

ACCESS = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
WHEN = datetime.datetime(2013, 5, 24, 0, 0, 0, tzinfo=datetime.timezone.utc)
HOST = "examplebucket.s3.amazonaws.com"


def _signature(headers):
    return headers["Authorization"].rsplit("Signature=", 1)[1]


def test_get_object_vector():
    signer = SigV4Signer(ACCESS, SECRET, "us-east-1")
    headers = signer.sign("GET", HOST, "/test.txt", {"Range": "bytes=0-9"}, now=WHEN)
    assert headers["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, Signature="
    )
    assert (
        _signature(headers)
        == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert headers["Range"] == "bytes=0-9"


def test_put_object_vector():
    signer = SigV4Signer(ACCESS, SECRET, "us-east-1")
    body = b"Welcome to Amazon S3."
    payload_hash = hashlib.sha256(body).hexdigest()
    assert (
        payload_hash
        == "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    )
    headers = signer.sign(
        "PUT",
        HOST,
        "/" + uri_encode_path("test$file.text"),
        {
            "Date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
        payload_hash=payload_hash,
        now=WHEN,
    )
    assert (
        _signature(headers)
        == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


def test_signing_key_is_cached_per_day():
    signer = SigV4Signer(ACCESS, SECRET, "us-east-1")
    signer.sign("GET", HOST, "/a", now=WHEN)
    key_day1 = signer._cached_key
    signer.sign("GET", HOST, "/b", now=WHEN + datetime.timedelta(hours=1))
    assert signer._cached_key is key_day1
    signer.sign("GET", HOST, "/c", now=WHEN + datetime.timedelta(days=1))
    assert signer._cached_key != key_day1


def test_uri_encode_path():
    assert uri_encode_path("a b/c+d/e~f.g") == "a%20b/c%2Bd/e~f.g"
    assert uri_encode_path("plain/key.webp") == "plain/key.webp"
