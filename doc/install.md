# 安装说明

本文只说明如何从零开始获取项目、安装依赖并启动程序。具体功能说明请看 [`features.md`](./features.md)。

## 1. 环境要求

建议环境：

- Windows 10 / Windows 11
- Python 3.13 或更高版本
- uv，用于管理 Python 环境和依赖
- Git，可选；使用 ZIP 包时不需要 Git

本项目会在项目目录内使用这些运行目录：

- `data/`：用户配置、SQLite 数据库、日志和临时文件
- `.mitmproxy/`：mitmproxy 证书和配置
- `.playwright-browsers/`：Playwright 浏览器
- `storages/`：文章归档数据

## 2. 获取项目

### 方式一：使用 Git clone

进入你希望保存项目的目录，然后执行：

```bash
git clone https://github.com/yeximm/Access_wechat_article.git
cd Access_wechat_article
```

如果你使用自己的 fork 或本地仓库地址，把仓库地址替换成实际地址即可。

### 方式二：使用 ZIP 包

1. 打开项目 GitHub 页面。
2. 点击 `Code`。
3. 点击 `Download ZIP`。
4. 解压到本地目录，例如：

```text
your_local_path\Access_wechat_article
```

建议不要解压到系统保护目录，也尽量避免路径过长。

## 3. 安装 Python 依赖

进入项目根目录：

```bash
cd your_local_path\Access_wechat_article
```

使用 uv 同步依赖：

```bash
uv sync
```

项目依赖以这两个文件为准：

- `pyproject.toml`
- `uv.lock`

本项目不再维护 `requirements.txt`。如果你在旧文档或旧版本中看到 `pip install -r requirements.txt`，请改用 `uv sync`。

不要把依赖安装到系统 Python、用户目录或全局环境。

如果提示找不到 `uv`，请先安装 uv。安装完成后重新打开终端，再回到项目根目录执行 `uv sync`。

## 4. 安装 Playwright 浏览器

Playwright Chromium 浏览器需要安装到项目目录 `.playwright-browsers/`。

PowerShell 中执行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH=".playwright-browsers"
uv run playwright install chromium
```

cmd 中执行：

```bat
set PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers
uv run playwright install chromium
```

安装后项目根目录下应出现：

```text
.playwright-browsers/
```

## 5. 安装项目 MITM CA 证书

在运行 Access 的 Windows 用户账户下，从项目根目录执行：

```powershell
& .\.venv\Scripts\python.exe .\scripts\install_mitm_ca.py
```

脚本读取系统配置及 `data/custom.yaml` 中的 `confdir`、`ca_cert_path`。首次运行会通过 mitmproxy 生成项目 CA；已有 CA 会保持原样。证书仅安装到当前用户的受信任根证书库 `Cert:\CurrentUser\Root`，无需管理员权限。重复执行会按指纹核验并跳过已安装的证书。

仅检查、不创建文件或安装证书：

```powershell
& .\.venv\Scripts\python.exe .\scripts\install_mitm_ca.py --check
```

输出为 JSON，包含证书路径、SHA-1 指纹和安装状态；退出码 `0` 表示证书已安装，`1` 表示检查发现缺失或未安装，`2` 表示错误。可通过 `--config path/to/custom.yaml` 指定自定义配置，路径相对于项目根目录解析。也可以从任意工作目录使用脚本的绝对路径调用。

证书安装后，在程序中重新执行启动自检或 CA 诊断。脚本不会启动采集或修改系统代理。`.mitmproxy/` 中包含签名私钥，应只保留在本机或受保护的备份中；不要提交到 Git 或复制到其他机器。若私钥缺失、公钥不匹配或 CA 已过期，脚本会报错并保留原文件，需先恢复或人工处理已有 CA。

通过 SSH 执行时，若直接导入返回 `0x80070032 (ERROR_NOT_SUPPORTED)`，先在 Windows 桌面启动 Access，再执行：

```powershell
& .\.venv\Scripts\python.exe .\scripts\install_mitm_ca.py --via-running-app
```

此模式调用已运行的 `127.0.0.1:8766` 桌面程序完成导入。安装前必须核对该程序的 CA 路径和指纹与当前项目一致，安装后再次读取当前用户根证书库验证；不会自行启动 GUI。若 Windows 弹出证书安全警告，需要在桌面点击“是”；现有桌面接口的安装超时为 20 秒，请先准备好操作，或直接在 Windows 桌面终端运行默认安装命令。若桌面程序无法访问或使用另一项目的证书，脚本会报错。

## 6. 启动桌面程序

在项目根目录执行：

```bash
uv run python main.py
```

程序会启动本地服务并打开桌面窗口。


## 7. 安装验证

完成安装后，执行：

```bash
uv run python main.py
```

桌面程序启动后，也可以在浏览器访问网页端：

```text
http://127.0.0.1:8766/
```

如果根路径没有自动打开页面，可以访问：

```text
http://127.0.0.1:8766/index.html
```

如果能打开桌面窗口，说明基础运行正常。


## 8. 更新项目

如果使用 Git clone：

```bash
git pull
uv sync
```

如果依赖或 Playwright 版本变化，再执行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH=".playwright-browsers"
uv run playwright install chromium
```

如果使用 ZIP 包更新，建议先备份：

```text
data/custom.yaml
data/awa_public.sqlite3
storages/
.mitmproxy/
```

然后再替换项目代码。

## 9. 安装相关常见问题

### uv 命令不存在

说明本机还没有安装 uv，或安装后终端没有刷新环境变量。安装 uv 后重新打开终端，再执行：

```bash
uv --version
```

### Playwright 提示找不到浏览器

重新在项目根目录执行：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH=".playwright-browsers"
uv run playwright install chromium
```

并确认项目根目录存在 `.playwright-browsers/`。

### 网页端无法访问

网页端依赖 `main.py` 启动的本地服务。请先确认桌面程序正在运行，然后访问：

```text
http://127.0.0.1:8766/
```

如果端口被占用，关闭旧的程序进程后重新执行：

```bash
uv run python main.py
```

