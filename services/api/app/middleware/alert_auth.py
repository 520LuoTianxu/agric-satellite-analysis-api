"""读取前端联合登录上下文，预警服务不解析 token 或调用农业登录校验。"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException


@dataclass(frozen=True)
class AlertContext:
    base_id: str
    user_id: str


async def get_alert_context(
    hr_base_id: Annotated[str | None, Header(alias="Hr-Base-Id")] = None,
    account_id: Annotated[str | None, Header(alias="X-Account-Id")] = None,
) -> AlertContext:
    """按传入的基地和账号 ID 划分已读记录，仅校验标识格式。"""
    base_id = (hr_base_id or "").strip()
    user_id = (account_id or "").strip()
    for value, max_length, detail in (
        (base_id, 64, "缺少或无效的租户基地 ID（Hr-Base-Id）"),
        (user_id, 128, "缺少或无效的账号 ID（X-Account-Id）"),
    ):
        if (
            len(value) > max_length
            or not value.isascii()
            or not value.isdecimal()
            or int(value) <= 0
        ):
            raise HTTPException(400, detail)

    # accountId 来自 jointLoginData.accountRoleList[0]，角色切换不改变个人已读归属。
    # 此处按客户端传入标识划分数据范围，不承担登录认证或基地授权验证。
    return AlertContext(base_id=str(int(base_id)), user_id=str(int(user_id)))
