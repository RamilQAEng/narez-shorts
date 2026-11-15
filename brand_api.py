"""Banner library endpoints (self-host): list, upload, delete. See brand_banner.py."""
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import brand_banner

router = APIRouter(prefix="/api/banners", tags=["banners"])


@router.get("")
def list_banners():
    return {"banners": brand_banner.list_all()}


@router.post("")
def upload_banner(file: UploadFile = File(...), position: str = Form("top")):
    try:
        return brand_banner.save(file.file, file.filename, position)
    except brand_banner.BannerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{banner_id}")
def delete_banner(banner_id: str):
    if not brand_banner.delete(banner_id):
        raise HTTPException(status_code=404, detail="Banner not found")
    return {"deleted": banner_id}


@router.get("/{banner_id}/file")
def banner_file(banner_id: str):
    """The stored banner itself, for the picker's thumbnail."""
    banner = brand_banner.get(banner_id)
    if not banner:
        raise HTTPException(status_code=404, detail="Banner not found")
    return FileResponse(banner["path"])
