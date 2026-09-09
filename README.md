# wechat-connector

目标：让 Agent 通过本地 MCP **只读查询 Mac 微信聊天记录**。
第一阶段已在本机完成数据定位、密钥捕获、解密和真实消息读取。尚未实现 MCP 服务。

## 当前结果（2026-09-08 本机检查）

- macOS 27.0，Apple Silicon；微信 4.1.13（269630）正在运行。
- 应用：`/Applications/WeChat.app`，原版签名未修改。
- 微信进程明确使用的数据根目录：
  `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files`。
- 初次读取被 macOS 拒绝；之后文件访问已恢复，实际发现 22 个数据库，共 563,527,680 字节（约 537 MiB）。
- 只提取聊天所需的 5 个库：`contact.db`、`session.db`、`message_0.db`、`message_1.db`、`biz_message_0.db`。
  5 把密钥均通过 SQLCipher 4 首页面 HMAC-SHA512 认证，5 个明文库均通过 `integrity_check`。
- 三个消息库分别有 287,743、11,994、31,315 行，共 **331,052 行**。
  已逐行解码正文（含 Zstandard），无解码失败；每行发送人 ID 均可关联 `Name2Id.rowid`。
  这是原始消息行数，包含系统消息和公众号记录，不等于去重后的真人发言数。
- 在 `.local/WeChat.app` 副本上完成签名处理和启动捕获；原版签名前后核对一致，SIP 保持开启。
- 快照保存在 `.local/snapshots/20260909T025226.475190Z/`，
  `receipt.json` 记录完整性结果，`read-verification.json` 记录全量读取检查（不包含正文）。

## 现在可运行

```bash
python3 doctor.py
.venv/bin/python read_chat.py --limit 20
.venv/bin/python read_chat.py --chat '<上一步返回的 chat_id>' --limit 20
```

`doctor.py` 只枚举 `xwechat_files/<账号>/db_storage` 下的 `.db`，读取 16 字节文件头，
记录文件大小及 WAL/SHM/journal 是否存在。它不读取消息正文、不提取密钥、不连接数据库或写回微信目录。
非 SQLite 文件头只表示“可能加密”，不能单凭文件头确认为 SQLCipher。
无数据库或遇到访问错误时返回退出码 2，JSON 中保留具体错误，不把权限错误当作空数据。

若再次遇到 `Operation not permitted`，在 **系统设置 → 隐私与安全性 → 完全磁盘访问权限** 中为 Codex 开启权限；
如系统提示退出并重新打开，照提示操作后重跑诊断。若在 Terminal 中运行，则需要对应终端具有权限。
这是 macOS 文件访问阻塞，不是数据库解密失败；完全磁盘访问权限也不等于进程调试权限。

`read_chat.py` 默认使用最近一次完整快照，支持 `--snapshot` 指定快照目录。
无 `--chat` 时列出会话，指定后按时间倒序读取最近消息（1–100 条）。保留数据库来源、
原始消息 ID、发送人 ID/用户名、时间、类型及正文。暂不解析 XML 引用、昵称或媒体文件，
也不做去重、实时同步、全文搜索和 MCP 封装。

诊断输出包含账号目录名；读取命令会输出真实聊天正文。若需落盘，请保存在已忽略的 `.local/`：

```bash
mkdir -p .local
chmod 700 .local
(umask 077; python3 doctor.py > .local/doctor.json)
```

## 环境与重做快照

本机已安装 SQLCipher 4.19.0，Python 虚拟环境依赖锁定在 `requirements.txt`。

```bash
brew install sqlcipher
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

目前已有有效密钥；微信正常退出后可直接重做快照：

```bash
python3 decrypt_snapshot.py
```

脚本复制加密数据库及现存 WAL/journal 到新的私有目录，在副本上调用 SQLCipher 导出明文，
检查认证与完整性，再生成成功回执。微信运行中会拒绝制作快照，不写回原始数据库。
新建分库若缺少密钥，需要重新捕获；现有快照不保证包含将来消息。

确需重新捕获时，先正常退出微信，保留旧 `.local/captured-keys.jsonl` 到另一个私有文件名，
再运行 `.venv/bin/python capture_keys.py`。它拒绝覆盖已有密钥文件；启动的是准备好的应用副本，
hook 安装后才恢复启动，必要时需要手机登录确认。捕获后正常退出副本再制作快照。
应用副本不隔离账号数据目录，不能与原版同时运行。更新微信后应重新准备副本并验证兼容性。

密钥文件权限为 `0600`，`.local/` 为 `0700`，源码不包含真实密钥或数据库。

## 回归检查

```bash
python3 test_doctor.py
python3 test_crypto.py
.venv/bin/python test_read_chat.py
```

使用临时合成数据验证目录发现、只读边界、SQLCipher 真实加密格式、错误密钥/损坏页拒绝、
明文导出、跨分库排序、发送人映射和 Zstandard 正文解码。

## 参考资料与路线

已读取用户提供的两篇原文：

- [2026-07-03：加密系统与密钥捕获](https://x.com/leaf_sanren/status/2073069608437764266)
- [2026-07-18：解密后的消息归属和提取问题](https://x.com/leaf_sanren/status/2078438766398873986)
- [作者项目 v-local-chat](https://github.com/YeJe-cpu/v-local-chat)，检查版本
  `5002aa70e5b1f72e86e26faf16c08e98a9b4217d`。
- [密钥获取指引](https://github.com/YeJe-cpu/v-local-chat/blob/5002aa70e5b1f72e86e26faf16c08e98a9b4217d/docs/01-%E8%8E%B7%E5%8F%96%E5%AF%86%E9%92%A5-%E6%8C%87%E5%BC%95.md)
- [SQLCipher 官方格式说明](https://www.zetetic.net/sqlcipher/design/)

作者使用 Frida 在微信启动暂停态安装 `CCKeyDerivationPBKDF` hook，
再恢复启动以捕获派生密钥；对应用副本处理签名，保留原版应用。
其指引明确列出测试版本 4.1.11.55 / 4.1.12；本项目已补充本机 **4.1.13** 的实际验证。
作者关于“不会封号”和密钥长期可用的说法不作为本项目保证；新增分库可能需要新密钥。

上游是非商业 source-available 许可；本项目未复制其实现或安装其 Skill。
数据库解密使用 SQLCipher 本身；捕获使用 Frida，压缩正文使用 zstandard。

后续依据实库实现会话列表、分页历史和消息搜索的 stdio MCP。
读取端固定只读连接，输出消息 ID、发送人、时间、类型及原文；引用关系只用数据库证据。
不提供发消息、修改记录、联系人操作、朋友圈采集或任意 SQL 工具。
聊天内容是非可信数据，不能成为 Agent 的指令。MCP 返回给远端模型的正文会进入模型上下文，
因此“本地读取”不等于“正文始终不离开本机”。
