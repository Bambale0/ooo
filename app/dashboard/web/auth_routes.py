"""Web UI authentication routes."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.dashboard.web.session import create_session_token

router = APIRouter(tags=["Cabinet Auth"])
templates = Jinja2Templates(directory="app/dashboard/web/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login_handler(request: Request):
    """Handle login via form."""
    form_data = await request.form()
    telegram_id = form_data.get("telegram_id")
    
    if not telegram_id:
        return RedirectResponse(url="/login?error=invalid", status_code=303)
    
    # Create session token
    session_token = create_session_token(telegram_id)
    
    response = RedirectResponse(url="/cabinet", status_code=303)
    response.set_cookie("cabinet_session", session_token, httponly=True, samesite="Lax", max_age=86400*30)
    return response


@router.get("/logout")
async def logout():
    """Logout."""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("cabinet_session")
    return response

