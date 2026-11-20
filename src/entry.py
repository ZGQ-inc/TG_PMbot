from workers import Response
from js import fetch, Object
from pyodide.ffi import to_js
import json
import random
import html
import base64
import struct
import traceback

MATRIX_7H = {
    "0": ["0111110", "1100011", "1100011", "1100011", "1100011", "1100011", "0111110"],
    "1": ["0001100", "0111100", "0001100", "0001100", "0001100", "0001100", "0111111"],
    "2": ["0111110", "1100011", "0000011", "0011110", "0110000", "1100000", "1111111"],
    "3": ["0111110", "1100011", "0000011", "0011110", "0000011", "1100011", "0111110"],
    "4": ["0000111", "0001111", "0110011", "1100011", "1111111", "0000011", "0000011"],
    "5": ["1111111", "1100000", "1100000", "0111110", "0000011", "1100011", "0111110"],
    "6": ["0111110", "1100000", "1100000", "1111110", "1100011", "1100011", "0111110"],
    "7": ["1111111", "0000011", "0000110", "0001100", "0011000", "0110000", "0110000"],
    "8": ["0111110", "1100011", "1100011", "0111110", "1100011", "1100011", "0111110"],
    "9": ["0111110", "1100011", "1100011", "0111111", "0000011", "1100011", "0111110"],
}
TRANS_TABLE = str.maketrans({"0": "-", "1": "0"})

def generate_captcha() -> tuple[str, str]:
    code = f"{random.randint(0, 99):02d}"
    d1, d2 = MATRIX_7H[code[0]], MATRIX_7H[code[1]]
    lines = [f"0{l1}000{l2}0".translate(TRANS_TABLE) for l1, l2 in zip(d1, d2)]
    return code, "\n".join(lines)

def rle_decode(data: bytes) -> bytes:
    res = bytearray()
    last = None
    for b in data:
        if last == 0:
            res.extend(b"\x00" * b)
            last = None
        else:
            if last is not None:
                res.append(last)
            last = b
    if last is not None:
        res.append(last)
    return bytes(res)

def extract_dc_id(file_id: str) -> int | None:
    try:
        padded = file_id + "=" * (-len(file_id) % 4)
        raw = base64.urlsafe_b64decode(padded)
        dec = rle_decode(raw)
        if len(dec) < 8:
            return None
        type_id, dc_id = struct.unpack("<II", dec[:8])
        if 1 <= dc_id <= 5:
            return dc_id
        return None
    except Exception:
        return None

async def tg_request(env, method: str, payload: dict | None = None) -> dict:
    token = env.BOT_TOKEN.strip()
    url = f"https://api.telegram.org/bot{token}/{method}"
    if payload is not None:
        init = to_js({
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload)
        }, dict_converter=Object.fromEntries)
        res = await fetch(url, init)
    else:
        res = await fetch(url)
    
    js_data = await res.json()
    if hasattr(js_data, "to_py"):
        return js_data.to_py()
    return js_data

async def set_message_reaction(env, chat_id: int, message_id: int, emoji: str = "👌"):
    try:
        await tg_request(env, "setMessageReaction", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
            "is_big": False
        })
    except Exception as e:
        print(f"Reaction error: {e}")

async def get_user_dc(env, user_id: int) -> str:
    try:
        res = await tg_request(env, "getUserProfilePhotos", {"user_id": user_id, "limit": 1})
        if res.get("ok"):
            photos = res.get("result", {}).get("photos", [])
            if photos and len(photos) > 0 and len(photos[0]) > 0:
                file_id = photos[0][0].get("file_id")
                if file_id:
                    dc = extract_dc_id(file_id)
                    if dc:
                        return f"DC{dc}"
    except Exception as e:
        print(f"Error getting user DC: {e}")
    return "null"

async def check_rate_limit(env, user_id: int) -> bool:
    key = f"ratelimit:{user_id}"
    val = await env.pmbot_captcha.get(key)
    count = int(val) if val else 0
    if count >= 20:
        return False
    await env.pmbot_captcha.put(key, str(count + 1), expirationTtl=60)
    return True

async def send_captcha(env, chat_id: int, original_msg_id: int | None = None):
    code, art = generate_captcha()
    text = (
        "🛡️ <b>请进行人机验证以解锁私聊</b>\n\n"
        f"<pre>{art}</pre>\n\n"
        "✍️ 请直接在此回复图片中的 <b>2 位数字</b> 验证码："
    )
    res = await tg_request(env, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    })
    captcha_msg_id = None
    if res.get("ok"):
        captcha_msg_id = res.get("result", {}).get("message_id")
    
    pending_data = {
        "code": code,
        "captcha_msg_id": captcha_msg_id,
        "original_msg_id": original_msg_id
    }
    await env.pmbot_captcha.put(f"captcha_pending:{chat_id}", json.dumps(pending_data), expirationTtl=300)

async def handle_user_verified_banner(env, user: dict, original_msg_id: int | None = None):
    user_id = user["id"]
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip()
    
    name_display = f"<code>{html.escape(full_name)}</code>" if full_name else "null"
    username = f"@{user['username']}" if user.get("username") else "null"
    uid_display = f"<code>{user_id}</code>"
    dc_display = await get_user_dc(env, user_id)
    
    banner_text = (
        "👤 <b>新用户验证通过</b>\n"
        f"<b>昵称:</b> {name_display}\n"
        f"<b>用户名:</b> {username}\n"
        f"<b>UID:</b> {uid_display}\n"
        f"<b>DC:</b> {dc_display}"
    )
    
    admin_id = int(str(env.ADMIN_UID).strip())
    await tg_request(env, "sendMessage", {
        "chat_id": admin_id,
        "text": banner_text,
        "parse_mode": "HTML"
    })
    
    if original_msg_id:
        fwd_res = await tg_request(env, "forwardMessage", {
            "chat_id": admin_id,
            "from_chat_id": user_id,
            "message_id": original_msg_id
        })
        if fwd_res.get("ok"):
            admin_msg_id = fwd_res["result"]["message_id"]
            await set_message_reaction(env, user_id, original_msg_id, "👌")
            await env.pmbot_captcha.put(
                f"a2u:{admin_msg_id}",
                json.dumps({"user_id": user_id, "user_msg_id": original_msg_id}),
                expirationTtl=2592000
            )
            await env.pmbot_captcha.put(
                f"u2a:{user_id}:{original_msg_id}",
                str(admin_msg_id),
                expirationTtl=2592000
            )

async def on_fetch(request, env):
    if request.method != "POST":
        return Response("TG_PMbot is running.", headers={"Content-Type": "text/plain;charset=utf-8"})
    
    try:
        raw_secret = getattr(env, "TG_WEBHOOK_SECRET", None)
        if raw_secret:
            webhook_secret = str(raw_secret).strip()
            incoming_secret = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
            if incoming_secret != webhook_secret:
                return Response("Unauthorized", status=403)

        raw_body = await request.text()
        if not raw_body:
            return Response("Empty body", status=400)
        
        update = json.loads(raw_body)
        message = update.get("message")
        if not message:
            return Response("OK")
        
        from_user = message.get("from", {})
        sender_id = from_user.get("id")
        chat_id = message.get("chat", {}).get("id")
        msg_id = message.get("message_id")
        text = message.get("text", "") or ""
        admin_id = int(str(env.ADMIN_UID).strip())
        
        if not sender_id or not chat_id:
            return Response("OK")
        
        tokens = text.strip().split()
        cmd = tokens[0].split("@")[0].lower() if tokens else ""

        if sender_id == admin_id:
            reply_to = message.get("reply_to_message")

            if cmd in ["/start", "/help"]:
                admin_welcome = (
                    "👋 <b>欢迎回来，主人！</b>\n\n"
                    "👑 <b>ZGQ_PMbot 管理员面板已就绪</b>\n"
                    f"• <b>您的 UID:</b> <code>{admin_id}</code>\n"
                    "• <b>运行环境:</b> Cloudflare Workers (Python Runtime)\n"
                    "• <b>服务状态:</b> 正常运行中 🟢\n\n"
                    "📖 <b>快捷指令与操作说明:</b>\n"
                    "1. <b>回复用户:</b> 直接【回复】某条用户的转发消息即可，Bot 会自动无引用传递给对方，并带上对原消息的引用。\n"
                    "2. <b>封禁用户:</b> 回复消息输入 <code>/ban [理由]</code>，或输入 <code>/ban &lt;UID&gt; [理由]</code>。\n"
                    "3. <b>解封用户:</b> 回复消息输入 <code>/unban</code>，或输入 <code>/unban &lt;UID&gt;</code>。\n"
                    "4. <b>查看名片:</b> 回复消息输入 <code>/info</code> 随时调取该用户的资料与头像 DC 节点。"
                )
                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": admin_welcome,
                    "parse_mode": "HTML"
                })
                return Response("OK")

            if cmd == "/ban":
                target_user_id = None
                reason = ""

                if reply_to:
                    admin_replied_msg_id = reply_to.get("message_id")
                    mapping_raw = await env.pmbot_captcha.get(f"a2u:{admin_replied_msg_id}")
                    if mapping_raw:
                        target_user_id = json.loads(mapping_raw)["user_id"]
                        if len(tokens) > 1:
                            reason = " ".join(tokens[1:]).strip()
                elif len(tokens) > 1 and tokens[1].isdigit():
                    target_user_id = int(tokens[1])
                    if len(tokens) > 2:
                        reason = " ".join(tokens[2:]).strip()

                if not target_user_id:
                    await tg_request(env, "sendMessage", {
                        "chat_id": admin_id,
                        "text": "💡 <b>用法错误</b>：请回复用户的某条消息输入 <code>/ban [理由]</code>，或直接输入 <code>/ban &lt;UID&gt; [理由]</code>。",
                        "parse_mode": "HTML",
                        "reply_parameters": {"message_id": msg_id}
                    })
                    return Response("OK")

                ban_data = {"reason": reason, "user_id": target_user_id}
                await env.pmbot_captcha.put(f"banned:{target_user_id}", json.dumps(ban_data))

                if reason:
                    user_notice = (
                        "🚫 <b>通知</b>：您已被管理员封禁，无法继续发送消息。\n"
                        f"<b>封禁原因:</b> {html.escape(reason)}"
                    )
                else:
                    user_notice = "🚫 <b>通知</b>：您已被管理员封禁，无法继续发送消息。"

                try:
                    await tg_request(env, "sendMessage", {
                        "chat_id": target_user_id,
                        "text": user_notice,
                        "parse_mode": "HTML"
                    })
                except Exception:
                    pass

                confirm_msg = f"✅ 已成功封禁用户 <code>{target_user_id}</code>。"
                if reason:
                    confirm_msg += f"\n<b>封禁原因:</b> {html.escape(reason)}"
                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": confirm_msg,
                    "parse_mode": "HTML",
                    "reply_parameters": {"message_id": msg_id}
                })
                return Response("OK")

            if cmd == "/unban":
                target_user_id = None
                if reply_to:
                    admin_replied_msg_id = reply_to.get("message_id")
                    mapping_raw = await env.pmbot_captcha.get(f"a2u:{admin_replied_msg_id}")
                    if mapping_raw:
                        target_user_id = json.loads(mapping_raw)["user_id"]
                elif len(tokens) > 1 and tokens[1].isdigit():
                    target_user_id = int(tokens[1])

                if not target_user_id:
                    await tg_request(env, "sendMessage", {
                        "chat_id": admin_id,
                        "text": "💡 <b>用法错误</b>：请回复用户的某条消息输入 <code>/unban</code>，或输入 <code>/unban &lt;UID&gt;</code>。",
                        "parse_mode": "HTML",
                        "reply_parameters": {"message_id": msg_id}
                    })
                    return Response("OK")

                await env.pmbot_captcha.delete(f"banned:{target_user_id}")

                try:
                    await tg_request(env, "sendMessage", {
                        "chat_id": target_user_id,
                        "text": "✅ <b>通知</b>：您的封禁已被管理员解除，现在可以重新发送消息。",
                        "parse_mode": "HTML"
                    })
                except Exception:
                    pass

                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": f"✅ 已成功解除对用户 <code>{target_user_id}</code> 的封禁。",
                    "parse_mode": "HTML",
                    "reply_parameters": {"message_id": msg_id}
                })
                return Response("OK")

            if cmd == "/info":
                if not reply_to:
                    await tg_request(env, "sendMessage", {
                        "chat_id": admin_id,
                        "text": "💡 请直接【回复】某条用户的消息并发送 <code>/info</code> 查看该用户详情。",
                        "parse_mode": "HTML",
                        "reply_parameters": {"message_id": msg_id}
                    })
                    return Response("OK")
                
                admin_replied_msg_id = reply_to.get("message_id")
                mapping_raw = await env.pmbot_captcha.get(f"a2u:{admin_replied_msg_id}")
                if not mapping_raw:
                    await tg_request(env, "sendMessage", {
                        "chat_id": admin_id,
                        "text": "⚠️ 未找到该消息的用户映射记录或已过期（超过30天）。",
                        "reply_parameters": {"message_id": msg_id}
                    })
                    return Response("OK")
                
                target_user_id = json.loads(mapping_raw)["user_id"]
                is_ver = await env.pmbot_captcha.get(f"verified:{target_user_id}")
                is_ban = await env.pmbot_captcha.get(f"banned:{target_user_id}")
                dc_info = await get_user_dc(env, target_user_id)
                
                u_chat = await tg_request(env, "getChat", {"chat_id": target_user_id})
                u_res = u_chat.get("result", {}) if u_chat.get("ok") else {}
                u_name = html.escape(f"{u_res.get('first_name', '')} {u_res.get('last_name', '')}".strip() or "未知")
                u_uname = f"@{u_res['username']}" if u_res.get("username") else "null"
                
                ver_status = "已验证 ✅" if is_ver else "未验证 ⏳"
                ban_status = "已封禁 🚫" if is_ban else "正常 🟢"
                
                info_card = (
                    "📋 <b>用户详情信息</b>\n\n"
                    f"<b>UID:</b> <code>{target_user_id}</code>\n"
                    f"<b>昵称:</b> <code>{u_name}</code>\n"
                    f"<b>用户名:</b> {u_uname}\n"
                    f"<b>数据中心:</b> {dc_info}\n"
                    f"<b>验证状态:</b> {ver_status}\n"
                    f"<b>账号状态:</b> {ban_status}"
                )
                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": info_card,
                    "parse_mode": "HTML",
                    "reply_parameters": {"message_id": msg_id}
                })
                return Response("OK")

            if not reply_to:
                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": "💡 <b>提示</b>：请直接【回复】某条用户的转发消息来发送回复给对方。",
                    "parse_mode": "HTML"
                })
                return Response("OK")
            
            admin_replied_msg_id = reply_to.get("message_id")
            mapping_raw = await env.pmbot_captcha.get(f"a2u:{admin_replied_msg_id}")
            if not mapping_raw:
                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": "⚠️ 未找到对应的用户会话或消息映射已过期（超过30天），无法送达。",
                    "reply_parameters": {"message_id": msg_id}
                })
                return Response("OK")
            
            mapping = json.loads(mapping_raw)
            target_user_id = mapping["user_id"]
            target_user_msg_id = mapping["user_msg_id"]
            
            copy_res = await tg_request(env, "copyMessage", {
                "chat_id": target_user_id,
                "from_chat_id": admin_id,
                "message_id": msg_id,
                "reply_parameters": {
                    "message_id": target_user_msg_id,
                    "allow_sending_without_reply": True
                }
            })
            
            if copy_res.get("ok"):
                delivered_user_msg_id = copy_res["result"]["message_id"]
                await set_message_reaction(env, admin_id, msg_id, "👌")
                await env.pmbot_captcha.put(
                    f"a2u:{msg_id}",
                    json.dumps({"user_id": target_user_id, "user_msg_id": delivered_user_msg_id}),
                    expirationTtl=2592000
                )
                await env.pmbot_captcha.put(
                    f"u2a:{target_user_id}:{delivered_user_msg_id}",
                    str(msg_id),
                    expirationTtl=2592000
                )
            else:
                desc = copy_res.get("description", "")
                err_code = copy_res.get("error_code")
                desc_lower = desc.lower()

                if err_code == 403 or "forbidden" in desc_lower:
                    if "bot was blocked by the user" in desc_lower:
                        fail_text = "🚫 <b>发送失败</b>：对方已将 Bot 屏蔽，消息无法送达。"
                    elif "user is deactivated" in desc_lower:
                        fail_text = "⚠️ <b>发送失败</b>：对方账号已注销，消息无法送达。"
                    else:
                        fail_text = f"🚫 <b>发送失败</b>：无权限向该用户发送消息（{html.escape(desc)}）。"
                else:
                    fail_text = f"❌ <b>发送失败</b>：{html.escape(desc)}"

                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": fail_text,
                    "parse_mode": "HTML",
                    "reply_parameters": {"message_id": msg_id}
                })
            
            return Response("OK")

        banned_raw = await env.pmbot_captcha.get(f"banned:{sender_id}")
        if banned_raw:
            if cmd == "/start":
                banned_data = json.loads(banned_raw) if banned_raw != "true" else {}
                r = banned_data.get("reason")
                if r:
                    t = f"🚫 您已被封禁，无法使用此机器人。\n<b>原因:</b> {html.escape(r)}"
                else:
                    t = "🚫 您已被封禁，无法使用此机器人。"
                await tg_request(env, "sendMessage", {
                    "chat_id": sender_id,
                    "text": t,
                    "parse_mode": "HTML"
                })
            return Response("OK")

        if not await check_rate_limit(env, sender_id):
            await tg_request(env, "sendMessage", {
                "chat_id": sender_id,
                "text": "⚠️ <b>发送过于频繁</b>，请稍候再试。",
                "parse_mode": "HTML"
            })
            return Response("OK")

        is_verified = await env.pmbot_captcha.get(f"verified:{sender_id}")
        
        if not is_verified:
            if cmd == "/start":
                await send_captcha(env, sender_id, None)
                return Response("OK")

            pending_raw = await env.pmbot_captcha.get(f"captcha_pending:{sender_id}")
            if pending_raw:
                pending = json.loads(pending_raw)
                expected_code = pending.get("code")
                cleaned_input = text.strip()
                
                if cleaned_input == expected_code:
                    await env.pmbot_captcha.put(f"verified:{sender_id}", "true")
                    await env.pmbot_captcha.delete(f"captcha_pending:{sender_id}")
                    
                    if pending.get("captcha_msg_id"):
                        try:
                            await tg_request(env, "deleteMessage", {
                                "chat_id": sender_id,
                                "message_id": pending["captcha_msg_id"]
                            })
                        except Exception:
                            pass
                    
                    await set_message_reaction(env, sender_id, msg_id, "👌")

                    await tg_request(env, "sendMessage", {
                        "chat_id": sender_id,
                        "text": "✅ <b>验证成功！</b>\n您的会话已解锁，通知消息已成功发送给主人。",
                        "parse_mode": "HTML"
                    })
                    
                    orig_id = pending.get("original_msg_id")
                    await handle_user_verified_banner(env, from_user, orig_id)
                    return Response("OK")
                else:
                    await tg_request(env, "sendMessage", {
                        "chat_id": sender_id,
                        "text": "❌ <b>验证码错误</b>，请仔细观察上方字符画展示的 2 位数字并重新输入：",
                        "parse_mode": "HTML"
                    })
                    return Response("OK")
            
            await send_captcha(env, sender_id, msg_id)
            return Response("OK")
        
        if cmd == "/start":
            visitor_welcome = (
                "👋 <b>您好！欢迎使用私聊机器人</b>\n\n"
                "✅ <b>您已通过人机验证</b>\n"
                "请直接在下方发送您想咨询的内容（支持文字、图片、语音、文件等），主人收到后会尽快给您回复！"
            )
            await tg_request(env, "sendMessage", {
                "chat_id": sender_id,
                "text": visitor_welcome,
                "parse_mode": "HTML"
            })
            return Response("OK")
        
        fwd_res = await tg_request(env, "forwardMessage", {
            "chat_id": admin_id,
            "from_chat_id": sender_id,
            "message_id": msg_id
        })
        
        if fwd_res.get("ok"):
            admin_msg_id = fwd_res["result"]["message_id"]
            await set_message_reaction(env, sender_id, msg_id, "👌")
            
            await env.pmbot_captcha.put(
                f"a2u:{admin_msg_id}",
                json.dumps({"user_id": sender_id, "user_msg_id": msg_id}),
                expirationTtl=2592000
            )
            await env.pmbot_captcha.put(
                f"u2a:{sender_id}:{msg_id}",
                str(admin_msg_id),
                expirationTtl=2592000
            )
            
        return Response("OK")
        
    except Exception as e:
        print(f"Exception in on_fetch: {e}\n{traceback.format_exc()}")
        return Response("OK")
