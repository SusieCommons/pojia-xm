# 参与

## 提交前

```bash
python -m pytest tests/ -q
```

全绿再提。测试全部在临时目录里跑，不会碰你真实的客户端配置。

## 改代码时注意

### 1. 注入写 UTF-8，还原写原始字节

这两个方向不能合并。`core/base.py` 里：

- `write_like()` — 写 UTF-8。客户端是 Node / Electron，只认 UTF-8
- `write_orig()` — 写原始字节。用户的文件不能因为我们注入一次就永久变编码

合并成一个函数，就会出「提示成功但没生效」或者「还原了但文件永久变了」。

### 2. 还原按标记剥，不按备份写回

备份是不可靠的：如果备份是在注入之后抓的，那份「原始内容」其实是注入态，写回去等于原地覆盖。唯一可靠的依据是 `xm-persona:begin/end` 这对标记。

### 3. 探测不到靶点就不动手

宁可报「没有可写的靶点」，也不要在猜出来的路径上写文件。

### 4. 新增客户端适配器

继承 `core/base.py` 的 `Target`，实现三个方法：

```python
class MyClient(Target):
    name = "myclient"
    label = "MyClient"
    procs = ("myclient.exe",)

    def detect(self) -> bool:
        """找到安装根目录，存到 self.root"""
        ...

    def collect(self) -> list[Hit]:
        """列出所有要改的文件。只读，不动磁盘。"""
        ...

    def body(self, persona: str) -> str:
        """把人格正文包成要写入的内容"""
        ...
```

再在 `app.py` 的靶点列表里注册，在 `tests/test_inject.py` 加一组 apply/revert 测试。

### 5. 加测试，不加断言

测试要验的是「还原之后用户的文件跟注入前逐字节一致」，不是「函数返回了 True」。

## 不要提交

- 任何密钥（`secret.key`、`signer.key`、`*.pem`）
- 生产服务器地址、后台账号密码
- 本机绝对路径
- 二维码、群号、推广链接
- 构建产物（`dist/`、`*.exe`）

CI 里有对应的检查，提上来会红。

## 提交信息

```
feat: 一句话说做了什么
fix: 修了什么现象
docs: 文档
test: 只改测试
```

正文里写清「为什么会出这个 bug」，比写「改了哪个函数」有用。
