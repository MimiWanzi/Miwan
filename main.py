"""exe 打包的入口脚本（PyInstaller 从这里开始分析依赖）。

不能直接把 ``gateway/__main__.py`` 喂给 PyInstaller：它满手相对导入，
脱离包上下文跑不起来。

双击 exe 的两个特殊行为（只在冻结态生效，``python -m gateway`` 不受影响）：

  - **不带参数 = ``serve --open``**。双击是普通用户的唯一动作，得直接起服务；
    命令行玩家仍然可以 ``RemoteAgent.exe tasks`` 这样用。
  - **出错不闪退**。控制台窗口在报错后停一下（回车再关），否则双击场景下
    报错一闪而过，"起不来"就成玄学了。
"""

import sys

from gateway.__main__ import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if getattr(sys, "frozen", False) and not argv:
        argv = ["serve", "--open"]

    code = 0
    try:
        code = main(argv)
    except SystemExit as exc:
        # argparse 的用法错误（code=2，用法已打印）和 load_config 的
        # SystemExit("中文原因")——后者的消息不会被打印，得自己来。
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            code = 1
        else:
            code = exc.code or 0
    except KeyboardInterrupt:
        code = 130
    except Exception:
        import traceback

        traceback.print_exc()
        code = 1

    if getattr(sys, "frozen", False) and code not in (0, 130):
        try:
            input("\n出错了。看完上面的信息后按回车关闭窗口…")
        except EOFError:
            pass
    raise SystemExit(code)
