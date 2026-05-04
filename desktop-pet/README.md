# Star Office Tauri Desktop Shell

这个目录用于把 `Star-Office-UI` 包成桌面应用（透明窗口），并在启动时自动拉起后端进程。

## 开发运行

先在仓库根目录准备 Node 依赖：

```bash
cd /Users/wangzhaohan/Documents/GitHub/Star-Office-UI
npm ci
```

再启动 Tauri：

```bash
cd /Users/wangzhaohan/Documents/GitHub/Star-Office-UI/desktop-pet
npm install
npm run dev
```

## 自动拉起后端逻辑

- 如果已有生产构建：`npm run start`
- 否则回退到开发服务：`npm run dev`

窗口默认会跳转到：

- `http://127.0.0.1:19000/?desktop=1`

## 可选环境变量

- `STAR_PROJECT_ROOT`：项目根目录（默认会自动探测）
- `STAR_BACKEND_NPM`：自定义 npm 可执行路径
- `STAR_BACKEND_URL`：自定义桌面窗口打开的 URL
