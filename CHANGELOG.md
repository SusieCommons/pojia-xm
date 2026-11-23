# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.3.7] - 2026-11-22

### 修复

- **GBK 文件注入后客户端读到乱码。** 旧实现「按原编码写回」，GBK 文件注入后仍是 GBK，而 Codex / WorkBuddy / ZCode / DSH 都按 UTF-8 读用户级指令 —— 界面报成功，客户端里人格没生效。现在注入统一写 UTF-8，还原按备份原始字节写回。
- **无 BOM 的 UTF-16 文件被写坏。** `b'#\x00 \x00...'` 按 UTF-8 解码不报错，只解出一串带 NUL 的垃圾，然后被原样写回，原文彻底毁掉。新增 `_looks_utf16()`，按零字节分布判断（长度偶数、零字节占比 8%~50%、85% 以上落在同一侧），不再靠「试解码成功」猜编码。

### 变更

- 注入与还原拆成两个函数：`write_like()` 写 UTF-8，`write_orig()` 写原始字节。合并会同时踩上面两个坑。
- 新增 `tools/verify_release.py`：对打包产物做校验，静态读 PE 版本资源与图标，动态在沙箱里跑真 exe 走完注入 / 校验 / 还原。CI 增加 `build` job，从源码打一次包再验，证明打包链路是通的。
- 新增 `tools/make_version_info.py`：版本号只从 `app.py:VERSION` 读，一次写完 `filevers` / `prodvers` / `FileVersion` / `ProductVersion`。`build/` 不进库，CI 打包前得靠它生成版本资源，否则打出来的 exe 没有 VS_VERSIONINFO。
- **命令行帮助与错误提示全中文化。** 界面上从文档到按钮都是中文，只有 `--help` 的输出是英文骨架（`usage:` / `positional arguments:` / `options:` / `show this help message and exit`），很割裂。新增 `core/zh_argparse.py` 接管 argparse 的 `_()`，覆盖 29 条内置文案，`app.py` 与 `xm.py` 两个入口共用同一份表。注意必须在构造 `ArgumentParser` **之前**调用 —— `add_help=True` 在构造时就把 `-h` 的帮助文字固化下来了，晚一步改不到。
- 测试里 `test_gbk` 的旧断言已改。它原本写的是「GBK 文件没被改成 UTF-8」，把 1.3.7 修掉的错误行为固化成了预期。
- 新增 `tests/test_zh_cli.py`：锁住命令行的中文输出。除了校验翻译表覆盖与幂等，还真跑一遍 `app.py --help` / `xm.py --help` / 一条错误路径，确认既没有漏翻也没有回退成英文。

## [1.3.6] - 2026-11-21

### 修复

- **`revert` 报成功但人格还在。** 旧实现用备份写回，而备份可能在注入之后才抓，那份「原始内容」其实是注入态，写回去等于原地覆盖。现在按 `xm-persona:begin/end` 标记剥离，备份只兜底。
- **有头无尾的残块清不掉。** `has_block()` 原来要求头尾都在，于是被截断的注入块永远识别不出来。现在只要看到 `BEGIN` 就算有痕迹，`_cut()` 从标记处切到末尾。
- **Codex 用户自己写的 `xm-persona.md` 被误删。** 改成三级优先：备份有原文就写回 → 确认是本工具产物才删 → 判不出来不动。

### 新增

- 只读文件自动清属性后写入。
- `os.replace` 遇 `PermissionError` 退避重试（杀软扫描窗口）。
- 数据目录多级兜底。
- WorkBuddy 多版本目录全写。
- 手动指定目录选错时给出可读原因。

## [1.3.0] - 2026-11-19

### 变更

- **取消在线授权。** 下载即用，不再需要机器码、授权码、token。`core/license.py` / `core/licnet.py` / `core/ticket.py` 保留函数签名但无条件放行 —— 删签名要改 `app.py` 十几处调用点，回归面太大。
- 引流信息改为服务端签名下发 + 内置兜底。远端拿不到或验签失败就用内置值，不锁功能。

## [1.2.4] - 2026-11-18

### 变更

- 界面客户端列表换成四家官方图标，全部内联进 `ui/index.html`（data URI / 内联 SVG）。不挂外链图片 —— onefile 打包每次解压路径不同，外挂资源容易丢。

## [1.2.0] - 2026-11-15

### 新增

- 支持 WorkBuddy / ZCode / DSH 三个客户端（此前只有 Codex）。
- 版本检查带 Ed25519 签名，客户端内置公钥验签。

## [1.0.0] - 2026-11-10

### 新增

- 首个版本。Codex 靶点注入与还原，图形界面 + 命令行。
