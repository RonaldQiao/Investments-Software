from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..db import create_fund
from ..web import flash_redirect

router = APIRouter()


@router.post("/funds/switch")
def switch_fund(request: Request, fund: str = Form(...)):
    referer = request.headers.get("referer")
    response = RedirectResponse(urlsplit(referer).path or "/" if referer else "/", status_code=303)
    response.set_cookie("fund", fund, max_age=365 * 24 * 60 * 60, path="/", samesite="lax")
    return response


@router.post("/funds")
def add_fund(name: str = Form(...)):
    try:
        slug = create_fund(name)
    except ValueError as exc:
        return flash_redirect("/settings", "error", str(exc))
    response = RedirectResponse("/settings?ok=Fund created", status_code=303)
    response.set_cookie("fund", slug, max_age=365 * 24 * 60 * 60, path="/", samesite="lax")
    return response
