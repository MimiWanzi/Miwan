@echo off
REM 打包单文件 exe：双击就能跑的 Keeper.exe（产物在 dist\ 目录）。
REM 前置：pip install pyinstaller；qrcode 已是运行依赖。
REM 打包后把 dist\Keeper.exe 拷到任意目录双击即可。
REM 配置和数据统一放在 %LOCALAPPDATA%\Keeper\（与 python 启动共用同一份），
REM 换机器/给别人用 = 把 exe 拷过去，首次运行自动生成全新配置。
cd /d %~dp0
python -m PyInstaller --noconfirm --clean --onefile --console --name Keeper ^
  --add-data "web;web" ^
  --hidden-import qrcode ^
  main.py
if errorlevel 1 (
  echo.
  echo 打包失败，看上面的报错。
  exit /b 1
)
echo.
echo 完成：dist\Keeper.exe  （双击 = 起服务；命令行参数和 python -m gateway 一致）
echo 配置和数据目录：%LOCALAPPDATA%\Keeper\ （config.toml 与 data\ 都在这）
