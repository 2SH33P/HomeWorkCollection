# 常见问题 FAQ

## URL 直达

| 地址 | 页面 |
|---|---|
| `/editor` | 整页框选 |
| `/library` | 错题库（科目列表）|
| `/library/数学` | 指定科目 |
| `/item/MA0003` | 直接打开该题的全屏编辑窗口 |
| `/paper` | 组卷打印 |
| `/settings` | 设置（AI 配置 + 科目 + 改名 + 仓库 + 日志）|

## 目录结构

```text
├── tools/crop-tool/
│   ├── app.py                # FastAPI 后端（全部接口 + Typst 排版）
│   └── static/index.html     # 单页前端（原生 JS）
├── typst-packages/           # 本地 Typst 包（mitex: LaTeX → Typst）
├── fonts/                    # 内置字体（宋体/黑体/楷体/Times）——换字体就换这里的同名文件
├── docs/                     # 本教程（MkDocs）
├── mkdocs.yml                # 教程站点配置
├── .github/workflows/
│   ├── build.yml             # 编译 Windows exe / macOS 产物（仅 Artifacts）
│   └── docs.yml              # 构建并发布本教程到 GitHub Pages
├── start.bat / start.sh      # 一键启动
├── build.bat                 # 本地 PyInstaller 打包
└── README-Windows.txt        # Windows 详细说明
```

## 识别相关

??? question "点「AI 识别」没反应 / 内容为空"
    1. 看**设置 → 运行日志**里的失败原因。
    2. 常见是**限流**（免费模型）、网络超时、或模型不支持图片。确认模型是**视觉模型**（如 `glm-4v-flash`），不要用 `deepseek-chat` 这类纯文本模型。
    3. 关掉「额外一轮 AI 校对」、把清晰度降到 1200/1600，再对单题重试。

??? question "一次框了很多题，只有一部分识别成功"
    保存后是 **3 个并发**后台识别，免费额度容易 429。做法：一次少框几题（5～8 道），失败的题在错题库里单独点「AI 识别」重试。

??? question "公式识别错了 / 化学式乱了"
    付费模型更稳；免费模型在复杂公式上会出错。重识别或手动改正即可。化学式支持 `\ce{}` 写法（内部会转成 mitex 能渲染的形式）。

??? question "识别结果里出现了「图N未裁好」"
    那是**排版时**的红色提示，表示正文引用了 `[图N]` 但还没有对应图片。去预览窗「图片附件」按提示「上传到 [图N]」或「框选到 [图N]」。

??? question "重新识别后图和文字对不上了"
    AI 重识别会重排 `[图N]` 的位置，图块本身保留旧编号。在图片附件的下拉框里把图**改绑**到新编号即可（会自动改写正文引用）。

## 运行相关

??? question "手机连不上"
    - 手机与电脑同一 WiFi；
    - 用 `http://电脑局域网IP:8091`（不是 `https`、不是 `localhost`）；
    - 电脑防火墙放行 8091（Windows 首次会弹「是否允许访问网络」，选允许）。

??? question "端口 8091 被占用 / 启动失败"
    ```bash
    pgrep -f "tools/crop-tool/app.py"   # 看旧进程 PID
    kill <PID>
    ```
    然后重新启动。不要用 `pkill -f "..."`，容易把执行命令的 shell 自己杀掉。

??? question "改了代码怎么生效？要重启服务吗？"
    要。前端刷新页面即可，**后端必须重启进程**：
    ```bash
    (setsid nohup .venv/bin/python tools/crop-tool/app.py > /tmp/cuoti.log 2>&1 < /dev/null &)
    ```

??? question "浏览器还是旧界面"
    `Ctrl+Shift+R` 强刷。服务对 html/js/css 已禁用缓存，但旧标签页可能仍在跑旧 JS。

??? question "exe 被杀毒软件报毒 / 打不开"
    PyInstaller 单文件 exe 常见误报，加信任即可。也可改用源码方式（`start.bat`）。注意 exe 必须和 `fonts/`、`typst-packages/` 在同一目录，否则 PDF（Typst）会失败。

## 数据相关

??? question "导入数据包会不会覆盖我现有的题？"
    不会。导入的每道题都会**重新编号 + 换新 id**，图片同名自动改名保留，现有数据不动。

??? question "删掉的题还能找回吗？"
    图片在 `.trash/`（带时间戳），`library.json` 每次写库前备份在 `backups/`（30 份），批量操作还可用「撤销上次修改」整体回退。

??? question "换电脑怎么迁移？"
    最稳：停服务后直接复制整个数据目录。或设置页「导出数据 ZIP」后在新机器导入（会重新编号）。

??? question "科目 / 大题 / 关键字改了名，以前的题会怎样？"
    会被一起改掉（一次全局覆盖）。详见 [一键全局改名](data-vault.md)。

??? question "能不能同时开多个题库、互不干扰？"
    可以，用**数据仓库**：新建/切换仓库，还能把别的仓库合并进来（源仓库保留）。见 [数据与仓库](data-vault.md)。

??? question "自动组卷说题目不足"
    看预检提示卡在哪个大题/星级：加题、放宽星级上下限、减少题量或份数；「关键字不重复」也会限制可选数量。

## 安全与隐私

??? question "这个服务安全吗？"
    本机自用是安全的；但它**没有登录鉴权**，不要暴露到公网/内网穿透。要远程用请加反代 + Basic Auth，或用 VPN/Tailscale 之类。

??? question "我的 API Key 会泄露吗？"
    不会推到 GitHub（`.ai_config.json` 已 gitignore），导出 ZIP 不包含它，界面只显示掩码。但文件本身是**明文**，注意目录权限；也可以改用环境变量 `AI_API_KEY`（不落盘）。

??? question "题目图片会被上传吗？"
    只有 AI 识别时，**裁剪后的单题图片**会发给你配置的服务商；其余数据不出本机。题含敏感内容请谨慎。

## 反馈

- 问题与建议：<https://github.com/2SH33P/HomeWorkCollection/issues>
- 源码与发布：<https://github.com/2SH33P/HomeWorkCollection>
