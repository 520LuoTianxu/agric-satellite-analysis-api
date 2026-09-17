"""预警使用农业登录身份；个人已读不能沿用旧接口的匿名用户。"""

from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import Header, HTTPException, Request

from app.core.config import settings


@dataclass(frozen=True)
class AlertContext:
    base_id: str
    user_id: str


async def _agric_get(client: httpx.AsyncClient, path: str, headers: dict, **kwargs):
    # 只向服务端配置的农业网关透传凭证，不接收客户端提供的目标地址。
    try:
        response = await client.get(
            f"{settings.cdfinance_soil_base_url.rstrip('/')}{path}",
            headers=headers,
            timeout=15.0,
            **kwargs,
        )
        if response.status_code in (401, 403):
            raise HTTPException(response.status_code, "农业登录已失效或无访问权限")
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "暂时无法验证农业登录，请稍后重试") from exc
    if not isinstance(body, dict):
        raise HTTPException(502, "农业身份服务响应格式异常")
    code = str(body.get("code", ""))
    if code in ("401", "403"):
        raise HTTPException(int(code), "农业登录已失效或无访问权限")
    if code != "200" or body.get("success") is False:
        raise HTTPException(502, "农业身份服务验证失败")
    return body


async def get_alert_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    hr_base_id: Annotated[str | None, Header(alias="Hr-Base-Id")] = None,
) -> AlertContext:
    """从农业服务验证用户与基地授权，禁止匿名或跨租户读写预警。"""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "请先登录")
    base_id = (hr_base_id or "").strip()
    if (
        len(base_id) > 64
        or not base_id.isascii()
        or not base_id.isdecimal()
        or int(base_id) <= 0
    ):
        raise HTTPException(400, "请选择有效的租户基地")
    base_id = str(int(base_id))

    client = request.app.state.http_client
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Hr-Base-Id": "-1",
        "Source-Channel": "WEB",
    }
    info = await _agric_get(client, "/getInfo", headers)
    user = info.get("user")
    user_id = user.get("userId") if isinstance(user, dict) else None
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
        raise HTTPException(401, "无法识别当前登录用户")
    user_id = str(user_id).strip()
    if not user_id or len(user_id) > 128:
        raise HTTPException(401, "无法识别当前登录用户")

    # 基地列表由农业服务按该用户的 baseIds 过滤；分页检查避免漏掉后续基地。
    page = 1
    while True:
        body = await _agric_get(
            client,
            "/agriculture/baseinfo/list",
            headers,
            params={"pageNum": page, "pageSize": 200},
        )
        rows = body.get("rows")
        if not isinstance(rows, list):
            raise HTTPException(502, "农业基地权限响应格式异常")
        if any(
            isinstance(row, dict) and str(row.get("baseId")) == base_id for row in rows
        ):
            return AlertContext(base_id=base_id, user_id=user_id)
        total = body.get("total")
        if not rows or (isinstance(total, int) and page * 200 >= total):
            break
        if total is None or page >= 100:
            break
        page += 1
    raise HTTPException(403, "无权访问该租户的预警")
