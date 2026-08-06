"""网易云登录：扫码登录 + 手机号验证码登录（获取登录 Cookie，解锁 VIP 歌曲直链）"""

import asyncio
import io
import time

import qrcode

from .log import get_logger

logger = get_logger()

# 扫码登录
QR_UNIKEY_URL = "https://music.163.com/api/login/qrcode/unikey"
QR_CHECK_URL = "https://music.163.com/api/login/qrcode/client/login"
# 手机验证码登录（验证码发送走明文接口，登录校验需加密参数）
SMS_SEND_URL = "https://music.163.com/api/sms/captcha/sent"


def _headers() -> dict:
    """网易云接口请求头（带基础 Cookie，降低风控概率）"""
    return {
        "Referer": "https://music.163.com/",
        "Cookie": "os=pc; appver=2.2.16; NMTID=00000000000000000000000000000000",
        "X-Real-IP": "",
    }


async def qrcode_login_get(session) -> str:
    """获取扫码登录的 unikey（异步），失败抛异常"""
    def _do():
        r = session.post(
            QR_UNIKEY_URL,
            params={"type": 1, "time": str(int(time.time() * 1000))},
            headers=_headers(),
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"获取二维码失败 HTTP {r.status_code}")
        j = r.json()
        if j.get("code") != 200:
            raise RuntimeError(f"获取二维码失败: code={j.get('code')} {j.get('message', '')}")
        return j.get("unikey", "")

    unikey = await asyncio.to_thread(_do)
    if not unikey:
        raise RuntimeError("获取二维码失败: 未返回 unikey")
    return unikey


async def qrcode_check(session, unikey: str) -> dict:
    """轮询二维码状态，返回 {code, message}（800 过期 / 801 等待扫码 / 802 已扫码待确认 / 803 成功+cookie）"""
    def _do():
        r = session.get(
            QR_CHECK_URL,
            params={"type": 1, "key": unikey},
            headers=_headers(),
            timeout=15,
        )
        # 803 登录成功：cookie 在响应头 Set-Cookie 中
        cookies = []
        for chunk in str(r.headers.get("set-cookie", "")).split(","):
            first = chunk.strip().split(";", 1)[0]
            if first and "=" in first:
                cookies.append(first)
        try:
            j = r.json()
        except Exception:
            j = {}
        return {"code": j.get("code"), "message": j.get("message", ""), "cookies": cookies, "raw": r}

    return await asyncio.to_thread(_do)


async def sms_send(session, phone: str) -> dict:
    """发送短信验证码（明文接口已确认可用）；返回 {ok, message}"""
    def _do():
        r = session.post(
            SMS_SEND_URL,
            data={"cellphone": phone, "ctcode": "86", "token": ""},
            headers=_headers(),
            timeout=15,
        )
        try:
            j = r.json()
        except Exception:
            return {"ok": False, "message": f"接口响应异常: HTTP {r.status_code}"}
        if j.get("code") == 200:
            return {"ok": True, "message": "验证码已发送，请查收短信（5 分钟内有效）"}
        if j.get("code") == 405:
            return {"ok": False, "message": "该手机号发送过于频繁，请稍后再试"}
        if j.get("code") == 403:
            return {"ok": False, "message": "该手机号被风控，请改用扫码登录"}
        return {"ok": False, "message": f"发送失败: code={j.get('code')} {j.get('message', '')}"}

    return await asyncio.to_thread(_do)


async def sms_login(session, phone: str, captcha: str) -> dict:
    """验证码登录：构造 weapi 加密参数，返回 {ok, message, cookie}"""
    from .crypto import weapi_params

    def _do():
        data = {"ctcode": "86", "cellphone": phone, "captcha": captcha, "rememberLogin": "true"}
        params = weapi_params(data)
        r = session.post(
            "https://music.163.com/weapi/login/cellphone",
            data=params,
            headers=_headers(),
            timeout=15,
        )
        if r.status_code != 200:
            return {"ok": False, "message": f"HTTP {r.status_code}"}
        # 服务器空响应说明被风控（可加代理解决）
        if not r.content:
            return {"ok": False, "message": "登录请求返回空（可能被网易云风控，请配置代理或改用扫码登录）"}
        try:
            j = r.json()
        except Exception:
            return {"ok": False, "message": "响应解析失败"}
        if j.get("code") == 200:
            cookies = []
            for chunk in str(r.headers.get("set-cookie", "")).split(","):
                first = chunk.strip().split(";", 1)[0]
                if first and "=" in first:
                    cookies.append(first)
            cookie_str = "; ".join(cookies)
            return {"ok": True, "message": "登录成功", "cookie": cookie_str}
        return {"ok": False, "message": f"登录失败: code={j.get('code')} {j.get('message', '')}"}

    return await asyncio.to_thread(_do)


def make_qrcode_image(data: str) -> bytes:
    """生成二维码 PNG 字节（供 Image.fromBytes 发送）"""
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()