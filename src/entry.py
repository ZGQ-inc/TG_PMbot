from workers import Response
from js import fetch, Object
from pyodide.ffi import to_js
import json
import time
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

def _to_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    try:
        if hasattr(row, "to_py"):
            return row.to_py()
        return dict(row)
    except Exception:
        try:
            return {k: getattr(row, k) for k in dir(row) if not k.startswith("_")}
        except Exception:
            return {}

class Database:
    def __init__(self, d1_binding):
        self.db = d1_binding

    def _sanitize_params(self, params):
        return ["" if p is None else p for p in params]

    async def fetch_one(self, sql: str, *params):
        if not self.db:
            return None
        stmt = self.db.prepare(sql)
        if params:
            stmt = stmt.bind(*self._sanitize_params(params))
        row = await stmt.first()
        return _to_dict(row)

    async def fetch_all(self, sql: str, *params):
        if not self.db:
            return []
        stmt = self.db.prepare(sql)
        if params:
            stmt = stmt.bind(*self._sanitize_params(params))
        res = await stmt.all()
        results = getattr(res, "results", res)
        if not results:
            return []
        return [_to_dict(r) for r in results if r is not None]

    async def execute(self, sql: str, *params):
        if not self.db:
            return None
        stmt = self.db.prepare(sql)
        if params:
            stmt = stmt.bind(*self._sanitize_params(params))
        return await stmt.run()

    # 1. 消息映射 (messages 表)
    async def save_message_mapping(self, admin_msg_id: int, user_id: int, user_msg_id: int):
        now = int(time.time())
        await self.execute(
            "INSERT OR REPLACE INTO messages (admin_msg_id, user_id, user_msg_id, created_at) VALUES (?, ?, ?, ?)",
            admin_msg_id, user_id, user_msg_id, now
        )

    async def get_message_by_admin_msg_id(self, admin_msg_id: int):
        now = int(time.time())
        row = await self.fetch_one(
            "SELECT user_id, user_msg_id, created_at FROM messages WHERE admin_msg_id = ?",
            admin_msg_id
        )
        if not row:
            return None
        if now - row.get("created_at", 0) > 2592000:
            return None
        return row

    async def get_admin_msg_id_by_user_msg(self, user_id: int, user_msg_id: int):
        now = int(time.time())
        row = await self.fetch_one(
            "SELECT admin_msg_id, created_at FROM messages WHERE user_id = ? AND user_msg_id = ?",
            user_id, user_msg_id
        )
        if not row:
            return None
        if now - row.get("created_at", 0) > 2592000:
            return None
        return row.get("admin_msg_id")

    # 2. 访客用户信息与验证状态 (users 表)
    async def is_user_verified(self, user_id: int) -> bool:
        row = await self.fetch_one("SELECT is_verified FROM users WHERE user_id = ?", user_id)
        return bool(row and row.get("is_verified") == 1)

    async def set_user_verified(self, user: dict, dc_id: str):
        now = int(time.time())
        user_id = user["id"]
        first_name = user.get("first_name", "") or ""
        last_name = user.get("last_name", "") or ""
        username = user.get("username", "") or ""
        await self.execute(
            """INSERT INTO users (user_id, first_name, last_name, username, dc_id, is_verified, verified_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   first_name = excluded.first_name,
                   last_name = excluded.last_name,
                   username = excluded.username,
                   dc_id = excluded.dc_id,
                   is_verified = 1,
                   verified_at = excluded.verified_at,
                   updated_at = excluded.updated_at""",
            user_id, first_name, last_name, username, dc_id, now, now, now
        )

    async def get_user_info(self, user_id: int):
        return await self.fetch_one("SELECT * FROM users WHERE user_id = ?", user_id)

    # 3. 黑名单 (banned_users 表)
    async def get_ban_info(self, user_id: int):
        return await self.fetch_one("SELECT reason, banned_at FROM banned_users WHERE user_id = ?", user_id)

    async def ban_user(self, user_id: int, reason: str = ""):
        now = int(time.time())
        await self.execute(
            "INSERT OR REPLACE INTO banned_users (user_id, reason, banned_at) VALUES (?, ?, ?)",
            user_id, reason or "", now
        )

    async def unban_user(self, user_id: int):
        await self.execute("DELETE FROM banned_users WHERE user_id = ?", user_id)

    # 4. 验证码待验证 (pending_captchas 表)
    async def set_pending_captcha(self, user_id: int, code: str, captcha_msg_id: int | None, original_msg_id: int | None):
        now = int(time.time())
        await self.execute(
            "INSERT OR REPLACE INTO pending_captchas (user_id, code, captcha_msg_id, original_msg_id, created_at) VALUES (?, ?, ?, ?, ?)",
            user_id, code, captcha_msg_id or 0, original_msg_id or 0, now
        )

    async def get_pending_captcha(self, user_id: int):
        now = int(time.time())
        row = await self.fetch_one(
            "SELECT code, captcha_msg_id, original_msg_id, created_at FROM pending_captchas WHERE user_id = ?",
            user_id
        )
        if not row:
            return None
        if now - row.get("created_at", 0) > 300:
            await self.execute("DELETE FROM pending_captchas WHERE user_id = ?", user_id)
            return None
        return row

    async def delete_pending_captcha(self, user_id: int):
        await self.execute("DELETE FROM pending_captchas WHERE user_id = ?", user_id)

    # 5. 频控 (rate_limits 表)
    async def check_rate_limit(self, user_id: int, limit: int = 20, window_sec: int = 60) -> bool:
        now = int(time.time())
        row = await self.fetch_one("SELECT count, window_start FROM rate_limits WHERE user_id = ?", user_id)
        if row and (now - row.get("window_start", 0)) < window_sec:
            if row.get("count", 0) >= limit:
                return False
            await self.execute("UPDATE rate_limits SET count = count + 1 WHERE user_id = ?", user_id)
            return True
        else:
            await self.execute("INSERT OR REPLACE INTO rate_limits (user_id, count, window_start) VALUES (?, 1, ?)", user_id, now)
            return True

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

async def send_captcha(env, db: Database, chat_id: int, original_msg_id: int | None = None):
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
    
    await db.set_pending_captcha(chat_id, code, captcha_msg_id, original_msg_id)

async def handle_user_verified_banner(env, db: Database, user: dict, original_msg_id: int | None = None):
    user_id = user["id"]
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip()
    
    name_display = f"<code>{html.escape(full_name)}</code>" if full_name else "null"
    username = f"@{user['username']}" if user.get("username") else "null"
    uid_display = f"<code>{user_id}</code>"
    dc_display = await get_user_dc(env, user_id)

    # 写入 users 表记录
    await db.set_user_verified(user, dc_display)
    
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
            await db.save_message_mapping(admin_msg_id, user_id, original_msg_id)

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
        
        body_bytes = await request.bytes()
        update = json.loads(bytes(body_bytes).decode("utf-8"))
        
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
        
        # 初始化 D1 数据库实例
        db = Database(getattr(env, "DB", None))
        
        tokens = text.strip().split()
        cmd = tokens[0].split("@")[0].lower() if tokens else ""

        # ==========================================
        # 管理员业务逻辑分支
        # ==========================================
        if sender_id == admin_id:
            reply_to = message.get("reply_to_message")

            if cmd in ["/start", "/help"]:
                admin_welcome = (
                    "👋 <b>欢迎回来，主人！</b>\n\n"
                    "👑 <b>ZGQ_PMbot 管理员面板已就绪 (Cloudflare D1 驱动)</b>\n"
                    f"• <b>您的 UID:</b> <code>{admin_id}</code>\n"
                    "• <b>数据库:</b> Cloudflare D1 SQL (pmbot)\n"
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
                    mapping = await db.get_message_by_admin_msg_id(admin_replied_msg_id)
                    if mapping:
                        target_user_id = mapping["user_id"]
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

                await db.ban_user(target_user_id, reason)

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
                    mapping = await db.get_message_by_admin_msg_id(admin_replied_msg_id)
                    if mapping:
                        target_user_id = mapping["user_id"]
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

                await db.unban_user(target_user_id)

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
                mapping = await db.get_message_by_admin_msg_id(admin_replied_msg_id)
                if not mapping:
                    await tg_request(env, "sendMessage", {
                        "chat_id": admin_id,
                        "text": "⚠️ 未找到该消息的用户映射记录或已过期（超过30天）。",
                        "reply_parameters": {"message_id": msg_id}
                    })
                    return Response("OK")
                
                target_user_id = mapping["user_id"]
                u_db = await db.get_user_info(target_user_id)
                is_ver = await db.is_user_verified(target_user_id)
                ban_info = await db.get_ban_info(target_user_id)
                dc_info = u_db.get("dc_id") if (u_db and u_db.get("dc_id")) else await get_user_dc(env, target_user_id)
                
                u_chat = await tg_request(env, "getChat", {"chat_id": target_user_id})
                u_res = u_chat.get("result", {}) if u_chat.get("ok") else {}
                u_name = html.escape(f"{u_res.get('first_name', '')} {u_res.get('last_name', '')}".strip() or "未知")
                u_uname = f"@{u_res['username']}" if u_res.get("username") else "null"
                
                ver_status = "已验证 ✅" if is_ver else "未验证 ⏳"
                ban_status = f"已封禁 🚫 ({html.escape(ban_info['reason'])})" if (ban_info and ban_info.get("reason")) else ("已封禁 🚫" if ban_info else "正常 🟢")
                
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

            # 管理员回复用户消息
            if not reply_to:
                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": "💡 <b>提示</b>：请直接【回复】某条用户的转发消息来发送回复给对方。",
                    "parse_mode": "HTML"
                })
                return Response("OK")
            
            admin_replied_msg_id = reply_to.get("message_id")
            mapping = await db.get_message_by_admin_msg_id(admin_replied_msg_id)
            if not mapping:
                await tg_request(env, "sendMessage", {
                    "chat_id": admin_id,
                    "text": "⚠️ 未找到对应的用户会话或消息映射已过期（超过30天），无法送达。",
                    "reply_parameters": {"message_id": msg_id}
                })
                return Response("OK")
            
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
                await db.save_message_mapping(msg_id, target_user_id, delivered_user_msg_id)
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

        # ==========================================
        # 访客业务逻辑分支
        # ==========================================
        ban_info = await db.get_ban_info(sender_id)
        if ban_info:
            if cmd == "/start":
                r = ban_info.get("reason")
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

        if not await db.check_rate_limit(sender_id):
            await tg_request(env, "sendMessage", {
                "chat_id": sender_id,
                "text": "⚠️ <b>发送过于频繁</b>，请稍候再试。",
                "parse_mode": "HTML"
            })
            return Response("OK")

        if cmd in ["/ban", "/unban", "/info"]:
            await tg_request(env, "sendMessage", {
                "chat_id": sender_id,
                "text": "⚠️ <b>权限不足</b>：该命令仅限管理员使用。",
                "parse_mode": "HTML",
                "reply_parameters": {"message_id": msg_id}
            })
            return Response("OK")

        if cmd == "/help":
            await tg_request(env, "sendMessage", {
                "chat_id": sender_id,
                "text": "💡 <b>使用说明</b>：直接在此发送您想咨询的内容即可，主人收到后会尽快回复您。\n（注：管理命令仅限管理员使用）",
                "parse_mode": "HTML",
                "reply_parameters": {"message_id": msg_id}
            })
            return Response("OK")

        is_verified = await db.is_user_verified(sender_id)
        
        if not is_verified:
            if cmd == "/start":
                await send_captcha(env, db, sender_id, None)
                return Response("OK")

            pending = await db.get_pending_captcha(sender_id)
            if pending:
                expected_code = str(pending.get("code", "")).strip()
                cleaned_input = text.strip()
                
                if cleaned_input == expected_code:
                    await db.delete_pending_captcha(sender_id)
                    
                    captcha_msg_id = pending.get("captcha_msg_id")
                    if captcha_msg_id:
                        try:
                            await tg_request(env, "deleteMessage", {
                                "chat_id": sender_id,
                                "message_id": captcha_msg_id
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
                    if orig_id == 0:
                        orig_id = None
                    await handle_user_verified_banner(env, db, from_user, orig_id)
                    return Response("OK")
                else:
                    await tg_request(env, "sendMessage", {
                        "chat_id": sender_id,
                        "text": "❌ <b>验证码错误</b>，请仔细观察上方字符画展示的 2 位数字并重新输入：",
                        "parse_mode": "HTML"
                    })
                    return Response("OK")
            
            await send_captcha(env, db, sender_id, msg_id)
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
            await db.save_message_mapping(admin_msg_id, sender_id, msg_id)
            
        return Response("OK")
        
    except Exception as e:
        print(f"Exception in on_fetch: {e}\n{traceback.format_exc()}")
        return Response("OK")
