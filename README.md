# 小熊猫破甲

**把一份人格文件装进本地 AI 客户端的配置里。** 装、验、还原，三个动作都有对应的代码路径和测试。

Windows x64 · Python 3.10+ · MIT

[![tests](https://github.com/OWNER/xiaoxiongmao/actions/workflows/tests.yml/badge.svg)](https://github.com/OWNER/xiaoxiongmao/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)

---

## 它做什么

四个客户端各自把「用户级指令」放在不同的地方。这个工具找到那些位置，把一段你写的正文写进去，并留下可撤销的痕迹。

| 客户端 | 写哪里 | 形态 |
| --- | --- | --- |
| Codex | `~/.codex/config.toml` 的 `model_instructions_file` + `~/.codex/xm-persona.md` | 配置键 + 正文文件 |
| WorkBuddy | `~/.workbuddy/plugins/cache/workbuddy-builtin/*/*/prompt.tpl` 的 `<content_policy>` 段 + `~/.workbuddy/SOUL.md` | 模板段替换 + 追加 |
| ZCode | `~/.zcode/AGENTS.md` | 整文件注入 |
| DSH | `~/.dsh/AGENTS.md`、`~/.agents/AGENTS.md`（后者不存在就不建） | 整文件注入 |

注入段用一对纯文本标记围起来：

```
========== xm-persona:begin ==========
...你的正文...
========== xm-persona:end ==========
```

`revert` 按这对标记剥离，不依赖备份 —— 备份只在「文件本来有内容」时作为第二道保险。

## 为什么还原要按标记剥，而不是按备份写回

这是踩过的坑，写在代码里也写在这里：

第一次注入时，如果目标文件里已经有注入内容（上一版留下的、或者备份是在注入之后才抓的），那份「原始内容」其实已经是注入态。`revert` 拿它写回去，等于原地覆盖 —— 界面报「已还原 N 个」，打开客户端人格照旧。

所以现在的顺序是：

1. 按 `xm-persona:begin/end` 剥离。这是唯一可靠的依据，因为标记是我们自己写的。
2. 剥完文件变空、且确认是本工具产物 → 删文件。
3. 判不出来 → 不动，报给用户。

备份只在第 1 步拿不到内容时兜底。

## 编码契约

两个方向必须分开，混在一起就会出「提示成功但没生效」：

| 阶段 | 做什么 | 为什么 |
| --- | --- | --- |
| 注入 | **统一写 UTF-8** | 四个客户端都是 Node / Electron，读用户级指令一律按 UTF-8。写 GBK 进去它解成乱码，人格等于没注入，界面却报成功 |
| 还原 | **按备份原始字节写回** | 用户的文件不能因为我们注入一次就永久变成 UTF-8 |

`core/base.py` 里对应两个函数：`write_like()` 写 UTF-8，`write_orig()` 写原始字节。别合并。

无 BOM 的 UTF-16 是最阴的一种：`b'#\x00 \x00...'` 按 UTF-8 解码**不报错**，只会解出一串带 NUL 的垃圾，然后被原样写回，原文彻底毁掉。`_looks_utf16()` 按零字节分布判断（长度偶数、零字节占比 8%~50%、85% 以上落在同一侧），不靠「试解码成功」。

## 快速开始

```bash
git clone https://github.com/OWNER/xiaoxiongmao
cd xiaoxiongmao
python -m pip install -r requirements.txt
```

图形界面：

```bash
python app.py
```

命令行：

```bash
python app.py --status              # 四个客户端装没装、注没注
python app.py --apply               # 全部注入
python app.py --apply codex zcode   # 只注入指定的
python app.py --verify              # 校验注入结果
python app.py --revert              # 全部还原
python app.py --dir                 # 看/改客户端目录
```

第一次跑会要求读一遍并接受使用条款，记录落在 `~/.xiaoxiongmao/consent.json`。`--accept` 可跳过交互（脚本用）。

## 目录

```
xiaoxiongmao/
├── app.py                入口：GUI / CLI / 更新检查 / 品牌信息
├── xm.py                 CLI 包装
├── core/
│   ├── base.py           注入引擎：编码、原子写、标记段、备份、状态
│   ├── codex.py          Codex 靶点（配置键 + 正文文件）
│   ├── wb.py             WorkBuddy 靶点（多版本目录全写）
│   ├── zcode.py          ZCode 靶点
│   ├── dsh.py            DSH 靶点
│   ├── brand.py          引流信息（远端签名 + 内置兜底）
│   ├── update.py         版本检查（Ed25519 验签）
│   ├── license.py        机器码
│   ├── licnet.py         旧授权层，1.3.0 起空实现
│   ├── ticket.py         票据，1.3.0 起空实现
│   ├── fastid.py         快速机器标识
│   └── proc.py           进程工具
├── server/               服务端：版本发布 + 引流配置下发 + 后台
├── bot/                  QQ 群机器人（可选）
├── ui/index.html         界面（单文件，图标内联）
└── tests/                测试
```

## 服务端

`server/server.py` 是单文件、只用标准库的 HTTP 服务。它做两件事：

- `/api/license/version` 下发最新版本号、下载地址、更新说明，带 Ed25519 签名
- `/api/license/brand` 下发引流信息（群号、群名、提示语），同样带签名

客户端内置公钥验签。验签失败时：不显示远端内容、不锁功能、不写缓存 —— 用内置默认值继续跑。

```bash
python server/server.py --port 8090 --db ./data.db
```

签名私钥不在仓库里。`server/server.py` 首次启动会自己生成一把（落在 `secret.key`），但**这只能用于本地调试** —— 客户端内置的是生产公钥，对不上就验签失败，界面会退回内置默认值。要发正式版本，得用你自己的密钥对重新打包客户端。


## 测试

```bash
python -m pytest tests/ -q
```

覆盖的是「真机会遇到但常规测试不会碰」的情形：

- 目标文件是 GBK / UTF-16（带 BOM / 不带 BOM）
- 文件带只读属性
- 文件被短暂占用（杀软扫描窗口）
- 备份目录写不了
- 同一插件有多个版本目录
- 模板里没有 `<content_policy>`（不能当靶点，往里塞会破坏 Jinja）
- 用户手动指定了错的目录
- 中文用户名路径
- 空文件（0 字节）
- 注入两次再还原（历史 bug 的现场）

全部在临时目录里跑，不碰真实客户端。

## 已知限制

- **Windows only**。靶点路径、只读属性、文件占用重试都是 Windows 语义。
- **WorkBuddy 的 `prompt.tpl` 是 Jinja 模板**。注入点必须落在 `<content_policy>` 段内部，塞到别处会破坏模板渲染。
- **客户端更新会覆盖配置**。Codex 升级后 `config.toml` 可能被重写，重跑一次 `--apply` 即可。
- **服务端不可用不影响注入**。版本检查和引流信息都是可选增强，拿不到就用内置值。

## 许可

MIT。见 [LICENSE](LICENSE)。
