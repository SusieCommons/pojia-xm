## 这一版修的是「装上了但客户端读不到」

两处都是静默失败：界面报成功、`verify` 一路绿灯，客户端里人格没生效。查起来最费时间的那种。

### 1. GBK 文件注入后客户端读到乱码

旧实现「按原编码写回」—— GBK 文件注入完还是 GBK。但 Codex / WorkBuddy / ZCode / DSH 都是 Node 写的，只按 UTF-8 读用户级指令。

实测一个 23 字节的 GBK 文件：

```
原始   23 B  b'# \xce\xd2\xb5\xc4\xc8\xcb\xb8\xf1'  ← GBK
注入后 143 B
  UTF-8 解码 FAIL (0xce)     ← 客户端走这条路，读到乱码
  GBK 解码 OK，人格确实在里面  ← 我们自己的校验走这条路，所以一路绿灯
```

现在拆成两个契约，缺一半都不对：

| 阶段 | 做什么 |
| --- | --- |
| 注入 | 统一写 UTF-8 |
| 还原 | 按备份的原始字节写回，逐字节还原编码 |

`Target.strip_target` 也改成优先用 `Backup.original_of()` 写回，拿不到才退「剥标记段 + 写 UTF-8」。

### 2. 无 BOM 的 UTF-16LE 会毁原文

`23 00 20 00` 这种字节，`decode("utf-8")` **不报错** —— 每个字节都合法。旧代码于是收下，得到 `'#\x00 \x00\x11b剉篘'`，然后把人格块一起写回。**用户原文彻底丢掉，`revert` 也救不回来。**

修法是解码前按零字节分布判断，不再靠「试解码成功」—— 那个判据对任何偶数长度字节都成立：

- 长度偶数
- 零字节占比 8% ~ 50%
- 85% 以上零字节落在同一侧

`latin1` 也必须放到候选编码最后，它对任何字节都「成功」。

### 3. 顺手

- 注入与还原彻底拆成两个函数（`write_like()` / `write_orig()`）。合并成一个「按原编码写回」的函数会同时踩上面两个坑。
- 测试里 `test_gbk` 的旧断言是错的，已改。它当时写的是 `ck("GBK 文件没被改成 UTF-8", not is_utf8)` —— 把错误行为固化成了预期，留着比没测试更危险。
- 命令行帮助全中文化。`--help` 原来冒出 `usage:` / `positional arguments:` / `options:` / `show this help message and exit` 四句英文，报错时还有 `error:` 前缀。`core/zh_argparse.py` 接管 argparse 的 `_()`，29 条内置文案一起换掉，`app.py` 和 `xm.py` 两个入口共用同一份表。必须在构造 `ArgumentParser` 之前调用 —— `add_help=True` 在构造时就把 `-h` 的帮助文字固化到选项上了。

---

### 校验

```
FileVersion   1.3.7
ProductVersion 1.3.7
体积          13,247,305 B
SHA-256       507711c937600096089fdd88fe4ab86012f1cdc00b7119d59155882f242016e6
```

自检（需要 Python 3.10+）：

```bash
python -m pytest tests/ -q                    # 63 passed
python tools/verify_release.py                # 30 项全过
```

`tools/verify_release.py` 是在临时目录里跑真 exe 的：`HOME` / `USERPROFILE` / `XM_DATA` / `CODEX_HOME` / `ZCODE_HOME` / `DSH_HOME` 全指到沙箱，不碰本机配置。覆盖条款闸门、codex 注入还原、zcode 追加模式幂等、无 BOM UTF-16LE 逐字节还原。

### 升级

覆盖旧 exe 即可。数据目录（`~/.xiaoxiongmao`）里的备份和 `consent.json` 不动，还原记录继续有效。

如果上一版注入过 GBK 文件、客户端里读出来是乱码，这一版重跑一次「一键破甲」就好 —— 它会用 UTF-8 重写一遍。
