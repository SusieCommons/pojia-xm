---
name: Bug 报告
about: 某个客户端注入失败、还原不干净、界面报错
title: "[bug] "
labels: bug
---

## 现象

<!-- 一句话说清出了什么事 -->

## 复现步骤

1.
2.
3.

## 预期

<!-- 你以为会发生什么 -->

## 环境

| 项 | 值 |
| --- | --- |
| 工具版本（`python app.py --version`） | |
| Windows 版本 | |
| Python 版本 | |
| 客户端 | Codex / WorkBuddy / ZCode / DSH |
| 客户端版本 | |

## 诊断输出

先跑这个，把输出贴上来（**贴之前检查有没有个人信息**）：

```bash
python app.py --status
python app.py --verify
```

<details>
<summary>展开贴输出</summary>

```
（贴这里）
```

</details>

## 相关文件状态

如果问题出在某个具体文件上，贴一下它的状态（**不要贴文件内容，只要编码和大小**）：

```bash
python -c "from pathlib import Path; p=Path(r'路径'); print(p.stat().st_size, p.read_bytes()[:4])"
```

## 补充

<!-- 截图、日志片段、你试过什么 -->
