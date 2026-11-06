# Jenkins Scheduler

Windows 上的 Jenkins 预约部署工具。每个人独立安装，在自己的电脑上配置 Jenkins 地址、账号和 API Token。

## 下载与安装

从 [Releases](https://github.com/Riang-00/jenkins-scheduler/releases/latest) 下载 `JenkinsScheduler-2.1.0-windows-x64-setup.exe`，双击安装，然后打开桌面的 **Jenkins Scheduler**。

- 支持 Windows 10/11 x64；安装包自带 Python，无需另行安装开发环境。
- 界面使用 Microsoft Edge WebView2。缺少该组件时，安装器需要联网下载并安装微软官方运行时。
- 安装器没有代码签名证书，Windows 可能显示“未知发布者”。请核对下载来源与发布页的 SHA256 校验值。
- 程序安装在 `%LOCALAPPDATA%\Programs\JenkinsScheduler`；数据保存在 `%LOCALAPPDATA%\JenkinsScheduler\data`。按 Windows 用户隔离，卸载保留数据。

## 开始使用

1. 首次打开点击“初始化配置”，在设置中添加一个或多个 Jenkins 地址。
2. 为每个 Jenkins 设置对应的用户名与 API Token，并验证连接。通常在 Jenkins 的个人设置 → Security/安全 → API Token 中生成；实际入口由 Jenkins 版本和管理员策略决定。
3. 读取项目列表，通过 Jenkins 分类、文件夹或关键字找到项目，在预约时直接选择。
4. 填写构建参数、预约时间，核对后确认。可以在预约记录中查看状态、取消待执行任务及打开 Jenkins 构建日志。

预约时间使用北京时间（UTC+8）。到了预约时间，Windows 任务计划程序会触发一次部署。电脑必须保持开机、不休眠、连接 Jenkins 网络，且预约的 Windows 用户保持登录；界面可以关闭。超过预约时间 5 分钟不补发。部分构建失败或结果不确定时停止后续项目，不自动重试触发构建。

## 功能与边界

- 多 Jenkins、多账号；支持 Jenkins Views 分类、Folder 和项目搜索。
- 普通构建和参数化构建，预约保存项目及参数快照。
- API Token 使用当前 Windows 用户的 DPAPI 加密，不能直接复制给其他用户使用。每个人应填写自己的账号；其他人无法看到本机预约。
- 可选 ZIP 发布包的 SHA256 校验。只支持程序明确识别的参数类型，不会猜测插件参数。
- 升级和卸载前先关闭界面，并取消待执行预约或等待其结束。数据不会随程序文件回滚，也不会重新执行已消费的预约。
- 本工具不需要专用服务器；多人独立安装也意味着各自可能对同一个项目预约，请自行协调部署窗口。

## 源码开发

需要 Python 3.13、Bun、Rust stable MSVC，以及 Visual Studio C++ Build Tools / Windows SDK。

```powershell
python -m unittest discover -s tests -v
cd desktop
bun install --frozen-lockfile
bun run lint
bun run typecheck
bun run tauri build --no-bundle
cd ..
python tools/build_release.py --exe desktop/src-tauri/target/release/tauri-native.exe
```

安装包与校验文件生成在 `artifacts/`。构建脚本从官方站点下载固定版本的 Python 和 NSIS，并校验 SHA256；WebView2 引导程序验证微软数字签名。只使用临时目录和本机模拟 Jenkins 做测试，不要用真实部署验证安装包。

详见 [发布说明](docs/RELEASE.md)、[桌面开发说明](desktop/README.md) 和 [第三方许可](docs/THIRD_PARTY_NOTICES.txt)。
