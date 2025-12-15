# 账单神探 (Bill Detective)

**账单神探** 是一款基于 Web 的本地化微信支付账单分析工具。它专为账单梳理、资金流向分析和调查取证场景设计，支持导入微信支付导出的 PDF 账单文件，提供直观的仪表盘、多维度筛选和可视化的资金分析功能。

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![FastAPI](https://img.shields.io/badge/backend-FastAPI-green)
![Vue.js](https://img.shields.io/badge/frontend-Vue.js%203-green)

## ✨ 主要功能

*   **📄 PDF 账单解析**：支持直接上传微信支付导出的标准 PDF 账单文件，自动解析交易明细。
*   **👥 嫌疑人/对象管理**：
    *   支持创建多个分析对象（嫌疑人）。
    *   **密码保护**：查看分析详情需输入独立密码，保护数据隐私。
    *   支持删除对象及其所有关联数据。
*   **📊 仪表盘分析**：
    *   **资金概览**：总收入、总支出、结余统计。
    *   **趋势图**：按日/月展示收支变化趋势（折线图）。
    *   **交易对象 TOP 10**：饼图展示主要资金往来对象（支持隐藏空/匿名对象）。
*   **🔍 深度查询**：
    *   支持按时间范围、金额区间、收支类型、交易类型、交易方式等多维度筛选。
    *   支持全局关键字搜索。
    *   支持“特殊金额”快速筛选。
*   **📂 档案管理**：
    *   支持查看已上传的文件列表。
    *   支持单独删除某个文件及其导入的交易记录。
*   **💻 现代化 UI**：基于 Vue 3 + Tailwind CSS 构建，界面简洁美观，响应式设计。

## 🛠️ 技术栈

*   **后端**：Python, FastAPI, Uvicorn, SQLAlchemy, SQLite, PDFPlumber
*   **前端**：Vue.js 3 (Composition API), Tailwind CSS, ECharts, Flatpickr
*   **运行环境**：推荐使用 [uv](https://github.com/astral-sh/uv) 进行依赖管理和运行。

## 🚀 快速开始

### 1. 环境准备

确保您的系统已安装 Python 3.8 或更高版本。推荐安装 `uv` 以获得更快的包管理体验：

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. 安装依赖

在项目根目录下运行：

```bash
# 使用 uv (推荐)
uv pip install -r requirements.txt

# 或者使用标准 pip
pip install -r requirements.txt
```

### 3. 启动应用

```bash
# 使用 uv (推荐)
uv run main.py

# 或者直接运行
python main.py
```

启动成功后，终端将显示：
```
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### 4. 访问系统

打开浏览器访问：[http://localhost:8000](http://localhost:8000)

## 📖 使用指南

1.  **创建对象**：在首页点击“+ 新建嫌疑人”，输入姓名和查看密码。
2.  **上传账单**：进入对象卡片，点击“上传账单”，选择微信支付导出的 PDF 文件（支持多文件）。
3.  **保存档案**：解析完成后，点击“保存档案”，系统将自动去重入库并跳转至分析页面。
4.  **查看分析**：在首页点击对象的“查看分析”按钮，输入密码后进入仪表盘。
5.  **数据管理**：在首页点击“管理”按钮，可删除特定文件记录或彻底删除该对象。

## 📁 目录结构

```
.
├── main.py              # 后端主程序 (FastAPI)
├── models.py            # 数据库模型定义
├── database.py          # 数据库连接配置
├── parser.py            # PDF 解析逻辑核心
├── requirements.txt     # 项目依赖
├── bill_app.db          # SQLite 数据库文件 (自动生成)
└── static/              # 前端静态资源
    ├── index.html       # 单页应用入口
    ├── favicon.ico      # 网站图标
    └── img/             # 图片资源
```

## ⚠️ 注意事项

*   本工具仅供个人记账分析或授权调查使用，请勿用于非法用途。
*   目前主要支持微信支付导出的标准 PDF 账单格式，支付宝支持正在计划中。

## 📝 待办计划

- [ ] 支付宝账单 PDF 解析支持
- [ ] 导出分析报告 (Excel/PDF)
- [ ] 更多可视化图表类型
