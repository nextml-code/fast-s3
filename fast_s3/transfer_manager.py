import boto3
import boto3.s3.transfer as s3transfer
import botocore
from s3transfer.manager import TransferManager


def transfer_manager(
    endpoint_url: str,
    aws_access_key_id: str,
    aws_secret_access_key: str,
    region_name: str,
    n_workers=32,
    **kwargs,
) -> TransferManager:
    session = boto3.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name,
    )
    client = session.client(
        "s3",
        endpoint_url=endpoint_url,
        config=botocore.config.Config(max_pool_connections=n_workers),
    )
    transfer_config = s3transfer.TransferConfig(
        use_threads=True, max_concurrency=n_workers, **kwargs
    )
    return s3transfer.create_transfer_manager(client, transfer_config)
