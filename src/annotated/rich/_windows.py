import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 【模块定位】本模块是 Rich 的“Windows 能力探测”小工具，位于渲染管线的**启动协商阶段**：
#   Console 在决定“用 ANSI/VT 转义序列输出，还是退化到老的 Win32 控制台 API”之前，
#   先调用本模块问一句“这台机器的控制台支持 VT 吗？支持真彩色吗？”。
#   它不画任何东西，只做两件事：加载 Windows DLL 并查询控制台模式位。
#   依赖：标准库 ctypes（通过 rich._win32_console 间接绑定 kernel32）、sys、dataclasses。
#   被依赖：rich.console 的 get_windows_console_features()（有全局缓存），
#   进而影响 Console.legacy_windows 与是否走 rich._windows_renderer 的绘制路径。
#   在非 Windows 平台上导入本模块不会失败——对外的函数会返回“什么都不支持”的默认值，
#   这样调用方（console.py）无需自己做平台分支。
# 【关键数据结构】WindowsConsoleFeatures：一个只有两个布尔的 dataclass
#   （vt、truecolor），它是本模块唯一的输出。
# 【设计要点】
#   - 用 try/except/else 三段式导入而不是函数内延迟导入：导入期就把 DLL 绑定结果定下来，
#     同时把“能否加载”这一事实编码进“模块里存在哪一个 get_windows_console_features 定义”。
#   - 失败路径不抛异常，而是提供一个返回全 False 的降级实现：探测失败必须等价于
#     “不支持高级特性”，因为错误的乐观假设会让终端输出乱码。
#   - truecolor 只在 vt 为真时才判断：没有 VT 就根本用不上 24 位色，Windows 的
#     legacy 控制台最多 16 色，所以这里刻意短路。
#   - 版本判断用 sys.getwindowsversion()（进程实际运行的内核版本）而不是 sys.version，
#     阈值取 Windows 10 build 15063（1703，首个把 VT 支持普惠到默认控制台的版本）。
# 【阅读建议】先看 WindowsConsoleFeatures 了解输出契约，再看 try/except/else 结构判断
#   “当前在哪个平台上”，最后读真正的探测实现（else 分支里的那个函数）。
#   注意：异常列表 (AttributeError, ImportError, ValueError) 分别对应
#   “非 Windows 平台”“DLL 缺失”“ctypes 在部分平台抛 ValueError”，读的时候可以对照。
# ---------------------------------------------------------------------------


# ==== 【输出契约：能力标志】 ====
# 【职责】承载一次能力探测的结果，供 Console 决定渲染策略。
# 【关键属性】vt：控制台是否启用了 ENABLE_VIRTUAL_TERMINAL_PROCESSING，
#   即能否把 ANSI 转义序列当命令解释（True 时 Rich 用普通 ANSI 输出路径）；
#   truecolor：能否显示 24 位真彩色（取决于 Windows 版本，见下）。
# 【不变量】两个字段都必须是严格 bool，不能是 0/1 或 None——调用方会直接做真值判断，
#   也可能被 repr 记录进日志，因此默认值显式写成 False。
# 【协作】由 get_windows_console_features() 构造，被 rich.console.Console 读取。
# 【兼容】之所以用 dataclass，是为了让 repr/== 免费获得，方便诊断输出（见文件末尾的演示）。
@dataclass
class WindowsConsoleFeatures:
    """Windows features available."""

    vt: bool = False
    """The console supports VT codes."""
    truecolor: bool = False
    """The console supports truecolor."""


# ==== 【导入期探测：能否加载 Win32 绑定】 ====
# 【要点】整个 try/except/else 在“模块首次被导入时”执行一次，因此下面的两个
# get_windows_console_features 定义中只会有一个真正存在，另一个从未被创建。
# 【为什么】用 LibraryLoader(ctypes.WinDLL) 而不是直接 ctypes.windll：前者可以显式
# 指定 stdcall 调用约定，也能在测试里被替换成假对象；真正的绑定发生在 _win32_console。
try:
    import ctypes
    from ctypes import LibraryLoader

    # 【要点】sys.platform == "win32" 才存在 WinDLL；其它平台这里必定失败，
    # 于是主动 raise ImportError 走进 except，得到“全 False”的降级实现。
    if sys.platform == "win32":
        windll = LibraryLoader(ctypes.WinDLL)
    else:
        # 【易错】把 windll 置为 None 后再 raise，是为了让“已定义但不可用”和
        # “根本没有这个模块”两种状态的错误信息更清晰。
        windll = None
        raise ImportError("Not windows")

    # 【要点】从 _win32_console 取三样东西：模式位常量、两个 API 包装函数、以及
    # 表示“句柄不是控制台”的异常类型。
    from rich._win32_console import (
        ENABLE_VIRTUAL_TERMINAL_PROCESSING,
        GetConsoleMode,
        GetStdHandle,
        LegacyWindowsError,
    )

except (AttributeError, ImportError, ValueError):
    # 【兼容】AttributeError/ValueError 覆盖 ctypes 在某些平台/某些 Python 版本上
    # 抛出的异常；只要命中任意一种，就退化为“不支持任何高级特性”。
    # Fallback if we can't load the Windows DLL
    # 【职责】降级实现：返回未修改的默认特征值（全 False）。
    # 【功能】在非 Windows 或无法加载 DLL 时，声明“控制台不支持 VT 与真彩色”。
    # 【返回】一个字段全为默认值（False）的 WindowsConsoleFeatures。
    # 【为什么】宁可低估也不能高估：误判为支持 VT 会向不认转义序列的控制台写出
    # 可见的乱码（形如 ←[1m），而误判为不支持只是让程序走更保守但正确的老路径。
    # 【注意】本函数没有 docstring——这是上游原样保留的写法，读代码时不要以为漏了。
    def get_windows_console_features() -> WindowsConsoleFeatures:
        features = WindowsConsoleFeatures()
        return features

else:

    # 【职责】真实探测实现（仅在 Windows 且成功绑定 DLL 时才被定义）。
    # 【功能】查询标准输出句柄的控制台模式与 Windows 版本，得出 vt / truecolor 两个结论。
    # 【算法】
    #   1) GetStdHandle() 取标准输出的伪句柄（-11）；
    #   2) 尝试 GetConsoleMode(handle)：
    #      - 成功 → success=True，console_mode 是模式位掩码；
    #      - 抛 LegacyWindowsError → 说明该句柄不是控制台（例如输出被重定向到文件/管道），
    #        置 success=False、console_mode=0；
    #   3) vt = success 且掩码里含 ENABLE_VIRTUAL_TERMINAL_PROCESSING（值 4）；
    #   4) 仅当 vt 为真时再看系统版本决定 truecolor：major > 10，或 major == 10 且
    #      build >= 15063（Windows 10 1703，VT 在该版本成为控制台默认能力）。
    # 【返回】WindowsConsoleFeatures(vt=..., truecolor=...)。
    # 【注意】这里只“读”控制台模式，从不调用 SetConsoleMode 去打开 VT，因此结论反映的是
    #   终端当时的真实状态：用户或其它程序已经开启 VT 才可能得到 True。
    # 【兼容】getwindowsversion() 只在 Windows 上存在，所以对本函数的调用必须发生在
    #   这个 else 分支里（非 Windows 上根本不会定义这个版本）。
    def get_windows_console_features() -> WindowsConsoleFeatures:
        """Get windows console features.

        Returns:
            WindowsConsoleFeatures: An instance of WindowsConsoleFeatures.
        """
        # 【要点】STDOUT（-11）是伪句柄，不是真实的 HANDLE 值；GetStdHandle 内部用它查表。
        handle = GetStdHandle()
        try:
            console_mode = GetConsoleMode(handle)
            success = True
        except LegacyWindowsError:
            # 【为什么】输出被重定向时句柄不是控制台，GetConsoleMode 会失败；
            # 这不是错误而是一种常见状态（例如 rich 的输出被管道接走），
            # 所以用异常表达“拿不到模式”而不是让程序崩掉。
            console_mode = 0
            success = False
        # 【易错】必须先判断 success 再与掩码按位与：console_mode 在失败时被置 0，
        # 但显式写 success and ... 才能表达“句柄确实是控制台”这一语义。
        vt = bool(success and console_mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        # 【为什么】默认 False：后面的版本判断只在 vt 成立时才做，因为未启用 VT 时
        # 即使系统支持 24 位色，Rich 也只能通过 legacy 路径用 16 色属性写屏。
        truecolor = False
        if vt:
            # 【要点】判断的是“内核版本”而非 Python 版本：VT/真彩色取决于 Windows 本身。
            win_version = sys.getwindowsversion()
            # 【要点】阈值 15063 = Windows 10 1703（Creators Update）的 build 号，
            # 该版本起 conhost 默认支持 VT 与 24 位色。
            truecolor = win_version.major > 10 or (
                win_version.major == 10 and win_version.build >= 15063
            )
        features = WindowsConsoleFeatures(vt=vt, truecolor=truecolor)
        return features


# ==== 【模块自测入口】 ====
# 【功能】直接 `python -m rich._windows` 时打印平台名和探测结果，便于人工确认
#   “这台机器到底走 ANSI 还是 legacy 渲染路径”。
# 【注意】只是诊断用的演示代码，不属于对外 API，也不会被正常导入执行。
# 【易错】`from rich import print` 放在函数调用之后导入，会让模块级导入顺序看起来奇怪，
#   但这样能在最外层演示中验证 get_windows_console_features 已可调用。
if __name__ == "__main__":
    import platform

    features = get_windows_console_features()
    from rich import print

    print(f'platform="{platform.system()}"')
    print(repr(features))
