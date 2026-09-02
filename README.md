# 三平台统一电商爬虫

本仓库把淘宝、Walmart 和 Wayfair 三个独立爬虫放在同一个目录中，并提供统一的选择入口。平台抓取逻辑仍然相互独立；统一启动器只负责选择平台、检查目录和启动原有入口。

## 快速开始（Windows）

建议使用 Python 3.10 或更高版本。Walmart 和 Wayfair 的启动脚本优先使用各自目录下的虚拟环境；Walmart 在本地虚拟环境不存在时会回退到统一启动器当前使用的 Python。淘宝使用仓库根目录的虚拟环境或当前 Python。

```powershell
# 淘宝/统一启动器使用的环境
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Walmart
py -3 -m venv walmart\.venv
.\walmart\.venv\Scripts\python.exe -m pip install -r walmart\requirements.txt

# Wayfair
py -3 -m venv wayfair\.venv
.\wayfair\.venv\Scripts\python.exe -m pip install -r wayfair\requirements.txt -r wayfair\requirements_http.txt
```

双击 [`start_scraper.bat`](start_scraper.bat) 可打开交互式菜单，也可以直接运行：

```powershell
.\.venv\Scripts\python.exe unified_scraper.py --platform taobao
.\.venv\Scripts\python.exe unified_scraper.py --platform walmart
.\.venv\Scripts\python.exe unified_scraper.py --platform wayfair

# 仅检查目录、入口和运行时，不联网、不启动爬虫
.\.venv\Scripts\python.exe unified_scraper.py --platform taobao --dry-run
```

省略 `--platform` 时会显示菜单。统一启动器仍支持原有的目录和运行时覆盖：

```powershell
.\.venv\Scripts\python.exe unified_scraper.py --platform taobao --taobao-dir D:\work\taobao
.\.venv\Scripts\python.exe unified_scraper.py --platform walmart --walmart-dir D:\work\walmart
.\.venv\Scripts\python.exe unified_scraper.py --platform wayfair --wayfair-dir D:\work\wayfair
```

也可以设置 `TAOBAO_SCRAPER_DIR`、`WALMART_SCRAPER_DIR`、`WAYFAIR_SCRAPER_DIR` 环境变量；命令行参数优先于环境变量。淘宝运行时还可用 `--taobao-python` 指定 Python，Wayfair BAT 可用 `--cmd-exe` 指定 `cmd.exe`。

## 浏览器和登录前提

- 淘宝：首次运行前启动带远程调试端口 9222 的 Chrome，并在该浏览器中登录淘宝。登录态会保存到本地运行时目录；登录态失效时需要重新登录。
- Walmart：先打开目标 AdsPower profile，并在同一 profile 中打开 `walmart.com` 标签页；V9 默认还会读取已登录 Sorftime 扩展的 token，通过 HTTP 获取预计月销量/预计月销售额。可用 `--no-sorftime` 关闭，或用 `--sorftime-mode browser` 强制旧的浏览器 DOM 模式。程序只读取已打开浏览器的登录态，不绕过 CAPTCHA 或人工验证。
- Wayfair：商品列表模式默认走 HTTP；评论或浏览器回退模式需要正常的 Chrome 会话和本地 Cookie 文件。Cookie 文件只在本机使用，不能提交到仓库。

请遵守目标网站的服务条款、访问频率限制和适用法律法规。

## 目录结构

```text
unified_scraper.py       # 统一调度器
start_scraper.bat        # ASCII-only Windows 入口
requirements.txt         # 合并依赖
tests/                   # 不联网的启动器测试
walmart/
  src/walmart_scraper/   # Walmart 源码包
  run.py                 # Walmart Python 入口
  README.md              # Walmart V9 / Sorftime 使用说明
  *.ps1 / *.bat          # Walmart 启动辅助脚本
  pyproject.toml
  requirements.txt
wayfair/
  wayfair_http_scraper.py
  embed_wayfair_images.py
  *.ps1 / *.bat
  requirements*.txt
taobao/
  taobao_scraper.py      # 淘宝兼容启动入口
  taobao_scraper/        # 淘宝源码包
  启动淘宝抓取.bat
```

各平台的 `runtime`、`taobao_runtime`、输出目录、结果文件、原始 HTML/JSON、断点、Cookie、登录态、浏览器 session、虚拟环境、缓存和生成的 `egg-info` 均被 `.gitignore` 排除。仓库不包含任何实际账号、密码、Token、Cookie 或其他认证状态；请勿把这些内容手动加入提交。

## 测试

测试只构造启动命令并使用临时目录，不执行真实爬取或网络请求：

```powershell
py -3 -m unittest discover -s tests -v
py -3 -m compileall -q unified_scraper.py walmart\run.py walmart\src taobao\taobao_scraper wayfair\wayfair_http_scraper.py wayfair\embed_wayfair_images.py
```
