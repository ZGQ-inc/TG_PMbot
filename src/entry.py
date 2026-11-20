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
    url = f"https://api.telegram.org/bot{env.BOT_TOKEN}/{method}"
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

async def send_captcha(env, chat_id: int, original_msg_id: int | None = None):
    code, art = generate_captcha()
    text = (
        "🛡️ <b>请进行人机验证以解锁私聊</b>\n\n"
        f"<pre>{art}</pre>\n\n"
        "✍️ 请直接回复图片中的 <b>2 位数字</b> 验证码。"
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
    # 5 minutes expiration
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
    
    # Send banner to Admin
    admin_id = int(env.ADMIN_UID)
    await tg_request(env, "sendMessage", {
        "chat_id": admin_id,
        "text": banner_text,
        "parse_mode": "HTML"
    })
    
    # If there was an original message sent before verification, forward it now
    if original_msg_id:
        fwd_res = await tg_request(env, "forwardMessage", {
            "chat_id": admin_id,
            "from_chat_id": user_id,
            "message_id": original_msg_id
        })
        if fwd_res.get("ok"):
            admin_msg_id = fwd_res["result"]["message_id"]
            # Save mapping (30 days TTL)
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
    # Only allow POST (for Telegram Webhook)
    if request.method != "POST":
        return Response("TG_PMbot (Cloudflare Python Worker) is running.", headers={"Content-Type": "text/plain;charset=utf-8"})
    
    try:
        # Check Telegram Webhook secret token if configured
        webhook_secret = getattr(env, "TG_WEBHOOK_SECRET", None)
        if webhook_secret:
            incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
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
        text = message.get("text", "")
        admin_id = int(env.ADMIN_UID)
        
        if not sender_id or not chat_id:
            return Response("OK")
        
        # -------------------------------------------------------------
        # 1. MESSAGE FROM ADMIN (Reply to user)
        # -------------------------------------------------------------
        if sender_id == admin_id:
            reply_to = message.get("reply_to_message")
            if not reply_to:
                if text in ["/start", "/help"]:
                    await tg_request(env, "sendMessage", {
                        "chat_id": admin_id,
                        "text": "👑 <b>管理员控制面板</b>\n\n• 直接【回复】用户的某条转发消息，Bot 将自动无引用转发给该用户，并携带回复关联。",
                        "parse_mode": "HTML"
                    })
                else:
                    await tg_request(env, "sendMessage", {
                        "chat_id": admin_id,
                        "text": "💡 <b>提示</b>：请直接【回复】某条用户的消息以发送回复给对方。",
                        "parse_mode": "HTML"
                    })
                return Response("OK")
            
            # Admin replied to a message in admin chat
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
            
            # Send to user via copyMessage (无引用转发) with reply_parameters to preserve quote
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
                # Map this reply so future replies to this message can be routed back
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
                desc = copy_res.get("description", "未知错误")
                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": f"❌ 发送失败: {desc}",
                    "reply_parameters": {"message_id": msg_id}
                })
            
            return Response("OK")
        
        # -------------------------------------------------------------
        # 2. MESSAGE FROM USER (Stranger)
        # -------------------------------------------------------------
        # Check if user is verified
        is_verified = await env.pmbot_captcha.get(f"verified:{sender_id}")
        
        if not is_verified:
            # Check if there is a pending captcha for this user
            pending_raw = await env.pmbot_captcha.get(f"captcha_pending:{sender_id}")
            if pending_raw:
                pending = json.loads(pending_raw)
                expected_code = pending.get("code")
                cleaned_input = text.strip()
                
                if cleaned_input == expected_code:
                    # Verification successful!
                    await env.pmbot_captcha.put(f"verified:{sender_id}", "true")
                    await env.pmbot_captcha.delete(f"captcha_pending:{sender_id}")
                    
                    # Delete the captcha message if possible to keep chat clean
                    if pending.get("captcha_msg_id"):
                        try:
                            await tg_request(env, "deleteMessage", {
                                "chat_id": sender_id,
                                "message_id": pending["captcha_msg_id"]
                            })
                        except Exception:
                            pass
                    
                    # Notify user
                    await tg_request(env, "sendMessage", {
                        "chat_id": sender_id,
                        "text": "✅ <b>验证成功！</b>\n您的会话已解锁，消息已成功发送给主人。",
                        "parse_mode": "HTML"
                    })
                    
                    # Send info banner to admin + forward original message if exists
                    orig_id = pending.get("original_msg_id")
                    await handle_user_verified_banner(env, from_user, orig_id)
                    return Response("OK")
                else:
                    # User entered wrong code
                    await tg_request(env, "sendMessage", {
                        "chat_id": sender_id,
                        "text": "❌ <b>验证码错误</b>，请仔细观察上方图片中的 2 位数字并重新输入：",
                        "parse_mode": "HTML"
                    })
                    return Response("OK")
            
            # No active captcha, generate one
            # If user sent /start, don't treat /start as original message to forward
            orig_msg_id = None if text.strip() == "/start" else msg_id
            await send_captcha(env, sender_id, orig_msg_id)
            return Response("OK")
        
        # User is already verified:
        # Handle /start for verified users
        if text.strip() == "/start":
            await tg_request(env, "sendMessage", {
                "chat_id": sender_id,
                "text": "👋 您好！您已通过人机验证，直接在此发送消息即可，主人收到后会尽快回复您。"
            })
            return Response("OK")
        
        # User forwards message to Admin:
        # Check if user message is a reply
        reply_to = message.get("reply_to_message")
        fwd_res = None
        
        if reply_to:
            replied_user_msg_id = reply_to.get("message_id")
            target_admin_msg_id = await env.pmbot_captcha.get(f"u2a:{sender_id}:{replied_user_msg_id}")
            if target_admin_msg_id:
                # Use copyMessage to Admin with reply_parameters so Admin sees the reply quote
                fwd_res = await tg_request(env, "copyMessage", {
                    "chat_id": admin_id,
                    "from_chat_id": sender_id,
                    "message_id": msg_id,
                    "reply_parameters": {
                        "message_id": int(target_admin_msg_id),
                        "allow_sending_without_reply": True
                    }
                })
        
        # If not a reply, or target_admin_msg_id not found, use forwardMessage (直接转发，展示对方名片)
        if not fwd_res or not fwd_res.get("ok"):
            fwd_res = await tg_request(env, "forwardMessage", {
                "chat_id": admin_id,
                "from_chat_id": sender_id,
                "message_id": msg_id
            })
        
        if fwd_res.get("ok"):
            admin_msg_id = fwd_res["result"]["message_id"]
            # Save mapping (30 days TTL)
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
        return Response("OK") # Always return 200 to Telegram
