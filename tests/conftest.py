import os
import uuid

import pytest

ENV = {
    "endpoint_url": "FAST_S3_TEST_ENDPOINT",
    "aws_access_key_id": "FAST_S3_TEST_KEY",
    "aws_secret_access_key": "FAST_S3_TEST_SECRET",
    "bucket_name": "FAST_S3_TEST_BUCKET",
}


@pytest.fixture(scope="session")
def s3_config():
    """Connection kwargs for a real (or MinIO) endpoint; skips if not configured."""
    if not all(os.environ.get(v) for v in ENV.values()):
        pytest.skip(
            "set FAST_S3_TEST_ENDPOINT/KEY/SECRET/BUCKET to run integration tests"
        )
    return {
        **{k: os.environ[v] for k, v in ENV.items()},
        "region_name": os.environ.get("FAST_S3_TEST_REGION", "us-east-1"),
    }


@pytest.fixture
def prefix():
    return f"fast-s3-test/{uuid.uuid4().hex}"
