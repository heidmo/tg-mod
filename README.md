# Telegram 群管理 Bot

按群规自动执法：删广告、禁言、踢人。

**这个仓库只有群管理功能，不含任何交易策略。**

---

## 功能

| 级别 | 内容 | 首次处理 |
|---|---|---|
| A 零容忍 | 政治、恐怖极端、歧视 | 直接踢出 |
| B 严重 | 广告、返佣、拉人、人身攻击 | 删消息 + 警告 |
| C 一般 | 带单话术、私聊推销 | 删消息 + 提醒 |
| D 轻微 | 刷屏 | 删消息 + 提醒 |

累犯按阶梯加重：删消息 → 禁言 → 踢出。

---

## 部署

### 1. 建 bot

Telegram 找 @BotFather：

```
/newbot
→ 名字
→ 用户名（必须以 bot 结尾）
→ 拿 token
```

### 2. 关键设置

**关闭隐私模式**（否则看不到普通消息）：

```
BotFather → /setprivacy → 选你的 bot → Disable
```

**把 bot 拉进群，设为管理员**，勾选：

- ✅ 删除消息
- ✅ 封禁用户
- ✅ 置顶消息

### 3. 配置仓库

**Settings → Secrets and variables → Actions**

Secrets：
```
TG_BOT_TOKEN = 你的 bot token
```

Variables：
```
TG_GROUP_ID   = @你的群用户名
TG_CHANNEL_ID = @你的频道用户名
TG_ADMIN_IDS  = 123456789,987654321
```

**`TG_ADMIN_IDS` 是管理员豁免名单**（Telegram 用户 ID，逗号分隔）。

**不填的话，你自己发广告测试也会被删。** 查自己的 ID：把任意消息转发给 @userinfobot。

**`TG_CHANNEL_ID` 可以不填**——填了会自动放行你自己频道的链接。

**Settings → Actions → General → Workflow permissions**

选 **Read and write permissions** → Save

**不开这个，状态提交不回去，会重复处理同一条消息。**

### 4. 启用

**Actions** 标签 → 群管理 → **Run workflow** 手动测一次。

看日志有输出就成功了。之后每 5 分钟自动跑。

---

## 群规与判罚

本仓库只负责**执行**，规则内容在：

| 文件 | 内容 |
|---|---|
| `state/mod_rules.json` | 判罚规则（级别、阶梯、豁免名单） |

改规则直接改这个文件，push 后立即生效。

### 豁免管理员

**用 `TG_ADMIN_IDS` 环境变量**，不要写进规则文件：

```
TG_ADMIN_IDS = 123456789,987654321
```

规则文件里留空：

```json
"admin_ids": []
```

---

## 命令

```bash
# 规则自测（不联网，验证正则）
python3 scripts/tg_mod.py self

# 试运行（只判不删，用来观察误判）
python3 scripts/tg_mod.py test

# 处理一次
python3 scripts/tg_mod.py once

# 持续监听（本地用）
python3 scripts/tg_mod.py run
```

---

## 已知限制

| 限制 | 说明 |
|---|---|
| GitHub 定时最小 5 分钟 | 广告最多存活 5–15 分钟 |
| 高峰期会延迟 | GitHub cron 不保证准时 |
| 每次运行是新容器 | 状态靠提交回仓库保存 |
| 正则可能误判 | 建议先跑 `test` 观察几天 |

---

## 许可

自用。随意 fork。
