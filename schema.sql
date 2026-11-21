-- ================================================================
-- Cloudflare D1 Schema for TG_PMbot
-- ================================================================

-- 1. 消息双向映射表 (替代原 a2u 和 u2a 两套 KV，一条记录即可双向查询)
CREATE TABLE IF NOT EXISTS messages (
    admin_msg_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    user_msg_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages (user_id, user_msg_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages (created_at);

-- 2. 访客用户信息与验证状态表 (替代 verified:uid)
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    dc_id TEXT,
    is_verified INTEGER NOT NULL DEFAULT 0,
    verified_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- 3. 黑名单表 (替代 banned:uid)
CREATE TABLE IF NOT EXISTS banned_users (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    banned_at INTEGER NOT NULL
);

-- 4. 待完成的验证码表 (替代 captcha_pending:uid)
CREATE TABLE IF NOT EXISTS pending_captchas (
    user_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    captcha_msg_id INTEGER,
    original_msg_id INTEGER,
    created_at INTEGER NOT NULL
);

-- 5. 消息频控表 (替代 ratelimit:uid，不再受 KV 60秒最小 TTL 限制)
CREATE TABLE IF NOT EXISTS rate_limits (
    user_id INTEGER PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 1,
    window_start INTEGER NOT NULL
);
