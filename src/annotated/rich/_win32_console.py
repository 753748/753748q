"""Light wrapper around the Win32 Console API - this module should only be imported on Windows

The API that this module wraps is documented at https://docs.microsoft.com/en-us/windows/console/console-functions
"""

# ---------------------------------------------------------------------------
# 【模块定位】本模块是 Rich 在 Windows 上的“终端驱动”：用 ctypes 把 kernel32 里的一组
#   控制台 API 包装成 Python 可调用的函数，并用 LegacyWindowsTerm 把它们组织成一个
#   “只能按老办法画屏的终端对象”。它处在渲染管线的最底层。
#   依赖：ctypes/wintypes（Win32 绑定的基础设施）、rich.color.ColorSystem（颜色降级目标）、
#   rich.style.Style（读取 bold/dim/reverse/color/bgcolor 等渲染意图）。
#   被依赖：rich._windows_renderer（唯一的绘制调用方）、rich._windows（读控制台模式位），
#   两者又都被 rich.console 使用。
#   【兼容】本模块只能在 Windows 上导入：第 11-14 行在非 win32 平台直接 raise ImportError，
#   因此所有调用方都必须做平台判断或延迟导入（rich 的做法是在函数内部 import）。
# 【关键数据结构】
#   - WindowsCoordinates：行/列坐标的 NamedTuple，用来对抗 Win32 的 (Y, X) 顺序；
#   - CONSOLE_SCREEN_BUFFER_INFO / CONSOLE_CURSOR_INFO：两个 ctypes 结构体，
#     分别描述“屏幕缓冲区状态”和“光标大小/可见性”；
#   - LegacyWindowsTerm：本模块的核心，持有输出句柄、默认颜色属性、底层输出文件，
#     并提供 write/移动光标/擦除/设置标题等方法；
#   - ANSI_TO_WINDOWS：ANSI 颜色号 → Win32 属性号的查表数组（16 项）。
# 【设计要点】
#   - 两段式绑定：每个 API 先取一个模块级私有函数对象（如 _GetStdHandle）并显式设置
#     argtypes/restype，再用同名的 Python 函数包装它，负责 byref、返回值转换和错误检查。
#     显式声明类型是必须的——否则 ctypes 会按 c_int 猜测参数/返回值，64 位下 HANDLE
#     会被截断成 32 位，导致句柄失效。
#   - 坐标一律用 WindowsCoordinates 封装，并在 from_param 里完成到 wintypes._COORD
#     的转换，这样它既能出现在参数注解里，又能被 ctypes 自动转换。
#   - 颜色不用 ANSI 序列而用 4 位“文本属性”（低 4 位前景、高 4 位背景、bit3/bit7 为
#     亮度位），所以任何真彩色都必须先 downgrade 到 Windows 的 16 色系统。
#   - 任何“读控制台状态”的操作都可能因为句柄不是控制台而失败（输出被重定向），
#     用 LegacyWindowsError 显式表达这一状态，让上层可以降级处理。
# 【阅读建议】先读常量与两个结构体，建立“文本属性字节”的直观概念（低 4 位前景、
#   高 4 位背景）；再快速扫过 Get/Set 系列的包装函数（它们形态高度一致，理解了
#   argtypes/restype 的套路即可）；最后精读 LegacyWindowsTerm——它是把 Segment 的
#   渲染意图翻译成 API 调用的地方，也是与 POSIX 行为差异最集中的地方。
#   注意文件末尾的 __main__ 是可交互的演示程序（含 sleep），不属于库 API。
# ---------------------------------------------------------------------------

import ctypes
import sys
from typing import Any

# ==== 【加载 kernel32 与常量】 ====
# 【要点】windll 是 ctypes 的 DLL 加载器实例；用 WinDLL（stdcall）而不是 CDLL（cdecl），
# 因为 Win32 API 使用 stdcall 调用约定，用错约定会破坏栈。
windll: Any = None
if sys.platform == "win32":
    windll = ctypes.LibraryLoader(ctypes.WinDLL)
else:
    # 【兼容】非 Windows 平台在导入期就失败：本模块的每一个绑定都依赖 kernel32，
    # 留着半个可导入的模块只会把错误推迟到更难诊断的地方。
    # 【易错】这里用的是字符串形式的模块名（__name__），保证错误信息与导入路径一致。
    raise ImportError(f"{__name__} can only be imported on Windows")

import time
from ctypes import Structure, byref, wintypes
from typing import IO, NamedTuple, Type, cast

from rich.color import ColorSystem
from rich.style import Style

# 【要点】STDOUT = -11 是 STD_OUTPUT_HANDLE 伪句柄的数值，不是真实句柄：
# GetStdHandle 会把这个特殊值翻译成当前进程的标准输出句柄。
STDOUT = -11
# 【要点】ENABLE_VIRTUAL_TERMINAL_PROCESSING 是控制台“输出模式”的一位（值 4）。
# 置位后控制台会把 ANSI/VT 转义序列当命令解释；Rich 用它判断能否走普通 ANSI 路径。
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 4

# 【要点】wintypes._COORD 是 ctypes 内部定义（下划线开头）的公开可用结构体：
# 两个 SHORT 字段 X、Y，正是 Win32 坐标的原始表示。
COORD = wintypes._COORD


# ==== 【错误类型】 ====
# 【职责】表示“调用了 Win32 控制台 API 但失败”的异常，典型场景是输出句柄根本不是
#   控制台（例如 stdout 被重定向到文件或管道）。
# 【为什么】单独定义一个异常类型而不是复用 OSError：上层（_windows.py）需要精确捕获
#   这一种失败并把“不支持 VT”作为正常结果处理，而不是把真正的 I/O 错误也吞掉。
# 【协作】GetConsoleMode 抛出它；rich._windows.get_windows_console_features 捕获它。
# 【注意】它是 Exception 的直接子类，且没有额外属性——因为失败原因只有 Win32 的
#   错误码，而错误码由 ctypes 的 use_last_error/GetLastError 体系管理，这里不需要携带。
class LegacyWindowsError(Exception):
    pass


# ==== 【坐标类型：对抗 (Y, X) 顺序】 ====
# 【职责】封装 Win32 控制台坐标，向调用方提供“行、列”语义的顺序，避免把 (x, y) 写反。
# 【关键属性】row：行号（0 基，自上而下）；col：列号（0 基，自左向右）。
# 【不变量】两个字段都是 0 基；row 增大表示向下，与终端坐标系一致。
# 【协作】LegacyWindowsTerm 的所有光标/擦除方法都接收或构造它；
#   ctypes 在调用 Win32 函数时通过 from_param 把它转成 wintypes._COORD。
# 【易错】Win32 原生 _COORD 的字段顺序是 (X, Y)，而本类型是 (row, col)=(Y, X)，
#   正是这一“故意相反”的设计提醒读者不要混用两种顺序。
# 【兼容】继承 NamedTuple 让它可以像普通元组一样解包（`row, col = self.cursor_position`），
#   同时又是不可变值对象，适合在视图布局这类无副作用的计算中传递。
class WindowsCoordinates(NamedTuple):
    """Coordinates in the Windows Console API are (y, x), not (x, y).
    This class is intended to prevent that confusion.
    Rows and columns are indexed from 0.
    This class can be used in place of wintypes._COORD in arguments and argtypes.
    """

    row: int
    col: int

    # 【功能】把 WindowsCoordinates 转换成 ctypes 需要的 wintypes._COORD 结构体。
    # 【算法】构造 COORD(col, row)——注意参数顺序：_COORD 的字段顺序是 (X, Y)，
    #   所以这里必须把 col 放第一个位置，恰好与本类型的 (row, col) 顺序相反。
    # 【参数】value：被转换的坐标对象；由 ctypes 在调用点自动传入。
    # 【返回】填充好的 wintypes._COORD 实例。
    # 【注意】这是 ctypes 的约定：参数类型被声明为某个类时，ctypes 会在运行时调用该类
    #   名为 from_param 的类方法（而不是实例方法）来转换实参，因此必须写成 classmethod。
    # 【易错】不要试图在参数注解里直接写 COORD 并用本类型传值——那会丢掉行列语义，
    #   让 (row, col) 悄悄变成 (X, Y)。
    @classmethod
    def from_param(cls, value: "WindowsCoordinates") -> COORD:
        """Converts a WindowsCoordinates into a wintypes _COORD structure.
        This classmethod is internally called by ctypes to perform the conversion.

        Args:
            value (WindowsCoordinates): The input coordinates to convert.

        Returns:
            wintypes._COORD: The converted coordinates struct.
        """
        return COORD(value.col, value.row)


# ==== 【Win32 结构体定义】 ====
# 【职责】映射 kernel32 的 CONSOLE_SCREEN_BUFFER_INFO，承载一次 GetConsoleScreenBufferInfo
#   查询的全部结果（缓冲区大小、光标位置、当前文本属性、可见窗口矩形）。
# 【关键字段】dwSize：屏幕缓冲区大小（单位是字符单元，含滚动历史）；
#   dwCursorPosition：光标当前位置（0 基）；
#   wAttributes：当前文本属性（颜色位掩码，低 4 位前景/高 4 位背景）；
#   srWindow：当前**可见窗口**在缓冲区中的矩形（Left/Top/Right/Bottom）；
#   dwMaximumWindowSize：窗口能达到的最大尺寸（取决于屏幕分辨率与字体）。
# 【不变量】字段顺序与 Win32 头文件 wincon.h 中定义完全一致——ctypes 是按内存布局
#   逐字段读取的，任何顺序或类型错误都会读出垃圾数据而不报错。
# 【协作】由 GetConsoleScreenBufferInfo 填充；LegacyWindowsTerm 从中取 wAttributes
#   作为“默认文本属性”，并取 dwSize、dwCursorPosition 实现 screen_size/cursor_position。
# 【易错】dwSize 是**缓冲区**大小而不是窗口可见区域大小。在开启回滚缓冲的终端里
#   缓冲区往往远高于窗口（例如 9001 行），因此这里得到的高度不能当作屏幕高度使用；
#   Rich 只取它的 X（列数）作为行宽，这也是注释里强调 X/Y 的原因。
class CONSOLE_SCREEN_BUFFER_INFO(Structure):
    # 【要点】字段名必须与 wincon.h 中的名字一致，便于与官方文档对照阅读。
    _fields_ = [
        # 【要点】缓冲区尺寸（列数=X、行数=Y），单位是字符单元而非像素。
        ("dwSize", COORD),
        # 【要点】光标位置（X=列、Y=行），0 基。
        ("dwCursorPosition", COORD),
        # 【要点】当前文本属性：低 4 位前景色、高 4 位背景色，bit3/bit7 为亮度位。
        ("wAttributes", wintypes.WORD),
        # 【要点】可见窗口矩形（相对缓冲区的左上/右下角），注意 Bottom/Right 是包含端点。
        ("srWindow", wintypes.SMALL_RECT),
        # 【要点】窗口可达到的最大字符尺寸，通常等于屏幕尺寸/字符尺寸。
        ("dwMaximumWindowSize", COORD),
    ]


# 【职责】映射 kernel32 的 CONSOLE_CURSOR_INFO：描述光标的厚度与可见性。
# 【关键字段】dwSize：光标填充单元格的百分比（1-100），同时决定光标形状（块/下划线）；
#   bVisible：光标是否可见（非 0 即可见）。
# 【不变量】修改可见性时必须把 dwSize 原样写回，否则光标形状会被重置。
# 【协作】由 Get/SetConsoleCursorInfo 读写；LegacyWindowsTerm.hide_cursor/show_cursor
#   先用 _get_cursor_size() 读回 dwSize，再只改 bVisible。
# 【注意】这里的 bVisible 用的是 wintypes.BOOL（4 字节 int），不是 1 字节的 bool，
#   所以赋值要写 0/1 这样的小整数，不能传 Python 的 True/False 之外的类型以免混淆。
class CONSOLE_CURSOR_INFO(ctypes.Structure):
    # 【要点】字段顺序同样必须与 wincon.h 严格一致。
    _fields_ = [("dwSize", wintypes.DWORD), ("bVisible", wintypes.BOOL)]


# ==== 【kernel32 绑定：句柄与模式】 ====
# 【要点】这是本文件通篇使用的“两段式绑定”套路的第一段：取到 C 函数对象后立刻声明
# 参数类型与返回类型。argtypes/restype 只在这里设置一次，之后所有调用都受益。
_GetStdHandle = windll.kernel32.GetStdHandle
_GetStdHandle.argtypes = [
    wintypes.DWORD,
]
# 【要点】HANDLE 在 64 位 Windows 上是 8 字节指针；不声明 restype 的话 ctypes 默认
# 按 c_int（4 字节）解释返回值，句柄会被截断，后续所有调用都失败。
_GetStdHandle.restype = wintypes.HANDLE


# 【功能】取标准设备（stdin/stdout/stderr）的句柄。
# 【参数】handle：设备标识，默认 STDOUT=-11（STD_OUTPUT_HANDLE）；-10 是 stdin、
#   -12 是 stderr。传真实句柄常量以外的值时行为由系统决定。
# 【返回】wintypes.HANDLE；若进程没有对应的标准设备，得到 NULL 或 INVALID_HANDLE_VALUE，
#   本函数不做检查（由使用处的 API 调用失败来体现）。
# 【注意】这是本模块唯一一个“不检查返回值”的包装：句柄是否可用要等到 GetConsoleMode
#   之类的调用才会暴露出来。
# 【兼容】POSIX 对应物是 sys.stdout.fileno()；Windows 上的伪句柄无法用 fileno 得到，
#   所以必须走这个 API。
def GetStdHandle(handle: int = STDOUT) -> wintypes.HANDLE:
    """Retrieves a handle to the specified standard device (standard input, standard output, or standard error).

    Args:
        handle (int): Integer identifier for the handle. Defaults to -11 (stdout).

    Returns:
        wintypes.HANDLE: The handle
    """
    return cast(wintypes.HANDLE, _GetStdHandle(handle))


_GetConsoleMode = windll.kernel32.GetConsoleMode
# 【要点】第二个参数是“输出参数”，类型写作 LPDWORD（指向 DWORD 的指针）。
_GetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
_GetConsoleMode.restype = wintypes.BOOL


# 【功能】读取控制台的当前模式位掩码：对输入缓冲区是输入模式，对屏幕缓冲区是输出模式。
# 【算法】Win32 的惯例是“函数返回成功与否，结果通过指针参数带出”：先造一个 DWORD 容器，
#   把它作为出参传入；函数返回 0 表示失败，此时主动抛 LegacyWindowsError。
# 【参数】std_handle：控制台输入缓冲区或屏幕缓冲区的句柄（Rich 只用输出句柄）。
# 【返回】模式位掩码的整数值（例如含 ENABLE_VIRTUAL_TERMINAL_PROCESSING=4 表示支持 VT）。
# 【注意】最常见的失败原因是句柄根本不是控制台（stdout 被重定向到文件/管道），
#   这在上层是**正常状态**而不是错误，所以 _windows.py 会捕获本异常并降级处理。
# 【易错】出参必须用 wintypes.DWORD() 实例而不是整数：ctypes 需要一块可写的内存来接收结果。
# 【易错】取 `.value` 才是掩码本身；直接把 DWORD 对象当整数用会得到错误的语义。
# 【兼容】这是 POSIX 上不存在的概念：Unix 终端没有“模式位”，而是靠 termios 结构；
#   本函数存在的意义就是回答“这个控制台认不认 ANSI 转义序列”。
def GetConsoleMode(std_handle: wintypes.HANDLE) -> int:
    """Retrieves the current input mode of a console's input buffer
    or the current output mode of a console screen buffer.

    Args:
        std_handle (wintypes.HANDLE): A handle to the console input buffer or the console screen buffer.

    Raises:
        LegacyWindowsError: If any error occurs while calling the Windows console API.

    Returns:
        int: Value representing the current console mode as documented at
            https://docs.microsoft.com/en-us/windows/console/getconsolemode#parameters
    """

    # 【要点】DWORD() 初值为 0；它的地址会被传给 Win32，函数成功后由系统填充。
    console_mode = wintypes.DWORD()
    # 【要点】BOOL 返回 0 表示失败；显式转 bool 是为了让下面的判断读起来是“是否成功”。
    success = bool(_GetConsoleMode(std_handle, console_mode))
    if not success:
        # 【为什么】把 Win32 的失败翻译成 Python 异常，让调用方可以用 try/except 表达
        # “这台终端没有控制台模式”这一业务分支，而不是到处检查返回码。
        raise LegacyWindowsError("Unable to get legacy Windows Console Mode")
    return console_mode.value


# ==== 【kernel32 绑定：写入字符与属性】 ====
# 【要点】这两个 API 是“擦除”功能的实现基础：控制台没有“删除”操作，
#   只能往指定区域重复写空格（清字符）并重置该区域的属性（清颜色）。
_FillConsoleOutputCharacterW = windll.kernel32.FillConsoleOutputCharacterW
_FillConsoleOutputCharacterW.argtypes = [
    wintypes.HANDLE,
    # 【易错】这里声明成 c_char（单字节），但函数是宽字符版（...W，接收 WCHAR）。
    # 之所以能工作，是因为 ctypes 按值传递这个 1 字节整数，系统会把它扩展成 WCHAR；
    # 而调用方（erase_* 系列）永远只传空格 " "，所以并未暴露出问题。
    # 若将来传入非 ASCII 字符，这里的类型声明就是第一个要改的地方（应为 c_wchar）。
    ctypes.c_char,
    wintypes.DWORD,
    # 【要点】坐标参数用 WindowsCoordinates 声明，ctypes 会调用它的 from_param 转换。
    cast(Type[COORD], WindowsCoordinates),
    # 【要点】最后一个参数是出参：实际写入的字符数，用 POINTER(DWORD) 接收。
    ctypes.POINTER(wintypes.DWORD),
]
_FillConsoleOutputCharacterW.restype = wintypes.BOOL


# 【功能】从指定坐标开始，把同一个字符连续写入屏幕缓冲区 length 次。
# 【算法】把 Python 字符编码成 1 字节、把长度与计数包装成 DWORD，调用 Win32 函数后
#   返回计数器的值（实际写入的字符数）。
# 【参数】std_handle：屏幕缓冲区句柄。char：要重复写入的字符，**必须长度为 1**。
#   length：重复次数（字符单元数）。start：起始坐标，从该点向右写，写满一行会自动续到下一行。
# 【返回】实际写入的字符数；正常情况下等于 length，靠近缓冲区末尾时可能更小。
# 【注意】本函数不检查 BOOL 返回值，失败时只能从“返回的写入数为 0”间接看出。
# 【易错】char 必须能编码成单字节：传入非 ASCII 字符会在这里抛异常（见上面 argtypes 的说明）。
# 【兼容】POSIX 上没有对应概念——Unix 终端通过转义序列（如 CSI nK）请求擦除，
#   由终端自己完成；Windows 上必须由程序自己把空格“画”上去。
def FillConsoleOutputCharacter(
    std_handle: wintypes.HANDLE,
    char: str,
    length: int,
    start: WindowsCoordinates,
) -> int:
    """Writes a character to the console screen buffer a specified number of times, beginning at the specified coordinates.

    Args:
        std_handle (wintypes.HANDLE): A handle to the console input buffer or the console screen buffer.
        char (str): The character to write. Must be a string of length 1.
        length (int): The number of times to write the character.
        start (WindowsCoordinates): The coordinates to start writing at.

    Returns:
        int: The number of characters written.
    """
    # 【要点】Python 的 str 必须先 encode 成 bytes 才能装进 c_char（单字节）。
    character = ctypes.c_char(char.encode())
    # 【要点】长度按 Win32 惯例包装成 DWORD；这个参数是“按值”传的，不需要 byref。
    num_characters = wintypes.DWORD(length)
    # 【要点】出参计数器必须先初始化为 0，再由系统填充。
    num_written = wintypes.DWORD(0)
    _FillConsoleOutputCharacterW(
        std_handle,
        character,
        num_characters,
        start,
        # 【要点】只有出参才用 byref 传址；其余参数按值传递。
        byref(num_written),
    )
    return num_written.value


_FillConsoleOutputAttribute = windll.kernel32.FillConsoleOutputAttribute
# 【要点】第二个参数是要刷进单元格的“文本属性”，类型是 WORD（16 位）。
_FillConsoleOutputAttribute.argtypes = [
    wintypes.HANDLE,
    wintypes.WORD,
    wintypes.DWORD,
    cast(Type[COORD], WindowsCoordinates),
    ctypes.POINTER(wintypes.DWORD),
]
_FillConsoleOutputAttribute.restype = wintypes.BOOL


# 【功能】把同一组“文本属性”（颜色位掩码）刷到从 start 开始的 length 个单元格上，
#   不改变这些单元格里的字符内容。
# 【算法】把属性包装成 WORD、长度包装成 DWORD，调用 Win32 后返回出参计数。
# 【参数】std_handle：屏幕缓冲区句柄。attributes：颜色属性整数，低 4 位前景、高 4 位背景
#   （bit3/bit7 是亮度位）。length：要刷的单元格数。start：起始坐标。
# 【返回】实际设置了属性的单元格数。
# 【注意】它只改颜色不改字符：所以“擦除”必须成对调用本函数与 FillConsoleOutputCharacter，
#   否则会留下“内容已清空但底色仍是旧颜色”的花屏（LegacyWindowsTerm.erase_* 正是成对调用）。
# 【易错】attributes 是位掩码而不是索引：不能传 ANSI 颜色号，必须先过 ANSI_TO_WINDOWS 映射。
# 【兼容】POSIX 终端的擦除序列（EL）由终端一次性完成字符与属性重置，无需两次 API 调用。
def FillConsoleOutputAttribute(
    std_handle: wintypes.HANDLE,
    attributes: int,
    length: int,
    start: WindowsCoordinates,
) -> int:
    """Sets the character attributes for a specified number of character cells,
    beginning at the specified coordinates in a screen buffer.

    Args:
        std_handle (wintypes.HANDLE): A handle to the console input buffer or the console screen buffer.
        attributes (int): Integer value representing the foreground and background colours of the cells.
        length (int): The number of cells to set the output attribute of.
        start (WindowsCoordinates): The coordinates of the first cell whose attributes are to be set.

    Returns:
        int: The number of cells whose attributes were actually set.
    """
    # 【要点】长度同样包装成 DWORD；属性包装成 WORD（16 位掩码）。
    num_cells = wintypes.DWORD(length)
    style_attrs = wintypes.WORD(attributes)
    # 【要点】出参计数器初始化为 0。
    num_written = wintypes.DWORD(0)
    _FillConsoleOutputAttribute(
        std_handle, style_attrs, num_cells, start, byref(num_written)
    )
    return num_written.value


# ==== 【kernel32 绑定：设置当前文本属性】 ====
_SetConsoleTextAttribute = windll.kernel32.SetConsoleTextAttribute
_SetConsoleTextAttribute.argtypes = [
    wintypes.HANDLE,
    wintypes.WORD,
]
_SetConsoleTextAttribute.restype = wintypes.BOOL


# 【功能】设置“此后写入的文本”所使用的颜色属性（相当于 POSIX 的 SGR 序列）。
# 【参数】std_handle：屏幕缓冲区句柄。attributes：属性掩码，低 4 位前景、高 4 位背景，
#   bit3/bit7 为亮度位——与 FillConsoleOutputAttribute 用的是同一套编码。
# 【返回】bool：True 表示设置成功。
# 【注意】这是**有状态**的：设置后一直生效，直到再次调用。Rich 的做法是
#   “设置 → 写文本 → 恢复默认属性”，见 LegacyWindowsTerm.write_styled。
# 【兼容】POSIX 用一条 \x1b[31m 之类的序列完成同样的事，且不需要在写完后显式恢复；
#   Windows 上没有“重置序列”，必须自己记住默认属性并写回（这也是 __init__ 要读
#   wAttributes 的原因）。
def SetConsoleTextAttribute(
    std_handle: wintypes.HANDLE, attributes: wintypes.WORD
) -> bool:
    """Set the colour attributes for all text written after this function is called.

    Args:
        std_handle (wintypes.HANDLE): A handle to the console input buffer or the console screen buffer.
        attributes (int): Integer value representing the foreground and background colours.


    Returns:
        bool: True if the attribute was set successfully, otherwise False.
    """
    # 【要点】BOOL 转 bool，把 Win32 的 0/1 约定统一成 Python 语义。
    return bool(_SetConsoleTextAttribute(std_handle, attributes))


# ==== 【kernel32 绑定：查询屏幕缓冲区信息】 ====
_GetConsoleScreenBufferInfo = windll.kernel32.GetConsoleScreenBufferInfo
# 【要点】第二个参数是出参结构体的指针，由 Python 侧分配内存后传址。
_GetConsoleScreenBufferInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO),
]
_GetConsoleScreenBufferInfo.restype = wintypes.BOOL


# 【功能】一次性查出屏幕缓冲区的尺寸、光标位置、当前文本属性与窗口矩形。
# 【算法】在 Python 侧分配一个 CONSOLE_SCREEN_BUFFER_INFO 并传址给 Win32，
#   系统把结果填进这块内存，函数返回该结构体。
# 【参数】std_handle：屏幕缓冲区（标准输出）句柄。
# 【返回】CONSOLE_SCREEN_BUFFER_INFO：**总是**返回一个结构体，即使调用失败；
#   失败时字段是全 0，调用方不会收到异常。
# 【易错】本函数不检查 BOOL 返回值：句柄无效时得到的是一堆 0，而不是错误。
#   上层（如 screen_size）会因此看到 0 列 0 行，必须理解这一点才能解释某些“尺寸为 0”的现象。
# 【注意】查询一次要付出一次系统调用；LegacyWindowsTerm 的光标相关方法常常连续查询
#   多次（cursor_position 与 screen_size 各查一遍），这是 legacy 路径较慢的原因之一。
# 【优化】若在同一段逻辑里既需要光标位置又需要屏幕尺寸，理论上可以只查一次再复用结果。
def GetConsoleScreenBufferInfo(
    std_handle: wintypes.HANDLE,
) -> CONSOLE_SCREEN_BUFFER_INFO:
    """Retrieves information about the specified console screen buffer.

    Args:
        std_handle (wintypes.HANDLE): A handle to the console input buffer or the console screen buffer.

    Returns:
        CONSOLE_SCREEN_BUFFER_INFO: A CONSOLE_SCREEN_BUFFER_INFO ctype struct contain information about
            screen size, cursor position, colour attributes, and more."""
    # 【要点】结构体先默认初始化为全 0，再传址让系统填充；失败时返回的就是这个全 0 对象。
    console_screen_buffer_info = CONSOLE_SCREEN_BUFFER_INFO()
    _GetConsoleScreenBufferInfo(std_handle, byref(console_screen_buffer_info))
    return console_screen_buffer_info


# ==== 【kernel32 绑定：光标位置】 ====
_SetConsoleCursorPosition = windll.kernel32.SetConsoleCursorPosition
# 【要点】坐标参数声明为 WindowsCoordinates 的强制转换形式，目的是让 ctypes 调用
#   from_param 完成到 _COORD 的转换（cast 只影响类型检查器，运行期无副作用）。
_SetConsoleCursorPosition.argtypes = [
    wintypes.HANDLE,
    cast(Type[COORD], WindowsCoordinates),
]
_SetConsoleCursorPosition.restype = wintypes.BOOL


# 【功能】把光标移动到屏幕缓冲区中的指定坐标（相当于 POSIX 的 CSI row;colH）。
# 【参数】std_handle：屏幕缓冲区句柄。coords：目标坐标，0 基 (row, col)。
# 【返回】bool：True 表示成功。
# 【注意】坐标必须在缓冲区范围内，否则失败——所以 LegacyWindowsTerm.move_cursor_to
#   会先做“负数直接返回”的保护，避免把负坐标交给系统。
# 【兼容】POSIX 的对应操作是写一条转义序列，终端自己保证在可见区域内；Windows 这里
#   移动的是“缓冲区”坐标，若缓冲区大于窗口，光标可能移到屏幕外（不可见）。
def SetConsoleCursorPosition(
    std_handle: wintypes.HANDLE, coords: WindowsCoordinates
) -> bool:
    """Set the position of the cursor in the console screen

    Args:
        std_handle (wintypes.HANDLE): A handle to the console input buffer or the console screen buffer.
        coords (WindowsCoordinates): The coordinates to move the cursor to.

    Returns:
        bool: True if the function succeeds, otherwise False.
    """
    # 【要点】WindowsCoordinates 在这里被 ctypes 自动转换成 _COORD（X, Y）。
    return bool(_SetConsoleCursorPosition(std_handle, coords))


# ==== 【kernel32 绑定：光标形状与可见性】 ====
_GetConsoleCursorInfo = windll.kernel32.GetConsoleCursorInfo
_GetConsoleCursorInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(CONSOLE_CURSOR_INFO),
]
_GetConsoleCursorInfo.restype = wintypes.BOOL


# 【功能】读取光标信息（厚度百分比与可见性）。
# 【算法】出参结构体由调用方分配并传入（本函数用 byref 传址，不属于返回值）。
# 【参数】std_handle：屏幕缓冲区句柄。cursor_info：一个 CONSOLE_CURSOR_INFO 实例，
#   调用成功后其字段被就地填充——注意它不是返回值，调用方必须自己保留这个对象。
# 【返回】bool：True 表示读取成功；无返回值意义上的“光标信息”。
# 【易错】这一点与本文件其它 API 不同：这里没有把结果 return 出来，因此调用方必须
#   先构造对象再读它的字段（见 LegacyWindowsTerm._get_cursor_size）。
# 【注意】隐藏/显示光标需要“先读后写”：Win32 的 SetConsoleCursorInfo 要求一次提供
#   完整的结构体，若只填 bVisible 而把 dwSize 留 0，光标形状会被破坏。
def GetConsoleCursorInfo(
    std_handle: wintypes.HANDLE, cursor_info: CONSOLE_CURSOR_INFO
) -> bool:
    """Get the cursor info - used to get cursor visibility and width

    Args:
        std_handle (wintypes.HANDLE): A handle to the console input buffer or the console screen buffer.
        cursor_info (CONSOLE_CURSOR_INFO): CONSOLE_CURSOR_INFO ctype struct that receives information
            about the console's cursor.

    Returns:
          bool: True if the function succeeds, otherwise False.
    """
    # 【要点】同样是传址填充：结构体由调用方提供，本函数只返回成败。
    return bool(_GetConsoleCursorInfo(std_handle, byref(cursor_info)))


_SetConsoleCursorInfo = windll.kernel32.SetConsoleCursorInfo
_SetConsoleCursorInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(CONSOLE_CURSOR_INFO),
]
_SetConsoleCursorInfo.restype = wintypes.BOOL


# 【功能】写回光标信息，用于修改可见性与光标厚度。
# 【参数】cursor_info：完整的光标信息结构体；**dwSize 与 bVisible 都要给正确值**。
# 【返回】bool：True 表示设置成功。
# 【注意】Win32 没有单独的“隐藏光标”API，只能整块替换结构体，因此调用方必须先把
#   当前的 dwSize 读回来（这也是 LegacyWindowsTerm.show_cursor/hide_cursor 的写法）。
# 【兼容】POSIX 对应 \x1b[?25l / \x1b[?25h，是一条无状态序列，不需要先读当前状态。
def SetConsoleCursorInfo(
    std_handle: wintypes.HANDLE, cursor_info: CONSOLE_CURSOR_INFO
) -> bool:
    """Set the cursor info - used for adjusting cursor visibility and width

    Args:
        std_handle (wintypes.HANDLE): A handle to the console input buffer or the console screen buffer.
        cursor_info (CONSOLE_CURSOR_INFO): CONSOLE_CURSOR_INFO ctype struct containing the new cursor info.

    Returns:
          bool: True if the function succeeds, otherwise False.
    """
    # 【要点】结构体按值传址（byref），其中的 dwSize 决定光标形状、bVisible 决定可见性。
    return bool(_SetConsoleCursorInfo(std_handle, byref(cursor_info)))


# ==== 【kernel32 绑定：窗口标题】 ====
# 【要点】绑定的是宽字符版 SetConsoleTitleW，因此 argtypes 用 LPCWSTR（宽字符串指针）；
#   ctypes 会自动把 Python str 转成 UTF-16。
_SetConsoleTitle = windll.kernel32.SetConsoleTitleW
_SetConsoleTitle.argtypes = [wintypes.LPCWSTR]
_SetConsoleTitle.restype = wintypes.BOOL


# 【功能】设置控制台窗口标题（相当于 POSIX 的 OSC 0/2 序列）。
# 【参数】title：新的窗口标题；由 ctypes 按 UTF-16 传给系统。
# 【返回】bool：True 表示成功。
# 【注意】本函数不需要句柄：标题是“当前控制台窗口”的属性，而不是某个缓冲区句柄的属性。
#   这与本文件其它 API 都要求 HANDLE 形成对比，读的时候不要以为漏了参数。
# 【约束】调用方 LegacyWindowsTerm.set_title 会先断言标题短于 255 字符；
#   该限制来自 SetConsoleTitle 的文档（过长可能被截断或失败）。
def SetConsoleTitle(title: str) -> bool:
    """Sets the title of the current console window

    Args:
        title (str): The new title of the console window.

    Returns:
        bool: True if the function succeeds, otherwise False.
    """
    return bool(_SetConsoleTitle(title))


# ==== 【LegacyWindowsTerm：老控制台绘制后端】 ====
# 【职责】把“渲染意图”（写一段带样式的文本、把光标移到某处、擦除某行）翻译成 Win32
#   控制台 API 调用；它是 rich._windows_renderer 唯一的绘制出口。
# 【关键属性】
#   _handle：标准输出句柄，所有 API 调用都用它；
#   _default_text：进程启动时缓冲区里的文本属性（颜色），作为“恢复默认色”的基准；
#   _default_fore / _default_back：从 _default_text 拆出的前景、背景（各 3 位）；
#   _default_attrs：由前两者重新拼回的属性（擦除时用它把单元格刷回默认色）；
#   _file / write / flush：底层输出文件对象及其绑定方法——把本对象伪装成文件，
#   使得它既能写文本（走文件层，自带编码与换行处理）又能改颜色（走 API 层）。
# 【不变量】_default_* 三个属性在构造后不再改变；任何一次 write_styled 都必须在写完
#   之后把属性恢复为 _default_text，否则后续文本会带着上一次的颜色。
# 【协作】由 rich.console.Console.render 在 legacy 分支构造，交给
#   rich._windows_renderer.legacy_windows_render 使用。
# 【注意】本类不做任何错误检查：CPU 层的失败会以“颜色不对/位置不对”的静默方式表现，
#   而不是异常——这是它作为“尽力而为的兼容后端”的定位。
# 【兼容】与 POSIX 的根本差异：颜色是**有状态**的文本属性而不是内联转义序列，
#   擦除要自己画空格，光标移动要自己处理回绕，且只有 16 色可用。
class LegacyWindowsTerm:
    """This class allows interaction with the legacy Windows Console API. It should only be used in the context
    of environments where virtual terminal processing is not available. However, if it is used in a Windows environment,
    the entire API should work.

    Args:
        file (IO[str]): The file which the Windows Console API HANDLE is retrieved from, defaults to sys.stdout.
    """

    # 【要点】亮度位：置位表示高亮（ANSI 的 bright 系列），清位表示普通色（dim 亦然）。
    BRIGHT_BIT = 8

    # 【设计要点】索引是 ANSI 颜色号（0-15），值是对应的 Windows 文本属性号。
    # 【为什么】两套编号的位序不同：Windows 的属性位按 BGR 排列
    #   （FOREGROUND_BLUE=1、GREEN=2、RED=4、INTENSITY=8），而 ANSI 按 RGB 直觉排列，
    #   所以红与蓝的前两位是互换的，必须查表而不能简单加减。
    # 【要点】ANSI 8-15 的“亮色”在 Windows 上就是同一色相再置亮度位：
    #   例如亮红 ANSI 9 → 4(RED) | 8(INTENSITY) = 12。
    # Indices are ANSI color numbers, values are the corresponding Windows Console API color numbers
    ANSI_TO_WINDOWS = [
        0,  # black                      The Windows colours are defined in wincon.h as follows:
        4,  # red                         define FOREGROUND_BLUE            0x0001 -- 0000 0001
        2,  # green                       define FOREGROUND_GREEN           0x0002 -- 0000 0010
        6,  # yellow                      define FOREGROUND_RED             0x0004 -- 0000 0100
        1,  # blue                        define FOREGROUND_INTENSITY       0x0008 -- 0000 1000
        5,  # magenta                     define BACKGROUND_BLUE            0x0010 -- 0001 0000
        3,  # cyan                        define BACKGROUND_GREEN           0x0020 -- 0010 0000
        7,  # white                       define BACKGROUND_RED             0x0040 -- 0100 0000
        8,  # bright black (grey)         define BACKGROUND_INTENSITY       0x0080 -- 1000 0000
        12,  # bright red
        10,  # bright green
        14,  # bright yellow
        9,  # bright blue
        13,  # bright magenta
        11,  # bright cyan
        15,  # bright white
    ]

    # 【功能】绑定标准输出句柄，记录“当前”文本属性作为默认色，并把本对象伪装成文件。
    # 【算法】
    #   1) GetStdHandle(STDOUT) 取标准输出句柄并保存；
    #   2) 读一次缓冲区信息，把 wAttributes 记为 _default_text；
    #   3) 从 _default_text 拆出前景（低 3 位）与背景（第 4-6 位），再拼回
    #      _default_attrs——这一份是“擦除时用来刷底色”的属性；
    #   4) 保存文件对象，并把 write/flush 绑定到它的同名方法上。
    # 【参数】file：真正执行文本输出的文件对象（通常是 sys.stdout）；句柄与它必须是
    #   同一个设备，否则会出现“颜色改了但文字写到别处”的错位。
    # 【注意】取属性用的是 `default_text & 7`（只取 3 位）而不是 & 15：刻意丢掉亮度位，
    #   这样 _default_attrs 的前景色不会把“高亮”误当成一个独立的色号。
    # 【易错】self.write/self.flush 是实例属性而非方法——这使对象可以采用
    #   duck typing 被当作文件使用，但也意味着这两个名字无法再被子类覆写为方法。
    # 【为什么】把文件的 write 缓存到实例上：write_text 是热路径，避免每次属性查找。
    def __init__(self, file: "IO[str]") -> None:
        # 【要点】-11 = STD_OUTPUT_HANDLE；本类不支持 stderr/in 的句柄。
        handle = GetStdHandle(STDOUT)
        self._handle = handle
        # 【要点】读取“当时”的属性作为默认色基准，因此它反映的是启动时的终端配色
        # （例如用户自定义的深色主题），而不是硬编码的黑底白字。
        default_text = GetConsoleScreenBufferInfo(handle).wAttributes
        self._default_text = default_text

        # 【要点】低 3 位 = 前景色号（0-7），不含亮度位。
        self._default_fore = default_text & 7
        # 【要点】第 4-6 位 = 背景色号，先右移再掩码。
        self._default_back = (default_text >> 4) & 7
        # 【要点】拼回成完整的属性字节：前 4 位前景（亮度位留在 0）、后 4 位背景。
        # 擦除时用它把单元格恢复成“默认底色”，而不是系统当前色。
        self._default_attrs = self._default_fore | (self._default_back << 4)

        self._file = file
        # 【要点】绑定而非包装：等价于 self.write = file.write，让本对象可直接当文件用。
        self.write = file.write
        self.flush = file.flush

    # 【职责】把 Win32 的 _COORD（X, Y 顺序）翻译成本模块的行列坐标（row, col）。
    # 【功能】返回光标当前位置（0 基）。
    # 【返回】WindowsCoordinates(row=Y, col=X)。
    # 【注意】每次访问都会做一次 GetConsoleScreenBufferInfo 系统调用；在连续操作里
    #   反复读它（例如 erase_* 与 move_cursor_* 都各自读一次）是 legacy 路径慢的主因。
    # 【易错】这里必须交换字段：结构体里是 (X, Y)，而 WindowsCoordinates 是 (row, col)。
    @property
    def cursor_position(self) -> WindowsCoordinates:
        """Returns the current position of the cursor (0-based)

        Returns:
            WindowsCoordinates: The current cursor position.
        """
        coord: COORD = GetConsoleScreenBufferInfo(self._handle).dwCursorPosition
        return WindowsCoordinates(row=coord.Y, col=coord.X)

    # 【职责】提供屏幕缓冲区的宽高，用于计算擦除范围与光标回绕边界。
    # 【功能】返回屏幕缓冲区尺寸，单位是字符列/行。
    # 【返回】WindowsCoordinates(row=缓冲区行数, col=缓冲区列数)。
    # 【易错】读的是 dwSize（缓冲区大小），不是可见窗口大小 srWindow。开启回滚缓冲的
    #   终端里行数可能远大于窗口高度，但 Rich 实际只依赖 col（列数）来判断行宽，
    #   因此这个“不精确”在现有用法下是安全的。
    # 【注意】同 cursor_position，每次访问都是一次系统调用。
    @property
    def screen_size(self) -> WindowsCoordinates:
        """Returns the current size of the console screen buffer, in character columns and rows

        Returns:
            WindowsCoordinates: The width and height of the screen as WindowsCoordinates.
        """
        screen_size: COORD = GetConsoleScreenBufferInfo(self._handle).dwSize
        return WindowsCoordinates(row=screen_size.Y, col=screen_size.X)

    # 【功能】直接写文本，不改动任何颜色属性。
    # 【算法】self.write(text) 之后立刻 self.flush()。
    # 【要点】立即 flush 很关键：颜色是通过 API 设置的，文字是通过文件对象写的，
    #   两者走不同的通道；如果不 flush，缓冲在文件层的文字可能晚于后续的
    #   SetConsoleTextAttribute 生效，从而带上错误的颜色。
    # 【兼容】POSIX 上写文本与改颜色都在同一个字节流里，顺序天然一致，无需 flush 对齐。
    def write_text(self, text: str) -> None:
        """Write text directly to the terminal without any modification of styles

        Args:
            text (str): The text to write to the console
        """
        self.write(text)
        self.flush()

    # 【功能】按给定 Style 输出文本：先算出 Windows 的 16 色属性，设置属性、写文字，
    #   最后恢复默认属性。
    # 【算法】
    #   1) 取 style.color / style.bgcolor；若 style.reverse 则把两者互换
    #      （Win32 没有“反显”位，只能用交换前景/背景来模拟）；
    #   2) 前景：color.downgrade(ColorSystem.WINDOWS).number 得到 0-15 的 ANSI 号，
    #      为 None（无色或降级失败）时按 ANSI 7（白）处理；
    #      bold 置亮度位、dim 清亮度位；最后经 ANSI_TO_WINDOWS 查表转成 Win32 属性号；
    #      若原本没有前景色，则沿用启动时的默认前景；
    #   3) 背景：同样降级取色号，None 时按 ANSI 0（黑）处理，再查表；无背景色时用默认背景；
    #   4) 用 fore | (back << 4) 拼出属性，SetConsoleTextAttribute 应用，写文本，
    #      再 SetConsoleTextAttribute(self._default_text) 恢复。
    # 【参数】text：要写的字符串。style：rich 的 Style；其 italic/underline/strike 等
    #   属性在 Win32 控制台属性里没有对应位，会被**静默忽略**（这是与 POSIX 的一大差异）。
    # 【注意】本方法不是原子的：写入过程中抛异常就会跳过“恢复默认属性”这一步，
    #   终端会一直停留在上一次的颜色。这是 legacy 后端的已知取舍（没有 try/finally）。
    # 【易错】亮度位是“借”前景色的 bit3 实现的，所以“亮背景”在这里表达不出来：
    #   只有前景会应用 bold/dim（见代码只对 fore 做位运算）。
    # 【兼容】真彩色会被 downgrade 到 Windows 的 16 色系统（ColorSystem.WINDOWS），
    #   因为 Win32 文本属性只有 4 位颜色；POSIX 终端则可以直接输出 24 位真彩色序列。
    def write_styled(self, text: str, style: Style) -> None:
        """Write styled text to the terminal.

        Args:
            text (str): The text to write
            style (Style): The style of the text
        """
        # 【要点】Style 的颜色字段可能为 None（表示“继承默认”）。
        color = style.color
        bgcolor = style.bgcolor
        # 【为什么】Win32 的文本属性里没有“反显”位，只能通过交换前景与背景来模拟。
        # 【易错】交换发生在“判空”之前：若只有前景色被设置，交换后它会变成背景色，
        # 前景回落到默认色——这正是反显应有的效果。
        if style.reverse:
            color, bgcolor = bgcolor, color

        if color:
            # 【要点】降级到 Windows 的 16 色系统：真彩色/256 色在这里被就近映射成色号。
            fore = color.downgrade(ColorSystem.WINDOWS).number
            # 【要点】降级仍可能拿不到色号（None），退化成 ANSI 7（白），保证总有颜色可用。
            fore = fore if fore is not None else 7  # Default to ANSI 7: White
            # 【为什么】Win32 靠前景属性的 bit3（INTENSITY）表达“亮色”，
            # 于是用 bold 来置这一位，等价于 ANSI 的 1;3xm 高亮。
            if style.bold:
                fore = fore | self.BRIGHT_BIT
            # 【要点】dim 清掉亮度位，实现 ANSI 的 2m（faint）效果——因为 Win32 没有独立的
            # “变暗”能力，只能用“取消高亮”近似。
            if style.dim:
                fore = fore & ~self.BRIGHT_BIT
            # 【易错】必须先做完 bold/dim 的位运算再查表：表里存的是 Win32 属性号，
            # 而亮度位是 Win32 侧的概念，对 ANSI 色号做位运算才有意义。
            fore = self.ANSI_TO_WINDOWS[fore]
        else:
            # 【要点】没有指定前景色时沿用启动时的默认前景，而不是硬编码白色。
            fore = self._default_fore

        if bgcolor:
            # 【要点】背景色同样只能取 16 色中的一种。
            back = bgcolor.downgrade(ColorSystem.WINDOWS).number
            # 【要点】背景降级失败时退化成 ANSI 0（黑）。
            back = back if back is not None else 0  # Default to ANSI 0: Black
            back = self.ANSI_TO_WINDOWS[back]
        else:
            # 【要点】未指定背景色时沿用默认背景。
            back = self._default_back

        # 【约束】这两个断言只服务于类型检查器（上面每个分支都必然赋了 int），
        # 运行期不会触发；它们存在的意义是让 mypy 接受下面的位运算。
        assert fore is not None
        assert back is not None

        # 【要点】属性字节的拼装规则：低 4 位前景、高 4 位背景。
        # c_ushort 的显式包装是多余的（argtypes 已声明为 WORD），保留原样以避免行为差异。
        SetConsoleTextAttribute(
            self._handle, attributes=ctypes.c_ushort(fore | (back << 4))
        )
        # 【要点】文字必须在“设置属性之后、恢复属性之前”写出，否则颜色不生效。
        self.write_text(text)
        # 【为什么】Win32 没有 SGR reset（\x1b[0m），所以必须自己把启动时记住的
        # 默认属性写回去；漏掉这一步会让后续所有输出都带上本次的颜色。
        SetConsoleTextAttribute(self._handle, attributes=self._default_text)

    # 【功能】把光标移动到指定坐标（等同于渲染控制码 CURSOR_MOVE_TO / HOME 的落地实现）。
    # 【算法】先做负数保护，再调用 SetConsoleCursorPosition。
    # 【参数】new_position：目标坐标，0 基 (row, col)。
    # 【返回】None；失败与否不反馈（见下）。
    # 【易错】负坐标被**静默忽略**而不是抛异常：渲染控制码里可能出现 -1（例如把
    #   1 基坐标忘记转换时的边界情况），静默忽略能避免整个渲染流程崩掉。
    # 【注意】SetConsoleCursorPosition 的失败（越界）也不会被检查，因此“光标没动”
    #   这种症状需要回到调用方去找原因。
    def move_cursor_to(self, new_position: WindowsCoordinates) -> None:
        """Set the position of the cursor

        Args:
            new_position (WindowsCoordinates): The WindowsCoordinates representing the new position of the cursor.
        """
        # 【易错】必须同时检查行与列：Win32 不接受任何负坐标。
        if new_position.col < 0 or new_position.row < 0:
            return
        SetConsoleCursorPosition(self._handle, coords=new_position)

    # 【功能】擦除光标所在的整行（对应控制码 ERASE_IN_LINE 的 mode=2 / ANSI CSI 2K）。
    # 【算法】取整行宽度，从该行第 0 列开始，先写 width 个空格清掉字符，
    #   再用默认属性刷一遍这些单元格清掉颜色。
    # 【注意】擦除不是“删除”，而是“画满空格”，因此光标位置在擦除后不变。
    # 【易错】两次调用缺一不可：只写空格会留下旧的背景色，只刷属性则旧文字还在。
    # 【兼容】POSIX 上一条 CSI 2K 序列即可，终端会一并处理字符与属性。
    def erase_line(self) -> None:
        """Erase all content on the line the cursor is currently located at"""
        # 【要点】整行擦除的宽度取屏幕缓冲区的列数（行首到行尾）。
        screen_size = self.screen_size
        cursor_position = self.cursor_position
        cells_to_erase = screen_size.col
        # 【要点】起点固定在第 0 列，保持行号不变。
        start_coordinates = WindowsCoordinates(row=cursor_position.row, col=0)
        # 【要点】第一步：用空格覆盖字符内容。
        FillConsoleOutputCharacter(
            self._handle, " ", length=cells_to_erase, start=start_coordinates
        )
        # 【要点】第二步：把这些单元格的颜色刷回默认，避免留下旧底色。
        FillConsoleOutputAttribute(
            self._handle,
            self._default_attrs,
            length=cells_to_erase,
            start=start_coordinates,
        )

    # 【功能】擦除从光标位置到行尾的内容（对应控制码 ERASE_IN_LINE 的 mode=0 / ANSI CSI 0K）。
    # 【算法】要擦的单元格数 = 屏幕列数 - 光标所在列；起点就是当前光标位置。
    # 【注意】用 self._default_attrs 而不是当前属性刷色，保证擦出来的是“默认底色”。
    # 【易错】若屏幕尺寸查询失败返回 0（句柄无效时），cells_to_erase 会是 0 或负数，
    #   调用 Win32 也只会写 0 个单元格，属于静默无效而非异常。
    def erase_end_of_line(self) -> None:
        """Erase all content from the cursor position to the end of that line"""
        # 【要点】起点就是当前光标，长度 = 到行尾的距离（列数 - 当前列）。
        cursor_position = self.cursor_position
        cells_to_erase = self.screen_size.col - cursor_position.col
        # 【易错】这里的 start 直接复用光标坐标（含列号），不像 erase_line 那样把列重置为 0。
        FillConsoleOutputCharacter(
            self._handle, " ", length=cells_to_erase, start=cursor_position
        )
        FillConsoleOutputAttribute(
            self._handle,
            self._default_attrs,
            length=cells_to_erase,
            start=cursor_position,
        )

    # 【功能】擦除从行首到光标位置的单元格（对应控制码 ERASE_IN_LINE 的 mode=1 / ANSI CSI 1K）。
    # 【算法】取出当前行与列，起点固定为本行第 0 列，要擦的单元格数就是当前列号（col 个）。
    # 【要点】这里的长度直接取 `col`（而不是像 end 分支那样做减法），
    #   把光标坐标解包成 (row, col) 正好满足需要。
    # 【注意】成对调用“写空格 + 刷默认属性”，与另外两个擦除方法一致。
    # 【兼容】POSIX 用一条 CSI 1K 序列完成；Windows 上必须自己计算范围并画空格。
    def erase_start_of_line(self) -> None:
        """Erase all content from the cursor position to the start of that line"""
        # 【要点】NamedTuple 支持解包，这里取出当前行与当前列。
        row, col = self.cursor_position
        # 【要点】起点是本行第 0 列；要擦除的长度恰好是当前列号（即从行首到光标）。
        # WindowsCoordinates(row, 0) 用的是位置参数，顺序是 (row, col)。
        start = WindowsCoordinates(row, 0)
        FillConsoleOutputCharacter(self._handle, " ", length=col, start=start)
        FillConsoleOutputAttribute(
            self._handle, self._default_attrs, length=col, start=start
        )

    # 【功能】光标上移一格（对应控制码 CURSOR_UP / ANSI CSI A）。
    # 【算法】读当前坐标，把 row 减 1，再写回；列保持不变。
    # 【注意】不做边界检查：row=0 时令其变成 -1，SetConsoleCursorPosition 会失败，
    #   本方法忽略失败，因此效果是“光标原地不动”。
    # 【兼容】POSIX 终端的 CSI A 通常被终端钳制在屏幕内，语义相同但实现位置不同。
    def move_cursor_up(self) -> None:
        """Move the cursor up a single cell"""
        cursor_position = self.cursor_position
        SetConsoleCursorPosition(
            self._handle,
            coords=WindowsCoordinates(
                row=cursor_position.row - 1, col=cursor_position.col
            ),
        )

    # 【功能】光标下移一格（对应控制码 CURSOR_DOWN / ANSI CSI B）。
    # 【算法】读当前坐标，row 加 1，列不变，写回。
    # 【注意】与上移对称：不做行数上限检查，越界时 Win32 调用失败且被忽略。
    # 【易错】向下移动不会滚动屏幕：它只改光标，缓冲区不会因为“移到底部”而滚动，
    #   这与终端里输出换行触发滚动的行为不同。
    def move_cursor_down(self) -> None:
        """Move the cursor down a single cell"""
        cursor_position = self.cursor_position
        SetConsoleCursorPosition(
            self._handle,
            coords=WindowsCoordinates(
                row=cursor_position.row + 1,
                col=cursor_position.col,
            ),
        )

    # 【功能】光标右移一格（对应控制码 CURSOR_FORWARD / ANSI CSI C）。
    # 【算法】在 Python 侧手工实现换行：若当前列已是最后一列（screen_size.col - 1），
    #   则行 +1、列归 0；否则列 +1。然后调用 SetConsoleCursorPosition。
    # 【为什么】必须自己写这段逻辑：Win32 的 SetConsoleCursorPosition 不会“越过行尾
    #   自动换行”，而终端里的 CSI C 在行尾会换到下一行行首，两者语义必须在此对齐。
    # 【易错】判断边界用的是 screen_size.col - 1（最后一列的 0 基下标），
    #   写成 screen_size.col 会导致多走一格、换行位置整体偏移。
    # 【注意】行号可能被加到缓冲区之外，此时调用失败且被忽略。
    def move_cursor_forward(self) -> None:
        """Move the cursor forward a single cell. Wrap to the next line if required."""
        row, col = self.cursor_position
        if col == self.screen_size.col - 1:
            row += 1
            col = 0
        else:
            col += 1
        SetConsoleCursorPosition(
            self._handle, coords=WindowsCoordinates(row=row, col=col)
        )

    # 【功能】把光标移动到同一行内的指定列（对应控制码 CURSOR_MOVE_TO_COLUMN / ANSI CSI G）。
    # 【算法】只读当前行号，列取参数，组成坐标后调用 SetConsoleCursorPosition。
    # 【参数】column：目标列号，**0 基**（调用方 _windows_renderer 已经做过 -1 转换）。
    # 【注意】列号越界不会报错，Win32 调用失败被忽略；行号保持不变，因此不会触发换行。
    def move_cursor_to_column(self, column: int) -> None:
        """Move cursor to the column specified by the zero-based column index, staying on the same row

        Args:
            column (int): The zero-based column index to move the cursor to.
        """
        row, _ = self.cursor_position
        SetConsoleCursorPosition(self._handle, coords=WindowsCoordinates(row, column))

    # 【功能】光标左移一格（对应控制码 CURSOR_BACKWARD / ANSI CSI D）。
    # 【算法】与右移对称的手工回绕：若已在第 0 列，则行 -1、列跳到最后一列；
    #   否则列 -1。
    # 【兼容】这是为 Live/进度条这类“原地重绘”场景服务的关键操作：Rich 需要把光标
    #   移回上一次输出的起点，而在不支持 VT 的老控制台上只能靠逐格移动实现。
    # 【易错】回绕后的列是 screen_size.col - 1（最后一列），不是 screen_size.col。
    # 【注意】行 -1 到负数时 Win32 调用失败并被忽略（光标停在原处）。
    def move_cursor_backward(self) -> None:
        """Move the cursor backward a single cell. Wrap to the previous line if required."""
        row, col = self.cursor_position
        if col == 0:
            row -= 1
            col = self.screen_size.col - 1
        else:
            col -= 1
        SetConsoleCursorPosition(
            self._handle, coords=WindowsCoordinates(row=row, col=col)
        )

    # 【功能】隐藏光标（对应控制码 HIDE_CURSOR / ANSI CSI ?25l）。
    # 【算法】先读回当前光标厚度，构造“厚度不变、bVisible=0”的结构体，再写回。
    # 【为什么】Win32 没有单独的隐藏接口：SetConsoleCursorInfo 要求整块结构体，
    #   若把 dwSize 留 0，光标形状（下划线/方块）会被破坏，所以必须先读再改。
    # 【易错】bVisible 用的是整数 0/1（wintypes.BOOL），不要写成 None。
    # 【兼容】POSIX 上是无状态的一条转义序列，不需要读当前状态。
    def hide_cursor(self) -> None:
        """Hide the cursor"""
        # 【要点】保留原厚度，只关可见性。
        current_cursor_size = self._get_cursor_size()
        invisible_cursor = CONSOLE_CURSOR_INFO(dwSize=current_cursor_size, bVisible=0)
        SetConsoleCursorInfo(self._handle, cursor_info=invisible_cursor)

    # 【功能】显示光标（对应控制码 SHOW_CURSOR / ANSI CSI ?25h）。
    # 【算法】与 hide_cursor 完全对称，只把 bVisible 置 1。
    # 【注意】它不会恢复“隐藏之前”的形状以外的任何状态，因为厚度本来就被保留了。
    # 【协作】legacy 渲染路径在渲染结束（Console 的收尾逻辑）中调用它，
    #   确保进度条/动画结束后光标不会一直消失。
    def show_cursor(self) -> None:
        """Show the cursor"""
        # 【要点】同样先读回厚度再改写可见性。
        current_cursor_size = self._get_cursor_size()
        visible_cursor = CONSOLE_CURSOR_INFO(dwSize=current_cursor_size, bVisible=1)
        SetConsoleCursorInfo(self._handle, cursor_info=visible_cursor)

    # 【功能】设置控制台窗口标题（对应控制码 SET_WINDOW_TITLE / OSC 0）。
    # 【参数】title：新标题；非 ASCII 字符由 ctypes 按 UTF-16 传递，可以正常显示中文。
    # 【约束】断言标题长度小于 255：这是 SetConsoleTitle 的实际上限，超出会被截断或失败。
    # 【易错】断言在 `python -O` 下会被移除，因此不能把它当成安全边界。
    def set_title(self, title: str) -> None:
        """Set the title of the terminal window

        Args:
            title (str): The new title of the console window
        """
        # 【约束】255 是 Win32 文档给出的标题长度上限。
        assert len(title) < 255, "Console title must be less than 255 characters"
        SetConsoleTitle(title)

    # 【功能】读取当前光标厚度（占单元格高度的百分比）。
    # 【算法】构造一个空的 CONSOLE_CURSOR_INFO 交给 GetConsoleCursorInfo 填充，
    #   再把 dwSize 转成 int 返回。
    # 【返回】1-100 的百分比整数；读取失败时结构体保持 0，因此会返回 0。
    # 【注意】这是本类唯一一个“先构造再读字段”的辅助方法，因为
    #   GetConsoleCursorInfo 把结果写在出参里而不是返回值里。
    # 【易错】若这里返回 0（句柄无效），随后构造的 cursor_info 会把光标厚度设为 0，
    #   系统会把它夹到最小值——也就是说失败时未必完全无副作用。
    def _get_cursor_size(self) -> int:
        """Get the percentage of the character cell that is filled by the cursor"""
        # 【要点】空结构体作为出参容器；成功后 dwSize/bVisible 被就地填充。
        cursor_info = CONSOLE_CURSOR_INFO()
        GetConsoleCursorInfo(self._handle, cursor_info=cursor_info)
        # 【易错】显式 int() 转换：wintypes.DWORD 字段读出来是 Python int，但保留转换
        # 能让类型检查器满意，也更明确地表达“这是普通整数”。
        return int(cursor_info.dwSize)


# ==== 【模块自测入口：手工冒烟测试】 ====
# 【功能】直接 `python -m rich._win32_console` 时运行一段交互式演示：依次检查
#   颜色输出、光标移动、行擦除三类能力，每步之间用 time.sleep 让肉眼能看清。
# 【为什么】legacy Windows 后端的行为（尤其是光标回绕与擦除范围）很难用断言验证，
#   作者选择用“人工观察 + 睡眠”的方式做冒烟测试；因此它不是自动化测试的一部分。
# 【注意】本段依赖真实控制台：在重定向输出或非 Windows 上运行会失败；
#   它也不属于库的公开 API，正式使用不应导入这段代码中的任何名字。
# 【提示】对照它读 LegacyWindowsTerm 的方法最直观：注释里写“检查光标移动”的地方
#   就是 move_cursor_* 的调用序列。
if __name__ == "__main__":
    # 【要点】先取一次句柄，验证模块级绑定在本机可用（失败会立刻暴露）。
    handle = GetStdHandle()

    from rich.console import Console

    console = Console()

    # 【要点】直接用真实的 sys.stdout 构造 term，与 Console 的输出目标保持一致。
    term = LegacyWindowsTerm(sys.stdout)
    term.set_title("Win32 Console Examples")

    # 【要点】准备两个样式，用来观察“属性 → 颜色”的映射是否正确。
    style = Style(color="black", bgcolor="red")

    heading = Style.parse("black on green")

    # 【要点】这一组用例覆盖：纯前景、纯背景、亮色（bright_*）、
    # dim+bright 的组合（验证亮度位被清除）、italic（验证不支持的属性被忽略）、
    # bold 与 reverse、以及真彩色（验证 downgrade 到 16 色）。
    # Check colour output
    console.rule("Checking colour output")
    console.print("[on red]on red!")
    console.print("[blue]blue!")
    console.print("[yellow]yellow!")
    console.print("[bold yellow]bold yellow!")
    console.print("[bright_yellow]bright_yellow!")
    console.print("[dim bright_yellow]dim bright_yellow!")
    console.print("[italic cyan]italic cyan!")
    console.print("[bold white on blue]bold white on blue!")
    console.print("[reverse bold white on blue]reverse bold white on blue!")
    console.print("[bold black on cyan]bold black on cyan!")
    console.print("[black on green]black on green!")
    console.print("[blue on green]blue on green!")
    console.print("[white on black]white on black!")
    console.print("[black on white]black on white!")
    console.print("[#1BB152 on #DA812D]#1BB152 on #DA812D!")

    # 【要点】以下连续两次 backward + 一次 up，演示“回绕到上一行”与“只动一格”的区别；
    # 每次 sleep 都是为了让观察者看到光标当前所在处。
    # Check cursor movement
    console.rule("Checking cursor movement")
    console.print()
    term.move_cursor_backward()
    term.move_cursor_backward()
    term.write_text("went back and wrapped to prev line")
    time.sleep(1)
    term.move_cursor_up()
    term.write_text("we go up")
    time.sleep(1)
    term.move_cursor_down()
    term.write_text("and down")
    time.sleep(1)
    term.move_cursor_up()
    term.move_cursor_backward()
    term.move_cursor_backward()
    term.write_text("we went up and back 2")
    time.sleep(1)
    term.move_cursor_down()
    term.move_cursor_backward()
    term.move_cursor_backward()
    term.write_text("we went down and back 2")
    time.sleep(1)

    # 【要点】这里故意把光标移到第 16 列再画一个 "<" 作为参照物，
    # 用来验证 erase_start_of_line / erase_end_of_line 的擦除范围是否与箭头方向一致。
    # Check erasing of lines
    term.hide_cursor()
    console.print()
    console.rule("Checking line erasing")
    console.print("\n...Deleting to the start of the line...")
    term.write_text("The red arrow shows the cursor location, and direction of erase")
    time.sleep(1)
    term.move_cursor_to_column(16)
    term.write_styled("<", Style.parse("black on red"))
    term.move_cursor_backward()
    time.sleep(1)
    term.erase_start_of_line()
    time.sleep(1)

    console.print("\n\n...And to the end of the line...")
    term.write_text("The red arrow shows the cursor location, and direction of erase")
    time.sleep(1)

    term.move_cursor_to_column(16)
    term.write_styled(">", Style.parse("black on red"))
    time.sleep(1)
    term.erase_end_of_line()
    time.sleep(1)

    console.print("\n\n...Now the whole line will be erased...")
    term.write_styled("I'm going to disappear!", style=Style.parse("black on cyan"))
    time.sleep(1)
    term.erase_line()

    term.show_cursor()
    print("\n")
