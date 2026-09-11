# 错题收集工具（HomeWorkCollection）

本地/自托管的错题收集 WebUI：**拍整页试卷 → 框选题目 → AI 识别文字与公式 → 错题管理 → Typst 排版生成试卷 PDF**。手机、电脑浏览器均可使用。

## 功能特性

- **整页上传**：手机拍照直接上传整页试卷（jpg/png/webp），自动生成缩略图
- **框选题目**：网页上拖拽框选（支持四角/边缘微调、跨页续块合并），多页框选统一列表、统一科目
- **AI 识别**：基于视觉大模型（智谱 GLM-4V-Flash，免费）一次性识别题干 + 选项（分开填充）、数学/化学公式转 LaTeX、自动忽略手写字迹与题号、答案
- **题内图形**：AI 自动定位图形位置并裁剪，也可手动在原图上框选裁图，未裁好的题目红色标记提醒
- **错题管理**：相册式错题库（科目 → 大题 → 日期三级分类）、点击预览编辑识别内容、大题模板按科目配置、批量删除、去红笔/橡皮擦清理
- **组卷打印**：按勾选顺序自动编号，按"大题归属"分组（黑体大题标题），**Typst 排版**输出 16 开高考卷风格 PDF
  - 16 开纸张，页边距上下 2cm 左右 2.2cm，正文宋体五号 + 1.5 倍行距
  - 英文 Times New Roman / 中文宋体 / 大题标题黑体 / 续块楷体
  - 选项按长度自适应一行四列 / 两列 / 一列，图形小块自动裁剪插入文字流

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python · FastAPI · Uvicorn |
| 图像处理 | OpenCV · NumPy · Pillow（裁剪/坐标换算/去红笔/收边） |
| PDF 排版 | Typst（0.15，PyPI 包）+ **mitex**（LaTeX 公式支持）+ 本地字体 |
| AI 识别 | 智谱 GLM-4V-Flash / DeepSeek `deepseek-flash`（均支持图片，OpenAI 兼容接口，可在设置页切换） |
| 前端 | 原生 HTML/CSS/JavaScript · Canvas 2D · Pointer Events（鼠标+触摸） |
| 存储 | JSON 文件（无数据库）：错题库.json、页/、错题/ |

无 Node、无数据库、无 Docker，单 Python 进程即可运行。

### 内置字体

项目自带排版字体（已随仓库打包，无需上传）：`fonts/` 目录中的宋体（SimSun）、黑体（SimHei）、楷体（KaiTi）与 Times New Roman，供 Typst 生成 PDF 使用。

## 快速开始

### Windows（推荐）

1. 安装 Python 3.10+（勾选 *Add Python to PATH*）
2. 双击 `启动.bat`，首次运行自动创建虚拟环境并安装依赖（约 2 分钟）
3. 浏览器自动打开 `http://localhost:8091`
4. 手机与电脑同一 WiFi 时，手机访问 `http://电脑IP:8091` 即可上传照片

### 手动运行（任意平台）

```bash
pip install fastapi "uvicorn[standard]" python-multipart pillow opencv-python-headless numpy typst
python tools/crop-tool/app.py
# 访问 http://localhost:8091
```

### 配置 AI（可选但强烈建议）

> **维护提示**：AI 识别对接可能偶有不稳定，如遇识别失败或超时，请稍后重试或直接手动框选/裁图；不影响其他功能。

启动后打开 **「④ 设置」** 页面填写，或在项目目录创建 `.ai_config.json`：

```json
{
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-flash",
  "key": "你的 API Key"
}
```

设置页提供预设，一键填入地址与模型名：

| 服务 | 地址 | 模型 | 说明 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash` | 支持图片识别；已自动关闭思考（reasoning_effort=none）提速 |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4v-flash` | 免费，速度快 |
| 通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-vl-plus` | 备用 |

设置页支持「测试连接」；已配置时直接显示模型名与 Key 掩码，点「修改配置」才能查看/编辑。

**重要**：识别图片必须使用**支持图片输入**的模型；纯文本模型（如 `deepseek-chat`）会静默忽略图片，输出残缺内容。

## 使用流程

```
1. ① 整页框选：上传整页照片 → 拖拽框选每道题 → （可选）点「裁图」框选题内图形 → 保存
2. 保存后自动 AI 识别：题干 + 选项分开、公式 LaTeX、图形自动裁剪；没裁好的题红框标记
3. ② 错题库：科目 → 大题 → 日期 三级浏览；点图片预览/编辑识别内容，可手动裁图
4. ③ 组卷：勾选题目（右上角圆圈按顺序编号）→ 调整顺序 → 设置大题归属 → 生成 PDF
```

## 目录结构

```
├── tools/crop-tool/
│   ├── app.py                # FastAPI 后端（全部接口）
│   └── static/index.html     # 单页前端
├── typst-packages/           # 本地 Typst 包（mitex: LaTeX 公式 → Typst）
├── fonts/                    # 内置排版字体（宋体/黑体/楷体/Times）
├── start.bat / start.sh      # 一键启动
├── build.bat                 # PyInstaller 打包
└── README-Windows.txt        # Windows 详细说明
```

运行时数据（英文目录名，界面上显示中文）：

```
pages/              整页上传照片
items/math/         裁剪出的错题图（按科目: math / physics / chemistry / ...）
library.json        错题库数据
chapter_templates.json  大题模板
code_prefix.json    编号前缀（MA/PH/CH/...）
.tmp/               临时文件
```

直接复制这些目录即可备份。排版字体内置在 `fonts/` 目录。

## URL 直达

| 地址 | 页面 |
|---|---|
| `/editor` | 整页框选 |
| `/library` | 错题库（科目列表） |
| `/library/数学` | 指定科目 |
| `/item/MA0003` | 直接打开该题的全屏编辑窗口 |
| `/paper` | 组卷 |
| `/settings` | 设置（AI 服务配置） |

## 隐私说明

- AI 识别会将题目图片发送至智谱 API（GLM-4V-Flash）进行识别，如题含敏感内容请谨慎
- API Key 仅保存在本地 `.ai_config.json`（已加入 .gitignore，不会提交）
- 所有数据仅存于本机，不上传任何其他服务
