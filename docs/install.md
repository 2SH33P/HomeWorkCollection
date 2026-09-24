# 安装与启动

## Windows（推荐）

1. 安装 Python 3.10+（<https://www.python.org/downloads/>），安装时务必勾选 **Add Python to PATH**。
2. 双击项目根目录的 **`start.bat`**。
   - 首次运行会自动创建虚拟环境并安装依赖（约 2 分钟，仅一次）
   - 之后自动打开浏览器 `http://localhost:8091`
3. 关闭：直接关掉那个命令行窗口（或在窗口里按 `Ctrl+C`）。

### 手机上传照片

手机与电脑连同一个 WiFi，手机浏览器访问 `http://电脑的局域网IP:8091`
（命令行窗口启动时会打印可用的局域网地址；`ipconfig` 也能查到 IPv4 地址）。

## macOS / Linux

```bash
bash start.sh
```

同样会首次自动建 `.venv` 并装依赖，然后启动并尝试打开浏览器。

## 手动运行（任意平台）

不想用脚本时：

```bash
pip install fastapi "uvicorn[standard]" python-multipart pillow opencv-python-headless numpy typst
python tools/crop-tool/app.py
# 访问 http://localhost:8091
```

## 后台常驻运行（服务器 / 长期开机）

让服务脱离当前终端，关掉 SSH 也不会挂：

```bash
cd /path/to/HomeWorkCollection
(setsid nohup .venv/bin/python tools/crop-tool/app.py > /tmp/cuoti.log 2>&1 < /dev/null &)
```

重启服务（改了代码后需要）：

```bash
pgrep -f "tools/crop-tool/app.py"        # 先看 PID
kill <PID>
(setsid nohup .venv/bin/python tools/crop-tool/app.py > /tmp/cuoti.log 2>&1 < /dev/null &)
```

!!! tip "端口"
    固定监听 **8091**，同时监听 IPv6 与 IPv4（`*:8091`），所以 `localhost`、`127.0.0.1`、局域网 IP 都能访问。

## 编译成可执行文件

### 方式一：GitHub Actions 自动编译（推荐）

推送到 `main` 分支，或在仓库 **Actions → build → Run workflow** 手动触发。运行结束后在该次运行的页面底部 **Artifacts** 下载：

| 产物 | 内容 |
|---|---|
| `HomeWorkCollection-windows` | `HomeWorkCollection.exe` + `fonts/` + `typst-packages/` |
| `HomeWorkCollection-macos-<arch>` | `HomeWorkCollection` 可执行文件 + `fonts/` + `typst-packages/`（zip）|

只上传 Artifacts，**不会创建 Release、不会发布任何东西**。

### 方式二：本地编译

- Windows：运行 `build.bat`（PyInstaller），产物在 `dist\HomeWorkCollection.exe`。
- macOS：

```bash
pip install pyinstaller
pyinstaller --onefile --name HomeWorkCollection \
  --add-data "tools/crop-tool/static:static" --collect-all typst \
  tools/crop-tool/app.py
```

### 运行 exe / 可执行文件

把 **可执行文件** 与 **`fonts/`**、**`typst-packages/`** 放在**同一目录**（编译产物里已经放好），双击运行，浏览器打开 `http://localhost:8091`。

!!! warning "exe 的数据目录"
    编译后数据不再放在项目源码目录，而是放在 **exe 所在的目录**：`pages/`、`items/`、`library.json` 等都在那里。换文件夹 = 换一套数据；升级时只替换可执行文件，数据不动。

!!! note "杀软误报"
    PyInstaller 单文件 exe 常被 Windows Defender/杀软误报。加信任即可；或用源码方式运行（`start.bat`）。

## 首次运行会创建什么

```text
pages/                 整页上传照片（P1/P2… 按上传时间编号）
items/{math,physics,...}/  裁剪出的错题图（所有图片都在这）
library.json           错题库数据
chapter_templates.json 大题模板（首次保存模板后生成）
code_prefix.json       编号前缀（首次修改后生成）
subjects.json          科目列表（首次增删科目后生成）
vaults.json            数据仓库注册表（首次建/切仓库后生成）
backups/               写库前自动备份（保留 30 份）
.trash/                删除的图片（可恢复）
.tmp/                  临时文件（排版用的 .typ / .pdf 等）
```

## 升级

1. 从 GitHub 拉取最新代码（或下载最新 exe）。
2. 重启服务（见上文「后台常驻运行」）。
3. 浏览器强制刷新一次（`Ctrl+Shift+R`）。

!!! tip "更省事的更新方式（仅源码方式）"
    本工具**没有内置自动更新**（按要求已移除）。源码方式用 `git pull` 即可；exe 方式重新下载 Artifacts 覆盖可执行文件。

## 启动排错

| 现象 | 处理 |
|---|---|
| `未找到 Python` | 装 Python 并勾选 Add to PATH，重开命令行 |
| 依赖安装失败 | 换国内源：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ...` |
| 端口被占用（`address already in use`） | 先杀掉旧进程：`pgrep -f "tools/crop-tool/app.py"` 拿到 PID 后 `kill <PID>` |
| 手机打不开 | 确认同一 WiFi、电脑防火墙放行 8091、用 `http://` 而不是 `https://` |
| 浏览器看到旧界面 | `Ctrl+Shift+R` 强刷（服务已对 html/js/css 设置禁用缓存）|
