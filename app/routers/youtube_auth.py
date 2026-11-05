"""Simplified YouTube login: paste cookies into one page, and the API checks and keeps them fresh.

Guarded by HTTP Basic auth (the browser's own prompt; any username, ADMIN_TOKEN as password).
"""

import html
import secrets
from datetime import datetime
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .. import config
from ..cookies import CookieStore, parse

_MAX_BODY_BYTES = 1024 * 1024


def _admin(credentials: Annotated[HTTPBasicCredentials, Depends(HTTPBasic())]) -> None:
    if not config.ADMIN_TOKEN:
        raise HTTPException(503, "Set ADMIN_TOKEN to enable YouTube login")
    if not secrets.compare_digest(credentials.password.encode(), config.ADMIN_TOKEN.encode()):
        raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": "Basic"})


router = APIRouter(prefix="/auth/youtube", dependencies=[Depends(_admin)])


def _store(request: Request) -> CookieStore:
    return request.app.state.cookies


def _status(store: CookieStore) -> dict:
    return {
        "cookies": len(store),
        "logged_in": store.logged_in,
        "last_refresh": store.last_refresh,
    }


@router.get("/status")
async def status(request: Request) -> dict:
    return _status(_store(request))


@router.post("")
async def import_cookies(request: Request):
    """Form post from the page, or `curl --data-binary @cookies.txt` (any non-form body is the cookie text)."""
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(413, "Cookie text is too large")
    text = body.decode("utf-8", "replace")
    # Only the page's form has a "cookies" field; curl sends raw text with a form content type by default.
    form = parse_qs(text).get("cookies") if "cookies=" in text and "\t" not in text else None
    from_form = form is not None
    if form:
        text = form[0]
    jar = parse(text)
    if not len(jar):
        raise HTTPException(422, "No cookies found. Paste a cookies.txt export or a Cookie header.")
    store = _store(request)
    store.replace(jar)
    await store.refresh()
    if from_form:
        return RedirectResponse(request.url.path, status_code=303)
    return JSONResponse(_status(store))


@router.delete("")
async def forget(request: Request) -> dict:
    store = _store(request)
    store.replace(parse(""))
    return _status(store)


@router.post("/refresh")
async def refresh(request: Request) -> dict:
    store = _store(request)
    await store.refresh()
    return _status(store)


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> str:
    store = _store(request)
    state = {None: "未確認", True: "ログイン済み ✅", False: "未ログイン ❌（Cookie を入れ直してください）"}
    last = datetime.fromtimestamp(store.last_refresh).strftime("%Y-%m-%d %H:%M:%S") if store.last_refresh else "-"
    return _PAGE.format(
        state=html.escape(state[store.logged_in]), count=len(store), last=html.escape(last),
        minutes=config.COOKIE_REFRESH_MINUTES,
    )


_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Create: Media Systems API — YouTube ログイン</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.6; }}
  textarea {{ width: 100%; height: 12rem; font-family: monospace; }}
  dl {{ display: grid; grid-template-columns: max-content auto; gap: .2rem 1rem; }}
</style></head><body>
<h1>YouTube ログイン</h1>
<dl>
  <dt>状態</dt><dd>{state}</dd>
  <dt>Cookie 数</dt><dd>{count}</dd>
  <dt>最終更新</dt><dd>{last}（{minutes}分ごとに自動更新）</dd>
</dl>
<h2>Cookie を入れる</h2>
<ol>
  <li>ブラウザのプライベートウィンドウで YouTube にログインします（このAPI専用のアカウント推奨）。</li>
  <li>次のどちらかをコピーします。
    <ul>
      <li>拡張機能「Get cookies.txt LOCALLY」で youtube.com の cookies.txt を書き出した中身</li>
      <li>開発者ツールの「ネットワーク」で youtube.com へのリクエストを選び、リクエストヘッダー <code>cookie</code> の値</li>
    </ul>
  </li>
  <li>下に貼り付けて保存します。</li>
  <li>プライベートウィンドウは<strong>ログアウトせずに</strong>閉じてください（ログアウトすると Cookie が無効になります）。</li>
</ol>
<form method="post">
  <textarea name="cookies" required placeholder="# Netscape HTTP Cookie File ... または SID=...; HSID=...; ..."></textarea>
  <p><button>保存してログインを確認</button></p>
</form>
</body></html>
"""
