"""Size limits for uploaded files (the app runs on your PC; this stops one bad file filling the disk)."""
from fastapi import HTTPException, UploadFile

MAX_BYTES = {"evidence": 25 * 1024 * 1024, "photo": 15 * 1024 * 1024, "csv": 10 * 1024 * 1024}


async def read_limited(file: UploadFile, kind: str) -> bytes:
    limit = MAX_BYTES[kind]
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File too large (limit {limit // (1024 * 1024)} MB).")
    return data
