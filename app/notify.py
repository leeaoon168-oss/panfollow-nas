#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载完成/失败的通知。

支持渠道：
- pushplus   ：微信（公众号推送），注册拿 token 即可，最省事
- wecom      ：企业微信群机器人（免费、额度高、最稳）
- wxpusher   ：微信（WxPusher 公众号）
- bark       ：iOS
- serverchan ：Server酱（微信，免费额度少）
- webhook    ：自定义
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

UA = {"User-Agent": "kdocs-tracker/1.0"}

# 应用内推送的接收器（server 启动时注册），用来在 App 里弹通知
_SINK = None


def set_sink(fn) -> None:
    global _SINK
    _SINK = fn


def configured() -> bool:
    return bool(settings_type() and settings_key())


def settings_type() -> str:
    import settings as S
    return (S.get("notify_type", "") or "").strip()


def settings_key() -> str:
    import settings as S
    return (S.get("notify_key", "") or "").strip()


def send(title: str, body: str) -> tuple[bool, str]:
    t, k = settings_type(), settings_key()
    if not t or not k:
        return False, "未配置通知"
    try:
        with httpx.Client(timeout=20, headers=UA, follow_redirects=True) as c:
            # ---- 微信：PushPlus ----
            if t == "pushplus":
                r = c.post("https://www.pushplus.plus/send",
                           json={"token": k, "title": title, "content": body,
                                 "template": "txt"})
                ok = r.status_code == 200 and '"code":200' in r.text.replace(" ", "")
                return ok, r.text[:200]

            # ---- 微信：企业微信群机器人 ----
            if t == "wecom":
                hook = k if k.startswith("http") else \
                    f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={k}"
                r = c.post(hook, json={"msgtype": "text",
                                       "text": {"content": f"{title}\n{body}"}})
                ok = r.status_code == 200 and '"errcode":0' in r.text.replace(" ", "")
                return ok, r.text[:200]

            # ---- 微信：WxPusher ----
            if t == "wxpusher":
                parts = [x.strip() for x in k.split(",") if x.strip()]
                app_token = parts[0] if parts else ""
                uids = parts[1:] if len(parts) > 1 else []
                payload = {"appToken": app_token, "content": f"{title}\n{body}",
                           "summary": title[:60], "contentType": 1}
                if uids:
                    payload["uids"] = uids
                r = c.post("https://wxpusher.zjiecode.com/api/send/message", json=payload)
                ok = r.status_code == 200 and '"code":1000' in r.text.replace(" ", "")
                return ok, r.text[:200]

            # ---- 企业微信「自建应用」（不需要群、不需要实名）----
            # key 格式：corpid:secret:agentid:接收人(默认 @all)
            if t == "wework_app":
                parts = k.split(":")
                if len(parts) < 3:
                    return False, "格式应为 corpid:secret:agentid[:toUser]"
                corpid, secret, agentid = parts[0], parts[1], parts[2]
                touser = parts[3] if len(parts) > 3 and parts[3] else "@all"
                r = c.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                          params={"corpid": corpid, "corpsecret": secret})
                tok = (r.json() or {}).get("access_token")
                if not tok:
                    return False, r.text[:200]
                r = c.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
                           params={"access_token": tok},
                           json={"touser": touser, "msgtype": "text", "agentid": int(agentid),
                                 "text": {"content": f"{title}\n{body}"}})
                ok = '"errcode":0' in r.text.replace(" ", "")
                return ok, r.text[:200]

            # ---- 微信公众号「测试号」模板消息（直达微信，不需要实名）----
            # key 格式：appid:secret:template_id:openid
            if t == "wxmp":
                parts = k.split(":")
                if len(parts) < 4:
                    return False, "格式应为 appid:secret:template_id:openid"
                appid, secret, tpl, openid = parts[0], parts[1], parts[2], parts[3]
                r = c.get("https://api.weixin.qq.com/cgi-bin/token",
                          params={"grant_type": "client_credential",
                                  "appid": appid, "secret": secret})
                tok = (r.json() or {}).get("access_token")
                if not tok:
                    return False, r.text[:200]
                data = {
                    "touser": openid,
                    "template_id": tpl,
                    "url": "",
                    "data": {
                        "first": {"value": title[:100]},
                        "keyword1": {"value": title[:60]},
                        "keyword2": {"value": body[:200]},
                        "remark": {"value": "来自 NAS 追更"},
                    },
                }
                r = c.post("https://api.weixin.qq.com/cgi-bin/message/template/send",
                           params={"access_token": tok}, json=data)
                ok = '"errcode":0' in r.text.replace(" ", "")
                return ok, r.text[:200]

            # ---- 邮件（配合微信「QQ邮箱提醒」＝ 变相微信通知）----
            # key 格式：smtp主机:端口:账号:授权码:收件人
            if t == "email":
                import smtplib
                from email.header import Header
                from email.mime.text import MIMEText
                from email.utils import formataddr
                parts = k.split(":")
                if len(parts) < 5:
                    return False, "格式应为 smtp主机:端口:账号:授权码:收件人"
                host, port, user, pwd, to = parts[0], int(parts[1]), parts[2], parts[3], parts[4]
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"] = Header(title, "utf-8")
                msg["From"] = formataddr((str(Header("追更", "utf-8")), user))
                msg["To"] = to
                if port == 465:
                    srv = smtplib.SMTP_SSL(host, port, timeout=20)
                else:
                    srv = smtplib.SMTP(host, port, timeout=20)
                    try:
                        srv.starttls()
                    except Exception:
                        pass
                srv.login(user, pwd)
                srv.sendmail(user, [to], msg.as_string())
                srv.quit()
                return True, "邮件已发出"

            # ---- iOS：Bark ----
            if t == "bark":
                base = k.rstrip("/")
                url = f"{base}/{quote(title)}/{quote(body)}"
                r = c.get(url)
                return (r.status_code == 200), r.text[:160]

            # ---- Server酱 ----
            if t == "serverchan":
                key = k.replace("https://sctapi.ftqq.com/", "").replace(".send", "")
                r = c.post(f"https://sctapi.ftqq.com/{key}.send",
                           data={"title": title, "desp": body})
                ok = r.status_code == 200 and '"code":0' in r.text.replace(" ", "")
                return ok, r.text[:160]

            # ---- 自定义 Webhook ----
            if t == "webhook":
                r = c.post(k, json={"title": title, "body": body})
                return (200 <= r.status_code < 300), r.text[:160]

        return False, f"未知类型 {t}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def notify(title: str, body: str) -> None:
    """发了就完，不抛异常；成功与否都记进历史。"""
    ok, msg = False, ""
    try:
        ok, msg = send(title, body)
        print(f"[notify] {ok} {msg[:100]}", flush=True)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[notify] 异常 {e}", flush=True)
    try:
        import store
        store.add_notification(title, body, ok, msg, settings_type())
    except Exception:
        pass
    try:
        if _SINK:
            _SINK("notify", title, body)
    except Exception:
        pass
