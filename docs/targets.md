# 靶点对照表

四个客户端把「用户级指令」放在不同地方，格式也不同。这份表是代码的唯一依据 ——
改靶点必须同时改 `core/*.py` 和这里。

## Codex

| 项 | 值 |
| --- | --- |
| 根目录 | `~/.codex`（或 `$CODEX_HOME`） |
| 主配置 | `config.toml` |
| 正文文件 | `xm-persona.md` |
| 注入方式 | 配置键 `model_instructions_file` 指向正文文件 |

`config.toml` 里只加这一个键：

```toml
model_instructions_file = "C:\\Users\\你\\.codex\\xm-persona.md"
```

Windows 路径的反斜杠要双写 —— TOML 基本字符串里 `\U` 是 Unicode 转义，不双写会解析失败。

还原时这个键必须一起删掉。留着会指向一个不存在的文件，Codex 启动时报错。

**特殊处理**：如果 `xm-persona.md` 在注入前就存在、且内容不是本工具写的，还原时不能删 —— 那是用户自己的文件。

## WorkBuddy

| 项 | 值 |
| --- | --- |
| 根目录 | `~/.workbuddy`（或 `$WB_HOME`） |
| 模板 | `plugins/cache/workbuddy-builtin/<插件>/<版本>/prompt.tpl` |
| 兜底 | `SOUL.md` |
| 注入方式 | 替换模板里的 `<content_policy>` 段内容 |

插件前缀：`welcomemode-`、`interactionmode-`、`prompt-`。

**多版本**：同一个插件可能有多个版本目录（`1.0.0` / `1.0.1` / `2.3.4-xxx`）。全部都要写到 —— 只写最新的那个，用户切回旧版本就看不到人格。目录名带构建号，不能按 mtime 认版本。

**`prompt.tpl` 是 Jinja 模板**。注入点必须落在 `<content_policy>` 开闭标签**之间**。没有这个标签的模板不能当靶点 —— 往里塞会破坏模板渲染，客户端直接打不开。

## ZCode

| 项 | 值 |
| --- | --- |
| 根目录 | `~/.zcode`（或 `$ZCODE_HOME`） |
| 靶点 | `AGENTS.md` |
| 注入方式 | 整文件追加标记段 |

## DSH

| 项 | 值 |
| --- | --- |
| 根目录 | `~/.dsh`（或 `$DSH_HOME`） |
| 靶点 | `AGENTS.md` |
| 共享目录 | `~/.agents/AGENTS.md`（或 `$DSH_AGENTS_HOME`） |
| 注入方式 | 整文件追加标记段 |

**`~/.agents` 不存在时不要创建。** 那是好几个工具共用的目录，我们没资格凭空建它。存在才处理。

## 标记段

```
========== xm-persona:begin ==========
（人格正文）
========== xm-persona:end ==========
```

纯文本，不是 HTML 注释 —— 有些客户端会把 `<!-- -->` 当 Markdown 注释吃掉。

## 编码

| 阶段 | 写什么 |
| --- | --- |
| 注入 | UTF-8（无 BOM） |
| 还原 | 备份里的原始字节 |

四个客户端都是 Node / Electron，读用户级指令一律 `fs.readFileSync(p, "utf8")`。写 GBK 进去它解成乱码，人格等于没注入，界面却报成功。

## 探测顺序

1. 环境变量（`CODEX_HOME` / `WB_HOME` / `ZCODE_HOME` / `DSH_HOME`）
2. 用户手动指定的目录（存在 `~/.xiaoxiongmao/paths.json`）
3. 默认位置（`~/.codex` 等）

手动指定的目录要通过 `looks_like()` 校验。选错目录（比如把安装目录当用户目录）要给出可读原因，不能静默当「未安装」。
