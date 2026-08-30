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
| PDF 排版 | Typst（0.15，PyPI 包）+ 本地字体（SimSun/SimHei/KaiTi/Times New Roman） |
| AI 识别 | 智谱 GLM-4V-Flash（OpenAI 兼容接口，免费） |
| 前端 | 原生 HTML/CSS/JavaScript · Canvas 2D · Pointer Events（鼠标+触摸） |
| 存储 | JSON 文件（无数据库）：错题库.json、页/、错题/ |

无 Node、无数据库、无 Docker，单 Python 进程即可运行。

### 内置字体

项目自带排版字体（已随仓库打包，无需上传）：`字体/` 目录中的宋体（SimSun）、黑体（SimHei）、楷体（KaiTi）与 Times New Roman，供 Typst 生成 PDF 使用。

## 快速开始

### Windows（推荐）

1. 安装 Python 3.10+（勾选 *Add Python to PATH*）
2. 双击 `启动.bat`，首次运行自动创建虚拟环境并安装依赖（约 2 分钟）
3. 浏览器自动打开 `http://localhost:8091`
4. 手机与电脑同一 WiFi 时，手机访问 `http://电脑IP:8091` 即可上传照片

### 手动运行（任意平台）

```bash
pip install fastapi "uvicorn[standard]" python-multipart pillow opencv-python-headless numpy typst
python 工具/错题裁剪工具/app.py
# 访问 http://localhost:8091
```

### 配置 AI（可选但强烈建议）

> **维护提示**：AI 识别对接（智谱视觉大模型 API）正在维修/偶有不稳定，如遇识别失败或超时，请稍后重试或直接手动框选/裁图；不影响其他功能。

创建 `.ai_config.json`（与 README 同目录）：

```json
{
  "key": "你的智谱 API Key",
  "base_url": "https://open.bigmodel.cn/api/paas/v4",
  "model": "glm-4v-flash"
}
```

- 申请地址：https://open.bigmodel.cn/（手机号注册，glm-4v-flash 免费）
- 不配置也能用，但 AI 识别/自动拆题功能不可用

## 使用流程

```
1. ① 整页框选：上传整页照片 → 拖拽框选每道题 → （可选）点「裁图」框选题内图形 → 保存
2. 保存后自动 AI 识别：题干 + 选项分开、公式 LaTeX、图形自动裁剪；没裁好的题红框标记
3. ② 错题库：科目 → 大题 → 日期 三级浏览；点图片预览/编辑识别内容，可手动裁图
4. ③ 组卷：勾选题目（右上角圆圈按顺序编号）→ 调整顺序 → 设置大题归属 → 生成 PDF
```

## 目录结构

```
├── 工具/错题裁剪工具/
│   ├── app.py                # FastAPI 后端（全部接口）
│   └── static/index.html     # 单页前端
├── 启动.bat / 启动.sh         # 一键启动
├── 打包.bat                   # PyInstaller 打包
└── Windows使用说明.txt        # Windows 详细说明
```

运行时数据（页面照片、错题、题库）保存在项目目录下的 `页/`、`错题/`、`错题库.json`，直接复制目录即可备份。排版字体内置在 `字体/` 目录。

## 隐私说明

- AI 识别会将题目图片发送至智谱 API（GLM-4V-Flash）进行识别，如题含敏感内容请谨慎
- API Key 仅保存在本地 `.ai_config.json`（已加入 .gitignore，不会提交）
- 所有数据仅存于本机，不上传任何其他服务
