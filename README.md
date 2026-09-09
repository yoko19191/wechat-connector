# wechat-connector

让 Agent 读取 Mac 微信聊天记录的本地 MCP 服务。支持显示联系人昵称、微信号和备注，
并按指定会话、时间范围分页读取历史。

**首次由用户主动获取密钥，日常只读加密快照。** MCP 不发送消息、不启动或注入微信、
不自动获取密钥、不自动刷新数据，也不直接读取微信正在使用的数据库。

## 快速开始

### 1. 准备环境和路径

要求 macOS、Python 3.11+、uv 和 SQLCipher。首次初始化方法此前在 Apple Silicon、微信 4.1.13 上跑通，
其他微信版本没有兼容保证。

```bash
brew install uv sqlcipher
```

把本项目保存在本机，然后在**项目目录**执行：

```bash
pwd -P
command -v uvx
```

后面的示例需要替换两个路径：

- `/absolute/path/to/wechat-connector`：上面 `pwd -P` 返回的项目目录。
- `/opt/homebrew/bin/uvx`：上面 `command -v uvx` 返回的可执行文件路径；Intel Mac 可能不同。

如果实际路径含空格，请在终端命令中用引号包裹路径。

本项目尚未发布到公共包仓库，**必须使用 `--from` 指定本地项目**，不要直接运行 `uvx wechat-connector`。
`uvx` 会创建并复用隔离的 Python 环境，不需要手动建虚拟环境或全局安装本项目。
SQLCipher 仍需由 Homebrew 安装。

先预热依赖并确认命令能启动：

```bash
uvx --from /absolute/path/to/wechat-connector wechat-connector --help
```

### 2. 准备密钥和第一份快照

| 你的情况 | 应做什么 |
|---|---|
| 新用户，没有密钥 | 在终端执行下面的 `init` |
| 已有用户级密钥和快照 | 跳过初始化，直接连接 MCP |
| 已有密钥，没有快照 | 正常退出微信，再执行 `snapshot`，见“刷新数据” |
| 已有旧项目的密钥和快照 | 使用“导入已有数据”，无需再次捕获 |

**首次初始化存在账号风险：** 它会重签名一个微信副本并对该副本进程插桩，腾讯可能检测到这些操作。
这不是腾讯授权接口，不保证免封号；请在理解风险后自行决定是否初始化。

先在原版微信登录需要读取的账号，确保聊天数据已存在于本机，再正常退出微信。
然后在交互式终端执行，注意保留路径后的 `[init]`：

```bash
uvx --from '/absolute/path/to/wechat-connector[init]' wechat-connector init
```

程序会说明风险并要求输入 `YES`。之后按提示完成：

1. 程序检查原版签名，创建并处理本次专用应用副本；不修改原版微信，不关闭 SIP。
2. 程序启动副本并等待密钥派生；如果出现登录提示，由你在手机上确认登录所选账号。
3. 所有目标分库密钥通过认证后，原子保存到本地。
4. **正常退出副本**，按提示继续生成第一份加密快照。完成后会输出 `"ready": true`。

若发现多个账号目录，按错误提示增加 `--account <账号目录名>`。自定义数据位置使用 `--root <xwechat_files目录>`。
如果 macOS 拒绝访问源目录，请检查运行命令的终端是否具有相应文件访问权限。

密钥已保存但快照失败时，先解决提示的问题，再运行 `snapshot`；再次执行 `init` 也会复用已有密钥。
捕获超时或中断后，请正常退出副本。程序不会强制退出已运行的微信，也不会自动覆盖旧密钥。

### 3. 连接 MCP 客户端

连接使用 **stdio**：客户端通过 `uvx` 启动服务并管理它的生命周期。
不需要手动保持一个运行 `serve` 的终端，也没有需要填写的 HTTP URL。
日常配置使用不带 `[init]` 的来源；查询环境不要求 Frida，服务不会导入捕获模块。

#### Codex

替换项目及 uvx 路径后，在终端执行一次：

```bash
codex mcp add wechat-connector \
  --env PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  -- /opt/homebrew/bin/uvx \
  --from /absolute/path/to/wechat-connector \
  wechat-connector serve
```

这会添加用户级 MCP 配置。也可以改用 TOML，将下面的块合并到 `~/.codex/config.toml`，
**不要覆盖整个配置文件，也不要重复添加同名配置块**：

```toml
[mcp_servers.wechat-connector]
command = "/opt/homebrew/bin/uvx"
args = ["--from", "/absolute/path/to/wechat-connector", "wechat-connector", "serve"]
startup_timeout_sec = 60

[mcp_servers.wechat-connector.env]
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
```

TOML 示例允许首次准备依赖等待 60 秒；CLI 添加后若启动超时，也可补充该设置。
用 `codex mcp get wechat-connector` 检查配置，然后重新加载 MCP 连接或重启客户端。
配置语法见 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

#### 使用 JSON 配置的其他客户端

将此服务条目合并到客户端的 MCP 配置中，保留已有服务：

```json
{
  "mcpServers": {
    "wechat-connector": {
      "command": "/opt/homebrew/bin/uvx",
      "args": ["--from", "/absolute/path/to/wechat-connector", "wechat-connector", "serve"],
      "env": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
      }
    }
  }
}
```

显式 `PATH` 用于让 GUI 客户端找到 Homebrew SQLCipher。配置中不填写密钥值。
本项目不会自动修改客户端设置；本机以前生成的 `.local/` 配置文件不随 Git 分发，新用户使用上面的示例即可。

### 4. 确认连接成功

客户端应能发现这两个工具：

| 工具 | 用途 | 参数 |
|---|---|---|
| `wechat_list_chats` | 显示会话列表和联系人名称 | `account?`、`limit=20`、`cursor?` |
| `wechat_get_chat_history` | 按会话和时间读取历史 | `chat_id`、`account?`、`start_time?`、`end_time?`、`limit=20`、`cursor?` |

可以直接对 Agent 说：

> 使用微信工具列出最近活跃的 20 个会话，显示备注名、昵称和 chat_id。

如果连接成功但查询返回 `KEYS_NOT_FOUND`，说明尚未初始化；服务不会替你捕获密钥。

## 如何找到对方并读取聊天

### 从名称取得 chat_id

先调用 `wechat_list_chats`：

```json
{"limit": 20}
```

在结果的 `rows` 中，根据以下字段确认对象：

| 字段 | 含义 |
|---|---|
| `display_name` | 展示名称，优先级为备注名 → 昵称 → 微信号 → 内部用户名 |
| `remark` | 你设置的备注名，可能为 `null` |
| `nickname` | 对方昵称或联系人库记录的群名称，可能为 `null` |
| `alias` | 联系人库的 alias，通常是自定义微信号，可能为 `null` |
| `chat_id` / `username` | 聊天对象的内部标识，用于准确定位会话 |
| `account` | 本地微信账号目录标识，多账号查询时需要原样传回 |

`chat_id` 来自消息库的 `Name2Id.user_name`，不是昵称，也不一定等于自定义微信号。
个人会话常见 `wxid_...`，群会话常见 `...@chatroom`。**从同一行复制 `chat_id` 和 `account`，不要用昵称代替。**
名称按“本地账号＋内部用户名”关联；缺少联系人记录时回退到内部标识，不靠同名昵称猜测身份。

### 读取指定时间段

以下示例读取北京时间 **2026 年 9 月 1 日至 3 日**的完整日期范围。
将 `chat_id`、`account` 替换成会话列表返回的实际值，再调用 `wechat_get_chat_history`：

```json
{
  "chat_id": "<会话列表返回的 chat_id>",
  "account": "<同一行的 account>",
  "start_time": "2026-09-01T00:00:00+08:00",
  "end_time": "2026-09-04T00:00:00+08:00",
  "limit": 100
}
```

- 时间范围为 **[开始时间, 结束时间)**：包含开始，不包含结束。整天查询的结束时间应设为次日零点。
- 时间必须带 `Z` 或明确的时区偏移；`+08:00` 表示北京时间，不自动使用 Mac 的本地时区。
- 开始和结束都可省略，也可只指定一个；支持最多六位小数秒，开始必须早于结束。
- 单账号可省略 `account`；多账号历史查询必须指定。
- 返回的是该会话的双向聊天记录，不是该用户在所有群里的发言。结果按时间等字段稳定倒序排列。

也可以直接让 Agent 执行：

> 先从微信会话列表找到备注为“某某”的人；如果重名，让我选择。读取与此人在北京时间 9 月 1 日至 3 日的聊天，继续翻页直到该时间段全部读完，再总结。

### 超过一页怎么办

每页最多 100 条。结果包含 `has_more` 和 `next_cursor`：

1. `has_more` 为 `true` 时，用 `next_cursor` 再调用同一个工具。
2. 下一页保持相同 `chat_id`、`account`、`start_time`、`end_time`，只增加或替换 `cursor`。
3. 继续到 `has_more` 为 `false`，不要把第一页当成全部聊天。

游标绑定快照、工具、账号、会话和时间范围，不能交叉使用。名称可能重名，消息不会自动去重。
所有结果还包含 `snapshot_id`、`snapshot_created_at` 和 `live: false`；名称、备注和消息均以该快照为准。

## 刷新数据与终端查询

**新消息不会自动进入已有快照。** 先正常退出微信，再手动执行：

```bash
uvx --from /absolute/path/to/wechat-connector wechat-connector snapshot
```

成功后可以重新打开微信。MCP 在首次成功查询时固定一份快照，**刷新后需要重启 MCP 连接**才能使用新数据。
微信中改过备注名，也要这样刷新才能看到更新。

无需 MCP 客户端，也能直接在终端查询：

```bash
uvx --from /absolute/path/to/wechat-connector wechat-connector read --limit 20

uvx --from /absolute/path/to/wechat-connector wechat-connector read \
  --chat '<chat_id>' \
  --start-time '2026-09-01T00:00:00+08:00' \
  --end-time '2026-09-04T00:00:00+08:00' \
  --limit 100
```

CLI 只返回范围内最近的 `limit` 条；完整遍历请使用 MCP 分页。
如果希望使用 `wechat-connector` 短命令，可在源码目录执行 `uv tool install .`，但这不是 uvx 连接的前提。

源代码更新后若仍命中旧缓存，先刷新包缓存，再重启 MCP：

```bash
uvx --refresh-package wechat-connector --from /absolute/path/to/wechat-connector wechat-connector --help
```

## 导入已有数据

已有旧项目的密钥和快照时，无需再次捕获：

```bash
uvx --from /absolute/path/to/wechat-connector wechat-connector init \
  --import-keys /absolute/path/to/old/captured-keys.jsonl \
  --import-snapshot /absolute/path/to/old/snapshot
```

把最后一个路径替换为实际目录。导入前验证密钥和加密快照，只迁移加密分支及回执；
旧密钥、旧明文快照和应用副本不会被删除。目标已有相同数据时复用，冲突时拒绝覆盖。
仅导入密钥时，会验证本地微信数据库，并要求微信退出后生成快照。

## 数据保存在哪里

```text
~/.local/wechat-connector-keys.jsonl             # 明文密钥，0600
~/.local/share/wechat-connector/                # 私有目录，0700
  snapshots/<时间戳>/
    encrypted/<账号>/db_storage/...            # 加密数据库，0600
    receipt.json                              # 快照时间、完整性、校验值
  init-.../WeChat.app                          # 初始化创建的应用副本
```

密钥文件不是机器通用的一把钥匙，而是一组账号/分库对应的密钥。它以明文 JSONL 保存，没有放进系统钥匙串。
程序检查所有权、文件权限，拒绝符号链接；允许 `~/.local` 为 `0755`，但不允许其他用户写入。
**不要把密钥文件、数据库或聊天内容提交到 Git、粘贴进 MCP 配置或发送给他人。**

MCP 每次调用都检查本地密钥文件；文件被删除后，下一次调用报错，不继续使用缓存密钥。
服务不向 Agent 返回密钥值，也不把密钥放进命令行参数或错误日志。

## 常见问题

| 现象或错误 | 处理方式 |
|---|---|
| 找不到 `uvx` / `sqlcipher` | 安装 uv、SQLCipher；检查配置中的绝对路径和 PATH |
| MCP 启动超时 | 先用 `--help` 预热 uvx；Codex 可设 `startup_timeout_sec = 60` |
| `KEYS_NOT_FOUND` | 用户在终端执行首次初始化，MCP 不会自动初始化 |
| `KEYS_INVALID` | 检查密钥格式、所有权和 `0600` 权限；不要覆盖已有文件 |
| `SNAPSHOT_NOT_FOUND` | 正常退出微信，再执行 `snapshot` |
| `KEY_MISSING` / `KEY_MISMATCH` | 新分库缺密钥或原密钥不匹配；停止，不自动重抓或跳过 |
| `ACCOUNT_REQUIRED` | 使用命令提示或会话列表中的账号标识 |
| `INVALID_TIME_RANGE` | 使用带时区的 RFC3339 时间，且开始早于结束 |
| `INVALID_CURSOR` | 从第一页重新开始，并保持账号、会话和时间范围不变 |
| `CONTACT_AMBIGUOUS` | 联系人库中同一内部用户名有冲突名称，程序未猜测身份 |
| `READ_FAILED` | 数据、表结构或查询异常；不会回退到原库或明文库 |
| 看不到刚收到的消息或新备注 | 手动刷新快照，再重启 MCP 连接 |

可用下面的只读诊断查看微信版本、数据路径和访问错误：

```bash
uvx --from /absolute/path/to/wechat-connector wechat-connector doctor
```

刷新期间程序检查微信已退出，将数据库及日志复制到私有目录，仅在副本上恢复、校验并发布。
源变化或失败会阻止新快照发布，上一份快照仍可使用。查询使用 SQLCipher 只读连接，在内存中解密，
不生成明文整库，不写回微信目录，单次数据库调用限制 30 秒。

## 开发与验证

基础测试环境不要安装 Frida；首次捕获测试使用单独环境：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .
python3 test_doctor.py
.venv/bin/python -m unittest -v test_runtime test_product test_time_ranges test_contact_names

uv venv .local/init-test-venv
uv pip install --python .local/init-test-venv/bin/python -e '.[init]'
.local/init-test-venv/bin/python -m unittest -v test_capture_native
```

基础测试覆盖 WAL、认证、只读拒写、密钥缺失、迁移、中断、分页、时区边界和联系人名称。
真实捕获桥接测试只运行合成 CommonCrypto 进程，不运行微信。
`evaluations.xml` 的十条固定合成问答通过 MCP 列表及分页结果校验，不包含真实聊天；这不是独立模型能力评测。

本机曾对一份 331,052 条消息的既有快照完成迁移前后逐行对比。拥有该基线的开发者可运行：

```bash
.venv/bin/python verify_existing_snapshot.py \
  --snapshot /absolute/path/to/old/snapshot \
  --against /absolute/path/to/migrated/encrypted-snapshot
```

本地打包：`uv build --out-dir .local/dist`。不会自动公开发布。
根目录旧脚本保留兼容入口；旧 `capture_keys.py` 已停用，首次获取仅通过交互式 `init` 执行。

## 风险与来源

初始化的签名差异和进程插桩具有可检测性。快照查询减少后续客户端干预，不能消除先前风险。
微信协议对未经授权的第三方读取也有限制，“本地、只读、自用”不自动等于平台许可。

聊天正文会进入调用工具的 Agent 上下文；如果宿主使用远端模型，正文就可能离开本机。
聊天文本均是不可信数据，不能当成工具执行指令。

- [微信软件许可及服务协议](https://weixin.qq.com/agreement?lang=zh_CN)
- [微信隐私保护指引](https://weixin.qq.com/cgi-bin/readtemplate?lang=zh_CN&t=weixin_agreement&s=privacy)
- [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [早期参考帖子一](https://x.com/leaf_sanren/status/2073069608437764266)、[帖子二](https://x.com/leaf_sanren/status/2078438766398873986)

早期调研参考了 [v-local-chat](https://github.com/YeJe-cpu/v-local-chat) 的技术说明（检查版本
`5002aa70e5b1f72e86e26faf16c08e98a9b4217d`），未复制其非商业 source-available 实现。
其“不封号”说法不作为本项目保证。
