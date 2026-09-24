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

!!! note "没有内置自动更新"
    本工具**没有自动更新**（按要求已移除自动更新接口）。升级 = 用新版本替换旧的可执行文件 / 代码，**数据不动**。
    新版本首次启动会自动补齐新增的数据字段（写库前先备份到 `backups/`）。

=== "exe 版（Windows）"

    数据就在 exe 所在目录，所以升级**只要换 exe 文件**：

    1. **先备份**：关掉程序，把 exe 所在整个文件夹复制一份
       （至少复制 `library.json` + `items\` + `subjects.json` + `code_prefix.json`）。
    2. **关掉正在运行的程序**：关闭那个命令行窗口；若在后台跑，任务管理器结束
       `HomeWorkCollection.exe`，或命令行 `taskkill /IM HomeWorkCollection.exe /F`。
    3. 到仓库 **Actions → 最新一次成功的 build → 页面底部 Artifacts** 下载
       `HomeWorkCollection-windows`，解压到临时目录。
    4. 把解压出的 **`HomeWorkCollection.exe`** 复制到**原来那个文件夹，覆盖旧 exe**
       （顺便覆盖 `fonts\`、`typst-packages\` 也可以，它们不常变，覆盖无副作用）。
    5. **不要动** `library.json`、`items\`、`pages\`、`vaults\`、`.ai_config.json` ——
       它们是数据，新产物里也没有这些文件。
    6. 双击新 exe → 控制台打印访问地址 → 浏览器打开 `http://localhost:8091`，
       按 `Ctrl+Shift+R` 强刷一次。

    !!! warning "两个坑"
        - **别把新产物解压到一个空文件夹就用**——那样会变成一套全新数据（题库是空的）。要放回原来那个目录。
        - Windows SmartScreen / 杀软可能拦新 exe：选「更多信息 → 仍要运行」，或把目录加入排除。

    更干净的做法（换机器 / 大版本时）：新建一个空文件夹 → 解压新产物（exe + `fonts/` + `typst-packages/`）
    → 再把旧目录里的数据（`library.json`、`items\`、`pages\`、`subjects.json`、`code_prefix.json`、
    `chapter_templates.json`、`vaults.json`、`vaults\`、`.ai_config.json`）复制过去。

=== "macOS"

    同样只换可执行文件：解压 `HomeWorkCollection-macos-<arch>.zip`，用新的 `HomeWorkCollection`
    覆盖旧的（保持 `fonts/`、`typst-packages/` 在同一目录）；若执行权限丢了：

    ```bash
    chmod +x HomeWorkCollection
    ```

=== "源码方式"

    ```bash
    cd HomeWorkCollection
    git pull
    ```

    然后重启服务（见「后台常驻运行」），浏览器 `Ctrl+Shift+R` 强刷。

## 启动排错

| 现象 | 处理 |
|---|---|
| `未找到 Python` | 装 Python 并勾选 Add to PATH，重开命令行 |
| 依赖安装失败 | 换国内源：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ...` |
| 端口被占用（`address already in use`） | 先杀掉旧进程：`pgrep -f "tools/crop-tool/app.py"` 拿到 PID 后 `kill <PID>` |
| 手机打不开 | 确认同一 WiFi、电脑防火墙放行 8091、用 `http://` 而不是 `https://` |
| 浏览器看到旧界面 | `Ctrl+Shift+R` 强刷（服务已对 html/js/css 设置禁用缓存）|
