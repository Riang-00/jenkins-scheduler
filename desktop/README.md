# Desktop

Tauri 2 + React + TypeScript + Vite。开发时在本目录运行 `bun install --frozen-lockfile`，再运行 `bun run tauri dev`，并确保 PATH 中有 Python 3.13。`bun run dev` 仅启动网页，不会连接本机预约引擎。

正式 EXE 由仓库根目录的打包脚本与 Python 引擎、内置运行时一起安装；不能单独复制 EXE。原生进程通过 JSON 标准输入调用 Python，不把 Token 放在命令行。WebView 和定时任务的数据位置由安装标记固定，避免不同启动方式读到不同配置。

前端检查：`bun run lint`、`bun run typecheck`、`bun run build`。
