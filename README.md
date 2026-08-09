# MC 自动翻译器

把 Minecraft 整合包 / mod / 地图里的英文自动翻译成中文的 Windows 桌面应用。目标语言可参数化。

## 技术栈

- 后端：Python 3.14 + FastAPI
- 前端：Vue3
- 依赖管理：`backend/requirements.txt`

## 目录结构

```
backend/
  app/           # 后端应用代码
  tests/         # 后端测试
```

## 开发运行

开发期散装多文件运行，不打包。

```bash
cd backend
python -m pytest tests/ -v   # 跑测试
```
