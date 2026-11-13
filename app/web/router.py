"""Public static bits of the web app. Temporary: the /clip-test page for the Step 0 phone test (no data, no login)."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC = Path(__file__).parent / "static"
router = APIRouter(tags=["web"])


@router.get("/clip-test", include_in_schema=False)
def clip_test() -> FileResponse:
    return FileResponse(STATIC / "clip-test.html", headers={"Cache-Control": "no-store"})


@router.get("/clip-test.webmanifest", include_in_schema=False)
def clip_test_manifest() -> FileResponse:
    return FileResponse(STATIC / "clip-test.webmanifest", media_type="application/manifest+json")


@router.get("/clip-test-sw.js", include_in_schema=False)
def clip_test_sw() -> FileResponse:
    return FileResponse(STATIC / "clip-test-sw.js", media_type="text/javascript", headers={"Service-Worker-Allowed": "/clip-test"})


@router.get("/icons/{name}", include_in_schema=False)
def icon(name: str) -> FileResponse:
    path = (STATIC / "icons" / name).resolve()
    if path.parent != (STATIC / "icons").resolve() or not path.is_file():
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")
