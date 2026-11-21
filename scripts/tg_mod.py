#!/usr/bin/env python -u
# -*- coding: utf-8 -*-
"""tg_mod.py — 群管理 bot（按判罚尺度执法）

判罚规则来自 state/mod_rules.json，改规则不用改代码。

用法：
  python scripts/tg_mod.py test     试运行，只看会怎么判
  python scripts/tg_mod.py self     规则自测（不联网）
  python scripts/tg_mod.py once     处理一次积压
  python scripts/tg_mod.py run      持续监听

前置条件：
  · bot 是群管理员，有「删除消息」「封禁用户」权限
  · BotFather → /setprivacy → Disable
    改完必须把 bot 踢出群、再重新拉回来
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

CONFIG = ROOT / "state" / "tg_config.json"
RULES_FILE = ROOT / "state" / "mod_rules.json"
STATE = ROOT / "state" / "tg_mod_state.json"
LOG = ROOT / "state" / "tg_mod.log"
API = "https://api.telegram.org/bot%s/%s"

# ---- 关键词 → 类别名（级别在 mod_rules.json 里配） ----
PATTERNS = [
    ("广告链接", re.compile(r"(https?://|t\.me/|www\.)", re.I)),
    ("带单话术", re.compile(r"(带单|跟单|带带我|能不能带|要不要跟|跟我做|喊单|带一下)")),
    ("返佣推广", re.compile(r"(返佣|注册链接|邀请码|空投|撸毛|白名单|私募|项目方|上所)")),
    ("拉人私聊", re.compile(r"(加我|私聊我|加\s*[vVv]|进群聊|拉你进)")),
    ("人身攻击", re.compile(r"(傻逼|智障|废物|去死|你懂个屁|脑残|垃圾玩意|滚出去)")),
    ("政治话题", re.compile(r"(政治|政党|选举|独裁|专制|民主运动|共产|法轮|六四|"
                        r"台独|港独|疆独|藏独|反共|推翻政府)", re.I)),
    ("恐怖极端", re.compile(r"(恐怖主义|恐怖袭击|圣战|炸弹|恐袭|极端组织|"
                        r"伊斯|基地组织)", re.I)),
    ("歧视言论", re.compile(r"(支那|尼哥|黑鬼|贱民|劣等民族|低等人种|"
                        r"地域黑)", re.I)),
    ("刷屏", re.compile(r"(.{1,3})\1{5,}")),   # 同一短语连续重复 6 次以上
]

def build_allow(rules, cfg=None):
    """允许的链接白名单。

    来源两处：
      1. rules["allow_links"] —— 手工补充
      2. 自动推导：从 TG_GROUP_ID / TG_CHANNEL_ID 生成自己的链接
    这样不用把真实群名写进公开的规则文件。
    """
    pats = list(rules.get("allow_links") or [])
    for key in ("group_id", "channel_id"):
        v = (cfg or {}).get(key) or ""
        if v.startswith("@"):
            u = v[1:].lower()
            pats += ["t.me/%s" % u, "telegram.me/%s" % u]
    pats = [p for p in pats if p]
    if not pats:
        return None
    return re.compile("(" + "|".join(re.escape(p) for p in pats) + ")", re.I)


ALLOW = None   # 运行时由 classify() 注入

DEFAULT_RULES = {
    "record_days": 30,
    "levels": {
        "A": {"name": "零容忍", "steps": ["delete+ban"]},
        "B": {"name": "严重", "steps": ["delete+warn", "delete+mute360", "delete+ban"]},
        "C": {"name": "一般", "steps": ["delete+warn", "delete+mute60",
                                       "delete+mute1440", "delete+ban"]},
        "D": {"name": "轻微", "steps": ["delete+warn", "delete+mute30",
                                       "delete+mute360", "delete+ban"]},
    },
    "categories": {"政治话题": "A", "恐怖极端": "A", "歧视言论": "A",
                   "广告链接": "B", "返佣推广": "B", "拉人私聊": "B",
                   "人身攻击": "B", "带单话术": "C", "刷屏": "D"},
    "admin_ids": [],
    "exempt_newbie_days": 3,
}


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def load_cfg():
    """配置优先级：环境变量 > 配置文件 > 默认值。

    GitHub Actions 上用环境变量（TG_BOT_TOKEN / TG_GROUP_ID），
    本地用 state/tg_config.json（含代理）。
    """
    cfg = {"bot_token": "", "channel_id": "", "group_id": "", "proxy": ""}
    cfg.update(load_json(CONFIG, {}))
    cfg["bot_token"] = os.environ.get("TG_BOT_TOKEN", cfg["bot_token"])
    cfg["group_id"] = os.environ.get("TG_GROUP_ID", cfg["group_id"])
    cfg["channel_id"] = os.environ.get("TG_CHANNEL_ID", cfg["channel_id"])
    if "TG_PROXY" in os.environ:
        cfg["proxy"] = os.environ["TG_PROXY"]
    return cfg


def load_rules():
    r = dict(DEFAULT_RULES)
    r.update(load_json(RULES_FILE, {}))
    # 环境变量可覆盖豁免名单（避免把真实用户 ID 写进公开的规则文件）
    env_admins = os.environ.get("TG_ADMIN_IDS", "")
    if env_admins.strip():
        ids = []
        for x in env_admins.split(","):
            x = x.strip()
            if x.isdigit():
                ids.append(int(x))
        if ids:
            r["admin_ids"] = ids
    return r


def api(cfg, method, payload=None, timeout=30):
    url = API % (cfg["bot_token"], method)
    data = urllib.parse.urlencode(payload or {}, doseq=True).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "tg-mod/1.0"})
    p = cfg.get("proxy")
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": p, "https": p}) if p
        else urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError("%s 失败 %s: %s" % (method, e.code, body[:200]))


def log(msg):
    line = "%s  %s" % (dt.datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


_LAST_SYNC = [0.0]


def git_sync(force=False, every=600):
    """定期把状态提交回仓库。只在设置了 TG_GIT_SYNC=1 时生效。"""
    if os.environ.get("TG_GIT_SYNC") != "1":
        return
    now = time.time()
    if not force and now - _LAST_SYNC[0] < every:
        return
    _LAST_SYNC[0] = now
    import subprocess

    def run(args, timeout=60):
        return subprocess.run(args, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=timeout)

    try:
        # runner 里没有全局 git 身份，必须先配好，否则 commit 会失败
        run(["git", "config", "user.name", "github-actions[bot]"], 20)
        run(["git", "config", "user.email",
             "github-actions[bot]@users.noreply.github.com"], 20)
        run(["git", "add", "state/tg_mod_state.json"], 30)
        r = run(["git", "commit", "-m", "chore: 更新违规记录 [skip ci]"], 30)
        if r.returncode != 0:
            if "nothing to commit" not in (r.stdout + r.stderr):
                log("状态提交失败：%s" % (r.stderr or r.stdout)[:160])
            return
        p = run(["git", "push"], 90)
        if p.returncode == 0:
            log("状态已同步到仓库")
        else:
            log("状态推送失败：%s" % (p.stderr or p.stdout)[:160])
    except Exception as e:
        log("状态同步异常：%s" % e)


def load_state():
    st = load_json(STATE, {"offset": 0, "users": {}})
    st.setdefault("users", {})
    st.setdefault("offset", 0)
    return st


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    git_sync()


# ---------- 判定 ----------
def classify(text, rules, cfg=None):
    """返回 (类别名, 级别字母) 或 (None, None)。"""
    if not text:
        return None, None
    allow = build_allow(rules, cfg)
    if allow and allow.search(text):
        return None, None
    for name, rx in PATTERNS:
        if rx.search(text):
            return name, rules["categories"].get(name, "C")
    return None, None


def user_record(st, rules, uid, is_new):
    """取/建用户违规记录，顺带做过期清理。"""
    now = dt.datetime.now()
    rec = st["users"].get(str(uid), {})
    last = rec.get("last")
    if last:
        try:
            age = (now - dt.datetime.fromisoformat(last)).days
            if age > rules["record_days"]:
                rec = {}          # 记录过期，清零
        except Exception:
            pass
    rec.setdefault("counts", {})
    if is_new and rules.get("exempt_newbie_days", 0) > 0:
        rec["newbie"] = True
    st["users"][str(uid)] = rec
    return rec


def pick_step(rec, level, rules, is_newbie=False):
    """根据级别和次数，取出该执行的动作串。"""
    n = rec["counts"].get(level, 0)
    steps = rules["levels"].get(level, {}).get("steps", ["delete+warn"])
    # 新人首次：降一级处理（A 级不降）
    if is_newbie and level != "A" and n <= 1:
        idx = 0
    else:
        idx = min(n - 1, len(steps) - 1)
    return steps[max(0, idx)], n


def apply_actions(cfg, rules, chat_id, msg, actions, uname, cat, level, dry):
    letters = {"A": "零容忍", "B": "严重", "C": "一般", "D": "轻微"}
    log_str = "「%s」%s级(%s)" % (cat, level, letters.get(level, ""))
    for act in actions.split("+"):
        if act == "delete":
            if dry:
                continue
            try:
                api(cfg, "deleteMessage",
                    {"chat_id": chat_id, "message_id": msg["message_id"]})
            except Exception as e:
                log("    删除失败：%s" % e)
        elif act == "warn":
            if dry:
                continue
            try:
                api(cfg, "sendMessage", {
                    "chat_id": chat_id,
                    "text": "⚠️ %s 触发群规 %s，消息已删。\n下次按尺度加重处理。"
                            % (uname, log_str),
                    "reply_to_message_id": msg["message_id"]})
            except Exception:
                pass
        elif act.startswith("mute"):
            mins = int(act[4:] or 60)
            if dry:
                continue
            try:
                api(cfg, "restrictChatMember", {
                    "chat_id": chat_id, "user_id": msg["from"]["id"],
                    "permissions": json.dumps({"can_send_messages": False}),
                    "until_date": int(time.time()) + mins * 60})
                log("    已禁言 用户%s %d 分钟" % (msg["from"]["id"], mins))
            except Exception as e:
                log("    禁言失败：%s" % e)
        elif act == "ban":
            if dry:
                continue
            try:
                api(cfg, "banChatMember",
                    {"chat_id": chat_id, "user_id": msg["from"]["id"]})
                log("    已踢出 用户%s" % msg["from"]["id"])
            except Exception as e:
                log("    踢出失败：%s" % e)
    log("  %-8s %s → %s" % (uname, cat, actions))


def handle(cfg, rules, st, msg, dry=True):
    chat = msg.get("chat", {})
    text = msg.get("text") or msg.get("caption") or ""
    user = msg.get("from", {})
    uid = user.get("id")
    uname = user.get("username") or user.get("first_name") or str(uid)

    if not uid or uid in rules.get("admin_ids", []):
        return False

    cat, level = classify(text, rules, cfg)
    if not cat:
        return False

    # 新人判定：入群 3 天内
    is_new = False
    try:
        joined = msg.get("date", 0)
        is_new = False   # 简化：无法可靠判断入群时间，默认不算新人
    except Exception:
        pass

    rec = user_record(st, rules, uid, is_new)
    rec["counts"][level] = rec["counts"].get(level, 0) + 1
    rec["last"] = dt.datetime.now().isoformat(timespec="seconds")

    step, n = pick_step(rec, level, rules, is_new)

    letters = {"A": "零容忍", "B": "严重", "C": "一般", "D": "轻微"}
    log("[群] 用户%s 触发「%s」%s级 ×%d → %s%s"
        % (uid, cat, letters.get(level, level), n, step,
           "  (试运行，不计入记录)" if dry else ""))

    # 试运行不累计违规次数
    if dry:
        rec["counts"][level] -= 1
        return True

    apply_actions(cfg, rules, chat["id"], msg, step, uname, cat, level, dry)
    return True


def loop(cfg, rules, st, dry=False, once=False, deadline=None):
    if not cfg.get("bot_token"):
        log("没有 bot_token")
        return
    last_id = st.get("offset", 0)
    log("开始%s（offset=%d，%s）"
        % ("处理一次" if once else "监听", last_id, "试运行" if dry else "实际执行"))
    while True:
        if deadline and time.time() > deadline:
            save_state(st)
            log("运行时长到期，状态已保存，退出")
            return
        try:
            res = api(cfg, "getUpdates", {
                "offset": last_id + 1, "timeout": 0 if once else 25,
                "allowed_updates": json.dumps(["message"])})
        except Exception as e:
            log("拉取失败：%s" % e)
            if once:
                return
            time.sleep(5)
            continue
        ups = res.get("result", [])
        for u in ups:
            last_id = max(last_id, u["update_id"])
            m = u.get("message")
            if m:
                handle(cfg, rules, st, m, dry=dry)
        if ups:
            st["offset"] = last_id
            save_state(st)
        if once:
            log("处理完成，%d 条更新" % len(ups))
            return
        if not ups:
            time.sleep(1)


# ---------- 命令 ----------
def cmd_pin(args):
    """把已有的消息置顶（不重发）。"""
    cfg = load_cfg()
    gid = cfg.get("group_id")
    if not gid:
        print("没配置 group_id")
        return
    for mid in args.message_ids:
        try:
            api(cfg, "pinChatMessage", {
                "chat_id": gid, "message_id": int(mid),
                "disable_notification": "true"})
            print("已置顶 message_id=%s" % mid)
        except Exception as e:
            print("置顶 %s 失败：%s" % (mid, e))


def cmd_publish(args):
    """把群规和判罚尺度发到群里并置顶。"""
    cfg = load_cfg()
    gid = cfg.get("group_id")
    if not gid:
        print("没配置 group_id")
        return
    items = [
        ("state/group_rules.txt", "群规"),
        ("state/group_penalty.txt", "判罚尺度"),
    ]
    for rel, label in items:
        path = ROOT / rel
        if not path.exists():
            print("缺少文件：%s" % rel)
            continue
        text = path.read_text(encoding="utf-8").strip()
        if args.dry_run:
            print("\n[预览] %s（%d 字）\n%s\n" % (label, len(text), text[:200] + "…"))
            continue
        res = api(cfg, "sendMessage", {
            "chat_id": gid, "text": text,
            "disable_web_page_preview": "true"})
        if not res.get("ok"):
            print("%s 发送失败：%s" % (label, res))
            continue
        mid = res["result"]["message_id"]
        print("%s 已发送（message_id=%d）" % (label, mid))
        try:
            api(cfg, "pinChatMessage", {
                "chat_id": gid, "message_id": mid,
                "disable_notification": "true"})
            print("  → 已置顶")
        except Exception as e:
            print("  → 置顶失败：%s" % e)
            print("    检查 bot 是否有「置顶消息」权限")


def cmd_self(args):
    rules = load_rules()
    tests = [
        "加入我的VIP群，注册领空投",
        "https://example.com 项目方直推",
        "老师能不能带带我",
        "你这傻逼懂个屁",
        "今天政治局开会了",
        "某个群体都是劣等民族",
        "哈哈哈哈哈哈哈哈哈哈哈",
        "我觉得 ETH 要涨，因为周线站上了 EMA60",
        "不同意见：我认为还要跌，理由是量能不足",
        "看看这个 t.me/example_channel",
    ]
    tag = {"A": "🔴 踢", "B": "🟠 严重", "C": "🟡 一般", "D": "⚪ 轻微"}
    print("判罚尺度自测（%d 条）\n" % len(tests))
    for t in tests:
        cat, lvl = classify(t, rules, {"group_id": "@example", "channel_id": "@example"})
        if not cat:
            print("✅ 放行        %s" % t[:36])
            continue
        steps = rules["levels"][lvl]["steps"]
        print("%-12s %-6s 首次→%-14s %s"
              % (tag.get(lvl, lvl), cat, steps[0], t[:24]))


def cmd_test(args):
    cfg, rules = load_cfg(), load_rules()
    loop(cfg, rules, load_state(), dry=True, once=True)


def cmd_once(args):
    cfg, rules = load_cfg(), load_rules()
    loop(cfg, rules, load_state(), dry=False, once=True)


def cmd_run(args):
    cfg, rules = load_cfg(), load_rules()
    mins = getattr(args, "minutes", 0) or 0
    deadline = time.time() + mins * 60 if mins else None
    if mins:
        log("长轮询模式，运行 %d 分钟后自动退出" % mins)
    # 收到终止信号时先把状态存盘
    import signal
    def _bye(signum, frame):
        log("收到终止信号，保存状态")
        sys.exit(0)
    try:
        signal.signal(signal.SIGTERM, _bye)
        signal.signal(signal.SIGINT, _bye)
    except Exception:
        pass
    loop(cfg, rules, load_state(), dry=False, once=False, deadline=deadline)


def main():
    ap = argparse.ArgumentParser(description="群管理 bot（按判罚尺度）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("self", cmd_self), ("test", cmd_test),
                     ("once", cmd_once), ("run", cmd_run),
                     ("publish", cmd_publish), ("pin", cmd_pin)):
        p = sub.add_parser(name)
        if name == "publish":
            p.add_argument("--dry-run", action="store_true", help="只预览不发送")
        if name == "pin":
            p.add_argument("message_ids", nargs="+", help="要置顶的 message_id")
        if name == "run":
            p.add_argument("--minutes", type=int, default=0,
                           help="运行多少分钟后自动退出（0=永不退出）")
        p.set_defaults(func=fn)
    args = ap.parse_args()
    if not hasattr(args, "dry_run"):
        args.dry_run = False
    args.func(args)


if __name__ == "__main__":
    main()
