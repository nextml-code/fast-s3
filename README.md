# Download/upload images from/to s3 very quickly

## Setup

```
poetry install
```

## Usage

Download image files

```python
from PIL import Image
from fast_s3 import Fetcher, Status


large_list_of_image_paths = [...]

fetcher = Fetcher(
    paths=large_list_of_image_paths,
    endpoint_url="https://s3.my-path-to-s3",
    aws_access_key_id="my-key-id",
    aws_secret_access_key="my-secret-key",
    region_name="my-region",
    bucket_name="my-bucket",
    ordered=True,  # returns files in the same order as paths
    buffer_size=1024,
    n_workers=32,
)

for file in fetcher:
    if file.status != Status.error:
        Image.open(file.buffer).save(file.path)

fetcher.close()
```

Upload files

```python
from fast_s3 import Uploader


large_list_of_files = [...]
large_list_of_paths = [...]

uploader = Uploader(
    endpoint_url="https://s3.my-path-to-s3",
    aws_access_key_id="my-key-id",
    aws_secret_access_key="my-secret-key",
    region_name="my-region",
    bucket_name="my-bucket",
)

uploader.upload_files(
    source=large_list_of_files,
    destination=large_list_of_paths,
)
uploader.await_futures()
uploader.close()
```
