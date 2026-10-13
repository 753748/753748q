# 【说明】本块导入顺序与 Rich 的模块分层一致：标准库 → rich 顶层公共模块 → 同包子模块。
# 【要点】threading 与 os/sys 是 Console 做线程安全与终端探测的基础；
#   dataclasses 只用来标记 ConsoleOptions/ConsoleThreadLocals 这类纯数据载体；
#   itertools.islice 用于"只取前 N 行"（高度限制）而不物化全部渲染结果。
import os
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
# 【要点】wraps 只服务于 @group 装饰器：保留被装饰方法的 __name__/__doc__，
#   否则 Rich 内部的调试输出与文档生成会看到一堆同名的 _replace。
from functools import wraps
from itertools import islice
from math import ceil
from time import monotonic
from types import FrameType, ModuleType, TracebackType
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    TextIO,
    Tuple,
    Type,
    Union,
    cast,
    runtime_checkable,
)
# 【要点】用绝对导入而不是相对导入，是因为 _null_file 属于更底层的公共工具，
#   Rich 里有多个包（如 rich.console 与 textual 复用）需要按同一路径引用它。
# NULL_FILE 是"黑洞文件"：file 属性在极端情况下（sys.stdout 被置 None）的兜底目标，
#   保证 render_lines/_write_buffer 不必到处判空。
# 【易错】这一行用了绝对导入（rich._null_file），与下文大量相对导入（.xxx）混用，
#   是历史原因：_null_file 曾被设计为可独立于 rich 包使用，所以沿用了顶层路径写法。

from rich._null_file import NULL_FILE

from . import errors, themes
from ._emoji_replace import _emoji_replace
# 【要点】LogRender 把"时间 + 路径 + 行号 + 内容"排版成一个整体 renderable，
#   Console.log 只负责收集数据；把排版逻辑抽出去可以让 log 的样式统一且可测试。
from ._export_format import CONSOLE_HTML_FORMAT, CONSOLE_SVG_FORMAT
from ._fileno import get_fileno
# 【要点】ColorSystem 是"终端能力"的枚举（STANDARD/EIGHT_BIT/TRUECOLOR/WINDOWS）；
#   blend_rgb 只在 export_svg 里用，用来实现 dim（SVG 没有变暗属性）。
from ._log_render import FormatTimeCallable, LogRender
from .align import Align, AlignMethod
# 【要点】EmojiVariant 是 "text"/"emoji" 二选一的字面量类型：
#   同一个 emoji 代码（如 :smile:）在两种变体下会渲染成不同码位，
#   因此 emoji_variant 需要一路传到 _emoji_replace（见 render_str）。
from .color import ColorSystem, blend_rgb
from .control import Control
from .emoji import EmojiVariant
# 【要点】Measurement 是宽度协商的载体；measure_renderables 在 Group 里取各子项最大值。
#   注意测量与渲染是两条独立路径，容器需要两者配合才能正确分配宽度。
from .highlighter import NullHighlighter, ReprHighlighter
from .markup import render as render_markup
from .measure import Measurement, measure_renderables
from .pager import Pager, SystemPager
from .protocol import rich_cast
from .region import Region
from .screen import Screen
from .segment import Segment
# 【要点】两个默认主题分别服务 HTML（浅色，DEFAULT_TERMINAL_THEME）与 SVG（深色，
#   SVG_EXPORT_THEME）导出；TerminalTheme 提供 fore/back 的 RGB 与颜色映射表。
from .style import Style, StyleType
from .styled import Styled
from .terminal_theme import DEFAULT_TERMINAL_THEME, SVG_EXPORT_THEME, TerminalTheme
# ---------------------------------------------------------------------------
# 【模块定位】本模块是 Rich 的"渲染出口"：所有最终要变成终端文本的东西，都要经由
#   Console。它向上承接 print/log/out/input 等用户级 API，向下持有 file 这一写入
#   目标；中间把任意对象（str、Text、实现了 __rich_console__ 的 renderable）统一
#   渲染成 Segment 序列（文本 + 样式 + 控制码），再按终端能力把样式降级为 ANSI 转义
#   序列或纯文本，最后一次性写文件并 flush。
#   它委派出去的事情：测量交给 measure.Measurement（width 决策）、换行/裁剪交给
#   segment.Segment（split_and_crop_lines 等方法）、颜色能力探测交给 _windows，
#   Windows 老控制台的真实写入交给 _win32_console + _windows_renderer。
#   上下文管理器（Capture/ThemeContext/PagerContext/ScreenContext 以及 Console 自身）
#   全部围绕"缓冲区深度 _buffer_index"这一机制工作：嵌套期间先攒 Segment，退出最外层
#   时才真正写出去。
# 【关键数据结构】
#   ConsoleDimensions(NamedTuple)：终端尺寸，width 单位是"单元格/字符列"，height 单位是行。
#   ConsoleOptions(dataclass)：一次渲染的约束集合（宽度区间、是否终端、编码、justify、
#     overflow、no_wrap、highlight、markup、height）。约定不可变：任何修改都返回副本，
#     改宽必须走 update/update_width/update_dimensions，不要原地赋值，否则会污染调用方。
#   Segment：渲染的最小单元（text, style, control）。本模块几乎所有内部数据流都是它的序列。
#   RenderHook(ABC)：插在"收集完 renderables 之后、真正渲染之前"的钩子，可以替换这个列表
#     （rich.live 等用它把一次性渲染接进刷新循环）。
#   ThemeStack（线程局部）：每个线程一份的主题栈。样式名先查主题再按字面量解析，
#     因此 get_style("bold red") 与 get_style("my.style") 都合法。
#   _record_buffer：record=True 时记录下来的全部 Segment，是 export_text/html/svg 的唯一数据来源。
#   _buffer（线程局部）+ _buffer_index：待写出的 Segment 与缓冲嵌套深度；>0 表示还在缓冲。
# 【设计要点】
#   1. 一切经过 Segment：print/render 都只产出 Segment，样式渲染（ANSI 编码）推迟到
#      _render_buffer 一次性完成，因此 Capture、Pager、Jupyter、record 可以复用同一份渲染结果。
#   2. 能力探测与颜色降级：is_terminal 由 force_terminal → Idle/Jupyter → TTY_COMPATIBLE →
#      FORCE_COLOR → file.isatty() 依次判定；color_system="auto" 时再据此降到 truecolor /
#      256 色 / standard，非终端直接 None（不输出任何样式）；NO_COLOR 环境变量在最后一道
#      环节剥掉颜色，保持"能力探测"与"用户意愿"解耦。
#   3. 换行与裁剪：普通渲染由 Segment.split_and_crop_lines 按 max_width 切成行并按需补齐；
#      软换行（soft_wrap）则反过来，强制 no_wrap=True、overflow="ignore"、crop=False，
#      把断行权完全交给终端——所以开启后 print 出来的行可能超过 Console.width，
#      并以 \n 原样落入缓冲区，这是一个会让调用方意外的语义。
#   4. Windows 单独一条路：老式 conhost 不理解 ANSI，需要按单元格调用 win32 API 写字符与属性；
#      老控制台宽度还要减 1（避免自动换行造成多出一行）。这些分支用 WINDOWS 常量在导入时
#      一次性判定，运行时不再判断平台。
#   5. 线程安全：写文件、录制、主题栈与缓冲区各由 RLock 或 threading.local 保护；
#      Console 可以在多线程里共享，但缓冲区与主题栈是每线程独立的。
#   6. 性能取向：热路径上刻意避免重复构造对象（render 局部变量缓存、Segment.text 直接拼接、
#      style_cache、MAX_WRITE 批量写），下文以【优化】标出。
# 【阅读建议】建议按下列区间顺序读：
#   1-56 导入与依赖（看 Rich 的模块分层）；58-99 常量与标准流文件描述符探测；
#   102-242 渲染配置：ConsoleDimensions 与 ConsoleOptions（update/copy 的不可变约定）；
#   245-306 渲染协议 RichCast/ConsoleRenderable 与最小的换行/屏幕更新 renderable；
#   309-501 Capture / ThemeContext / PagerContext / ScreenContext / Group 与 @group；
#   504-577 运行环境探测：_is_jupyter、颜色系统表、线程局部、RenderHook、Windows 能力；
#   580-986 Console.__init__ 与属性：能力探测、宽度求解、主题栈、is_terminal 判定链；
#   988-1096 options/size/width/height 的读写；
#   1091-1260 终端控制类副作用 API（bell/clear/status/cursor/alt screen/window title）；
#   1276-1467 渲染核心：render → render_lines → render_str（对象如何变成 Segment）；
#   1469-1607 样式解析与 renderables 收集：get_style、_collect_renderables、rule；
#   1609-1752 打印路径：control/out/print（软换行、justify、裁剪都在这里落定）；
#   1754-2024 print_json / update_screen / print_exception / _caller_frame_info / log；
#   2026-2150 写出缓冲：_check_buffer / _write_buffer / _render_buffer（编码与 Windows 分叉）；
#   2152-2439 input 与导出：export_text/save_text/export_html/save_html；
#   2342-2632 export_svg/save_svg（把 Segment 直接画成矢量图）；
#   2635-2688 if __name__ == "__main__" 自测；阅读时可回看 1499-1586 理解 print 与 log 的共用收集逻辑。
# ---------------------------------------------------------------------------
from .text import Text, TextType
from .theme import Theme, ThemeStack

if TYPE_CHECKING:
# 【要点】115×100 是 Rich 观察到的 Jupyter 前端典型可视区域（不是精确值）：
#   用作无法从 JUPYTER_COLUMNS/JUPYTER_LINES 读到值时（老版本 notebook）的兜底。
# 【为什么不用 80】notebook 的可视宽度远大于传统终端，用 80 会让输出频繁折行。
# 【约束】这两个常量只在"检测到 Jupyter"且环境变量缺失时生效，
#   普通终端与显式指定尺寸的场景都不会用到它们。
    from ._windows import WindowsConsoleFeatures
    from .live import Live
# 【兼容】平台判定在导入期做一次并作为模块常量：
#   1) 避免每次写缓冲都判断平台；2) 便于测试通过打补丁模拟 Windows 行为。
# 【注意】这里只判断"是不是 Windows"，不判断"是不是老控制台"（后者要运行时探测，
#   见 detect_legacy_windows）。
    from .status import Status
# 【要点】三个类型别名把"渲染策略"变成可插拔的协议：
#   HighlighterType：字符串/Text 进、Text 出（详见 render_str 的用法）；
#   JustifyMethod：default 表示交给 renderable 自己决定，full 表示两端对齐；
#   OverflowMethod：超出宽度时的四种策略——fold 折到下一行、crop 直接截断、
#     ellipsis 截断并加省略号、ignore 完全不管（任由终端折行）。

JUPYTER_DEFAULT_COLUMNS = 115
JUPYTER_DEFAULT_LINES = 100
# screen/bell/clear 等方法都遵循同一模式：先判断环境是否支持，再通过 control() 发控制码。
# 它们不返回内容，只改变终端状态，因此在非终端环境下必须能安全地"什么都不做"。
# 这些方法都是 print 的"特化包装"：先构造一个合适的 renderable（JSON、Traceback、
# LogRender），再复用 print 的收集与写出流程。
# ==== 【哨兵类型与常量】 ====
# 【功能】一个"什么都没有变"的标记类型，仅作为 NO_CHANGE 的载体存在。
# 【为什么】ConsoleOptions.update 的默认值是 NO_CHANGE 而不是 None：很多字段本身就以 None
#   表示"显式取消"（例如 height=None 表示高度不受限、justify=None 表示沿用外层），
#   因此必须用一个与所有合法取值都不同的哨兵，才能区分"参数没传"和"显式传 None"。
# 【易错】判定方式必须是 isinstance(x, NoChange) 身份/类型判断，不能改成 == 比较；
#   也不要用 None 当哨兵。
WINDOWS = sys.platform == "win32"

# _check_buffer/_write_buffer 是"缓冲区 → 终端"的最后一段；input 则是反向的读取入口。
# 读这一段时要始终记得：真正决定"写不写"的是 _buffer_index。
# export_text/export_html/export_svg 三者共享同一份 _record_buffer，差别只在"如何表达样式"：
# 文本用 ANSI、HTML 用 CSS class 或内联 style、SVG 用 CSS + 几何图形。
# 它们都要求构造时 record=True，并且默认导出后清空缓冲区（clear=True）。
# 全局唯一的哨兵实例，所有 ConsoleOptions.update 调用默认都用它。
# ==== 【常量与标准流探测】 ====
HighlighterType = Callable[[Union[str, "Text"]], "Text"]
# 这一段是全文件最长也最"图形化"的部分：不再拼接文本，而是按单元格算坐标，
# 用 <rect> 画背景、<text> 画文字、<clipPath> 限界，最后套进一个终端窗口外观的模板。
JustifyMethod = Literal["default", "left", "center", "right", "full"]
# 【易错】三个 try 块结构完全相同但兜底值分别是 0/1/2（按 POSIX 惯例），
#   不要以为是复制粘贴错误：这是为了让后面的 get_terminal_size 至少能试一次标准 fd。
OverflowMethod = Literal["fold", "crop", "ellipsis", "ignore"]


# 【职责】“未提供该参数”的哨兵类型，用来区分“参数没传”与“参数传了 None”。
# 【关键属性】无实例状态（类体只有 pass）。
# 【不变量】模块级单例 NO_CHANGE 是唯一被使用的实例；判断一律用 isinstance(x, NoChange)，
# 因此不要用等值比较，也不要另建实例来表达“未提供”。
# 【协作】ConsoleOptions.update 与 Console.print 的 width/height/justify 等可选参数默认值。
class NoChange:
    pass


NO_CHANGE = NoChange()

try:
    _STDIN_FILENO = sys.__stdin__.fileno()  # type: ignore[union-attr]
# 下面这段只在直接运行时执行，用 record=True 演示 log/print 对复杂结构的渲染效果；
# 它同时是"最简用法示例"：Console(record=True) → 输出 → 观察录制结果。
# 【注意】因为 record=True，运行它会累积 _record_buffer；演示结束后可调用 export_* 查看。
# 三个标准流描述符的集合，供尺寸探测（POSIX 下三者都试）使用。
except Exception:
# 只包含输出的两个：Windows 上从 stdin 取尺寸容易拿到不可靠的值，故限制在 stdout/stderr。
    _STDIN_FILENO = 0
# 【功能】把 TERM 变量里"最后一个连字符之后"的片段映射到颜色能力。
# 【算法】_detect_color_system 用 term.rpartition("-") 取 colors 段，再查这张表；如
#   "xterm-256color" → "256color" → EIGHT_BIT，"screen-kitty" → "kitty" → EIGHT_BIT。
# 【注意】表里没有的取值（例如 "xterm"）默认落到 ColorSystem.STANDARD，这是保守策略：
#   宁可少给颜色，也不要往不认识的终端里灌它解释不了的转义序列。
# 【约束】只处理 TERM，不处理 COLORTERM；后者在检测函数里单独优先判断。
try:
    _STDOUT_FILENO = sys.__stdout__.fileno()  # type: ignore[union-attr]
except Exception:
    _STDOUT_FILENO = 1
try:
# 【职责】终端尺寸的只读数据载体，把"宽（列/单元格）"和"高（行）"打包成一个可解构的值。
# 【关键属性】width：以单元格计的宽度（不是字节数，宽字符按 2 格算，由 cell_len 负责）；
#   height：行数。
# 【不变量】两个字段都是正整数；探测不到时由 Console.size 兜底为 80×25，绝不会出现 None。
# 【为什么用 NamedTuple】既支持 dims.width 也支持 width, height = dims 解构，且不可变——
#   尺寸对象会被 ConsoleOptions 共享，做成可变对象容易产生别名副作用。
# 【协作】由 Console.size 构造，被 ConsoleOptions.size/max_height 使用，被 Table/Panel 等读取。
    _STDERR_FILENO = sys.__stderr__.fileno()  # type: ignore[union-attr]
except Exception:
# 【要点】NamedTuple 允许字段下方再写字符串字面量作为该字段的"文档"——
#   它不会成为 __doc__，只是给类型检查器与阅读者看的说明。
    _STDERR_FILENO = 2

_STD_STREAMS = (_STDIN_FILENO, _STDOUT_FILENO, _STDERR_FILENO)
_STD_STREAMS_OUTPUT = (_STDOUT_FILENO, _STDERR_FILENO)
# ==== 【渲染选项 ConsoleOptions】 ====
# 【职责】描述"这一次渲染允许做什么"：可用宽度区间、是否面向终端、编码、对齐方式、
#   溢出策略、是否换行、是否高亮/markup、高度限制。它是 renderable 与 Console 之间的
#   唯一契约对象（__rich_console__(console, options) 的第二个参数）。
# 【关键属性】
#   size：终端整体尺寸（长期不变）；max_height 是容器可用的总高度；
#     height 是"当前这一层渲染剩余的高度预算"（None 表示不限）。
#   min_width/max_width：可用宽度的下界与上界；update(width=...) 会把两者同时设为同一个值。
#   legacy_windows：老式 conhost，宽度语义要减 1、颜色只能走 win32 API。
#   is_terminal / encoding：决定能不能用颜色（ascii_only 由 encoding 是否以 utf 开头推出，
#     用来决定 emoji 与制表字符要不要退化成 ASCII）。
#   justify / overflow / no_wrap / highlight / markup：本层的样式与换行覆盖值，None 表示沿用上层。
# 【不变量】
#   1. min_width <= max_width（update(width=w) 会同时赋值，天然满足）；
#   2. 宽度相关字段恒 >= 0（构造时统一 max(0, width) 夹紧）；
#   3. 对象逻辑上不可变：所有 update*/reset_height 都返回新实例，调用方不得原地改字段；
#   4. copy() 是浅拷贝，size 等值类型字段共享也无妨，但不要在副本上改可变子对象。
# 【协作】Console.options 生成根对象 → print/measure/render 用 update* 派生新对象逐层收窄
#   → 传给 renderable.__rich_console__。Table/Panel 等容器靠它给子 renderable 分宽度。
# 【职责】threading.local 子类：为每个线程保存一份主题栈与输出缓冲区。
# 【关键属性】theme_stack：该线程的主题栈（跨线程不共享主题，避免并发下样式串味）；
#   buffer：待写出的 Segment 列表；buffer_index：缓冲嵌套深度。
# 【不变量】buffer_index 只能通过 _enter_buffer/_exit_buffer 成对增减；
#   只有 buffer_index == 0 时才允许真正写文件。
# 【为什么用 threading.local】Console 常被当作全局单例共享，若缓冲区是实例级字段，
#   两个线程同时 print 时 Segment 会交错。
# 【注意】线程局部意味着"在某线程 begin_capture，到另一线程 end_capture"无法工作。


_TERM_COLORS = {
    "kitty": ColorSystem.EIGHT_BIT,
    "256color": ColorSystem.EIGHT_BIT,
    "16color": ColorSystem.STANDARD,
}


# ==== 【尺寸与选项数据结构】 ====
class ConsoleDimensions(NamedTuple):
    """Size of the terminal."""

    width: int
    """The width of the console in 'cells'."""
    height: int
    """The height of the console in lines."""


# 【职责】渲染时向下传递的“可用空间 + 渲染策略”上下文快照。
# 【关键属性】size（终端尺寸）、min_width/max_width（宽度约束）、justify/overflow（对齐与溢出策略）、no_wrap、highlight、markup、height；legacy_windows 与 is_terminal 影响颜色与字形选择。
# 【不变量】不可变对象：所有 update* 方法都返回新实例，调用方可以安全地收窄宽度而不影响兄弟节点；min_width <= max_width。
# 【协作】由 Console.options 生成，逐层传给 __rich_console__；Panel/Table 等组件用 update_width 收窄后再渲染内部对象。
@dataclass
class ConsoleOptions:
    """Options for __rich_console__ method."""

    size: ConsoleDimensions
    """Size of console."""
    legacy_windows: bool
    """legacy_windows: flag for legacy windows."""
    min_width: int
    """Minimum width of renderable."""
# 【功能】判断当前输出目标是否只能承受 ASCII（只读属性）。
# 【算法】看 encoding 是否以 "utf" 开头（utf-8/utf-16/utf_8 都算），不是则视为非 Unicode 环境。
# 【注意】这里只做字符串前缀判断，不探测实际能否编码某个字符；因此 GBK 之类的多字节编码
#   也会被判为 ascii_only=True，从而让 Box/Emoji 退化为 ASCII 画法，避免写文件时抛
#   UnicodeEncodeError（真抛了的话 _write_buffer 只会补一句 PYTHONIOENCODING 提示）。
# 【功能】把构造时的元组惰性物化成列表并缓存。
# 【为什么惰性】Group 常在模块导入期创建（占位），真正渲染才需要列表；
#   这样对象创建更快，也允许子项延迟构造。
# 【功能】返回实际写入目标（stdout/stderr/自定义流），并做 NULL_FILE 兜底。
# 【算法】1) 优先 _file；否则按 stderr 开关取 sys.stderr/sys.stdout；
#   2) getattr(file, "rich_proxied_file", file)：如果 file 是 Rich 的代理（如 Live 的
#      重定向对象），取它背后的真实文件——宽度/编码探测应该看真身；
#   3) 极端情况（sys.stdout 被设为 None）退回 NULL_FILE，保证后续 write 不炸。
# 【返回】IO[str]；永远返回一个可用对象，不会返回 None。
# 【协作者】FileProxy 来自 rich.file_proxy，这里只做单向解包，不反向写入。
# ==== 【一、默认渲染选项与尺寸】 ====
# ---- 【能力查询属性组：color_system / encoding / is_terminal / is_dumb_terminal】 ----
# 这四个属性共同回答"这台终端能显示什么"：先看颜色与编码能力，再判断是不是真终端。
# 注意它们是属性而非构造期定值：file 可以在运行中被替换，因此每次访问都会重新求值。
    max_width: int
    """Maximum width of renderable."""
    is_terminal: bool
    """True if the target is a terminal, otherwise False."""
# 【功能】返回一个浅拷贝。
# 【算法】绕过 __init__，先用 __new__ 造空壳，再整体替换 __dict__，比逐字段拷贝更快，
#   也自动兼容将来新增的字段。
# 【为什么】ConsoleOptions 是热路径上的高频对象；dataclasses.replace() 会重新走 __init__
#   做参数校验，开销更大。这里假定实例只有普通字段（无 __slots__）。
# 【注意】拷贝出的 __dict__ 是新的，但里面的对象（如 size）是共享的；不要改它们。
    encoding: str
    """Encoding of terminal."""
    max_height: int
    """Height of container (starts as terminal)"""
# 【返回】bool。下面是完整判定链（自上而下，首个命中即返回）：
#   1) 用户强制值；2) IDLE 特例；3) Jupyter 特例；4) TTY_COMPATIBLE；
#   5) FORCE_COLOR；6) file.isatty()（异常时 False）。
# 【易错】第 3 步在 Jupyter 里即使 FORCE_COLOR 已设置也返回 False，
#   但第 1 步的 force_terminal=True 仍然能覆盖它。
    justify: Optional[JustifyMethod] = None
    """Justify value override for renderable."""
    overflow: Optional[OverflowMethod] = None
    """Overflow value override for renderable."""
# 【功能】替换当前屏幕内容（或只替换样式）并立即重画。
# 【参数】renderables：0 个表示不改内容；1 个直接作为内容；多个用 Group 包起来；
#   style：None 表示不改样式（与 renderables 的"可留空"语义一致）。
# 【算法】end="" 保证重画不会额外换行——备用屏幕里靠光标绝对定位，多一个 \n 会把画面顶上去。
# 【注意】每次 update 都会重新渲染整个 Screen，因此大画面下应控制刷新频率。
    no_wrap: Optional[bool] = False
    """Disable wrapping for text."""
    highlight: Optional[bool] = None
    """Highlight override for render_str."""
    markup: Optional[bool] = None
    """Enable markup when rendering strings."""
    height: Optional[int] = None

    # 【功能】判断本次渲染是否必须只使用 ASCII 字符。
    # 【返回】bool：编码不含 UTF-8 时（例如 Windows 旧代码页）为 True。
    # 【注意】组件据此避免输出框线字符与 emoji。
    @property
    def ascii_only(self) -> bool:
        """Check if renderables should use ascii only."""
        return not self.encoding.startswith("utf")

# 【功能】按关键字覆盖若干字段，返回新对象（自身保持不变）。
# 【算法】先 copy()，再用 isinstance(x, NoChange) 逐个判断"这次要不要改"；
#   width 是双向约束，一旦给出就把 min_width 与 max_width 同时设成 max(0, width)。
# 【参数】所有参数默认 NO_CHANGE，语义为"不改这一项"；显式传 None 表示"改为 None"。
#   width：可用宽度（同时改 min/max，负数夹到 0）；height：当前层的剩余高度预算。
# 【返回】ConsoleOptions 副本（新实例），因此调用方可以放心把派生结果存起来复用。
# 【注意】height 的处理是个特例：传具体值时同时抬升 max_height（容器总高），
#   传 None 时只把 height 置空而不动 max_height。这样"暂时不限制本层高度"不会连带
#   破坏外层已经算好的总高。
# 【为什么不可变】同一份 options 会同时传给多个子 renderable，就地修改会让后渲染的对象
#   看到前面对象留下的痕迹；返回副本是最省心的隔离方式。
# 【易错】width/justify/overflow 等参数的默认值不能换成 None——否则"不传"与"显式取消"
#   无法区分，这也正是 NoChange 哨兵存在的唯一理由。
    def copy(self) -> "ConsoleOptions":
        # 【要点】max(0, width) 兜住两种危险输入：print(width=-1)、容器分配宽度时没扣边框。
        """Return a copy of the options.
#   宽度一旦为负，split 逻辑会切出空行甚至死循环，所以必须在入口夹紧。

        Returns:
            ConsoleOptions: a copy of self.
        """
        options: ConsoleOptions = ConsoleOptions.__new__(ConsoleOptions)
        options.__dict__ = self.__dict__.copy()
        return options

    # 【功能】按传入的关键字参数生成一份新的 ConsoleOptions。
    # 【参数】width 会同时改写 min_width/max_width 与 size；height 为 None 表示“不限高”而非“保持原值”。
    # 【返回】新的 ConsoleOptions（原对象不变）。
    # 【为什么】不可变更新让嵌套组件可以各自收窄宽度而互不影响，是嵌套布局正确性的前提。
    def update(
        self,
        *,
        width: Union[int, NoChange] = NO_CHANGE,
        min_width: Union[int, NoChange] = NO_CHANGE,
        max_width: Union[int, NoChange] = NO_CHANGE,
        justify: Union[Optional[JustifyMethod], NoChange] = NO_CHANGE,
        overflow: Union[Optional[OverflowMethod], NoChange] = NO_CHANGE,
        no_wrap: Union[Optional[bool], NoChange] = NO_CHANGE,
        highlight: Union[Optional[bool], NoChange] = NO_CHANGE,
        markup: Union[Optional[bool], NoChange] = NO_CHANGE,
        height: Union[Optional[int], NoChange] = NO_CHANGE,
    ) -> "ConsoleOptions":
# 【功能】只改宽度（同时设 min_width 与 max_width），返回副本。
# 【参数】width：新的可用宽度，单位是单元格；负数会被夹到 0。
# 【为什么单独存在】容器按子项宽度渲染时（例如 Table 的列），只需要动宽度；
#   单独的方法比 update(width=...) 意图更清楚，也避免把 justify/overflow 一起带错。
# 【注意】它不动 size：size 描述终端本身，而 min/max_width 描述"这一层可用的空间"。
        """Update values, return a copy."""
        options = self.copy()
        if not isinstance(width, NoChange):
            options.min_width = options.max_width = max(0, width)
        if not isinstance(min_width, NoChange):
            options.min_width = min_width
        if not isinstance(max_width, NoChange):
            options.max_width = max_width
        if not isinstance(justify, NoChange):
            options.justify = justify
# 【功能】同时设置 height 与 max_height，返回副本。
# 【参数】height：剩余高度预算（行数）；注意这里不做负数夹紧，与 update(width=...) 不同。
# 【注意】和 update(height=...) 的差别在于本方法无条件覆盖 max_height，适合"容器已定高"
#   的场景；只想临时限制本层高度请用 update(height=h)。
        if not isinstance(overflow, NoChange):
            options.overflow = overflow
        if not isinstance(no_wrap, NoChange):
            options.no_wrap = no_wrap
        if not isinstance(highlight, NoChange):
            options.highlight = highlight
        if not isinstance(markup, NoChange):
            options.markup = markup
        if not isinstance(height, NoChange):
            if height is not None:
# 【功能】把 height 置为 None（不限制高度），返回副本。
# 【为什么】Console.render 在递归渲染子对象前会调用它：高度预算属于当前这一层，
#   子对象看到的是"整块画布"而不是剩下的几行。这也是 Table 内部能自由换行而不被
#   父容器的剩余高度提前截断的原因。
                options.max_height = height
            options.height = None if height is None else max(0, height)
        return options

    # 【功能】只调整宽度，返回新对象。
    # 【参数】width：新的可用宽度（终端单元格数）。
    # 【返回】新的 ConsoleOptions；同时同步 max_width 与 size.width。
    def update_width(self, width: int) -> "ConsoleOptions":
        # 【功能】一次性改宽（min/max 都设）和高（height/max_height 都设），返回副本。
        # 【参数】width 会夹紧到 >=0；height 不夹紧。
        # 【协作】update_screen 在按 Region 更新屏幕时用它把 options 收窄到目标区域。
        """Update just the width, return a copy.

        Args:
            width (int): New width (sets both min_width and max_width)

        Returns:
            ~ConsoleOptions: New console options instance.
        """
        options = self.copy()
        options.min_width = options.max_width = max(0, width)
        return options

    # 【功能】只调整高度，返回新对象。
    # 【参数】height：新的可用高度；None 表示不限高。
    # 【返回】新的 ConsoleOptions；同时同步 max_height 与 size.height。
    def update_height(self, height: int) -> "ConsoleOptions":
        # 【职责】声明"可以被转换成 renderable 的对象"协议：只要求一个无参的 __rich__。
        # 【协作】rich_cast() 用 isinstance(obj, RichCast) 判定，返回 obj.__rich__()，
        # 【注意】__rich__ 返回的对象会再次走 rich_cast 递归转换，返回值不应是自身，否则死循环。
        # 【易错】@runtime_checkable 只让 isinstance 可用，它检查的是"方法是否存在"，
        # ==== 【渲染协议：对象 → 渲染结果的两种方式】 ====
        """Update the height, and return a copy.

# rich.protocol.rich_cast 会优先调用对象的 __rich__ 把"可转换对象"变成 renderable，
# 再由 Console 调用 __rich_console__ 真正产出内容。两者都是运行时检查的 Protocol，
# 因此第三方类不需要继承任何 Rich 基类，只要实现同名方法即可（结构化类型）。
        Args:
#   于是 dataclass/第三方模型可以只实现一个轻量转换方法，而不必写完整的渲染协议。
#   不检查签名；因此实现了 __rich__ 但有参数的对象也会通过检查，运行时才报错。
            height (int): New height

        Returns:
            ~ConsoleOptions: New Console options instance.
        """
        options = self.copy()
# 与 RichCast 相对的另一半协议：直接产出渲染结果，不再返回 renderable。
        options.max_height = options.height = height
# 【职责】"可直接被 Console 渲染"的协议：__rich_console__(console, options) 产出
#   Segment 或更多 renderable 的迭代器。
# 【参数】console：调用方（renderable 可用它取样式、宽度、写入控制码）；
#   options：本次渲染的约束（宽度/高度/justify/overflow…），不可修改，只能据它派生。
# 【返回】RenderResult，即 Iterable[Union[RenderableType, Segment]]——可以边算边 yield，
#   这正是 Rich 能让大表格只渲染可见部分的基础。
# 【协作】Console.render 判定顺序：rich_cast 之后先用 __rich_console__，字符串则先经
#   render_str 变成 Text 再走同一协议。
        return options

# 【功能】产出一个内容为反斜杠 n 重复 count 次的 Segment。
# 【注意】这里不读 options：换行符数量与宽度无关，所以无需拆分或补齐。
    def reset_height(self) -> "ConsoleOptions":
        # 【要点】RenderableType 是 Console.print 的入参上界：ConsoleRenderable 或 RichCast 或 str。
        """Return a copy of the options with height set to ``None``.

        Returns:
#   注意 Segment 不在其中——Segment 是渲染结果而非渲染输入（render 内部单独识别）。
# A type that may be rendered by Console.
            ~ConsoleOptions: New console options instance.
        """
# 【要点】RenderResult 允许混合 Segment 与 renderable，Console.render 会逐项判断：
#   Segment 直接透传，其他对象递归 render。这种"惰性混合"是避免深度递归爆炸的关键。
# The result of calling a __rich_console__ method.
        options = self.copy()
# 全局共享的空高亮器：单例复用，避免在 _collect_renderables 的热路径上反复构造。
# 注意它与 self.highlighter 的区别：后者在用户传 highlighter=None 时也会退化成这个实例。
# ==== 【可供渲染的最小 renderable 与上下文管理器】 ====
# 【约束】这个单例是全局共享的：NullHighlighter 无状态，所以共享是安全的；
#   如果换成有状态的高亮器，就必须按实例持有。
        options.height = None
# 【职责】capture() 使用姿势错误时抛出的异常（例如上下文还没退出就取结果）。
# 【为什么单独定义】让调用方能精确捕获"用错了 Capture"而不是笼统的 RuntimeError。
        return options

# 【职责】一个极简 renderable，只负责吐出 count 个换行符。
# 【关键属性】count：换行个数（Console.line() 与无参 print() 都靠它）。
# 【不变量】count 由调用方保证 >= 0（Console.line 里有 assert）。
# 【协作】print 在 objects 为空时把 objects 换成 (NewLine(),)，于是 print() 等价于输出一个换行。
    def update_dimensions(self, width: int, height: int) -> "ConsoleOptions":
        # 【功能】逐行生成"移动到 (x, y) → 该行 Segment"的序列。
        # 【算法】把 Control.move_to 存进局部变量后循环调用（【优化】省一次属性查找）；
        # 【注意】不输出换行符：定位完全靠绝对光标移动，因此可以任意顺序重画某几行，
        # 【职责】把 render_lines 得到的"行列表"按给定绝对坐标写回屏幕（配合 alt screen 使用）。
        # 【关键属性】_lines：List[List[Segment]]，每行已按目标宽度切好并补齐；x/y：屏幕左上角坐标。
        # 【不变量】行内的 Segment 不包含换行符；y 偏移按行递增，每行开头先发一次光标移动控制码。
        # 【协作】Console.update_screen_lines 用它生成 Segment，再经 render 写入 _buffer。
        """Update the width and height, and return a copy.

        Args:
#   enumerate(self._lines, self.y) 让行号直接等于屏幕 y 坐标，x 固定为 self.x。
#   这是 Live/Screen 局部刷新的基础。
            width (int): New width (sets both min_width and max_width).
            height (int): New height.

        Returns:
            ~ConsoleOptions: New console options instance.
        """
        options = self.copy()
        options.min_width = options.max_width = max(0, width)
        options.height = options.max_height = height
        return options
# 【功能】原样按顺序产出子 renderable（不做换行、不插入分隔）。
# 【注意】yield from 保持惰性：Group 包装不会造成"先把所有子项算完"的性能损失。


# ==== 【渲染协议与最小 renderable】 ====
@runtime_checkable
class RichCast(Protocol):
    """An object that may be 'cast' to a console renderable."""

    # 【功能】渲染协议的“轻量转换”入口：返回另一个可渲染对象代替自身。
    # 【返回】本对象自身（RichCast 的默认实现即“无需转换”）。
    def __rich__(
        self,
# 【职责】capture() 返回的上下文管理器：进入时开始缓冲，退出时把渲染结果存成字符串。
# 【关键属性】_console：被捕获的 Console；_result：退出后才可用的捕获结果（未退出为 None）。
# 【不变量】get() 必须在 __exit__ 之后调用，否则抛 CaptureError。
# 【协作】begin_capture/end_capture 就是 _enter_buffer/_exit_buffer 的语义化包装。
    ) -> Union["ConsoleRenderable", "RichCast", str]:  # pragma: no cover
        ...


# 【职责】渲染协议的基类（Protocol），声明“可被 Console 渲染”这一能力。
# 【不变量】只要求实现 __rich_console__；Rich 内部用 rich.protocol.is_renderable 做运行时判定，而非 isinstance。
# 【协作】Console.render_lines 按此协议递归展开嵌套可渲染对象。
@runtime_checkable
class ConsoleRenderable(Protocol):
    """An object that supports the console protocol."""

    # 【功能】实现渲染协议，产出该对象对应的 Segment 流。
    # 【参数】console：当前 Console；options：本次渲染的可用空间与策略快照。
    # 【返回】可迭代的 Segment 或嵌套可渲染对象（由 Console 继续展开）。
    def __rich_console__(
# 【功能】进入时递增缓冲区深度，使后续 print 只进 _buffer 不落盘。
# 【返回】self，因此可以写成 with console.capture() as capture: ...
# 【注意】只递增 _buffer_index，不清空既有缓冲区：嵌套 capture 时外层未提交的内容仍会保留。
        self, console: "Console", options: "ConsoleOptions"
    ) -> "RenderResult":  # pragma: no cover
        ...
# 【功能】退出时结束捕获并把结果字符串记录到 self._result。
# 【注意】无论是否发生异常都会执行：print 已经产生的 Segment 会被取走（end_capture 里
#   会 del 缓冲区），因此异常路径下缓冲区不会残留脏数据。


# A type that may be rendered by Console.
RenderableType = Union[ConsoleRenderable, RichCast, str]
"""A string or any object that may be rendered by Rich."""

# The result of calling a __rich_console__ method.
# 【功能】返回捕获到的文本（已含 ANSI 样式，取决于终端能力）。
# 【返回】str；若上下文尚未退出则抛 CaptureError。
# 【易错】捕获内容是按 _render_buffer 渲染过的字符串，也就是说它取决于当前的
#   is_terminal/color_system——想拿到稳定文本请在 Console 上设 force_terminal=False。
RenderResult = Iterable[Union[RenderableType, Segment]]

_null_highlighter = NullHighlighter()


# ==== 【上下文管理器与可组合 renderable】 ====
class CaptureError(Exception):
    """An error in the Capture context manager."""
# 【职责】use_theme() 返回的上下文管理器：临时压入主题，退出时恢复。
# 【关键属性】theme：临时主题；inherit：是否继承当前主题已有样式（False 表示完全替换）。
# 【不变量】push/pop 必须成对；实现上 __exit__ 无条件 pop_theme()，即使块内抛异常也能恢复。
# 【协作】主题栈是线程局部的（ConsoleThreadLocals.theme_stack），多线程互不影响。


class NewLine:
    """A renderable to generate new line(s)"""

    # 【功能】记录要输出的换行个数。
    # 【参数】count：换行个数（即 "\n" 的重复次数），默认 1。
    # 【注意】这里不校验 count；传 0 会得到空字符串，即什么都不输出。
    def __init__(self, count: int = 1) -> None:
# 【功能】压入主题，返回自身。
# 【注意】self.inherit 在 __init__ 里保存，但这里调用 push_theme 时没有传 inherit——
#   实际继承行为由 ThemeStack.push_theme 的默认参数（inherit=True）决定，
#   因此 ThemeContext(inherit=False) 目前不会生效，这是已知的实现与命名不一致之处。
        self.count = count

    # 【功能】实现渲染协议，产出该对象对应的 Segment 流。
    # 【参数】console：当前 Console；options：本次渲染的可用空间与策略快照。
    # 【返回】可迭代的 Segment 或嵌套可渲染对象（由 Console 继续展开）。
    def __rich_console__(
# 【功能】弹出主题，恢复上一层。
# 【注意】不检查主题是否匹配：如果块内又手动 push_theme 了一次且没 pop，栈会失衡。
        self, console: "Console", options: "ConsoleOptions"
    ) -> Iterable[Segment]:
        yield Segment("\n" * self.count)


# 【职责】把若干行内容渲染到屏幕的指定偏移处，用于 Console.update_screen_lines。
# 【关键属性】lines（内容行）、y（目标行号，0 起）。
# 【协作】Console.update_screen / update_screen_lines 构建它并交给 Live 局部刷新使用。
class ScreenUpdate:
    """Render a list of lines at a given offset."""
# 【职责】pager() 返回的上下文管理器：把块内输出交给分页器（less 等）而不是直接写终端。
# 【关键属性】pager：Pager 实现（默认 SystemPager，用 $PAGER）；styles：是否保留样式；
#   links：是否保留超链接。
# 【不变量】进入时 _enter_buffer、退出时 _exit_buffer，保证内部 print 的内容不会提前落盘。
# 【协作】退出时直接取走缓冲区内容（而不是走 _render_buffer 的写文件路径），
#   渲染成字符串后交给 pager.show()。

    def __init__(self, lines: List[List[Segment]], x: int, y: int) -> None:
# 【功能】构造 Console：确定输出目标、终端能力、尺寸、主题与各类默认策略。
# 【参数】（非显然项）
#   color_system："auto" 走探测；None 表示明确不要颜色；其余取 COLOR_SYSTEMS 里的名字。
#   force_terminal：True/False 直接指定是否终端；None 表示自动探测。
#   force_jupyter / force_interactive：同上，用于覆盖环境探测。
#   width/height：显式尺寸（单位为列/行）；不传则依次尝试 Jupyter 变量、COLUMNS/LINES、
#     最后是 os.get_terminal_size。
#   record：记录所有输出以便 export_*；代价是常驻内存。
#   safe_box：老控制台上避免使用可能画错的粗框字符。
#   _environ：仅供测试注入环境变量映射。
# 【返回】None（构造函数）。
# 【注意】构造过程只做"探测与配置"，不写任何输出；能力探测的最终结果要等 file 被赋值后
#   才可能变化——所以 is_terminal 是属性而不是构造期定死的布尔值（除非用户 force）。
        self._lines = lines
        self.x = x
        self.y = y

    # 【功能】实现渲染协议，产出该对象对应的 Segment 流。
    # 【参数】console：当前 Console；options：本次渲染的可用空间与策略快照。
    # 【返回】可迭代的 Segment 或嵌套可渲染对象（由 Console 继续展开）。
    def __rich_console__(
        self, console: "Console", options: ConsoleOptions
    ) -> RenderResult:
        x = self.x
# 【要点】默认用 SystemPager 读环境变量 $PAGER（Windows 上通常是 more）；
#   注入自定义 Pager 可以在测试里把分页内容直接收进内存。
        move_to = Control.move_to
        for offset, line in enumerate(self._lines, self.y):
            yield move_to(x, offset)
            yield from line


# 【功能】块正常结束时把缓冲区内容交给分页器；随后恢复缓冲区深度。
# 【算法】1) 只在 exc_type is None 时收集内容（异常时直接丢弃，避免掩盖异常）；
#   2) 加锁后复制并清空当前线程缓冲区；3) styles/links 决定剥掉多少信息：
#      不保留样式 → Segment.strip_styles；保留样式但不保留链接 → Segment.strip_links；
#   4) _render_buffer 生成最终字符串（此时仍按终端能力着色）；5) pager.show() 显示。
# 【注意】清空只影响"记录点之前"的内容：buffer[:] 复制后 del buffer[:]，因此分页期间
#   新写入的 Segment 不会被重复分页。
# 【易错】即使不显示样式，ANSI 序列也可能来自控制码——_render_buffer 会按 is_terminal
#   决定是否丢弃 control 段。
class Capture:
    """Context manager to capture the result of printing to the console.
    See :meth:`~rich.console.Console.capture` for how to use.

    Args:
        console (Console): A console instance to capture output.
    """
# 【要点】加的是可重入锁：分页期间可能在其它线程继续打印，必须与 _write_buffer 互斥。

    def __init__(self, console: "Console") -> None:
        self._console = console
        self._result: Optional[str] = None
# 【易错】这是二选一的分支：不显示样式时整段剥样式；只有"显示样式但不显示链接"时
#   才走 strip_links。也就是说 links=True 而 styles=False 时链接同样会丢。

    def __enter__(self) -> "Capture":
        self._console.begin_capture()
        return self

    # 【功能】退出上下文管理器：恢复进入前的状态（缓冲、主题、屏幕或文件）。
    # 【返回】False，表示不吞掉异常。
    # 【注意】即使方法体内发生异常也必须恢复状态，因此恢复动作不依赖正常返回路径。
    def __exit__(
        self,
# 【职责】screen() 返回的上下文管理器：进入时切到备用屏幕并可选隐藏光标，退出时还原。
# 【关键属性】hide_cursor：是否隐藏光标；screen：被渲染的 Screen 对象（承载 renderable 与 style）；
#   _changed：本次是否真的切换成功（非终端下 set_alt_screen 返回 False）。
# 【不变量】只有当 _changed 为真时才在退出时关闭备用屏幕，避免影响调用方原本的终端状态。
# 【协作】update() 内部用 console.print(self.screen, end="") 重画整屏，
#   光标隐藏依赖 Control.show_cursor，走的是 Console.control 的缓冲路径。
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self._result = self._console.end_capture()

    # 【功能】取出上下文管理器期间录制的输出。
    # 【返回】录制到的文本（str）。
    # 【注意】必须在退出上下文之后调用，否则缓冲区尚未回填。
    def get(self) -> str:
        """Get the result of the capture."""
        if self._result is None:
            raise CaptureError(
                "Capture result is not available until context manager exits."
            )
        return self._result


# 【职责】临时主题的上下文管理器：进入时压栈、退出时弹栈。
# 【协作】由 Console.use_theme 返回；退出时调用 Console.pop_theme 恢复原主题。
class ThemeContext:
    """A context manager to use a temporary theme. See :meth:`~rich.console.Console.use_theme` for usage."""

    # 【功能】保存目标 Console、主题与继承标志。
    # 【参数】console：要切换主题的 Console；theme：Theme 实例；inherit：是否继承当前样式。
    # 【注意】inherit 被保存但当前实现从未读取（__enter__ 调 push_theme 时未传它），
    # 因此 ThemeContext(inherit=False) 目前不生效——这是已知的实现与命名不一致。
    def __init__(self, console: "Console", theme: Theme, inherit: bool = True) -> None:
        self.console = console
        self.theme = theme
        self.inherit = inherit

    # 【功能】进入上下文管理器，切换到该上下文对应的状态。
    # 【返回】self，便于 ``with ... as`` 绑定。
    # 【注意】进入与退出必须严格配对，否则缓冲索引或主题栈会失衡。
    def __enter__(self) -> "ThemeContext":
        self.console.push_theme(self.theme)
# 【功能】切到备用屏幕（记录是否成功），成功后按需隐藏光标。
# 【兼容】legacy Windows 或非终端下 set_alt_screen 返回 False，此时不会隐藏光标，
#   update() 也就无处可画——这类环境下 Screen 只是一个"什么都没发生"的上下文。
        return self

    # 【功能】退出上下文管理器：恢复进入前的状态（缓冲、主题、屏幕或文件）。
    # 【返回】False，表示不吞掉异常。
    # 【注意】即使方法体内发生异常也必须恢复状态，因此恢复动作不依赖正常返回路径。
    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
# 【功能】还原终端：关掉备用屏幕、恢复光标显示。
# 【注意】还原顺序与进入时相反，且以 _changed 为门槛；异常退出同样会还原，
#   因为 __exit__ 不检查 exc_type。
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.console.pop_theme()


# 【职责】分页器上下文管理器：退出时把录制的内容交给分页程序（如 less）。
# 【注意】若平台没有可用分页器，则退化为直接输出。
# 【协作】Console.pager 使用。
class PagerContext:
    """A context manager that 'pages' content. See :meth:`~rich.console.Console.pager` for usage."""

    # 【功能】记录 Console、分页器实现与两个显示开关。
    # 【参数】pager：Pager 实例，传 None 时现场创建 SystemPager()；styles/links：是否保留样式/超链接。
    # 【注意】分页器在实例化时才创建（不共享全局状态），并会读取 PAGER 环境变量。
    def __init__(
# 【职责】把多个 renderable 当成一个 renderable（Rich 的组合子），逐项 yield 出去。
# 【关键属性】_renderables：构造时传入的元组（先存元组，惰性转列表）；
#   fit：True 时测量取各子项的最大值，False 时直接占满可用宽度；
#   _render：renderables 属性的缓存。
# 【不变量】renderables 返回的列表只构造一次并缓存，之后修改它不会影响其他调用（但也不可依赖）。
# 【协作】Group 本身不做布局、不插入分隔符：垂直堆叠靠调用方或其他容器（如 Columns 内部）
#   处理；它的 __rich_measure__ 决定"组需要多宽"，这是嵌套容器的宽度协商入口。
        self,
        console: "Console",
        pager: Optional[Pager] = None,
        styles: bool = False,
        links: bool = False,
    ) -> None:
        self._console = console
        self.pager = SystemPager() if pager is None else pager
        self.styles = styles
        self.links = links
# 【功能】暴露线程局部的输出缓冲区（_buffer）。
# 【为什么用属性】缓冲区存在 threading.local 上，用属性转发可以让 __init__、
#   _write_buffer、begin/end_capture 等处的 self._buffer 写法保持一致。
# 【注意】返回的是可变列表本身，不是副本：调用方可以就地 append/extend（内部正是这么用的），
#   但外部代码不应长期持有它。
# 【功能】求终端尺寸（宽×高，单位列/行）。
# 【算法】分四步：
#   1) 宽高都被显式指定 → 直接返回，宽再减去 legacy_windows（0/1）；
#   2) dumb 终端 → 返回固定 80×25（不探测，因为探测结果往往不可靠）；
#   3) 遍历标准流调用 os.get_terminal_size，任一成功即 break；Windows 只试输出流；
#   4) 用 COLUMNS/LINES 环境变量覆盖探测结果，最后对 0/None 兜底 80 / 25；
#      返回时若 _width/_height 曾被显式设置，则用显式值覆盖对应分量。
# 【返回】ConsoleDimensions(width, height)。
# 【注意】伪终端下 get_terminal_size 可能返回 (0, 0)，所以必须 width or 80 兜底；
#   legacy_windows 的宽减 1 只在"宽来自探测或环境变量"时生效，显式传入的 _width 不减。
# 【兼容】Windows 上只用 stdout/stderr：把 stdin 的尺寸当输出宽度在某些终端上会不一致。

    def __enter__(self) -> "PagerContext":
        self._console._enter_buffer()
        return self

# 【功能】测量整个组需要的宽度。
# 【算法】fit=True：measure_renderables 取各子项 Measurement 的最大值（内容宽度）；
#   fit=False：返回 (max_width, max_width)，即"给我多少我就占多少"。
# 【参数】options：上层给出的可用宽度，用于把子项测量结果夹到可用范围内。
# 【注意】fit=False 常用于 Panel/Table 这类需要撑满宽度的容器。
    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if exc_type is None:
            with self._console._lock:
                buffer: List[Segment] = self._console._buffer[:]
                del self._console._buffer[:]
                segments: Iterable[Segment] = buffer
# 【功能】装饰器：把"返回可迭代 renderable 的方法"变成"返回 Group 的方法"。
# 【参数】fit：透传给 Group，决定组内宽度是取最大值还是撑满。
# 【算法】内层 decorator 用 functools.wraps 保留原方法的元信息，
#   运行时先调用原方法拿到 renderables，再用 Group(*renderables, fit=fit) 包起来。
# 【为什么】Renderable 类型自己经常需要在运行时决定子项（例如 Table 的列渲染），
#   用装饰器可以写出"返回若干 renderable"的直觉代码，而不必手工构造 Group。
# 【注意】原方法在每次调用时都会执行，返回的是新 Group；不要把它当属性缓存。
                if not self.styles:
                    segments = Segment.strip_styles(segments)
                elif not self.links:
                    segments = Segment.strip_links(segments)
                content = self._console._render_buffer(segments)
            self.pager.show(content)
        self._console._exit_buffer()


# 【要点】用 wraps 而不是手写 __name__ 赋值：它还会复制 __doc__/__module__/__dict__
#   与 __wrapped__，使 group 装饰后的方法在文档与调试器里仍显示原方法的信息。
# 【注意】被装饰方法的返回值必须是可迭代的 renderable；返回单个对象会在 Group(*...)
#   处抛 TypeError（星号展开要求可迭代）。
class ScreenContext:
    """A context manager that enables an alternative screen. See :meth:`~rich.console.Console.screen` for usage."""

    # 【功能】记录目标 Console 与光标开关，并创建承载整屏内容的 Screen 对象。
    # 【参数】hide_cursor：进入备用屏后是否隐藏光标；style：Screen 的初始样式。
    # 【注意】_changed 初始为 False，只有 __enter__ 中 set_alt_screen(True) 返回真值时才置为 True。
    def __init__(
        self, console: "Console", hide_cursor: bool, style: StyleType = ""
    ) -> None:
# 【功能】判断当前是不是在 Jupyter/Colab/Databricks 之类的交互式 notebook 里运行。
# 【算法】1) 名字 get_ipython 不存在（普通解释器）→ False（NameError 分支）；
#   2) 取 ipython 实例的类名；3) Google Colab（类路径含 google.colab）、
#      设置了 DATABRICKS_RUNTIME_VERSION、或 shell 类名为 ZMQInteractiveShell → True；
#   4) TerminalInteractiveShell（终端里跑 IPython）→ False，因为它就是普通 TTY；
#   5) 其他未知实现 → False（保守：宁可当普通终端）。
# 【注意】get_ipython 是 IPython 注入到内建作用域的全局名，所以这里故意不导入而是直接引用；
#   它也只在交互式 shell 里存在，因此函数本身标了 pragma: no cover。
# 【协作】Console.__init__ 用它决定 is_jupyter，进而决定尺寸默认值与颜色系统（TRUECOLOR）
#   以及是否走 jupyter.display 输出路径。
        self.console = console
        self.hide_cursor = hide_cursor
        self.screen = Screen(style=style)
        self._changed = False

    # 【功能】按传入的关键字参数生成一份新的 ConsoleOptions。
    # 【参数】width 会同时改写 min_width/max_width 与 size；height 为 None 表示“不限高”而非“保持原值”。
    # 【返回】新的 ConsoleOptions（原对象不变）。
    # 【为什么】不可变更新让嵌套组件可以各自收窄宽度而互不影响，是嵌套布局正确性的前提。
    def update(
        self, *renderables: RenderableType, style: Optional[StyleType] = None
    ) -> None:
# 【算法】三种"看起来像 notebook"的特征任一命中即认定：
#   1) 类路径里出现 google.colab（Colab 的 shell 类名不一定是 ZMQInteractiveShell）；
#   2) 存在 DATABRICKS_RUNTIME_VERSION（Databricks notebook）；
#   3) shell 类名为 ZMQInteractiveShell（标准 Jupyter/qtconsole）。
# 【易错】条件里用的是 os.getenv（真实环境）而不是 self._environ：
#   本函数是模块级函数，拿不到 Console 注入的假环境，因此测试 DATABRICKS 分支
#   必须真的设置环境变量。
        # 【阅读提示】__init__ 的参数虽多，但可以按四组理解：
        """Update the screen.

        Args:
            renderable (RenderableType, optional): Optional renderable to replace current renderable,
#   (a) 能力覆盖：color_system / force_terminal / force_jupyter / force_interactive / no_color；
#   (b) 环境与几何：stderr / file / width / height / legacy_windows；
#   (c) 渲染策略：soft_wrap / markup / emoji / emoji_variant / highlight / highlighter / style / safe_box；
#   (d) 日志与测试：log_* / get_datetime / get_time / theme / _environ。
# 下面 100 行代码基本上是"按这四组依次落位"，遇到分支时再回到参数表对照即可。
                or None for no change. Defaults to None.
            style: (Style, optional): Replacement style, or None for no change. Defaults to None.
        """
        if renderables:
            self.screen.renderable = (
                Group(*renderables) if len(renderables) > 1 else renderables[0]
# 【功能】用户可见的颜色系统名 → ColorSystem 枚举。
# 【注意】"windows" 只对 Windows 老控制台有意义（16 色 + win32 API 写属性），
#   在其它平台上强制指定它也没有意义；"standard/256/truecolor" 是跨平台的三级能力。
            )
        if style is not None:
            self.screen.style = style
        self.console.print(self.screen, end="")

    # 【功能】进入上下文管理器，切换到该上下文对应的状态。
    # 【返回】self，便于 ``with ... as`` 绑定。
    # 【注意】进入与退出必须严格配对，否则缓冲索引或主题栈会失衡。
    def __enter__(self) -> "ScreenContext":
# 反向映射：ColorSystem → "standard"/"256"/"truecolor"/"windows"，
# 供 Console.color_system 属性把内部枚举还原成用户字符串。
        self._changed = self.console.set_alt_screen(True)
        if self._changed and self.hide_cursor:
            self.console.show_cursor(False)
        return self

    # 【功能】退出上下文管理器：恢复进入前的状态（缓冲、主题、屏幕或文件）。
    # 【返回】False，表示不吞掉异常。
    # 【注意】即使方法体内发生异常也必须恢复状态，因此恢复动作不依赖正常返回路径。
    def __exit__(
        self,
# 【职责】渲染流程的扩展点：在 renderables 收集完成、尚未渲染成 Segment 之前介入。
# 【为什么需要】Live/Status 之类的实时刷新需要"把本次要渲染的内容接管过来"，
#   但又不该侵入 print 的实现，于是把这段逻辑做成可插拔的钩子。
# 【协作】Console.push_render_hook/pop_render_hook 管理钩子栈；
#   print 与 log 都会在收集后、渲染前把列表交给每个钩子处理（先复制一份列表快照）。
# 【注意】钩子是就地替换列表语义：既可以返回新列表，也可以修改并返回同一个列表。
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
# 【功能】抽象方法：接收本次要渲染的 renderable 列表，返回（可能是新的）列表。
# 【参数】renderables：已是 ConsoleRenderable 的列表（字符串在收集阶段就被转成 Text 了）。
# 【返回】替换后的列表；返回空列表即"本次什么都不打印"。
# 【注意】这是抽象方法且函数体是文档字符串——子类必须实现；
#   钩子内不要递归调用 console.print，否则会再次触发钩子而无限递归。
        exc_tb: Optional[TracebackType],
    ) -> None:
        if self._changed:
            self.console.set_alt_screen(False)
            if self.hide_cursor:
                self.console.show_cursor(True)


# ==== 【组合器 Group 与运行环境探测】 ====
class Group:
    # 【功能】返回 Windows 控制台能力（首次调用时真正探测，之后走缓存）。
    # 【算法】先查全局缓存；未命中再延迟导入 ._windows 里的同名函数并调用，结果写回缓存。
    # 【为什么延迟导入】._windows 依赖 ctypes/Windows API，非 Windows 平台根本不应导入它；
    # 【注意】缓存是全局的（不是每实例）：同一进程内多次 Console() 不会重复探测。
    # ==== 【Windows 能力探测（进程级缓存）】 ====
    """Takes a group of renderables and returns a renderable object that renders the group.

# 进程级缓存：Windows 控制台能力（是否支持 VT 转义、是否 24 位色）探测一次就够。
# 类型注解用字符串，因为 _windows 只在 Windows 上才有意义，避免非 Windows 环境导入它。
    Args:
#   把 import 放进函数体，可以保证 Linux/macOS 上永远不会触发这条路径。
        renderables (Iterable[RenderableType]): An iterable of renderable objects.
        fit (bool, optional): Fit dimension of group to contents, or fill available space. Defaults to True.
    """

# 【兼容】延迟导入的关键作用：rich._windows 内部会调用 ctypes 的 WinDLL
#   并引用仅在 Windows 上存在的 API，非 Windows 平台导入它会直接失败。
#   由于 detect_legacy_windows 里 WINDOWS 为假时会短路，这里在 POSIX 上永不执行。
    def __init__(self, *renderables: "RenderableType", fit: bool = True) -> None:
        self._renderables = renderables
        self.fit = fit
# 【功能】判断是否处于"老式 Windows 控制台"（不支持 VT 转义序列）。
# 【算法】WINDOWS 为假直接 False（短路，避免非 Windows 上调用 Windows API）；
#   否则看能力探测结果里的 vt 标志取反。
# 【兼容】老式 conhost（未开启 VT / 旧系统）不支持 ANSI，Rich 必须改用 win32 API 逐格写字符，
#   这就是 legacy_windows 模式的由来；它会连带影响宽度（减 1）、颜色（WINDOWS 16 色）
#   和画框字符（safe_box 由调用方决定）。
# 【协作】Console.__init__ 里：legacy_windows = detect_legacy_windows() and not is_jupyter，
#   Jupyter 里即使宿主是 Windows 也不走老控制台路径（输出会被 notebook 前端接管）。
        self._render: Optional[List[RenderableType]] = None

    @property
# ==== 【核心类 Console】 ====
# 【职责】Rich 渲染子系统的门面（facade）：持有输出目标与全局配置，把任意 renderable
#   渲染成 Segment 并写出；同时提供能力探测（终端/颜色/尺寸）、主题栈、缓冲区、录制、
#   上下文管理器与导出等横切能力。
# 【关键属性】_file/stderr/quiet：输出目标与静默开关；_force_terminal/_color_system：能力探测结果
#   （None 表示不支持颜色）；_width/_height：用户显式指定或从环境变量得到的尺寸覆盖值；
#   legacy_windows：是否走 win32 直写；soft_wrap/markup/emoji/highlight：默认渲染策略；
#   _lock：写文件与录制用可重入锁；_thread_locals：每线程的主题栈与缓冲区；
#   _record_buffer：record=True 时的录制内容；_render_hooks/_live_stack：扩展点与实时区栈。
# 【不变量】1) 同一个 Console 可跨线程共享，但缓冲区/主题栈是线程局部的；
#   2) _buffer_index 为 0 时才允许 flush，嵌套期间内容只累积；
#   3) 任何 print 都必须经由 self（上下文管理器）进入缓冲，保证原子写出。
# 【协作】渲染委托给 render/render_lines/render_str；换行委托给 Segment；
#   颜色渲染委托给 Style.render；尺寸/能力探测委托给 os、环境变量与 ._windows。
# 【易错】Console 是"有状态"的：width 一旦指定就不再跟随终端变化；record 打开后不调用
#   export_* 会一直占用内存。
    def renderables(self) -> List["RenderableType"]:
        if self._render is None:
            self._render = list(self._renderables)
        return self._render

    # 【功能】测量 Group 整体所需的宽度。
    # 【算法】fit=True 时交给 measure_renderables 综合各子对象的最小/最大宽度；
    # fit=False 时直接返回 Measurement(max_width, max_width)，即“占满可用宽度”。
    # 【返回】Measurement。
    # 【协作】Panel/Table 等容器在“自适应”与“填满”之间取舍时会用到它。
    def __rich_measure__(
# 【要点】force_* 三兄弟（force_terminal/force_jupyter/force_interactive）是"覆盖探测结果"的开关，
#   它们只影响能力判断，不会改变输出目标；真正的输出永远去 file 属性指向的地方。
        self, console: "Console", options: "ConsoleOptions"
    ) -> "Measurement":
        if self.fit:
            return measure_renderables(console, options, self.renderables)
        else:
            return Measurement(options.max_width, options.max_width)

    # 【功能】实现渲染协议，产出该对象对应的 Segment 流。
    # 【参数】console：当前 Console；options：本次渲染的可用空间与策略快照。
    # 【返回】可迭代的 Segment 或嵌套可渲染对象（由 Console 继续展开）。
    def __rich_console__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> RenderResult:
        yield from self.renderables

# 【注意】record 打开后录制的是"已渲染好的 Segment"（含样式信息），
#   与终端是否支持颜色无关——因此可以把彩色输出录下来，再导出到支持样式的目标。

def group(fit: bool = True) -> Callable[..., Callable[..., Group]]:
    """A decorator that turns an iterable of renderables in to a group.

    Args:
        fit (bool, optional): Fit dimension of group to contents, or fill available space. Defaults to True.
    """

# 【要点】log_time_format 既可以是 strftime 格式串，也可以是可调用对象——
#   LogRender 内部会区分这两种形态，因此传入 lambda 做自定义时间格式也是受支持的。
    def decorator(
        method: Callable[..., Iterable[RenderableType]],
    ) -> Callable[..., Group]:
        """Convert a method that returns an iterable of renderables in to a Group."""

        # 【功能】Group.async_render 中用于标记“首次取值”的哨兵函数。
        # 【返回】Group 的可渲染包装。
        # 【注意】仅作为对象身份标记使用，不承载实际渲染逻辑。
        @wraps(method)
        def _replace(*args: Any, **kwargs: Any) -> Group:
            renderables = method(*args, **kwargs)
# 【要点】环境变量默认取自 os.environ，且是类属性：测试可以通过构造参数 _environ 注入
#   一个假映射，从而在不污染真实环境的前提下验证 COLUMNS/TERM/NO_COLOR 等分支。
#   实例化时若传了 _environ，会在 self 上遮蔽类属性。
            return Group(*renderables, fit=fit)

        return _replace

    return decorator


# 【功能】判断当前是否运行在支持富输出的 notebook 前端（Jupyter/qtconsole/Colab/Databricks）。
# 【算法】先看全局名 get_ipython 是否存在；再按三项任一命中判定：宿主类名含 "google.colab"、
# 存在 DATABRICKS_RUNTIME_VERSION 环境变量、shell 名为 ZMQInteractiveShell。
# 【返回】bool；IPython 终端（TerminalInteractiveShell）与未知类型都返回 False。
# 【注意】纯启发式判断；Console 可用 force_jupyter 显式覆盖。
def _is_jupyter() -> bool:  # pragma: no cover
    """Check if we're running in a Jupyter notebook."""
    try:
        get_ipython  # type: ignore[name-defined]
    except NameError:
        return False
    ipython = get_ipython()  # type: ignore[name-defined]
    shell = ipython.__class__.__name__
    if (
        "google.colab" in str(ipython.__class__)
        or os.getenv("DATABRICKS_RUNTIME_VERSION")
        or shell == "ZMQInteractiveShell"
    ):
        return True  # Jupyter notebook or qtconsole
    elif shell == "TerminalInteractiveShell":
        return False  # Terminal running IPython
    else:
        return False  # Other type (?)


COLOR_SYSTEMS = {
    "standard": ColorSystem.STANDARD,
    "256": ColorSystem.EIGHT_BIT,
    "truecolor": ColorSystem.TRUECOLOR,
    "windows": ColorSystem.WINDOWS,
}

_COLOR_SYSTEMS_NAMES = {system: name for name, system in COLOR_SYSTEMS.items()}
# 【要点】只有显式传入 _environ 时才在实例上覆盖类属性，这样默认路径零额外开销。


# 【兼容】is_jupyter 是实例属性（普通 bool），后续 is_terminal/color_system/size 都要看它。
#   在 Jupyter 里没有真正的 TTY，但输出会被 notebook 前端按 HTML/ANSI 处理，
#   所以颜色系统直接认定为 TRUECOLOR，宽度用 notebook 的默认列数。
@dataclass
# 【算法】Jupyter 环境没有可靠的终端尺寸：优先读 JUPYTER_COLUMNS/JUPYTER_LINES
#   （Jupyter 前端会注入），无效则退回常量 115×100。
# 【易错】这里只在 width/height 为 None 时兜底，用户显式传入的值优先。
# 【约束】isdigit() 过滤掉非数字（例如空字符串或 "abc"），避免 int() 抛异常。
class ConsoleThreadLocals(threading.local):
    """Thread local values for Console context."""

    theme_stack: ThemeStack
    buffer: List[Segment] = field(default_factory=list)
    buffer_index: int = 0


# 【职责】渲染过程的扩展点：在渲染前后对可渲染对象列表做变换。
# 【算法】process_renderables 依次调用每个已注册 hook 的 process_renderables，允许 hook 替换或包装列表。
# 【协作】Console.push_render_hook / pop_render_hook 管理注册；Live、Progress 借此注入自己的渲染逻辑。
class RenderHook(ABC):
    """Provides hooks in to the render process."""

    # 【功能】接收待渲染的 renderable 列表，返回（可能是新的）列表，用于插入、替换或包装渲染对象。
    # 【参数】renderables：Console 收集到的可渲染对象列表；可以就地修改后返回同一个列表。
    # 【返回】List[ConsoleRenderable]。
    # 【注意】基类实现返回入参不变；实现方应保持“输入顺序即输出顺序”，否则会改变输出顺序。
    @abstractmethod
    def process_renderables(
# 【要点】这一组是"渲染策略"的原始值，它们不会立即影响输出，而是在渲染时被读取：
#   tab_size 由 Text 展开制表符时使用；_markup/_emoji/_emoji_variant/_highlight 由
#   render_str 与 _collect_renderables 三态解析时使用。
# 【易错】这些字段以 _ 开头，表示"默认值"：
#   单次 print(..., markup=...) 的显式参数优先，且不会写回这些字段（不粘性）。
        self, renderables: List[ConsoleRenderable]
    ) -> List[ConsoleRenderable]:
        # 【易错】emoji_variant=None 表示"不指定变体"，此时由 _emoji_replace 使用其内置默认值；
        # 【算法】legacy_windows 的三态处理：显式 True/False 直接采用；None 时自动探测，
        # 【兼容】一旦为 True，后续会同时影响：宽度减 1、颜色系统降为 WINDOWS、
        # 【注意】Status 对象创建后并不立即显示：它作为上下文管理器进入时才会启动刷新，
        # 【算法】非 Jupyter 时依次尝试 COLUMNS/LINES 环境变量（许多 shell 会导出它们，
        # 【易错】COLUMNS 分支里写的是 int(columns) - self.legacy_windows——用的是布尔值参与整数运算
        """Called with a list of objects to render.

#   它不是布尔开关，不要与本行的 emoji（总开关）混淆。
        This method can return a new list of renderables, or modify and return the same list.

#   除非在 Jupyter 里（Jupyter 输出不经过 conhost，属性探测没有意义）。
#   _write_buffer 走 win32 API 直写、safe_box 建议开启。
        Args:
            renderables (List[ConsoleRenderable]): A number of renderable objects.

        Returns:
#   因此这里不涉及任何 I/O，构造函数是廉价的。
            List[ConsoleRenderable]: A replacement list of renderables.
#   好处是启动早期就能拿到正确宽度）。
#   （True 即 1），目的是在老控制台上预留一列以避免写满整行触发自动换行；
#   注意 LINES 分支没有这个减一处理。
        """


_windows_console_features: Optional["WindowsConsoleFeatures"] = None


# 【功能】探测（并缓存）Windows 控制台的能力。
# 【算法】模块级全局 _windows_console_features 首次调用时延迟 import ._windows 完成探测，
# 之后直接返回缓存，避免重复调用 Win32 API。
# 【返回】WindowsConsoleFeatures（含 truecolor / vt 等字段）。
# 【注意】延迟导入保证非 Windows 平台不会因为引用 ctypes/WinDLL 而导入失败。
def get_windows_console_features() -> "WindowsConsoleFeatures":  # pragma: no cover
    global _windows_console_features
# 保存"未显式给出尺寸"的状态：_width/_height 为 None 表示按需动态探测（见 size 属性）。
# 这一点很重要——只有 None 才能让 Console 跟随终端尺寸变化（如 SIGWINCH 后重读）。
    if _windows_console_features is not None:
        return _windows_console_features
    from ._windows import get_windows_console_features
# 【要点】先只做类型声明（Python 里就是一条注解语句，不产生赋值），
#   真正的赋值在下面的 if/elif/else 三个分支中完成——三个分支覆盖了"明确无颜色/自动探测/指定"。

# 【要点】用 None 表示"不强制"，非 None 才是用户的强制值；
#   写成两行而不是 self._force_terminal = force_terminal，是为了让类型保持 Optional[bool]。
    _windows_console_features = get_windows_console_features()
    return _windows_console_features

# 【要点】file 为 None 是常态：此时 file 属性会在每次访问时按 stderr 开关选 sys.stdout/sys.stderr，
#   好处是 Console 可以在重定向 sys.stdout 之后再创建，也能正确跟随。
# 【易错】quiet/stderr 也在这里落位；stderr 只在 _file 为 None 时起作用。

def detect_legacy_windows() -> bool:
    """Detect legacy Windows."""
# 【算法】颜色系统的三级决策：None → 完全不要颜色（_color_system 置 None，后续渲染不带样式）；
#   "auto" → _detect_color_system() 探测；其他字符串 → 查 COLOR_SYSTEMS 表（未知名字会 KeyError）。
# 【注意】探测发生在构造期，但探测结果里依赖 is_terminal 的部分是在此时求值的；
#   如果之后通过 console.file = ... 换了目标，_color_system 不会自动重算。
    return WINDOWS and not get_windows_console_features().vt


# ==== 【Console：构造与能力探测】 ====
class Console:
    # 【要点】可重入锁：_write_buffer 持锁期间可能再次进入（例如钩子内部调用 print），
    # 【要点】highlighter=None 时退化成共享的空高亮器单例：
    # 【为什么可注入】log() 的时间戳与计时（Live 的刷新）需要可替换的实现：
    # 【要点】Console 级别的 style 是"外层样式"，在 _collect_renderables 末尾统一包装，
    # 【算法】NO_COLOR 遵循 https://no-color.org 约定：只要环境变量存在且非空字符串即视为启用。
    # 【注意】no_color 与 color_system 是两条独立的线：前者在渲染最后一步剥掉颜色，
    # 【算法】NO_COLOR 的判定细节：get("NO_COLOR", "") != "" ——
    # 【算法】TTY_INTERACTIVE 是手工覆盖交互性的开关："0" 强制非交互、"1" 强制交互，
    # 【要点】默认值是 None（而不是空串）：只有变量存在时才进入内层判断，
    # 【算法】交互性 = 是终端且不是 dumb 终端（除非用户强制）。
    # 【为什么单独存在】Progress/Live 等会据此决定"用动画刷新"还是"只在结束时打印一次"，
    # 【阅读提示】到这里 __init__ 已近尾声，剩下三件事：
    # 【要点】这里故意写成两个条件而不是复用 is_dumb_terminal 属性：
    # 【要点】录制缓冲区单独一把锁：_write_buffer 会在持有 _lock 的情况下再取它，
    # 【约束】锁顺序固定为 _lock → _record_buffer_lock，不要反过来，否则可能死锁。
    # 【要点】theme 参数为 None 时用 themes.DEFAULT（内置主题）；用户主题作为栈底。
    # 【注意】主题栈是线程局部的：新线程拿不到主线程 push 的主题（除了栈底这份）。
    """A high level console interface.

#   普通 Lock 会死锁，RLock 不会。
    Args:
        color_system (str, optional): The color system supported by your terminal,
            either ``"standard"``, ``"256"`` or ``"truecolor"``. Leave as ``"auto"`` to autodetect.
        force_terminal (Optional[bool], optional): Enable/disable terminal control codes, or None to auto-detect terminal. Defaults to None.
        force_jupyter (Optional[bool], optional): Enable/disable Jupyter rendering, or None to auto-detect Jupyter. Defaults to None.
        force_interactive (Optional[bool], optional): Enable/disable interactive mode, or None to auto detect. Defaults to None.
#   这样 render_str 里不必到处判空，且不会构造无意义的 ReprHighlighter。
        soft_wrap (Optional[bool], optional): Set soft wrap default on print method. Defaults to False.
        theme (Theme, optional): An optional style theme object, or ``None`` for default theme.
#   测试可以注入固定时钟；monotonic 用于不受系统时间跳变影响的耗时计算。
        stderr (bool, optional): Use stderr rather than stdout if ``file`` is not specified. Defaults to False.
        file (IO, optional): A file object where the console should write to. Defaults to stdout.
#   因此它会与内容自身的样式叠加（内层样式优先，因为 Styled 是包在外面的）。
        quiet (bool, Optional): Boolean to suppress all output. Defaults to False.
#   显式传入的 no_color 优先级最高（True/False 都尊重）。
#   后者决定能渲染出哪一级颜色。二者都为真时，结果是不带颜色的文本。
#   值为空串时视为未设置（因此 NO_COLOR= 不会关色），任何非空值都会关色，
#   包括 "0"、"false" 这类"看起来像假值"的字符串。
        width (int, optional): The width of the terminal. Leave as default to auto-detect width.
        height (int, optional): The height of the terminal. Leave as default to auto-detect height.
        style (StyleType, optional): Style to apply to all output, or None for no style. Defaults to None.
        no_color (Optional[bool], optional): Enabled no color mode, or None to auto detect. Defaults to None.
        tab_size (int, optional): Number of spaces used to replace a tab character. Defaults to 8.
#   其他值（含空串）忽略。它用于"程序 stdin 不是 TTY 但确实有人在看"这类场景。
        record (bool, optional): Boolean to enable recording of terminal output,
#   这样"变量不存在"与"变量为空串"都被忽略，语义与文档一致。
            required to call :meth:`export_html`, :meth:`export_svg`, and :meth:`export_text`. Defaults to False.
        markup (bool, optional): Boolean to enable :ref:`console_markup`. Defaults to True.
        emoji (bool, optional): Enable emoji code. Defaults to True.
        emoji_variant (str, optional): Optional emoji variant, either "text" or "emoji". Defaults to None.
        highlight (bool, optional): Enable automatic highlighting. Defaults to True.
        log_time (bool, optional): Boolean to enable logging of time by :meth:`log` methods. Defaults to True.
#   与是否有颜色无关（例如只有 FORCE_COLOR 而无 TTY 时应保持非交互）。
#   1) is_interactive 的计算（依赖前面已经算好的 is_terminal）；
#   2) 四把锁/线程局部/缓冲区的初始化（跨线程基础设施）；
#   3) 主题栈的压栈（把 theme 或内置默认主题作为栈底）。
# 也就是说：位置越靠后的赋值越依赖前面的结果，这正是本节不能随意重排的原因。
        log_path (bool, optional): Boolean to enable the logging of the caller by :meth:`log`. Defaults to True.
#   is_dumb_terminal 内部已经包含 is_terminal 判断，但显式并列表述更清楚"必须是终端"这一前提。
        log_time_format (Union[str, TimeFormatterCallable], optional): If ``log_time`` is enabled, either string for strftime or callable that formats the time. Defaults to "[%X] ".
        highlighter (HighlighterType, optional): Default highlighter.
        legacy_windows (bool, optional): Enable legacy Windows mode, or ``None`` to auto detect. Defaults to ``None``.
        safe_box (bool, optional): Restrict box options that don't render on legacy Windows.
#   把两把锁分开可以避免与导出路径（export_* 只取 _record_buffer_lock）互相阻塞过久。
        get_datetime (Callable[[], datetime], optional): Callable that gets the current time as a datetime.datetime object (used by Console.log),
            or None for datetime.now.
        get_time (Callable[[], time], optional): Callable that gets the current time in seconds, default uses time.monotonic.
    """
# 录制缓冲区、渲染钩子栈、Live 栈、备用屏幕标记：这四项都是实例级（跨线程共享），
# 与线程局部的 _buffer/_theme_stack 形成对照。

    _environ: Mapping[str, str] = os.environ

    # 【功能】初始化 Console：确定输出目标、探测终端能力、设定默认渲染策略与线程相关基础设施。
    # 【参数】多为 keyword-only：color_system（"auto" 探测、None 表示不用颜色）；
    # force_terminal/force_jupyter/force_interactive 覆盖自动探测；width/height 覆盖终端尺寸；
    # file/stderr/quiet 指定输出目标；record 开启录制以便 export_*；_environ 供测试注入环境变量。
    # 【注意】1) 赋值顺序有依赖：is_interactive 依赖已算好的 is_terminal，不能随意重排；
    # 2) _thread_locals（含 _buffer/_theme_stack）是线程局部的，而 _record_buffer/_render_hooks/_live_stack 是实例级；
    # 3) color_system="auto" 在构造时即完成探测，之后不再跟随终端变化。
    def __init__(
# 【功能】调试用的简短表示，显示宽度与颜色系统。
# 【注意】故意不含 file 与主题：那些内容太长，且可能在 repr 期间触发 I/O。
        self,
        *,
# 【功能】读取缓冲嵌套深度（线程局部）。
# 【不变量】0 表示不在缓冲中，此时 print 的内容会立刻写出；>0 表示内容还在累积。
# 【协作】__enter__/__exit__、begin_capture/end_capture、PagerContext 都通过
#   _enter_buffer/_exit_buffer 改这个值，是 Console 实现"原子输出"的核心。
# 【功能】取宽度（size.width 的快捷方式）。
# 【协作】print 裁剪、Table 布局、Segment.split_and_crop_lines 都以它为准。
        color_system: Optional[
            Literal["auto", "standard", "256", "truecolor", "windows"]
        ] = "auto",
# 【易错】每次访问 file 都会重新读 sys.stdout/sys.stderr：
#   在运行中替换 sys.stdout（例如把输出重定向到日志）后，已有 Console 也会跟着改变。
#   这是特性（方便重定向）也是陷阱（构造期探测到的能力可能与新目标不符）。
        force_terminal: Optional[bool] = None,
        force_jupyter: Optional[bool] = None,
        force_interactive: Optional[bool] = None,
        soft_wrap: bool = False,
        theme: Optional[Theme] = None,
# 【功能】设置输出目标；只改 _file，不做能力重算。
# 【易错】换文件后 _color_system/is_terminal 的缓存值不会更新：
#   从小终端换到大终端需要重新构造 Console，或者重新赋值 _width/_height。
        stderr: bool = False,
        file: Optional[IO[str]] = None,
        quiet: bool = False,
        width: Optional[int] = None,
# 【功能】暴露当前线程的主题栈。
# 【协作】get_style 查主题、push_theme/pop_theme/use_theme 都经由它；
#   因为它是线程局部的，多线程同时渲染不同主题不会互相覆盖。
# 【功能】取高度（行数）。
# 【协作】ConsoleOptions.max_height、Live/Progress 的可用行数、SVG 导出的行数上限都会参考它。
        height: Optional[int] = None,
        style: Optional[StyleType] = None,
        no_color: Optional[bool] = None,
        tab_size: int = 8,
# 【功能】把内部 ColorSystem 枚举还原成用户可见字符串。
# 【返回】"standard"/"256"/"truecolor"/"windows" 之一；_color_system 为 None 时返回 None
#   （表示当前环境不输出颜色）。
# 【协作】_COLOR_SYSTEMS_NAMES 是 COLOR_SYSTEMS 的反向映射，
#   保证属性返回值与构造参数取值一致，便于"读出来再构造回去"。
# 【功能】查询备用屏幕是否已开启（由本 Console 记录）。
# 【注意】它反映的是"Rich 是否成功切过"，不是终端的真实状态；
#   如果有人绕过 Rich 直接写转义序列，这里会与实际不符。
        record: bool = False,
        markup: bool = True,
        emoji: bool = True,
        emoji_variant: Optional[EmojiVariant] = None,
# 【功能】设置嵌套深度（供 _enter_buffer/_exit_buffer 使用）。
# 【易错】不要手工把它设为 0 来"强行刷新"：会绕过 __exit__ 的记账，
#   使后续 _exit_buffer 把深度减成负数。
        highlight: bool = True,
        log_time: bool = True,
        log_path: bool = True,
# 【功能】返回输出目标的编码（小写）。
# 【算法】getattr(self.file, "encoding", "utf-8") or "utf-8"：
#   有的流没有 encoding 属性（如自定义 IO），有的有但值为 None（如某些包装流），
#   两种都退回 utf-8。
# 【注意】编码会影响 ConsoleOptions.ascii_only（是否以 utf 开头）与 emoji/制表字符退化，
#   进而影响渲染结果——所以这个属性在渲染期被频繁读取。
        log_time_format: Union[str, FormatTimeCallable] = "[%X]",
        highlighter: Optional["HighlighterType"] = ReprHighlighter(),
        legacy_windows: Optional[bool] = None,
        safe_box: bool = True,
# 【功能】根据运行环境推断终端支持的颜色级别（"auto" 时调用）。
# 【算法】按优先级：
#   1) Jupyter → TRUECOLOR（notebook 前端能显示 24 位色）；
#   2) 不是终端、或 TERM=dumb/unknown → None（不输出任何样式）；
#   3) Windows：老控制台 → WINDOWS（16 色，走 win32 API）；
#      否则看 GetConsoleMode 探测结果里的 truecolor 标志 → TRUECOLOR / EIGHT_BIT；
#   4) 其他平台：COLORTERM 为 truecolor/24bit → TRUECOLOR；
#      否则用 TERM 的最后一段查 _TERM_COLORS（256color/kitty → EIGHT_BIT），
#      查不到则保守返回 STANDARD（16 色）。
# 【返回】Optional[ColorSystem]：None 表示不支持颜色。
# 【注意】这是"降级表"：能力从高到低逐项判断，任何一步不确定都往低一级落，
#   宁可少颜色也不输出终端无法解释的转义序列。
        get_datetime: Optional[Callable[[], datetime]] = None,
        get_time: Optional[Callable[[], float]] = None,
        _environ: Optional[Mapping[str, str]] = None,
    ):
        # Copy of os.environ allows us to replace it for testing
        if _environ is not None:
# 【兼容】Windows 分两条历史路径：老 conhost 只能 16 色且必须用 win32 API 写属性；
#   支持 VT 的新控制台（Win10 1511+ 且已启用 VT）可以直接用 ANSI，甚至 24 位色，
#   因此这里再看 truecolor 探测结果决定 TRUECOLOR 还是 EIGHT_BIT。
            self._environ = _environ

        self.is_jupyter = _is_jupyter() if force_jupyter is None else force_jupyter
# 【兼容】新旧 Windows 控制台的判定依据来自 GetConsoleMode 的探测结果：
#   vt=True 说明已启用虚拟终端处理（可以直接用 ANSI）；
#   truecolor=True 说明支持 24 位色（否则只能 8 位色）。
#   两个标志在 rich/_windows.py 里由 ctypes 调用 Win32 API 得出，且被进程级缓存。
        if self.is_jupyter:
            if width is None:
                jupyter_columns = self._environ.get("JUPYTER_COLUMNS")
                if jupyter_columns is not None and jupyter_columns.isdigit():
                    width = int(jupyter_columns)
                else:
                    width = JUPYTER_DEFAULT_COLUMNS
# 【兼容】POSIX 侧的探测顺序：先看 COLORTERM（这是"终端自称的能力"，最可靠），
#   再退回解析 TERM 后缀。strip().lower() 是为了容忍 "TrueColor"、" truecolor " 之类写法。
            if height is None:
                jupyter_lines = self._environ.get("JUPYTER_LINES")
                if jupyter_lines is not None and jupyter_lines.isdigit():
# 【算法】rpartition("-") 取最后一个连字符之后的部分作为"颜色能力后缀"：
#   "xterm-256color" → ("xterm", "-", "256color")；没有连字符时 colors 为空串，
#   查表自然落回 STANDARD。
                    height = int(jupyter_lines)
# 【易错】_term_name 与 _hyphen 被刻意用下划线前缀命名（本行解构出但未使用）：
#   rpartition 必须同时接收三段，这里只关心最后一段 colors。
                else:
                    height = JUPYTER_DEFAULT_LINES

# 【功能】进入缓冲上下文：深度 +1。
# 【为什么只要 +1】真正的内容提交推迟到深度回到 0，因此嵌套（with console:）是安全的，
#   内层退出不会把尚未完成的内容写出去。
# 【注意】它不返回任何状态，配对使用 _exit_buffer 是调用方的责任。
        self.tab_size = tab_size
        self.record = record
# 【约束】只能用 += / -= 且必须成对：任何"直接赋值 0"的写法都会破坏嵌套计数。
        self._markup = markup
# 【功能】离开缓冲上下文：深度 -1，然后尝试提交。
# 【算法】_check_buffer 内部只在深度为 0 时才写文件，所以这里的调用是"每次都试、
#   但只有最外层真的写"。
# 【注意】先减后查的顺序很关键：如果先查，最外层退出时深度仍为 1，内容永远写不出去。
        self._emoji = emoji
        self._emoji_variant: Optional[EmojiVariant] = emoji_variant
# 【易错】减到负数不会被发现（属性没有下限校验），表现为后续所有输出都被当作"仍在缓冲"，
#   从而永远不写文件——这是最难排查的一类问题，务必保持成对调用。
        self._highlight = highlight
        self.legacy_windows: bool = (
# 【功能】把 Live 实例压入实时区栈（由 Live 上下文管理器调用，用户不必直接调）。
# 【返回】True 表示压栈后本 Live 处于栈顶（最外层）；Live 用它决定是否真正接管终端刷新。
# 【注意】返回值不是"成功与否"：嵌套多个 Live 时内层会拿到 False。
# 【为什么加锁】_live_stack 是实例级共享结构，Live 常在工作线程里进入/退出。
            (detect_legacy_windows() and not self.is_jupyter)
            if legacy_windows is None
            else legacy_windows
        )

        if width is None:
            columns = self._environ.get("COLUMNS")
            if columns is not None and columns.isdigit():
                width = int(columns) - self.legacy_windows
# 【要点】render_lines 是会被其它线程调用的公共入口（Panel/Table 内部都会调），
#   必须与 _write_buffer 共用同一把锁，避免写文件的同时读取正在变化的状态。
        if height is None:
            lines = self._environ.get("LINES")
            if lines is not None and lines.isdigit():
# 【功能】弹出栈顶 Live（与 set_live 配对）。
# 【易错】不做匹配校验：如果 Live 的进入/退出顺序错乱，pop 到的是别的实例，
#   这是 Rich 依赖调用方正确使用上下文管理器的体现。
                height = int(lines)

        self.soft_wrap = soft_wrap
        self._width = width
# 【功能】压入一个渲染钩子（后进先出）。
# 【协作】print/log 每次都会把 _render_hooks 复制成快照后遍历，所以钩子内部再增删钩子
#   不会影响当前这次渲染。
        self._height = height

        self._color_system: Optional[ColorSystem]

        self._force_terminal = None
        if force_terminal is not None:
            self._force_terminal = force_terminal
# 【功能】弹出最近压入的渲染钩子。
# 【注意】空栈时 list.pop() 会抛 IndexError，属于调用方用法错误；
#   钩子栈本身是实例级的（跨线程共享），多线程使用时需自行保证配对。

        self._file = file
        self.quiet = quiet
        self.stderr = stderr
# 【功能】把 Console 自身当上下文管理器：进入缓冲区。
# 【为什么】with console: 是 Rich 内部最常用的"批量输出原子化"手段——
#   print 内部本身就是 with self:，因此一次 print 的所有 Segment 只会触发一次写文件。
# 【返回】self，便于 with Console() as console: 的写法。

        if color_system is None:
            self._color_system = None
        elif color_system == "auto":
# 【功能】退出缓冲区并尝试提交（异常时同样提交：已产生的输出不应被吞掉）。
# 【注意】不检查异常类型，也不回滚缓冲区内容。
            self._color_system = self._detect_color_system()
        else:
            self._color_system = COLOR_SYSTEMS[color_system]
# 【功能】开始捕获：进入缓冲区，使内容留在 _buffer 里不被写出。
# 【协作】由 Capture.__enter__ 调用；也可以手工配对使用（不推荐，异常时会漏内容）。

        self._lock = threading.RLock()
        self._log_render = LogRender(
# 【功能】结束捕获：把缓冲区渲染成字符串返回，并清空缓冲区、退出缓冲上下文。
# 【算法】1) _render_buffer(self._buffer) 得到最终字符串（此时缓冲区尚未清空，
#      因此缓冲区内容会被渲染两次——一次取字符串、一次留给 _exit_buffer 的写出）；
#   2) del self._buffer[:] 清空，避免这些内容再被写到终端；
#   3) _exit_buffer 提交（此时缓冲区已空，实际不会写出东西）。
# 【返回】str：捕获到的、已按终端能力着色的文本（无颜色终端上就是纯文本）。
# 【注意】返回的是"渲染后"的字符串，样式是否出现取决于 color_system/is_terminal；
#   控制码（光标移动等）在非终端下会被 _render_buffer 丢弃。
            show_time=log_time,
            show_path=log_path,
            time_format=log_time_format,
        )
        self.highlighter: HighlighterType = highlighter or _null_highlighter
# 【要点】先渲染成字符串、再清空：这两步的顺序决定了捕获内容不会丢，
#   而随后 _exit_buffer 提交的是一份空缓冲区（因此不会重复输出到终端）。
        self.safe_box = safe_box
        self.get_datetime = get_datetime or datetime.now
        self.get_time = get_time or monotonic
        self.style = style
# 【功能】把主题压到当前线程主题栈顶，后续 get_style 优先在其中查名字。
# 【参数】inherit=True 表示保留下层已有的样式定义，只覆盖同名项；
#   False 表示完全替换（下层样式名不再可见）。
# 【协作】通常不直接调用，而是用 use_theme() 拿上下文管理器，保证成对 pop。
        self.no_color = (
            no_color
            if no_color is not None
            else self._environ.get("NO_COLOR", "") != ""
        )
        if force_interactive is None:
            tty_interactive = self._environ.get("TTY_INTERACTIVE", None)
            if tty_interactive is not None:
                if tty_interactive == "0":
# 【功能】弹出栈顶主题，恢复上一层。
# 【注意】栈底是构造时压入的默认主题，理论上不会被弹空；
#   若手工多弹一次，ThemeStack 会抛异常。
                    force_interactive = False
                elif tty_interactive == "1":
                    force_interactive = True
# 【功能】返回临时切换主题的上下文管理器（退出时自动恢复）。
# 【返回】ThemeContext（尚未 push，进入 with 时才 push）。

        self.is_interactive = (
            (self.is_terminal and not self.is_dumb_terminal)
            if force_interactive is None
            else force_interactive
        )

        self._record_buffer_lock = threading.RLock()
# 【要点】这里只构造上下文对象，尚未压栈：真正生效发生在 with 语句进入时，
#   因此把 use_theme 的返回值赋给变量但忘记 with，是不会改变主题的（也不会报错）。
        self._thread_locals = ConsoleThreadLocals(
# 【功能】判断输出目标是不是能理解转义序列的终端（决定是否输出 ANSI）。
# 【算法】优先级从上到下：
#   1) _force_terminal 非 None → 直接采用（force_terminal=True/False 的最高优先级）；
#   2) sys.stdin 来自 idlelib → False（IDLE 自称 tty，但它的输出窗口不解释 ANSI）；
#   3) is_jupyter → False（Jupyter 可能设置了 FORCE_COLOR，但输出走 jupyter.display）；
#   4) TTY_COMPATIBLE="0"/"1" → 直接返回（供包装脚本显式声明）；
#   5) FORCE_COLOR 存在 → 空串表示 False，非空表示 True（https://force-color.org/）；
#   6) 最后看 file.isatty()，没有 isatty 属性则 False。
# 【返回】bool。
# 【注意】isatty() 可能抛 ValueError（例如 pytest 收尾时文件已关闭），此时按"不是终端"处理；
#   这也是 rich 在 CI 日志里不输出颜色的原因。
# 【兼容】is_jupyter 分支与 force_terminal 的关系是：force_terminal 优先，
#   所以 Jupyter 里想要强制颜色可以显式传 force_terminal=True。
            theme_stack=ThemeStack(themes.DEFAULT if theme is None else theme)
        )
        self._record_buffer: List[Segment] = []
        self._render_hooks: List[RenderHook] = []
        self._live_stack: List[Live] = []
        self._is_alt_screen = False
# 【易错】_color_system 为 None 有两种来源：用户显式传 color_system=None，
#   或自动探测判定该环境不支持颜色；两种情况下本属性都返回 None（表示无样式输出）。

    def __repr__(self) -> str:
        return f"<console width={self.width} {self._color_system!s}>"

# 【功能】判断是否连接了"哑终端"（TERM=dumb/unknown）。
# 【算法】TERM 小写后是否为 dumb/unknown，且必须是终端（非终端不需要谈哑不哑）。
# 【为什么单独存在】dumb 终端既不能显示颜色也不支持光标控制：
#   color_system 会降为 None，is_interactive 变 False，Control 相关方法直接短路。
    # 【功能】替换输出目标文件对象（self._file）。
    # 【参数】new_file：新的文本流；传 None 表示回退到 sys.stderr（stderr=True 时）或 sys.stdout。
    # 【注意】这是 file 属性的 setter，与 getter 成对；改文件不会重算已经探测好的颜色等级等能力。
    # 【协作】getter 会处理代理文件对象并保证不为 None（否则回退 NULL_FILE）。
    @property
    def file(self) -> IO[str]:
        """Get the file object to write to."""
        file = self._file or (sys.stderr if self.stderr else sys.stdout)
        file = getattr(file, "rich_proxied_file", file)
        if file is None:
# 【兼容】编码统一小写：不同平台可能报 "UTF-8"/"utf-8"/"cp936"，
#   小写化让 ascii_only 的前缀判断与日志输出保持稳定。
# 【性能】encoding 在每次渲染字符串（ConsoleOptions.ascii_only）时都可能被读取，
#   而 self.file 又是一次属性访问 + 可能的 getattr 解包，因此不要在循环里反复取它。
            file = NULL_FILE
        return file

    # 【功能】获取 / 设置 Console 的输出文件对象。
    # 【参数】new_file：新的目标文件（任意带 write 的对象）。
    # 【注意】设置时会重新评估 is_terminal 与颜色等级，因此把输出重定向到文件等于关闭颜色。
    @file.setter
    def file(self, new_file: IO[str]) -> None:
        """Set a new file object."""
        self._file = new_file

# 【要点】这是整条判定链的第一优先级：用户的显式意图永远覆盖环境探测。
    @property
    def _buffer(self) -> List[Segment]:
# 【兼容】IDLE 的 stdout 是伪 tty（isatty() 为真），但它把输出当纯文本处理，
#   显示 ANSI 会变成一堆方框字符；因此这里通过 __module__ 前缀识别 idlelib 并强制关闭。
        """Get a thread local buffer."""
        return self._thread_locals.buffer

    # 【功能】读取当前线程的缓冲嵌套深度。
    # 【返回】int：0 表示不在任何缓冲上下文中（此时输出会立即写出）。
    # 【注意】它是 property，与下面的同名 setter 成对；数据存放在 threading.local 上，不跨线程共享。
    @property
    def _buffer_index(self) -> int:
        """Get a thread local buffer."""
        return self._thread_locals.buffer_index

    # 【功能】读取 / 设置录制缓冲的起点索引。
    # 【注意】capture 上下文靠它实现“只录制其中一段”，进入时记录、退出时回填。
    @_buffer_index.setter
    def _buffer_index(self, value: int) -> None:
        self._thread_locals.buffer_index = value

# 【要点】FORCE_COLOR 的约定是"存在即强制，空串表示显式禁用"，
#   所以不能写成 if force_color:（空串为假会继续往下探测）。
    @property
    def _theme_stack(self) -> ThemeStack:
        """Get the thread local theme stack."""
# 【要点】兜底路径：用真实的 isatty() 结果。getattr 默认 None 是为了兼容没有该方法的流。
# 【易错】某些流（pytest 的捕获、已关闭文件）会抛 ValueError，必须捕获，
#   否则在测试收尾或重定向场景会抛出与业务无关的异常。
        return self._thread_locals.theme_stack
# 【易错】__rich_console__ 允许返回任何东西；这里用 iter() 验证可迭代性，
#   把"返回了 None/整数"这类实现错误提前变成 NotRenderableError，而不是渲染到一半才崩。
# 【易错】主题查不到时并不是立刻失败：还会尝试把 name 当字面样式解析，
#   所以 get_style("bold red") 与 get_style("table.header") 走的是同一入口。
#   只有两者都失败才抛异常（或在给了 default 时回退）。

# 【易错】isatty() 的返回不一定是 bool（某些实现返回 0/1 或真值对象），
#   这里直接把它当返回值，最终会被上层按真值使用——行为上没问题，但类型上并不严格。
    def _detect_color_system(self) -> Optional[ColorSystem]:
        """Detect color system from env vars."""
        if self.is_jupyter:
            return ColorSystem.TRUECOLOR
        if not self.is_terminal or self.is_dumb_terminal:
            return None
        if WINDOWS:  # pragma: no cover
            if self.legacy_windows:  # pragma: no cover
                return ColorSystem.WINDOWS
            windows_console_features = get_windows_console_features()
# 【兼容】TERM 的取值在真终端上是 "xterm-256color" 之类，在 Emacs shell、
#   某些 IDE 控制台与 CI 的伪终端里会是 "dumb" 或 "unknown"——
#   这两种情况都必须按"无颜色、无光标控制"处理，否则会输出满屏乱码。
            return (
                ColorSystem.TRUECOLOR
                if windows_console_features.truecolor
# 【功能】生成本 Console 的"根"渲染选项（每次调用都新建一个）。
# 【算法】用当前 size 填 max_height/max_width/size，min_width 固定为 1，
#   并把 legacy_windows/encoding/is_terminal 一并带上——这三项是渲染期各种降级判断的依据。
# 【返回】ConsoleOptions；justify/overflow/no_wrap 等覆盖值均为默认（None/False）。
# 【注意】min_width=1 而不是 0：宽度为 0 会让部分 renderable 陷入"永远切不完"的循环，
#   render() 也专门为 max_width < 1 做了短路保护。
# 【协作】print/measure/update_screen 都以它为起点，再用 update* 派生更窄的选项。
                else ColorSystem.EIGHT_BIT
# 【易错】options 每次访问都会新建一棵 ConsoleOptions（含一次 NamedTuple 构造），
#   在极热路径中应把结果存下来复用，而不是反复调用 console.options。
            )
        else:
            color_term = self._environ.get("COLORTERM", "").strip().lower()
            if color_term in ("truecolor", "24bit"):
                return ColorSystem.TRUECOLOR
            term = self._environ.get("TERM", "").strip().lower()
            _term_name, _hyphen, colors = term.rpartition("-")
            color_system = _TERM_COLORS.get(colors, ColorSystem.STANDARD)
            return color_system

    # 【功能】压入一层新的输出缓冲（用于 create_app / screen 等需要独立缓冲的场景）。
    # 【注意】与 _exit_buffer 配对使用；嵌套层数会影响 _buffer_index 的基准。
    def _enter_buffer(self) -> None:
        """Enter in to a buffer context, and buffer all output."""
        self._buffer_index += 1
# 【易错】size 有四个返回点，其中最后一个返回点还会用 _width/_height 覆盖探测值：
#   读代码时容易漏掉"显式尺寸优先于环境变量、环境变量优先于探测"这一优先级链。

    def _exit_buffer(self) -> None:
        """Leave buffer context, and render content if required."""
        self._buffer_index -= 1
        self._check_buffer()

    # 【功能】把 Live 实例压入 _live_stack，并返回它是否位于栈顶。
    # 【返回】bool：True 表示它是最上层的一个，Live 据此决定是否真正启动刷新。
    # 【注意】加锁执行；必须与 clear_live 配对，否则 _live_stack 失衡。
    # 【协作】由 Live 上下文管理器在进入时自动调用，用户通常不需要直接用。
    def set_live(self, live: "Live") -> bool:
        # 【算法】POSIX 上依次试 stdin/stdout/stderr：交互式终端里三者通常指向同一个 tty，
        """Set Live instance. Used by Live context manager (no need to call directly).

        Args:
#   但重定向输出时只有 stdin 还连着终端，这时仍能拿到正确的窗口尺寸。
# os.get_terminal_size 既能收整数 fd 也能收带 fileno() 的对象，这里传的是整数 fd。
            live (Live): Live instance using this Console.

        Returns:
            Boolean that indicates if the live is the topmost of the stack.

        Raises:
            errors.LiveError: If this Console has a Live context currently active.
        """
# 【要点】环境变量 COLUMNS/LINES 覆盖探测值：部分环境（如某些 CI、tmux 嵌套、
#   通过 ssh 执行）下 get_terminal_size 拿不到窗口大小，而 shell 会导出这两个变量。
        with self._lock:
            self._live_stack.append(live)
# 【易错】注意 size 里用 COLUMNS 覆盖时不做 legacy_windows 减一（与本文件 __init__ 里的写法不同），
#   但最终返回值那一行仍会按 _width/legacy_windows 规则再做一次处理，读的时候要连起来看。
            return len(self._live_stack) == 1

    # 【功能】解除当前 Console 上的 Live 绑定。
    # 【注意】只清除本线程的绑定；若绑定已被其它线程替换则不做处理。
    def clear_live(self) -> None:
        """Clear the Live instance. Used by the Live context manager (no need to call directly)."""
# 【易错】伪终端（pty、部分 IDE 终端、CI 伪 tty）会返回 (0, 0)，
#   若直接采用会导致 max_width=0，整个渲染输出为空。
        with self._lock:
            self._live_stack.pop()
# 【算法】最后这层的优先级容易被看错：宽度的形式是
#   "(探测值 - legacy_windows) if _width is None else _width"，
#   即显式尺寸完全跳过 legacy_windows 的减一；高度同理。
# 【为什么】显式尺寸代表用户已经自己扣除了这一列，Rich 不应再动它。

    def push_render_hook(self, hook: RenderHook) -> None:
        # 【功能】显式指定尺寸（覆盖自动探测）。
        # 【参数】new_size：(width, height) 元组，单位列/行。
        # 【注意】一旦设置，_width/_height 就不再是 None，Console 便不再跟随终端变化；
        """Add a new render hook to the stack.

#   想恢复自动探测需要手动把 _width/_height 置回 None。
        Args:
            hook (RenderHook): Render hook instance.
        """
        with self._lock:
            self._render_hooks.append(hook)

    # 【功能】弹出最近一次 push_render_hook 注册的渲染钩子。
    # 【注意】没有钩子时静默返回，便于在异常路径上无条件调用。
    def pop_render_hook(self) -> None:
        """Pop the last renderhook from the stack."""
        with self._lock:
            self._render_hooks.pop()

    # 【功能】进入上下文管理器，切换到该上下文对应的状态。
    # 【返回】self，便于 ``with ... as`` 绑定。
    # 【注意】进入与退出必须严格配对，否则缓冲索引或主题栈会失衡。
    def __enter__(self) -> "Console":
        """Own context manager to enter buffer context."""
        self._enter_buffer()
        return self
# 【要点】width 每次都重新求值（可能触发一次系统调用），不是缓存值；
#   热路径代码通常会先把它存进局部变量（print 内部就是直接用 self.width 的）。

# 【功能】只设置宽度（高度仍走自动探测）。
# 【易错】这里的 width 是渲染宽度，不含 extra 宽度概念；设小了会触发裁剪或换行。
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Exit buffer context."""
        self._exit_buffer()

    # 【功能】开始把后续渲染结果录制进缓冲。
    # 【注意】必须在录制前把 Console.record 置为 True，否则 _buffer 不会被填充。
    def begin_capture(self) -> None:
        """Begin capturing console output. Call :meth:`end_capture` to exit capture mode and return output."""
        self._enter_buffer()

    # 【功能】结束捕获：把缓冲内容渲染成字符串、清空缓冲、退出缓冲上下文并返回该字符串。
    # 【返回】str：本次捕获期间产生的文本（是否含 ANSI 序列取决于 color_system）。
    # 【注意】顺序是“先渲染、再清空、最后退出缓冲”，因此退出时的检查不会把已取走的内容再写一遍。
    def end_capture(self) -> str:
        """End capture mode and return captured string.

        Returns:
            str: Console output.
        """
# 【功能】只设置高度。
# 【注意】大部分 print 场景不看高度（打印多长都可以），高度主要影响 Live/Screen 与测量上限。
        render_result = self._render_buffer(self._buffer)
        del self._buffer[:]
        self._exit_buffer()
        return render_result

    # 【功能】把主题压入 Console 的主题栈，使其成为当前生效主题。
    # 【参数】inherit：True 时新主题继承默认主题，未定义的名字仍可解析。
    # 【注意】必须与 pop_theme 配对，否则主题栈会持续增长。
    def push_theme(self, theme: Theme, *, inherit: bool = True) -> None:
        # 【功能】让终端响铃（BEL）。
        # 【算法】走 control(Control.bell())，也就是往缓冲区插入一个控制 Segment，
        # 【功能】返回捕获上下文管理器：块内的输出不写终端，改存在字符串里。
        # 【返回】Capture（尚未进入，with 时才 begin_capture）。
        # 【注意】捕获的是"渲染后的文本"，包含 ANSI 样式（取决于终端能力）；
        # 【协作】典型用法：with console.capture() as capture: console.print(...) 再 capture.get()。
        # ==== 【二、终端控制类 API（副作用型）】 ====
        """Push a new theme on to the top of the stack, replacing the styles from the previous theme.
#   因此它同样遵守 is_dumb_terminal 短路与缓冲区提交时机。
        Generally speaking, you should call :meth:`~rich.console.Console.use_theme` to get a context manager, rather
        than calling this method directly.

#   想要纯文本应传 force_terminal=False 或另用 export_text。
        Args:
            theme (Theme): A theme instance.
            inherit (bool, optional): Inherit existing styles. Defaults to True.
        """
        self._theme_stack.push_theme(theme, inherit=inherit)

    # 【功能】弹出主题栈栈顶，恢复到上一层主题。
    # 【注意】栈为空时抛 IndexError，因此应保证 push/pop 成对。
    def pop_theme(self) -> None:
        """Remove theme from top of stack, restoring previous theme."""
        self._theme_stack.pop_theme()

    # 【功能】返回一个临时启用指定主题的上下文管理器。
    # 【参数】inherit：是否继承默认主题。
    # 【返回】ThemeContext。
    # 【协作】等价于 push_theme + 退出时 pop_theme，推荐用它以免忘记弹栈。
    def use_theme(self, theme: Theme, *, inherit: bool = True) -> ThemeContext:
        # 【功能】返回分页上下文管理器：块内输出交给系统分页器显示。
        # 【参数】pager：自定义 Pager（默认 SystemPager，读 $PAGER）；styles：是否保留颜色；
        # 【返回】PagerContext。
        # 【注意】分页器是外部进程，内容会先完整渲染成字符串再一次性交给它，
        """Use a different theme for the duration of the context manager.

        Args:
#   links：是否保留超链接（注意 styles=False 时链接也会被剥掉）。
#   因此超大输出会占用相应内存。
            theme (Theme): Theme instance to user.
            inherit (bool, optional): Inherit existing console styles. Defaults to True.

        Returns:
            ThemeContext: [description]
        """
        return ThemeContext(self, theme, inherit)

    # 【功能】返回当前生效的颜色等级名（如 "truecolor"、"256"、"standard"）。
    # 【返回】Optional[str]；重定向到文件或 dumb 终端时为 None。
    # 【注意】导出 HTML/SVG 时也读它来决定颜色精度。
    @property
    def color_system(self) -> Optional[str]:
        """Get color system string.

        Returns:
            Optional[str]: "standard", "256" or "truecolor".
        """

        if self._color_system is not None:
            return _COLOR_SYSTEMS_NAMES[self._color_system]
        else:
# 【功能】输出 count 个换行。
# 【参数】count：换行个数，必须 >= 0（用 assert 校验，-O 模式下会被跳过）。
# 【算法】直接 print(NewLine(count))：换行符作为普通 Segment 进入缓冲区，
#   因此同样受 crop/宽度逻辑影响（换行分段不影响内容）。
            return None

    # 【功能】返回输出目标的字符编码名（小写）。
    # 【返回】str；无法确定时返回 "utf-8"。
    # 【协作】组件用它判断能否输出框线字符与 emoji。
    @property
    def encoding(self) -> str:
        # 【功能】清屏。
        # 【参数】home=True 时额外把光标移回左上角（等价于 Ctrl+L 的效果）。
        # 【注意】清屏不重置 Rich 的内部状态：已缓冲但未提交的内容仍会照常输出。
        # 【兼容】dumb 终端下 control() 会直接短路，因此 clear() 什么都不做。
        """Get the encoding of the console file, e.g. ``"utf-8"``.

        Returns:
            str: A standard encoding string.
        """
        return (getattr(self.file, "encoding", "utf-8") or "utf-8").lower()

    # 【功能】判断输出目标是否是能理解 ANSI 转义序列的终端。
    # 【算法】优先级：显式 force_terminal > Jupyter 特判(False) > TTY_COMPATIBLE 环境变量 >
    # FORCE_COLOR 环境变量(非空即真) > file.isatty()；isatty 抛 ValueError（文件已关闭）时按非终端处理。
    # 【返回】bool。
    # 【注意】每次访问都重新判断（不缓存），但 _force_terminal 与颜色等级是构造时确定的。
    @property
    def is_terminal(self) -> bool:
        # 【功能】创建并返回一个 Status（转圈等待指示器）对象，通常配合 with 使用。
        # 【参数】spinner：动画名（内置见 rich.spinner）；spinner_style：动画样式名（默认主题里的
        # 【返回】Status 实例（其本身是上下文管理器，未进入前不会启动刷新线程）。
        # 【注意】这里延迟导入 .status 以避免 console ↔ status 的循环导入。
        """Check if the console is writing to a terminal.

        Returns:
#   status.spinner）；speed：速度倍率；refresh_per_second：刷新频率（Hz），12.5 是"不刺眼"的默认值。
            bool: True if the console writing to a device capable of
                understanding escape sequences, otherwise False.
        """
        # If dev has explicitly set this value, return it
        if self._force_terminal is not None:
            return self._force_terminal

# 【参数】refresh_per_second 的单位是 Hz：12.5 意味着每 80ms 刷新一次，
#   在 CPU 占用与动画流畅度之间取得平衡；速度倍率 speed 则缩放动画帧推进速度，
#   两者独立（一个控制"多久画一次"，一个控制"每次画第几帧"）。
        # Fudge for Idle
        if hasattr(sys.stdin, "__module__") and sys.stdin.__module__.startswith(
            "idlelib"
        ):
            # Return False for Idle which claims to be a tty but can't handle ansi codes
            return False

        if self.is_jupyter:
            # return False for Jupyter, which may have FORCE_COLOR set
            return False

        environ = self._environ
# 【要点】函数内导入：status 模块反过来导入 console，只能在使用点导入以打破循环。

        tty_compatible = environ.get("TTY_COMPATIBLE", "")
        # 0 indicates device is not tty compatible
        if tty_compatible == "0":
            return False
        # 1 indicates device is tty compatible
        if tty_compatible == "1":
            return True

        # https://force-color.org/
# 【功能】显示/隐藏光标。
# 【返回】True 表示控制码已写入，False 表示当前不是终端（什么都没做）。
# 【注意】返回值是"是否写入"，不代表终端真的照做了。
        force_color = environ.get("FORCE_COLOR")
        if force_color is not None:
            return force_color != ""

        # Any other value defaults to auto detect
        isatty: Optional[Callable[[], bool]] = getattr(self.file, "isatty", None)
        try:
            return False if isatty is None else isatty()
        except ValueError:
# 【功能】开启/关闭备用屏幕（alternate screen）。
# 【返回】True 表示成功切换；False 表示环境不支持。
# 【算法】必须同时满足 is_terminal 且非 legacy_windows 才发送控制码，
#   老控制台不支持该转义序列，发了只会打印出乱码。
# 【注意】手工开启后必须自己负责关闭（否则退出后终端停在备用屏幕，
#   用户看不到自己的 shell 滚动历史）；推荐用 console.screen() 上下文管理器。
            # in some situation (at the end of a pytest run for example) isatty() can raise
            # ValueError: I/O operation on closed file
            # return False because we aren't in a terminal anymore
            return False

    # 【功能】判断是否为 TERM=dumb 这类功能极弱的终端。
    # 【返回】bool。
    # 【注意】dumb 终端下禁用颜色与光标控制，只输出纯文本。
    @property
    def is_dumb_terminal(self) -> bool:
        """Detect dumb terminal.

        Returns:
            bool: True if writing to a dumb terminal, otherwise False.

        """
        _term = self._environ.get("TERM", "")
        is_dumb = _term.lower() in ("dumb", "unknown")
        return self.is_terminal and is_dumb

    # 【功能】用当前终端尺寸与策略生成一份 ConsoleOptions 快照。
    # 【返回】ConsoleOptions。
    # 【注意】每次访问都新建（尺寸可被随时改变），组件不要缓存它。
    @property
    def options(self) -> ConsoleOptions:
        """Get default console options."""
        size = self.size
        return ConsoleOptions(
            max_height=size.height,
# 【功能】设置终端窗口标题。
# 【返回】True 表示控制码已写入（不代表终端支持标题功能）。
# 【注意】Rich 无法把标题"恢复"为原值：设置后即便程序退出，标题也会留在终端上，
#   直到 shell/别的程序改写它。fish 与 Windows Terminal 会自动重置，其他多数终端不会。
# 【兼容】部分终端需要在配置里显式允许 OSC 标题转义序列（例如某些 xterm 配置）。
            size=size,
            legacy_windows=self.legacy_windows,
            min_width=1,
            max_width=size.width,
            encoding=self.encoding,
            is_terminal=self.is_terminal,
        )

    # 【功能】返回控制台的尺寸（列 × 行）。
    # 【算法】依次尝试：显式 _width/_height > 哑终端(80×25) > os.get_terminal_size
    # （Windows 用输出流、其它平台用标准流）> 环境变量 COLUMNS/LINES > 兜底 80×25。
    # 【返回】ConsoleDimensions（namedtuple(width, height)）。
    # 【注意】一旦显式设置过宽度/高度就固定不变，之后终端改变大小也不会跟随。
    @property
    def size(self) -> ConsoleDimensions:
        """Get the size of the console.

        Returns:
            ConsoleDimensions: A named tuple containing the dimensions.
        """

        if self._width is not None and self._height is not None:
            return ConsoleDimensions(self._width - self.legacy_windows, self._height)

        if self.is_dumb_terminal:
            return ConsoleDimensions(80, 25)

        width: Optional[int] = None
        height: Optional[int] = None

# 【功能】返回备用屏幕上下文管理器（进入切换、退出还原、可隐藏光标、可加整体样式）。
# 【参数】hide_cursor：是否隐藏光标（默认 True）；style：屏幕整体样式（None 视为空样式）。
# 【返回】ScreenContext。
# 【注意】进入失败（非终端/老 Windows）时 _changed 为 False，退出时也不会误关别人的备用屏幕。
        streams = _STD_STREAMS_OUTPUT if WINDOWS else _STD_STREAMS
        for file_descriptor in streams:
            try:
                width, height = os.get_terminal_size(file_descriptor)
            except (AttributeError, ValueError, OSError):  # Probably not a terminal
                pass
            else:
                break

        columns = self._environ.get("COLUMNS")
        if columns is not None and columns.isdigit():
# ==== 【三、渲染管道：renderable → Segment】 ====
# 本段是 Rich 渲染的核心：先把对象渲染成 Segment 流（render），
# 再切成行并按需补齐（render_lines），字符串则先经 markup/emoji/高亮处理成 Text（render_str）。
            width = int(columns)
        lines = self._environ.get("LINES")
        if lines is not None and lines.isdigit():
# 【返回】Measurement(minimum, maximum)：最小值是"不可压缩时至少需要多宽"，
#   最大值是"内容自然想要多宽"。容器据此决定是否折行、是否截断。
            height = int(lines)

        # get_terminal_size can report 0, 0 if run from pseudo-terminal
        width = width or 80
        height = height or 25
        return ConsoleDimensions(
            width - self.legacy_windows if self._width is None else self._width,
            height if self._height is None else self._height,
        )
# 【要点】Measurement.get 会优先调用对象的 __rich_measure__（容器可以精确报告所需宽度），
#   没有该协议则退化为"渲染一遍取最大行宽"；结果被夹在 options 的 [min_width, max_width] 内。
# 【易错】对没有测量协议的 renderable，measure 会真的渲染一次，因此不要在渲染循环里反复调用。

# 【要点】返回的是 Measurement 值对象，调用方通常只看 .maximum；
#   若 renderable 未实现测量协议，这个值来自"真渲染一遍"的结果。
    @size.setter
# 【功能】把任意 renderable 渲染成 Segment 的可迭代对象（生成器）。
# 【算法】1) 取选项（默认 self.options）；2) max_width < 1 时直接返回，不产出任何内容；
#   3) rich_cast 处理 __rich__ 转换；4) 有 __rich_console__ 且不是类对象 → 采用其返回值；
#   5) 是 str → 先 render_str（按 options 的 markup/highlight 决定）再走同一协议；
#   6) 否则抛 NotRenderableError；7) iter() 失败同样抛 NotRenderableError；
#   8) 遍历结果：Segment 直接 yield，其他对象递归 self.render（此时 options 已 reset_height）。
# 【参数】renderable：str/Segment/实现 __rich_console__ 或 __rich__ 的对象；
#   options：本次渲染约束，None 表示用 self.options。
# 【返回】生成器（Iterable[Segment]）——惰性的，不被消费就不会真正渲染。
# 【注意】递归用的是"重置了高度"的选项：高度预算只约束当前层，否则嵌套容器会被上层剩余高度
#   提前截断；而宽度是逐层收窄的，子项不允许超出父容器给的宽度。
# 【易错】isinstance(renderable, type) 是在排除类对象本身：类也有 __rich_console__ 属性
#   （未绑定函数），直接调用会因缺少 self 而报错。
    def size(self, new_size: Tuple[int, int]) -> None:
        # 【易错】返回的是生成器而不是列表：直接打印它的结果是生成器对象；
        """Set a new size for the terminal.

#   想查看内容需要 list(console.render(...))。另外生成器只能消费一次。
        Args:
            new_size (Tuple[int, int]): New width and height.
        """
        width, height = new_size
        self._width = width
        self._height = height

    # 【功能】读取 / 设置可用宽度（单元格数）。
    # 【注意】以 size 为准；显式设置会覆盖终端探测结果。
    @property
    def width(self) -> int:
        # 【要点】零宽度短路：既避免 split 逻辑产生空行，也防止"容器给子项 0 宽度、
        """Get the width of the console.

#   子项又请求测量"造成的无限递归。裸 return 结束生成器，产出空序列。
        Returns:
            int: The width (in characters) of the console.
        """
# 【要点】rich_cast 只处理"实现了 __rich__ 的对象"，返回原对象或转换结果；
#   转换结果仍可能是字符串或 renderable，所以后面按转换后的类型再判断一次。
        return self.size.width

    @width.setter
# 【要点】字符串走 render_str 时显式透传 options 的 highlight/markup：
#   这样 print(..., highlight=False) 之类的调用能一路影响到字符串的解析方式。
    def width(self, width: int) -> None:
        """Set width.

        Args:
            width (int): New width.
        """
        self._width = width

    # 【功能】读取 / 设置可用高度（行数）。
    # 【注意】height 只影响需要限额的场景（如 Status、分页），普通输出不因它而截断。
    @property
    def height(self) -> int:
        """Get the height of the console.

        Returns:
            int: The height (in lines) of the console.
        """
        return self.size.height

# 【要点】递归前重置 height：高度只对本层有效（详见 reset_height 的说明）。
    @height.setter
    def height(self, height: int) -> None:
        # 【功能】把 renderable 渲染成"行的列表"，每行是 Segment 列表（供 Panel 等容器二次排版）。
        # 【参数】options：渲染选项，None 用 self.options；style：额外整体样式（既作用于内容，
        # 【返回】List[List[Segment]]；若 options.height 有值，返回行数恰好等于 height
        # 【算法】1) 加锁（渲染会触碰共享状态）；2) render 得到 Segment 流并可选套 style；
        # 【注意】height 为 None 时不做补齐，返回行数完全由内容决定；
        # 【易错】style 既作为 apply_style 的内容样式、又传给 split_and_crop_lines 作为补齐样式，
        """Set height.

        Args:
            height (int): new height.
#   也用于补齐出来的空白段）；pad：短行是否用空格补齐到 max_width；
#   new_lines：行尾是否附上换行 Segment。
#   （不足的部分用整行空格补齐，超出则截断）。
#   3) Segment.split_and_crop_lines 按 max_width 切行并按需补齐；
#   4) islice 截到 height；5) height 未满时用空格行补足。
#   new_lines=True 时补齐行也带换行 Segment，因此两种行表示一致。
#   空白段因此也带样式——对带背景色的容器这是必要的（否则背景会断在内容末尾）。
        """
        self._height = height

    # 【功能】向终端发送响铃控制字符。
    # 【注意】仅在 is_terminal 为真时写出，避免把 BEL 写进日志文件。
    def bell(self) -> None:
        """Play a 'bell' sound (if supported by the terminal)."""
        self.control(Control.bell())

    # 【功能】返回一个录制后续输出的上下文管理器。
    # 【返回】Capture。
    # 【注意】内部会临时把 record 置为 True，并在退出时恢复原值。
    def capture(self) -> Capture:
        # 【协作】Panel 的实现正是先对内部内容调用 render_lines，再给每行加左右边框字符；
        """A context manager to *capture* the result of print() or log() in a string,
#   所以 render_lines 的 pad/height 语义直接决定了面板是否能画出规整的矩形。
        rather than writing it to the console.

        Example:
            >>> from rich.console import Console
            >>> console = Console()
            >>> with console.capture() as capture:
            ...     console.print("[bold magenta]Hello World[/]")
            >>> print(capture.get())

        Returns:
            Capture: Context manager with disables writing to the terminal.
        """
        capture = Capture(self)
        return capture
# 【要点】render 是惰性生成器，这里的 list(islice(...)) 才是真正触发渲染的地方；
#   因此 render_lines 内部不存在"先渲染一遍再复制"的额外开销。

    def pager(
        self, pager: Optional[Pager] = None, styles: bool = False, links: bool = False
# 【易错】height 可能是负数（容器计算剩余空间时会产生），
#   负数传给 islice 的 stop 会抛 ValueError，所以先夹到 0。
    ) -> PagerContext:
        # 【算法】补齐只在 height 显式给出时启用：先构造一条"整行空格（按需带换行）"的模板行，
        #   再用列表乘法复制 extra_lines 份——【优化】模板只构造一次。
        """A context manager to display anything printed within a "pager". The pager application
        is defined by the system and will typically support at least pressing a key to scroll.

        Args:
            pager (Pager, optional): A pager object, or None to use :class:`~rich.pager.SystemPager`. Defaults to None.
            styles (bool, optional): Show styles in pager. Defaults to False.
            links (bool, optional): Show links in pager. Defaults to False.

        Example:
            >>> from rich.console import Console
            >>> from rich.__main__ import make_test_card
            >>> console = Console()
            >>> with console.pager():
                    console.print(make_test_card())

        Returns:
            PagerContext: A context manager.
        """
        return PagerContext(self, pager=pager, styles=styles, links=links)

    # 【功能】输出指定数量的换行。
    # 【参数】count：换行个数，默认 1。
    # 【注意】用一次 write 写出多个换行，避免逐行刷屏。
    def line(self, count: int = 1) -> None:
        # 【要点】new_lines=True 时补齐行由两个 Segment 组成（空格 + 换行），
        """Write new line(s).
#   与 split_and_crop_lines(include_new_lines=True) 的输出形态保持一致；
#   new_lines=False 时只有一个空格 Segment，便于按"行"调用方自行拼接。

        Args:
            count (int, optional): Number of new lines. Defaults to 1.
        """

        assert count >= 0, "count must be >= 0"
        self.print(NewLine(count))

# 【功能】把字符串转成 Text 实例，途中完成 emoji 替换、markup 解析与自动高亮。
# 【参数】style：整体样式（样式名或 Style）；justify/overflow：写到 Text 上的属性；
#   emoji/markup/highlight：三态开关，None 表示沿用 Console 默认；
#   highlighter：本次使用的高亮器（None 表示用 self.highlighter）。
# 【返回】Text（已应用高亮与标记）；注意函数标注为 Text，而文档字符串写的是 ConsoleRenderable。
# 【算法】1) 三态开关解析：x or (x is None and default)，保证显式 False 能覆盖默认 True；
#   2) markup 开：render_markup 解析标签并附加 style，随后覆写 justify/overflow；
#      markup 关：直接 Text(...)，emoji 开启时先做 _emoji_replace；
#   3) 高亮开启时用高亮器产生新 Text，并把原文样式 copy_styles 回来（保住 style/span）；
#   4) 否则直接返回 Text。
# 【注意】高亮分支返回的是高亮器产出的新对象：render_markup 设好的 justify/overflow 在
#   Text.copy_styles 中不会被复制（那里只搬样式与 span），因此"同时开启 markup 与高亮"时
#   justify/overflow 可能丢失——这是容易被忽略的交互。
# 【为什么高亮在 markup 之后】用户显式写的 markup 样式应当压过自动推断的语法高亮，
#   所以顺序必须是"先高亮、再把原文样式套回去"。
    def clear(self, home: bool = True) -> None:
        """Clear the screen.

        Args:
            home (bool, optional): Also move the cursor to 'home' position. Defaults to True.
        """
        if home:
            self.control(Control.clear(), Control.home())
        else:
            self.control(Control.clear())

    # 【功能】返回一个在底部显示状态行（可带 spinner）的上下文管理器。
    # 【参数】status：状态文本；spinner：动画样式；refresh_per_second：刷新频率。
    # 【返回】Status。
    # 【注意】非终端环境下自动退化为静态输出。
    def status(
# 【协作】调用链：Console.print(str) → _collect_renderables → render_str →
#   markup.render（解析标签）→ Text；随后 Console.render 再把 Text 渲染成 Segment。
        self,
        status: RenderableType,
        *,
        spinner: str = "dots",
        spinner_style: StyleType = "status.spinner",
        speed: float = 1.0,
        refresh_per_second: float = 12.5,
    ) -> "Status":
        # 【要点】这种三态写法让"显式 False"优先于 Console 默认的 True：
        # 【易错】markup 开与关走的是两条完全不同的构造路径：
        """Display a status and spinner.

        Args:
            status (RenderableType): A status renderable (str or Text typically).
            spinner (str, optional): Name of spinner animation (see python -m rich.spinner). Defaults to "dots".
            spinner_style (StyleType, optional): Style of spinner. Defaults to "status.spinner".
#   print(文本, emoji=False) 能真正关掉 emoji，而不是被默认值覆盖。
#   markup/highlight 上重复出现同样的模式，是本文件最常见的惯用法。
            speed (float, optional): Speed factor for spinner animation. Defaults to 1.0.
            refresh_per_second (float, optional): Number of refreshes per second. Defaults to 12.5.

#   markup 分支由 render_markup 负责 emoji 替换（它内部会调用 _emoji_replace），
#   并直接返回一个可继续加工 Text；非 markup 分支则自己处理 emoji 并构造 Text。
#   两条路径都必须保留 justify/overflow，否则后续对齐行为会不一致。
        Returns:
            Status: A Status object that may be used as a context manager.
        """
        from .status import Status

        status_renderable = Status(
            status,
# 【要点】render_markup 返回的 Text 允许就地改属性（它是一次性对象，不会被共享），
#   于是这里用两步赋值而不是构造参数，避免再走一遍 Text 的构造逻辑。
            console=self,
            spinner=spinner,
            spinner_style=spinner_style,
# 【算法】非 markup 分支的三元表达式把 emoji 替换内联在 Text 构造里：
#   emoji 关闭时直接复用原字符串（不做任何扫描），这是热路径上的常见优化写法。
            speed=speed,
            refresh_per_second=refresh_per_second,
        )
        return status_renderable

    # 【功能】显示 / 隐藏终端光标。
    # 【参数】show：True 显示、False 隐藏。
    # 【返回】bool，表示本次是否真的发送了控制序列。
    # 【注意】非终端环境直接返回 False，不做任何输出。
    def show_cursor(self, show: bool = True) -> bool:
        # 【要点】只有高亮启用时才解析高亮器：省一次属性访问与判断（热路径【优化】），
        """Show or hide the cursor.

        Args:
            show (bool, optional): Set visibility of the cursor.
#   也让 highlight=False 时完全不产生额外的 Text 对象。
        """
        if self.is_terminal:
# 【算法】高亮器的契约是"字符串进、Text 出"，所以这里先 str() 拍平，
#   再把原 Text 的样式/span 搬回高亮结果（copy_styles）。
# 【易错】str(rich_text) 会丢掉 Text 上的 justify/overflow 等属性，只保留文本与 span；
#   这正是上面"高亮会吞掉 justify/overflow"的根因。
            self.control(Control.show_cursor(show))
            return True
        return False

# 【功能】把"样式名或样式定义字符串"解析成 Style 实例（主题优先）。
# 【参数】name：样式名（如 table.header）或字面定义（如 bold red on white），也可以是 Style；
#   default：解析失败时的回退样式（同样会被递归解析）。
# 【返回】Style；命中主题且该样式带 link 时返回副本，其余情况直接返回缓存对象。
# 【算法】1) 传入已是 Style → 原样返回；2) 先 self._theme_stack.get(name) 查主题；
#   3) 未命中则 Style.parse(name) 按字面解析；4) StyleSyntaxError 时若有 default 则递归解析它，
#   否则抛 MissingStyle（并带上原始语法错误信息）。
# 【注意】为什么带 link 的要 copy()：link 在渲染时会按终端能力被处理，共享缓存对象若被
#   就地修改会污染全局样式表，所以先复制一份。
# 【协作】主题栈是线程局部的，因此同一 Console 在不同线程可以有各自的样式解析结果。
    def set_alt_screen(self, enable: bool = True) -> bool:
        """Enables alternative screen mode.

        Note, if you enable this mode, you should ensure that is disabled before
        the application exits. See :meth:`~rich.Console.screen` for a context manager
        that handles this for you.

        Args:
            enable (bool, optional): Enable (True) or disable (False) alternate screen. Defaults to True.

        Returns:
            bool: True if the control codes were written.

        """
        changed = False
# 【算法】两条渲染分支：无整体样式时直接 extend（最快路径）；
#   有样式时必须先按行拆分再逐行 apply_style，最后把换行 Segment 补回
#   （split_lines_terminator 返回"行内容 + 是否原本以换行结尾"，正是为此设计）。
# 【为什么不能直接对整段套样式】直接套会把换行符也纳入样式范围，
#   在带背景色的样式下会渲染出一整行背景色延伸到行尾之外的效果。
        if self.is_terminal and not self.legacy_windows:
            self.control(Control.alt_screen(enable))
# 【为什么】link 会在 Segment 渲染时按终端能力决定是否输出超链接（见 Style/Segment 的实现），
#   若多个调用方共享同一个 Style 对象并有人就地修改，就会相互影响，故先复制。
#   link 为空时 Style 是纯不可变数据，直接共享更省内存。
            changed = True
            self._is_alt_screen = enable
        return changed

    # 【功能】查询当前是否处于备用屏幕（alternate screen）模式。
    # 【返回】bool。
    @property
    def is_alt_screen(self) -> bool:
        # 【功能】print/log 共用的"参数收集器"：把 *objects 与 sep/end 规整成一个 renderable 列表。
        # 【参数】objects：任意对象；sep：相邻对象之间的分隔串；end：末尾追加的串；
        # 【返回】List[ConsoleRenderable]：元素依次是 Align（当 justify 为 left/center/right 时）
        # 【算法】1) 若 justify 属于 left/center/right，把 append 换成 align_append，
        # 【注意】justify 为 left/center/right 时，文本片段先被合并再被 Align 包裹，
        # 【为什么这样设计】字符串与 renderable 混排时 sep 的插入位置很难统一，
        """Check if the alt screen was enabled.
#   justify/emoji/markup/highlight：三态开关。
#   以及合并后的 Text、ConsoleRenderable、Pretty 等。
#      使每个 renderable 都被 Align 包一层；2) 选出高亮器；
#   3) 遍历 objects：str → render_str；Text → 直接收集；ConsoleRenderable → 先 flush 文本再收；
#      可展开对象（dict/list 等）→ Pretty；其他 → 用高亮器 str() 成 Text；
#   4) 连续的 Text 用 Text(sep, justify=justify, end=end).join(text) 合并——
#      join 会把 sep 插在中间、end 追加到末尾；5) 若 Console 设了整体 style，
#      再给每个 renderable 套一层 Styled。
#   因此 sep 的插入位置仍由 join 决定；而 full 不包 Align，它只影响 Text 内部的两端对齐。
#   于是用 text 列表攒连续的文本片段，遇到真正的 renderable 才 flush 一次。

        Returns:
            bool: True if the alt screen was enabled, otherwise False.
        """
        return self._is_alt_screen

    # 【功能】设置终端窗口标题。
    # 【返回】bool：True 表示已写出控制序列，False 表示当前不是终端（未做任何事）。
    # 【注意】Rich 无法把标题恢复成原值，设置后即使程序退出也可能保留；
    # 部分终端/复用器需要额外配置，shell 提示符也可能覆盖它，因此返回 True 并不保证标题真的变了。
    def set_window_title(self, title: str) -> bool:
        # 【功能】判断对象是否适合交给 Pretty 美化打印（dict/list/实现了 __rich_repr__ 的对象等）。
        # 【算法】函数内延迟导入 pretty.is_expandable：pretty 会间接依赖 Console 相关模块，
        # 【优化】每次调用都会执行一次 import，但 Python 命中 sys.modules 缓存，开销极小。
        # 【要点】把 append 缓存在局部变量（【优化】）：这个循环每次 print 都会跑，
        # 【算法】用闭包替换 append，而不是在循环里逐项判断：对齐包装只影响"如何追加"，
        # 【易错】default 与 full 不走这里：前者表示交给 renderable 自己决定，
        """Set the title of the console terminal window.

        Warning: There is no means within Rich of "resetting" the window title to its
        previous value, meaning the title you set will persist even after your application
        exits.

        ``fish`` shell resets the window title before and after each command by default,
        negating this issue. Windows Terminal and command prompt will also reset the title for you.
        Most other shells and terminals, however, do not do this.

        Some terminals may require configuration changes before you can set the title.
        Some terminals may not support setting the title at all.

        Other software (including the terminal itself, the shell, custom prompts, plugins, etc.)
        may also set the terminal window title. This could result in whatever value you write
        using this method being overwritten.
#   放在模块级会形成循环导入。

        Args:
            title (str): The new title of the terminal window.

#   提前取出方法可以省下大量属性查找开销。
        Returns:
            bool: True if the control code to change the terminal title was
                written, otherwise False. Note that a return value of True
                does not guarantee that the window title has actually changed,
                since the feature may be unsupported/disabled in some terminals.
#   收集逻辑本身因此保持单一分支。
#   后者是 Text 内部的两端对齐算法。
        """
        if self.is_terminal:
# 【要点】cast 只是给类型检查器的提示：justify 在运行时的取值已被上一行的
#   in ("left", "center", "right") 收窄，但类型系统看不到这层收窄。
            self.control(Control.title(title))
            return True
        return False

# ==== 【二·补、终端副作用 API 的收尾】 ====
    def screen(
# 【算法】把攒下的 Text 片段合并成一个：Text(sep, ...).join(text) 负责插入分隔符，
#   end 参数保证末尾串只追加一次；合并后清空列表，等待下一批文本。
        self, hide_cursor: bool = True, style: Optional[StyleType] = None
    ) -> "ScreenContext":
        # 【要点】clear() 而不是新建列表：保持闭包捕获的同一个列表对象，
        # 【要点】rich_cast 让"实现了 __rich__ 的对象"在这里就被转换，后续分支只需处理具体类型。
        # 【易错】isinstance(renderable, Text) 必须排在 ConsoleRenderable 之前判断：
        """Context manager to enable and disable 'alternative screen' mode.

#   这样 append_text 引用始终有效（否则要在闭包里重新绑定，写法会变复杂）。
        Args:
#   Text 自身也实现了 __rich_console__，顺序颠倒会让 Text 被单独渲染，
#   从而失去与相邻字符串共享 sep/end 的能力。
            hide_cursor (bool, optional): Also hide the cursor. Defaults to False.
            style (Style, optional): Optional style for screen. Defaults to None.

        Returns:
            ~ScreenContext: Context which enables alternate screen on enter, and disables it on exit.
        """
        return ScreenContext(self, hide_cursor=hide_cursor, style=style or "")

    # 【功能】测量一个可渲染对象占用的最小与最大宽度。
    # 【参数】renderable：目标对象；options：可选的渲染上下文。
    # 【返回】Measurement(minimum, maximum)。
    # 【算法】对象实现 __rich_measure__ 则调用它，否则渲染成 Segment 后按行统计单元格宽度。
    # 【注意】这是 Table 列宽分配的基础：先量后排，避免“排完才知道多宽”的循环依赖。
    def measure(
        self, renderable: RenderableType, *, options: Optional[ConsoleOptions] = None
    ) -> Measurement:
        # 【要点】直接传入的 Text 不经过 render_str：调用方已经构造好了 Text，
        # 【算法】dict/list/set 等"可展开"对象交给 Pretty 美化（这就是 console.print 输出字典时
        """Measure a renderable. Returns a :class:`~rich.measure.Measurement` object which contains
#   再解析一遍会破坏其内部的 span/样式，也会重复付出解析开销。
        information regarding the number of characters required to print the renderable.

        Args:
            renderable (RenderableType): Any renderable or string.
            options (Optional[ConsoleOptions], optional): Options to use when measuring, or None
#   会看到彩色结构体的原因）；不可展开的对象（如普通类实例）退化为 str() 后高亮。
                to use default options. Defaults to None.

        Returns:
            Measurement: A measurement of the renderable.
        """
# 【要点】最后的兜底分支把所有"不是 renderable、不可展开"的对象变成高亮后的文本。
#   这就是 print(3.14)、print(自定义对象) 也能有颜色的原因；
#   代价是对对象调用了 str()（可能触发 __str__ 的副作用）与高亮器的正则匹配。
        measurement = Measurement.get(self, options or self.options, renderable)
        return measurement
# 【要点】Console 级别的整体 style 最后统一包一层 Styled：实现简单（不必在每个收集分支
#   里处理），代价是多一层包装对象、多一次渲染转发。
# 【要点】判断用 is not None 而不是真值判断：
#   style="" 是合法的"空样式"，用真值判断会把它当作未设置而跳过包装。

    def render(
# 【注意】这里重新赋值了 renderables 变量（整体替换而不是就地修改），
#   调用方拿到的是新列表，不会影响传入的 objects。
        self, renderable: RenderableType, options: Optional[ConsoleOptions] = None
    ) -> Iterable[Segment]:
# 【功能】打印一条水平分隔线（可带标题）。
# 【参数】title：标题文本；characters：组成线条的字符（默认 ─）；
#   style：线条样式名（默认主题的 rule.line）；align：标题对齐方式。
# 【算法】构造 rich.rule.Rule 后走普通 print，因此宽度、裁剪与高亮规则与其它输出完全一致。
        """Render an object in to an iterable of `Segment` instances.

        This method contains the logic for rendering objects with the console protocol.
        You are unlikely to need to use it directly, unless you are extending the library.

        Args:
            renderable (RenderableType): An object supporting the console protocol, or
                an object that may be converted to a string.
            options (ConsoleOptions, optional): An options object, or None to use self.options. Defaults to None.

        Returns:
            Iterable[Segment]: An iterable of segments that may be rendered.
        """

        _options = options or self.options
        if _options.max_width < 1:
            # No space to render anything. This prevents potential recursion errors.
            return
# ==== 【四、打印路径：control / out / print】 ====
# 【功能】把若干 Control（光标移动、清屏、标题等转义序列）插入输出流。
# 【算法】with self: 进入缓冲区，把每个 Control 的 Segment 追加到 _buffer，
#   退出时统一提交——保证一串控制码要么整段发出，要么都不发。
# 【注意】dumb 终端下整个方法短路：TERM=dumb 的终端不解释任何转义序列，
#   发过去只会看到乱码。
# 【易错】这些 Segment 的 control 字段非空，非终端环境（管道/重定向）下
#   _render_buffer 会丢弃它们，因此重定向文件里不会出现光标控制字符。
        render_iterable: RenderResult

        renderable = rich_cast(renderable)
        if hasattr(renderable, "__rich_console__") and not isinstance(renderable, type):
            render_iterable = renderable.__rich_console__(self, _options)
        elif isinstance(renderable, str):
# 【要点】用 with self 而不是手工 _enter/_exit_buffer：保证控制码序列作为一个原子块提交，
#   即使 _buffer.extend 抛异常，__exit__ 也会把缓冲深度还原（不会让 Console 卡在缓冲状态）。
            text_renderable = self.render_str(
# 【要点】生成器表达式在这里是安全的：extend 会立刻消费它；
#   注意变量名用的是 _control 以避免遮蔽方法参数 control（元组）。
# 【易错】非终端环境下这些 Segment 最终会被 _render_buffer 丢弃，
#   因此控制码不会污染重定向的文件内容。
                renderable, highlight=_options.highlight, markup=_options.markup
# 【功能】低层输出：像内置 print 一样原样写出对象，不做 markup、不美化、不换行处理。
# 【参数】sep/end：与内置 print 一致；style：可选整体样式；highlight：是否高亮（None 用默认）。
# 【算法】先把所有对象 str() 后用 sep 拼接，再调用 print 并显式传入：
#   emoji=False、markup=False（不求值标签与 emoji）、no_wrap=True（不折行）、
#   overflow="ignore"（超出宽度也不裁剪）、crop=False（保留完整内容）。
# 【注意】它是 print 的一层"取消所有智能行为"的包装，用于写入日志行、进度条等
#   需要精确控制字节内容的场景。
# 【易错】因为 crop=False，长行会超过终端宽度并依赖终端自动折行——
#   预计算行高（如 Live）时不能假设每行只占一行屏幕。
            )
            render_iterable = text_renderable.__rich_console__(self, _options)
        else:
# 【注意】log 的参数表刻意与 print 保持一致的命名（sep/end/style/justify/emoji/markup/highlight），
#   但缺少 width/height/crop/soft_wrap/new_line_start：日志行宽由 self.width 统一裁剪。
            raise errors.NotRenderableError(
                f"Unable to render {renderable!r}; "
                "A str, Segment or object with __rich_console__ method is required"
            )

        try:
            iter_render = iter(render_iterable)
        except TypeError:
            raise errors.NotRenderableError(
                f"object {render_iterable!r} is not renderable"
# 【易错】文档字符串里的换行写法在源码里是转义后的 "\\n"，实际默认值是单个换行符 "\n"。
#   阅读时不要把文档里的双反斜杠当成两个字符。
            )
        _Segment = Segment
        _options = _options.reset_height()
        for render_output in iter_render:
            if isinstance(render_output, _Segment):
                yield render_output
            else:
                yield from self.render(render_output, _options)

    # 【功能】把 renderable 渲染成“按行切好的 Segment 列表”，供需要逐行处理的场景使用（如 Panel 画边框）。
    # 【参数】options：None 时用 self.options；style：额外套用的样式；
    # pad=True 把短行补空格到 max_width；new_lines=True 在每行末尾附加换行段。
    # 【返回】List[List[Segment]]；受 height 限制时会截断并补足空白行。
    # 【注意】整个函数在 self._lock 内执行，可多线程安全调用；height 为负时按 max(0, ...) 处理。
    def render_lines(
        self,
        renderable: RenderableType,
        options: Optional[ConsoleOptions] = None,
        *,
        style: Optional[Style] = None,
        pad: bool = True,
        new_lines: bool = False,
# 【功能】Rich 的主输出入口：把任意对象渲染成带样式、按宽度排版的内容并写出。
# 【参数】objects：待输出对象；sep/end：分隔与结尾；style：整体样式（会逐行套用并补回换行）；
#   justify：default/left/center/right/full；overflow：ignore/crop/fold/ellipsis；
#   no_wrap：禁止自动换行（长词也会被硬裁）；emoji/markup/highlight：三态开关；
#   width：限制本次输出宽度（取 min(width, console.width)）；height：限制高度预算；
#   crop：是否把输出裁剪到终端宽度（默认 True）；soft_wrap：软换行模式（None 用 Console 默认）；
#   new_line_start：当内容超过一行时，先在前面补一个空行（用于与上文隔开）。
# 【算法】完整流程：
#   1) objects 为空 → 换成 (NewLine(),)，于是 print() 等价于输出一个换行；
#   2) 解析 soft_wrap：为真则强制 no_wrap=True、overflow="ignore"、crop=False
#      （即完全不干预换行，交给终端决定）；
#   3) 复制渲染钩子快照（钩子内再增删钩子不影响本次）；
#   4) with self: 进入缓冲区，_collect_renderables 收集（markup/emoji/highlight 在此生效）；
#   5) 依次调用每个钩子处理 renderable 列表；
#   6) 由 self.options 派生 render_options（justify/overflow/no_wrap/markup/highlight/宽度/高度）；
#   7) 逐个 render 成 Segment；若给了 style，则按行拆分后逐行 apply_style 并补回换行 Segment
#      （这样样式不会跨行丢失，也不会吃掉行尾换行）；
#   8) new_line_start 时按"渲染结果是否多于一行"决定是否插入前导空行；
#   9) crop 为真 → Segment.split_and_crop_lines(..., self.width, pad=False) 裁到终端宽度；
#      否则把原始 Segment 直接放进缓冲区。
# 【返回】None（内容进入缓冲区，退出 with 时写出）。
# 【注意】几个容易踩的点：
#   1) soft_wrap=True 会同时关掉 crop 与换行，因此输出行可能远宽于终端（见下）；
#   2) width 只会收窄不会放宽（min(width, self.width)），无法用它放大输出；
#   3) 一次 print 的所有内容在同一个缓冲区提交中写出，因此多线程下不会交错；
#   4) crop 切行是按 Segment 内容宽度做的，宽字符（中文、emoji）按单元格计宽。
# 【易错】soft_wrap 与 overflow 的关系是最容易被误解的：soft_wrap=True 时即使
#   overflow="crop" 也会被 crop=False 覆盖（第 3 步先设 overflow 再设 crop），
#   所以"软换行"实质是"不做任何换行与裁剪"。
    ) -> List[List[Segment]]:
        """Render objects in to a list of lines.

        The output of render_lines is useful when further formatting of rendered console text
        is required, such as the Panel class which draws a border around any renderable object.

        Args:
            renderable (RenderableType): Any object renderable in the console.
            options (Optional[ConsoleOptions], optional): Console options, or None to use self.options. Default to ``None``.
            style (Style, optional): Optional style to apply to renderables. Defaults to ``None``.
            pad (bool, optional): Pad lines shorter than render width. Defaults to ``True``.
            new_lines (bool, optional): Include "\n" characters at end of lines.

        Returns:
            List[List[Segment]]: A list of lines, where a line is a list of Segment objects.
        """
        with self._lock:
            render_options = options or self.options
            _rendered = self.render(renderable, render_options)
            if style:
                _rendered = Segment.apply_style(_rendered, style)

            render_height = render_options.height
            if render_height is not None:
                render_height = max(0, render_height)

            lines = list(
                islice(
                    Segment.split_and_crop_lines(
                        _rendered,
                        render_options.max_width,
                        include_new_lines=new_lines,
# 【要点】官方文档用一句话点明了 soft_wrap 的本质：它同时关闭"词换行"与"裁剪"。
#   因此开启后 Console.width 只影响 renderable 自身的排版决策，不再约束最终写出宽度。
# 【易错】这也意味着 soft_wrap=True 时无法用 width 参数限制行宽——
#   width 只收窄 options，而 crop=False 让超宽的行原样进入缓冲区。
                        pad=pad,
                        style=style,
                    ),
                    None,
# 【要点】空参数不是错误：print() 输出一个换行，与内置 print() 行为对齐，
#   也保证 progress 清行等场景可以直接调用 console.print()。
                    render_height,
# 【易错】这里是把整个 objects 元组替换成单元素元组，
#   因此 print() 不会输出 sep（本来也没有对象可分隔），只输出一个换行。
                )
# 【算法】soft_wrap 的三态：None → 用 Console 构造时的默认值；
#   随后若为真，同时把 no_wrap 默认设为 True、overflow 默认设为 ignore、crop 强制为 False。
# 【易错】注意这里用的是"若用户没显式给 no_wrap/overflow 才设置"的写法，
#   但 crop = False 是无条件覆盖——所以 soft_wrap=True 时传入 crop=True 也无效。
            )
            if render_options.height is not None:
# 【算法】soft_wrap 的三个副作用一次设定：no_wrap（只在用户没给时）、
#   overflow="ignore"（只在用户没给时）、crop=False（无条件）。
# 【易错】第 3 条无条件覆盖意味着 soft_wrap=True 与 crop=True 同时传入时，前者胜出。
                extra_lines = render_options.height - len(lines)
                if extra_lines > 0:
                    pad_line = [
# 【约束】只在用户没显式指定时设置 overflow：soft_wrap 是"默认策略"，
#   但 crop = False 是无条件的——因为裁剪会直接改变行结构，与软换行的意图冲突。
                        (
                            [
                                Segment(" " * render_options.max_width, style),
# 【要点】快照式复制：钩子列表可能在渲染过程中被增删（例如 Live 内部再套 Live），
#   复制后本次遍历的对象集合固定，避免"边遍历边修改"导致的跳项或异常。
                                Segment("\n"),
                            ]
                            if new_lines
                            else [Segment(" " * render_options.max_width, style)]
                        )
                    ]
                    lines.extend(pad_line * extra_lines)

            return lines

    # 【功能】把字符串转成 Text（或高亮后的 Text）；print/log 收到 str 时会自动调用它。
    # 【参数】style：整体样式；emoji/markup/highlight：None 表示沿用 Console 的默认开关；highlighter：指定高亮器。
    # 【返回】Text：markup 开启时经标记解析，否则直接构造 Text 并做 emoji 替换；
    # 高亮开启时返回的是高亮器产出的 Text（原样式已被复制过去）。
    # 【注意】返回值不保证是“输入字符串对应的那个 Text”——高亮路径下它是高亮器的结果对象。
    def render_str(
# 【易错】钩子拿到的是"已收集但未渲染"的 renderable 列表，
#   因此钩子可以替换内容、插入额外内容，甚至清空列表让本次 print 无输出；
#   但它无法修改渲染选项（那些在下一步才生成）。
        self,
        text: str,
# 【算法】派生本次渲染选项：justify/overflow/no_wrap/markup/highlight 直接透传（None/NO_CHANGE
#   表示沿用根选项）；width 用 min(width, self.width) ——只允许收窄，不允许超过终端宽度；
#   height 用于限制高度预算（如放进 Live 区域时）。
# 【注意】这里把 markup/highlight 也写进 options，是为了让 render() 对字符串走正确的解析分支。
        *,
        style: Union[str, Style] = "",
        justify: Optional[JustifyMethod] = None,
# 【易错】width=None 时传 NO_CHANGE 而不是 None：
#   否则 update 会把 min_width/max_width 都置为 None，后续比较会抛 TypeError。
        overflow: Optional[OverflowMethod] = None,
        emoji: Optional[bool] = None,
        markup: Optional[bool] = None,
        highlight: Optional[bool] = None,
        highlighter: Optional[HighlighterType] = None,
    ) -> "Text":
# 【优化】缓存 extend 与 render 到局部变量：下面的循环是 print 的热点路径，
#   避免每次迭代都做两次属性查找。
        # 【算法】样式只解析一次（在循环外），循环内对每行 Segment 套用同一个 Style 对象——
        # 【算法】split_lines_terminator 返回 (该行 Segment 列表, 该行是否原本以换行结尾)，
        """Convert a string to a Text instance. This is called automatically if
        you print or log a string.

        Args:
            text (str): Text to render.
            style (Union[str, Style], optional): Style to apply to rendered text.
            justify (str, optional): Justify method: "default", "left", "center", "full", or "right". Defaults to ``None``.
#   这也是 get_style 对带 link 的样式返回副本的实际收益点之一。
            overflow (str, optional): Overflow method: "crop", "fold", or "ellipsis". Defaults to ``None``.
            emoji (Optional[bool], optional): Enable emoji, or ``None`` to use Console default.
            markup (Optional[bool], optional): Enable markup, or ``None`` to use Console default.
#   于是这里可以对每行独立套样式，再把换行补回去——样式不会跨行泄漏。
            highlight (Optional[bool], optional): Enable highlighting, or ``None`` to use Console default.
            highlighter (HighlighterType, optional): Optional highlighter to apply.
        Returns:
            ConsoleRenderable: Renderable object.

        """
# 【算法】判断是否需要前导空行：把已渲染的 Segment 文本拼起来按行切分，
#   行数 > 1（splitlines 对结尾换行的处理使其恰好等于"实际行数"）才插入 Segment.line()。
# 【注意】这里的拼接只为计数，不改变缓冲区内容；对超长输出会带来一次额外的字符串拼接开销。
        emoji_enabled = emoji or (emoji is None and self._emoji)
        markup_enabled = markup or (markup is None and self._markup)
# 【易错】用 splitlines() 数行数意味着"末尾的换行不计入行数"：
#   单个 print("a\n") 的结果是 1 行，因此不会插入前导空行；
#   而 print("a\nb") 是 2 行，会插入——这正是 new_line_start 想要的语义。
        highlight_enabled = highlight or (highlight is None and self._highlight)

        if markup_enabled:
            rich_text = render_markup(
# 【算法】裁剪分支：split_and_crop_lines 把 Segment 流按 self.width 切行（pad=False 表示
#   不补齐短行），逐行 extend 进缓冲区；不裁剪时整段原样放入。
# 【注意】注意这里用的是 self.width 而不是 render_options.max_width：
#   width 参数只影响 renderable 内部排版，最终裁切基准仍是终端宽度。
# 【兼容】在 legacy Windows 上 self.width 已减去 1，可避免写满整行触发自动换行。
                text,
# 【优化】把缓冲区的 extend 存到局部变量：下面的循环对每一行调用一次，
#   提前取可以避免重复的 self._buffer 属性访问（_buffer 还会走 threading.local）。
                style=style,
# 【注意】pad=False 意味着短行不会被补齐到宽度：普通打印不需要补齐，
#   终端在换行时会自动忽略行尾多余空格，补齐只是徒增字节。
#   （需要矩形对齐的容器如 Panel 用的是 render_lines，那里 pad 为 True。）
                emoji=emoji_enabled,
                emoji_variant=self._emoji_variant,
            )
            rich_text.justify = justify
            rich_text.overflow = overflow
        else:
# 【功能】美化打印 JSON（输出本身仍是合法 JSON 文本）。
# 【参数】json：JSON 字符串；data：要序列化的对象（与 json 二选一）；indent：缩进；
#   highlight：是否语法高亮；skip_keys/ensure_ascii/check_circular/allow_nan/default/sort_keys
#   与标准库 json.dumps 同名同义。
# 【算法】延迟导入 rich.json.JSON；有 json 字段时校验其类型并直接构造，
#   否则用 JSON.from_data 从对象序列化；最后 print(..., soft_wrap=True) 避免换行破坏 JSON 结构。
# 【注意】soft_wrap=True 是关键：JSON 的缩进与行结构必须原样保留，
#   否则终端宽度不足时会被折叠成难以阅读的多行。
# 【易错】json 参数传了非字符串会抛 TypeError 并提示改用 data=。
            rich_text = Text(
                (
                    _emoji_replace(text, default_variant=self._emoji_variant)
                    if emoji_enabled
                    else text
                ),
                justify=justify,
                overflow=overflow,
                style=style,
            )

        _highlighter = (highlighter or self.highlighter) if highlight_enabled else None
        if _highlighter is not None:
            highlight_text = _highlighter(str(rich_text))
            highlight_text.copy_styles(rich_text)
            return highlight_text

        return rich_text

    # 【功能】把样式名（或内联样式定义）解析成 Style 对象。
    # 【参数】name：Style 实例（直接返回）、主题样式名或样式定义字符串；default：解析失败时的兜底样式。
    # 【返回】Style：带 link 的样式会返回副本，避免调用方改到主题里缓存的实例。
    # 【注意】解析失败且没有 default 时抛 errors.MissingStyle（内部包装了 StyleSyntaxError）。
    def get_style(
        self, name: Union[str, Style], *, default: Optional[Union[Style, str]] = None
    ) -> Style:
        """Get a Style instance by its theme name or parse a definition.

        Args:
            name (str): The name of a style or a style definition.

        Returns:
            Style: A Style object.

        Raises:
            MissingStyle: If no style could be parsed from name.

        """
        if isinstance(name, Style):
            return name

        try:
            style = self._theme_stack.get(name)
            if style is None:
                style = Style.parse(name)
            return style.copy() if style.link else style
# 【易错】这里只校验 json 参数的类型：如果用户把要序列化的对象传给了 json=，
#   会得到明确的 TypeError 提示（而不是 JSON 解析失败这种难以定位的错误）。
        except errors.StyleSyntaxError as error:
            if default is not None:
                return self.get_style(default)
            raise errors.MissingStyle(
                f"Failed to get style {name!r}; {error}"
            ) from None

    # 【功能】把 print/log 的可变参数归一成“可渲染对象 + 分隔符”的序列。
    # 【算法】逐个判断参数：字符串按 markup/highlight 转成 Text；可渲染对象原样保留；其余用 pretty 转换；在对象之间插入 sep，最后一个对象之后追加 end。
    # 【返回】RenderableType 列表。
    # 【注意】end 只作用于最后一个对象，这是 separators 容易写错的地方。
    def _collect_renderables(
        self,
        objects: Iterable[Any],
        sep: str,
        end: str,
        *,
        justify: Optional[JustifyMethod] = None,
        emoji: Optional[bool] = None,
        markup: Optional[bool] = None,
# 【功能】在备用屏幕的指定区域重画一个 renderable（局部刷新）。
# 【参数】renderable：要画的内容；region：Region 或 None（None 表示整屏）；
#   options：渲染选项，None 用 self.options。
# 【算法】1) 非备用屏幕状态直接抛 NoAltScreen（否则控制码会把正常终端画面搞乱）；
#   2) 区域为 None → x=y=0，并把高度设为 self.height 后按整屏渲染；
#   3) 否则解构 region 得到 x/y/width/height，把选项收窄到该区域；
#   4) render_lines 得到行，交给 update_screen_lines 写入。
# 【注意】render_lines 在 height 给定时会补齐空格行，因此局部刷新总是覆盖整个矩形区域，
#   上一次残留的内容会被空格盖掉（这是刻意的：不清屏也能获得干净画面）。
        highlight: Optional[bool] = None,
    ) -> List[ConsoleRenderable]:
        """Combine a number of renderables and text into one renderable.

        Args:
            objects (Iterable[Any]): Anything that Rich can render.
            sep (str): String to write between print data.
            end (str): String to write at end of print data.
            justify (str, optional): One of "left", "right", "center", or "full". Defaults to ``None``.
            emoji (Optional[bool], optional): Enable emoji code, or ``None`` to use console default.
            markup (Optional[bool], optional): Enable markup, or ``None`` to use console default.
            highlight (Optional[bool], optional): Enable automatic highlighting, or ``None`` to use console default.

        Returns:
            List[ConsoleRenderable]: A list of things to render.
        """
# 【约束】update_screen 与其底层 update_screen_lines 都做了这个检查：
#   双重校验是为了保护"直接调用 update_screen_lines"的路径不被误用。

        def is_expandable(obj: object) -> bool:
            """Check if an object is expandable by pretty printer."""
            # Permit lazy loading
            from .pretty import is_expandable as _is_expandable

            return _is_expandable(obj)

        renderables: List[ConsoleRenderable] = []
        _append = renderables.append
        text: List[Text] = []
        append_text = text.append

# 【功能】把已渲染好的行列表写到屏幕的 (x, y) 处。
# 【参数】lines：render_lines 的产物；x/y：目标坐标（左上角为原点，y 向下）。
# 【算法】构造 ScreenUpdate 作为 renderable，经 render 变成"移动光标 + 内容"的 Segment 流，
#   追加到缓冲区后立刻 _check_buffer 提交。
# 【注意】这里显式调用 _check_buffer 而没用 with self:：本方法通常在 Live 的刷新线程里
#   被高频调用，需要立即写出以保证动画流畅；注意此时若外层已经在缓冲中（_buffer_index>0），
#   _check_buffer 内部不会写文件，内容会等到外层提交时才出现。
        append = _append
        if justify in ("left", "center", "right"):

            # 【功能】把一段可渲染内容按对齐方式追加到输出行列表的闭包。
            # 【算法】按 align 与可用宽度计算左右填充，再拼出该行。
            # 【注意】闭包捕获外层的行缓冲与宽度，因此只应在一次 render 内使用。
            def align_append(renderable: RenderableType) -> None:
                _append(Align(renderable, cast(AlignMethod, justify)))

            append = align_append

        _highlighter: HighlighterType = _null_highlighter
        if highlight or (highlight is None and self._highlight):
            _highlighter = self.highlighter

        # 【功能】export_text 的内部闭包：把 Segment 追加到结果缓冲区。
        # 【注意】闭包捕获外层 buffer 与 styles 开关，避免为每个 Segment 传递上下文。
        def check_text() -> None:
            if text:
                sep_text = Text(sep, justify=justify, end=end)
# 【要点】这里直接 extend 生成器（不是列表），因为 segments 会被立即消费；
#   随后显式调用 _check_buffer 而不是 with self:，以保证局部刷新立刻可见。
                append(sep_text.join(text))
                text.clear()
# 【功能】打印当前异常的富文本回溯（带源码摘录、局部变量、语法高亮）。
# 【参数】width：代码区域宽度（默认 100）；extra_lines：出错行上下额外显示的行数；
#   theme：pygments 主题名；word_wrap：是否对超长源码行折行；
#   show_locals：是否展示局部变量；suppress：要从回溯里排除的模块/路径；
#   max_frames：最多显示的栈帧数（0 表示不限制）。
# 【算法】延迟导入 Traceback 构造 renderable（它会读取 sys.exc_info），再走普通 print。
# 【注意】必须在 except 块内调用才有内容；默认宽度 100 而不是终端宽度，
#   是为了让回溯在日志里保持稳定的可读宽度。

        for renderable in objects:
            renderable = rich_cast(renderable)
            if isinstance(renderable, str):
                append_text(
                    self.render_str(
                        renderable,
                        emoji=emoji,
                        markup=markup,
                        highlight=highlight,
                        highlighter=_highlighter,
                    )
                )
            elif isinstance(renderable, Text):
                append_text(renderable)
            elif isinstance(renderable, ConsoleRenderable):
                check_text()
                append(renderable)
            elif is_expandable(renderable):
                check_text()
                from .pretty import Pretty

                append(Pretty(renderable, highlighter=_highlighter))
            else:
                append_text(_highlighter(str(renderable)))

        check_text()

        if self.style is not None:
            style = self.get_style(self.style)
            renderables = [Styled(renderable, style) for renderable in renderables]

# 【功能】沿着调用栈回溯，取"调用者"的文件名、行号与局部变量。
# 【参数】offset：向上跳过的帧数（含本辅助函数自身那一帧，见实现第 1 行）；
#   currentframe：可注入的取帧函数，默认用 inspect.currentframe（测试可替换）。
# 【返回】(filename, lineno, locals) 三元组。
# 【算法】1) offset += 1 先跳过本辅助函数的帧；2) 用 currentframe() 拿到当前帧后
#   循环 frame.f_back 递减 offset；3) 拿到帧就返回其文件名/行号/局部变量；
#   4) currentframe 不可用（某些解释器/受限环境返回 None）时退回 inspect.stack() 按索引取。
# 【注意】locals 是真实字典，可能很大；log_locals 只在使用时才渲染它。
# 【易错】靠 f_back 遍历依赖 offset 精确计数，log()/print 内部任何新增的辅助调用层
#   都需要相应调整 _stack_offset 的默认值——这也是 Rich 把偏移做成参数的原因。
        return renderables

    # 【功能】输出一条水平分隔线，可选标题。
    # 【参数】title：标题文本；characters：组成线条的字符；align：标题对齐方式。
    # 【返回】Rule 可渲染对象。
    # 【注意】宽度取当前 options.max_width，因此嵌套在 Panel 内时会自动变窄。
    def rule(
        self,
        title: TextType = "",
        *,
        characters: str = "─",
        style: Union[str, Style] = "rule.line",
        align: AlignMethod = "center",
    ) -> None:
        # 【要点】+1 是补偿"本函数自己占用的那一帧"，使 offset 语义变成"相对调用方"。
        """Draw a line with optional centered title.

        Args:
            title (str, optional): Text to render over the rule. Defaults to "".
            characters (str, optional): Character(s) to form the line. Defaults to "─".
            style (str, optional): Style of line. Defaults to "rule.line".
            align (str, optional): How to align the title, one of "left", "center", or "right". Defaults to "center".
        """
        from .rule import Rule

        rule = Rule(title=title, characters=characters, style=style, align=align)
# 【兼容】inspect.currentframe() 在某些实现/受限环境下返回 None（CPython 文档明确说
#   它可能因实现差异而不可用），因此必须有 else 分支用 inspect.stack() 兜底。
        self.print(rule)

    # 【功能】把控制码（光标移动、清屏、设置标题等）写进输出缓冲。
    # 【参数】*control：Control 对象，可传多个，按参数顺序写出。
    # 【注意】哑终端下整段跳过；写缓冲仍走 `with self`，因此与其它输出保持先后顺序。
    def control(self, *control: Control) -> None:
        """Insert non-printing control codes.

        Args:
            control_codes (str): Control codes, such as those that may move the cursor.
        """
        if not self.is_dumb_terminal:
            with self:
# 【功能】像 print 一样输出内容，但会在前面加上时间戳、文件名与行号（可选：局部变量）。
# 【参数】与 print 基本一致（无宽度/裁剪类参数）；log_locals：是否附带局部变量表；
#   _stack_offset：调用栈偏移，供 log 的包装函数使用（下划线前缀表示内部参数）。
# 【算法】1) 空参数同样换成 (NewLine(),)；2) 复制钩子快照；3) with self: 收集 renderables；
#   4) 传入 style 时用 Styled 逐项包装；5) _caller_frame_info 取调用者信息，
#      文件名以 < 开头（<stdin>、<string> 等伪文件）时不生成超链接路径；
#      只取 basename 作为显示路径；6) log_locals 时把去掉 dunder 的局部变量表用
#      render_scope 追加为一个 renderable；7) 用 LogRender 把时间/路径/行号与内容组合成
#      一个整体 renderable（各段按主题上色）；8) 钩子处理；9) render 后按 self.width 裁剪写出。
# 【返回】None。
# 【注意】log 与 print 的差异：log 固定用默认渲染选项（不接受 width/justify 等），
#   但会把渲染结果裁剪到终端宽度；时间戳来自 self.get_datetime()，可注入以便测试。
                self._buffer.extend(_control.segment for _control in control)

    # 【功能】低层输出：不做 pretty、不加换行、不解析标记，只做可选的高亮与统一整体样式。
    # 【参数】sep/end 与 print 一致；style：整体样式；highlight：None 表示沿用 Console 默认。
    # 【注意】内部调用 print 时固定 emoji=False、markup=False、no_wrap=True、overflow="ignore"、crop=False，
    # 因此超宽内容既不会折行也不会被裁剪——这是它与 print 的关键差别。
    def out(
        self,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
        style: Optional[Union[str, Style]] = None,
        highlight: Optional[bool] = None,
    ) -> None:
        """Output to the terminal. This is a low-level way of writing to the terminal which unlike
        :meth:`~rich.console.Console.print` won't pretty print, wrap text, or apply markup, but will
        optionally apply highlighting and a basic style.

        Args:
            sep (str, optional): String to write between print data. Defaults to " ".
            end (str, optional): String to write at end of print data. Defaults to "\\\\n".
            style (Union[str, Style], optional): A style to apply to output. Defaults to None.
            highlight (Optional[bool], optional): Enable automatic highlighting, or ``None`` to use
                console default. Defaults to ``None``.
        """
        raw_output: str = sep.join(str(_object) for _object in objects)
        self.print(
# 【注意】locals 的内容来自 _caller_frame_info 抓到的真实帧字典，
#   因此它会包含当时的全部局部变量（除 dunder）——对含大对象/密钥的场景要谨慎开启。
            raw_output,
            style=style,
            highlight=highlight,
            emoji=False,
            markup=False,
            no_wrap=True,
            overflow="ignore",
            crop=False,
            end=end,
        )

    # 【功能】Console 的主输出入口：把任意对象渲染成 Segment 写入缓冲（必要时立即刷出）。
    # 【算法】soft_wrap 生效时强制 no_wrap=True、overflow="ignore"、crop=False；_collect_renderables 收集对象 →
    # 依次调用 render hooks → 用 options.update(...) 覆盖参数渲染 → 可选按宽度裁剪每行 → 写入缓冲。
    # 【参数】width 取 min(width, self.width)（不会超过终端宽度）；style 非空时按整行套样式（含换行符）；
    # new_line_start=True 且内容多于一行时在最前面插入一个空行。
    # 【注意】整个函数包在 `with self:` 里，缓冲嵌套是安全的；render hooks 用的是进入时复制的快照。
    def print(
        self,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
        style: Optional[Union[str, Style]] = None,
        justify: Optional[JustifyMethod] = None,
        overflow: Optional[OverflowMethod] = None,
        no_wrap: Optional[bool] = None,
# 【要点】伪文件名（<stdin>、<string>、<frozen ...>）无法构成有效路径，
#   直接置 None，使日志里的路径不带超链接（终端不会生成点不开的链接）。
        emoji: Optional[bool] = None,
        markup: Optional[bool] = None,
# 【算法】过滤掉以 __ 开头的键（__name__、__builtins__ 等噪声），
#   再用 render_scope 渲染成缩进的变量表——它本身就是一个 renderable，
#   因此会与其他内容一起被裁剪、着色。
# 【注意】局部变量的值会被 str()/repr() 求值，对包含副作用 __repr__ 的对象需谨慎。
        highlight: Optional[bool] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        crop: bool = True,
        soft_wrap: Optional[bool] = None,
        new_line_start: bool = False,
    ) -> None:
        # 【算法】整个日志行交给 LogRender 统一排版：时间、路径、行号、分隔线、内容，
        """Print to the console.
#   这样样式与对齐只在一处定义（见 rich._log_render）。

        Args:
            objects (positional args): Objects to log to the terminal.
            sep (str, optional): String to write between print data. Defaults to " ".
            end (str, optional): String to write at end of print data. Defaults to "\\\\n".
            style (Union[str, Style], optional): A style to apply to output. Defaults to None.
            justify (str, optional): Justify method: "default", "left", "right", "center", or "full". Defaults to ``None``.
            overflow (str, optional): Overflow method: "ignore", "crop", "fold", or "ellipsis". Defaults to None.
            no_wrap (Optional[bool], optional): Disable word wrapping. Defaults to None.
            emoji (Optional[bool], optional): Enable emoji code, or ``None`` to use console default. Defaults to ``None``.
            markup (Optional[bool], optional): Enable markup, or ``None`` to use console default. Defaults to ``None``.
            highlight (Optional[bool], optional): Enable automatic highlighting, or ``None`` to use console default. Defaults to ``None``.
            width (Optional[int], optional): Width of output, or ``None`` to auto-detect. Defaults to ``None``.
            crop (Optional[bool], optional): Crop output to width of terminal. Defaults to True.
            soft_wrap (bool, optional): Enable soft wrap mode which disables word wrapping and cropping of text or ``None`` for
                Console default. Defaults to ``None``.
            new_line_start (bool, False): Insert a new line at the start if the output contains more than one line. Defaults to ``False``.
        """
        if not objects:
            objects = (NewLine(),)

        if soft_wrap is None:
            soft_wrap = self.soft_wrap
# 【功能】管道被下游关闭（如 rich 输出被 head 截断）时的默认处理：静默并退出。
# 【算法】1) self.quiet = True 让后续输出全部丢弃；2) 把 stdout 重定向到 os.devnull，
#      使得解释器退出时的清理写入不会再次触发 BrokenPipeError；3) raise SystemExit(1)。
# 【返回】不返回（总是抛 SystemExit）。
# 【为什么】Python 在解释器关闭时还会 flush 标准流，若不换掉 fd 会在退出时
#   打印"Exception ignored in: <_io.TextIOWrapper name='<stdout>'>"这类噪声。
# 【兼容】这是 POSIX 管道语义的处理；Windows 上很少出现，但同一段代码也无害。
# 【易错】重定向用的是 sys.stdout.fileno()：如果 stdout 已被替换成没有 fileno 的对象，
#   这里会抛异常——不过既然已经发生 BrokenPipeError，通常说明它背后是真实的 fd。
        if soft_wrap:
# 【协作】_check_buffer 在捕获 BrokenPipeError 后调用本方法；
#   子类可覆写以实现"忽略并继续"或"记录日志"等不同策略。
            if no_wrap is None:
                no_wrap = True
            if overflow is None:
                overflow = "ignore"
            crop = False
        render_hooks = self._render_hooks[:]
        with self:
            renderables = self._collect_renderables(
# 【兼容】dup2 直接替换文件描述符 1，绕过 Python 层的 sys.stdout 对象，
#   因此解释器退出时的隐式 flush 也不会再触发管道写入。
# 【易错】需要 sys.stdout 具备可用的 fileno()；在 stdout 被替换成无 fd 的对象时失效。
                objects,
                sep,
# ==== 【五、写出缓冲：_check_buffer / _write_buffer / _render_buffer】 ====
# 【功能】尝试提交缓冲区内容（静默模式下直接丢弃）。
# 【算法】quiet 为真 → 清空缓冲区并返回（注意是清空而不是保留，避免内存无限增长）；
#   否则调用 _write_buffer，并捕获 BrokenPipeError 转交 on_broken_pipe。
# 【注意】本方法是 _exit_buffer 的落点，因此每次退出缓冲上下文都会被调用；
#   真正"是否写出"由 _write_buffer 里的 _buffer_index 判断决定。
# 【兼容】把 BrokenPipeError 在这里收敛，可以让所有输出路径（print/log/control）
#   共享同一套管道断开处理，而不必各自 try。
                end,
# 【协作】调用点：_exit_buffer（每次退出缓冲上下文）、update_screen_lines（局部刷新后立即写出）、
#   Live 的刷新线程。三处都依赖"_buffer_index == 0 才真正写"这一条件。
                justify=justify,
                emoji=emoji,
                markup=markup,
                highlight=highlight,
            )
# 【要点】quiet 时连录制也不做（直接返回，不进 _write_buffer），
#   所以 export_* 在 quiet 模式下拿不到内容——这与"静默即完全无输出"的语义一致。
            for hook in render_hooks:
                renderables = hook.process_renderables(renderables)
            render_options = self.options.update(
                justify=justify,
# 【要点】把 BrokenPipeError 的捕获点收在这里，意味着所有输出路径（含 update_screen_lines
#   的显式调用）都经过同一处兜底，不必各自重复 try/except。
                overflow=overflow,
                width=min(width, self.width) if width is not None else NO_CHANGE,
                height=height,
# 【功能】把缓冲区内容真正写到输出目标，并按需录制、按平台选择写路径。
# 【算法】整体持 self._lock（可重入），流程：
#   1) record 打开且 _buffer_index == 0（即这是最外层的完整内容）→ 在 _record_buffer_lock
#      保护下把缓冲区副本追加进 _record_buffer；
#   2) _buffer_index != 0（仍在嵌套缓冲中）→ 什么都不做，内容留待最外层；
#   3) Jupyter：交给 jupyter.display 把 Segment 渲染成 IPython 显示对象，随后清空缓冲区；
#   4) Windows：老控制台且目标是标准输出流 → 走 win32 直写（legacy_windows_render）；
#      否则走常规文本写入；
#   5) 常规写入时限制单次写入不超过 32KiB（见下方注释），并捕获 UnicodeEncodeError
#      追加 PYTHONIOENCODING 提示；
#   6) flush 并清空缓冲区。
# 【注意】录制用的是 self._buffer[:] 的副本，因此随后 del self._buffer[:] 不影响录制内容；
#   no_color 开启时老控制台路径会先把颜色 Segment 剥掉再交给 win32 渲染器。
# 【兼容】旧版 Windows 的 GetConsoleMode/WriteConsoleW 路径不支持 ANSI，
#   必须逐格写字符与颜色属性，见 rich/_windows_renderer.py。
                no_wrap=no_wrap,
                markup=markup,
                highlight=highlight,
# 【要点】用 not self._buffer_index 而不是 == 0：既表达"最外层"，又利用 int 的真值语义；
#   只有最外层才录制，避免嵌套缓冲把同一段内容重复录进 _record_buffer。
            )

            new_segments: List[Segment] = []
            extend = new_segments.extend
# 【兼容】Jupyter 下不能直接写 sys.stdout（会被 notebook 前端当纯文本吞掉），
#   要把 Segment 交给 IPython 的 display，由前端渲染成 ANSI/HTML。
#   传入两份数据：原始 Segment 列表（供前端取样式）与渲染后的字符串。
            render = self.render
            if style is None:
                for renderable in renderables:
                    extend(render(renderable, render_options))
            else:
# 【兼容】Windows 三分支：只有"老控制台 + 目标是标准输出流"才用 win32 API 直写；
#   如果 file 是自定义流（StringIO、文件、测试代理），仍然按普通文本写。
#   判断靠 get_fileno(self.file) 是否落在 _STD_STREAMS_OUTPUT 里。
                render_style = self.get_style(style)
                new_line = Segment.line()
                for renderable in renderables:
# 【兼容】get_fileno 包装了 file.fileno()：第三方"类文件对象"的 fileno 实现千奇百怪
#   （可能抛 OSError/ValueError 甚至任意异常），统一在这里吞掉并返回 None，
#   从而让"拿不到 fd"退化为"走普通文本写入"。
                    for line, add_new_line in Segment.split_lines_terminator(
                        render(renderable, render_options)
                    ):
                        extend(Segment.apply_style(line, render_style))
                        if add_new_line:
# 【兼容】老控制台直写路径：延迟导入两个 Windows 专用模块（非 Windows 平台永不导入），
#   复制缓冲区后按需剥掉颜色，再交给 legacy_windows_render + LegacyWindowsTerm。
# 【注意】这里同样用副本，因为 win32 渲染器可能会修改 Segment 序列。
                            new_segments.append(new_line)

            if new_line_start:
                if (
                    len("".join(segment.text for segment in new_segments).splitlines())
# 【要点】老控制台路径下 no_color 需要提前剥色：
#   win32 直写是按单元格设置颜色属性的，颜色信息在 Segment 里而不是 ANSI 序列里，
#   所以不能用 _render_buffer 那套过滤方式。
                    > 1
                ):
                    new_segments.insert(0, Segment.line())
# 【兼容】Windows 下的 32KiB 写入限制源自两个 CPython 缺陷（见下方两条链接）：
#   向控制台一次性写入过大数据会被截断或抛异常，所以按行累积到接近上限就分次写。
# 【优化】MAX_WRITE 取 32 * 1024 // 4：按最坏情况（每个字符 4 字节 UTF-8）估算字符数上限，
#   保证字节数不会超过 32KiB。
# 【注意】分批是按行切的：单行本身超限时也会整行写出（不做行内切分），
#   因为切在行中间会破坏样式转义序列。
            if crop:
# 【兼容】两条上游缺陷链接指向的具体问题：向控制台/管道一次写入超过 32KiB 时，
#   可能出现数据截断或抛 OSError，因此这里显式做了分批写入的兜底。
# 【优化】把绑定方法取到局部变量：分批写入循环里可能调用多次，
#   提前取可以减少属性查找（典型的热路径微优化）。
                buffer_extend = self._buffer.extend
# 【易错】预算单位是"字符数"而非字节数，故除以 4 取最坏情况；
#   中文/emoji 在这些平台上更接近这个上界。
                for line in Segment.split_and_crop_lines(
                    new_segments, self.width, pad=False
                ):
                    buffer_extend(line)
            else:
                self._buffer.extend(new_segments)

# ==== 【四·补、打印路径的收尾：print_json / 屏幕更新 / 异常 / 日志】 ====
    def print_json(
# 【要点】splitlines(True) 保留行尾换行符，保证分批写出的内容与原文逐字节一致
#   （否则会在批与批之间丢掉换行，输出会被压成一行）。
        self,
        json: Optional[str] = None,
        *,
        data: Any = None,
        indent: Union[None, int, str] = 2,
        highlight: bool = True,
        skip_keys: bool = False,
        ensure_ascii: bool = False,
        check_circular: bool = True,
        allow_nan: bool = True,
# 【兼容】编码失败时给异常补一句可操作的建议（PYTHONIOENCODING=utf-8），
#   然后原样抛出：不吞异常，调用方仍能感知输出失败。
# 【易错】error.reason 是可写属性，直接赋值即可影响最终打印的异常信息（CPython 实现细节）。
        default: Optional[Callable[[Any], Any]] = None,
        sort_keys: bool = False,
    ) -> None:
# 【要点】非 Windows（或非标准流的 Windows）走最朴素的路径：渲染成字符串后一次写入。
#   注意这里没有 32KiB 限制——POSIX 的 write 对普通文件/管道不会有同样的截断问题。
        # 【要点】非 Windows 分支同样传 self._buffer[:] 的副本：
        # 【兼容】非 Windows 分支同样补 PYTHONIOENCODING 提示：
        # 【要点】每次提交都 flush：Rich 的输出常用于实时进度显示，缓冲会让动画"卡住"；
        # 【功能】把 Segment 序列编码成最终字符串（ANSI 样式在此落地）。
        # 【参数】buffer：Segment 可迭代对象（通常是 _buffer 的副本）。
        # 【返回】str：可直接写入文件的内容。
        # 【算法】1) 缓存 color_system/legacy_windows/not_terminal 到局部变量（【优化】循环内不再取属性）；
        # 【注意】控制码在非终端（管道、文件、StringIO）下被丢弃，这是重定向输出里看不到
        # 【易错】no_color 只剥颜色，不会剥粗体/下划线等属性（那些不依赖 color_system）。
        # 【优化】三个属性都依赖 is_terminal（会探测 isatty），在循环外取一次可避免每段都探测。
        """Pretty prints JSON. Output will be valid JSON.
#   _render_buffer 只读，但传副本可以避免"渲染过程中缓冲区被本线程的其他代码修改"
#   （例如 style.render 触发的用户回调）导致的迭代异常。

        Args:
            json (Optional[str]): A string containing JSON.
#   典型场景是 LANG=C / LC_ALL=C 的容器里 stdout 编码为 ascii，写中文即失败。
            data (Any): If json is not supplied, then encode this data.
            indent (Union[None, int, str], optional): Number of spaces to indent. Defaults to 2.
            highlight (bool, optional): Enable highlighting of output: Defaults to True.
#   代价是高频刷新时系统调用较多（Live 因此提供 refresh_per_second 限流）。
            skip_keys (bool, optional): Skip keys not of a basic type. Defaults to False.
            ensure_ascii (bool, optional): Escape all non-ascii characters. Defaults to False.
#   2) no_color 且有颜色系统时，先用 Segment.remove_color 过滤掉所有颜色；
#   3) 逐段处理：有样式 → style.render(text, color_system, legacy_windows) 生成转义序列；
#      无样式 → 只有在"不是终端且该段是控制码"时才跳过，否则原样追加文本。
#   光标移动/清屏序列的原因，也是 Capture 在非终端下得到干净文本的原因。
            check_circular (bool, optional): Check for circular references. Defaults to True.
            allow_nan (bool, optional): Allow NaN and Infinity values. Defaults to True.
            default (Callable, optional): A callable that converts values that can not be encoded
                in to something that can be JSON encoded. Defaults to None.
            sort_keys (bool, optional): Sort dictionary keys. Defaults to False.
        """
# 【易错】not_terminal 单独取出来是因为它下面的判断是 "not (not_terminal and control)"，
#   直接写两次 self.is_terminal 会导致 isatty 被探测两次（性能与一致性都不好）。
        from rich.json import JSON
# 【要点】no_color 的处理放在这里而不是渲染阶段：Segment 中仍保留颜色信息
#   （录制、导出 HTML/SVG 因此不受影响），只在最终文本输出时剥离。
# 【约束】只有本来就有颜色系统时才需要剥，否则白跑一遍过滤。

        if json is None:
            json_renderable = JSON.from_data(
                data,
                indent=indent,
# 【要点】样式渲染发生在这里，是"样式对象 → 字节序列"的唯一落地点：
#   color_system 决定用 3/4 位还是 24 位色、是否退化为纯文本；
#   legacy_windows 影响部分控制序列的写法（老控制台只支持有限的属性）。
                highlight=highlight,
                skip_keys=skip_keys,
                ensure_ascii=ensure_ascii,
                check_circular=check_circular,
                allow_nan=allow_nan,
                default=default,
# 【易错】这个条件容易看错：等价于"是终端 或 该段不是控制码"。
#   即控制码只在终端下输出；普通文本无论是否终端都要输出。
                sort_keys=sort_keys,
            )
        else:
            if not isinstance(json, str):
# 【功能】显示提示并读取一行输入（内置 input 的富文本版本）。
# 【参数】prompt：提示内容（str 或 Text）；markup/emoji：提示串的解析开关；
#   password：True 时用 getpass 隐藏输入；stream：替代 stdin 的读取源。
# 【返回】读到的字符串（含行尾换行，与 file.readline 语义一致）。
# 【算法】1) 提示非空则 print(prompt, end="") 不加换行；2) password 走 getpass；
#   3) 指定 stream 则 readline()，否则调用内置 input()。
# 【注意】readline 与内置 input 的差异：前者保留 \n，后者去掉；
#   若 Python 已加载 readline 模块，内置 input 会自动获得行编辑与历史功能。
# 【兼容】password 分支用显式 "" 作为提示（提示已由 Rich 打印），避免 getpass 重复输出。
                raise TypeError(
                    f"json must be str. Did you mean print_json(data={json!r}) ?"
                )
            json_renderable = JSON(
                json,
                indent=indent,
                highlight=highlight,
                skip_keys=skip_keys,
                ensure_ascii=ensure_ascii,
                check_circular=check_circular,
                allow_nan=allow_nan,
                default=default,
                sort_keys=sort_keys,
            )
        self.print(json_renderable, soft_wrap=True)

    # 【功能】把 renderable 渲染到备用屏幕的指定区域（默认整屏）。
    # 【算法】region 为 None 时起点 (0,0)、高度取 options.height 或终端高度；否则解包 Region 并收紧渲染选项，
    # 最后 render_lines + update_screen_lines 落盘。
    # 【注意】未开启备用屏幕时抛 errors.NoAltScreen；早期文档里的 x/y 参数已废弃，位置改由 region 提供。
    def update_screen(
        self,
        renderable: RenderableType,
        *,
        region: Optional[Region] = None,
        options: Optional[ConsoleOptions] = None,
    ) -> None:
        # 【功能】把录制下来的内容导出为纯文本（可选带 ANSI 样式）。
        # 【参数】clear：导出后是否清空录制缓冲区（默认 True）；styles：是否让样式生效。
        # 【返回】str。
        # 【算法】加 _record_buffer_lock；styles=True → 对每段调用 style.render(text)（不带
        # 【注意】必须构造时 record=True，否则 assert 失败（-O 模式下 assert 被跳过，会返回空串）。
        # 【易错】导出的文本是"当时产出的 Segment"，与终端宽度无关：
        # ==== 【六、导出：text / html / svg】 ====
        """Update the screen at a given offset.

        Args:
            renderable (RenderableType): A Rich renderable.
            region (Region, optional): Region of screen to update, or None for entire screen. Defaults to None.
            x (int, optional): x offset. Defaults to 0.
            y (int, optional): y offset. Defaults to 0.

#   color_system 参数，即按默认 8 位/真彩能力输出转义序列）；
#   styles=False → 只拼接 text 且跳过 control 段（控制码不写进文本文件）。
#   录制时已经按当时的宽度换好行了。
        Raises:
            errors.NoAltScreen: If the Console isn't in alt screen mode.

        """
        if not self.is_alt_screen:
            raise errors.NoAltScreen("Alt screen must be enabled to call update_screen")
        render_options = options or self.options
        if region is None:
            x = y = 0
# 【约束】用 assert 而不是抛异常：这是开发期契约（-O 下会被优化掉），
#   所以生产代码不要依赖它做错误处理。
# 【约束】与 export_text 相同的 record 契约；注意参数列表里的 theme/code_format
#   都允许为 None（分别表示默认终端主题与默认 HTML 模板）。
            render_options = render_options.update_dimensions(
                render_options.max_width, render_options.height or self.height
            )
# 【要点】导出全程持录制锁：既避免遍历时缓冲区被写入线程改动，
#   也保证 clear=True 的"读取 + 清空"是原子的（不会丢掉导出期间新产生的内容）。
# 【算法】在锁内取一份过滤掉控制码的 Segment 列表；clear=True 时立刻清空录制缓冲区。
# 【注意】与 export_html 不同，这里用 list() 把生成器物化：后面要遍历两次
#   （一次算 unique_id 的哈希、一次真正绘制），生成器只能消费一次。
        else:
# 【要点】styles=True 时每个 Segment 用 style.render(text) 生成 ANSI；
#   注意这里没有传 color_system，即按 Style.render 的默认真彩能力输出，
#   因此导出的文本里可能出现当前终端并不支持的转义序列（用于粘贴到别处）。
            x, y, width, height = region
            render_options = render_options.update_dimensions(width, height)

# 【注意】带样式导出时不过滤 control 段：样式渲染会把控制码原样保留，
#   这是刻意的——用于回放到终端时应当保留光标控制。
        lines = self.render_lines(renderable, options=render_options)
        self.update_screen_lines(lines, x, y)

    # 【功能】把已经渲染好的行写到屏幕的 (x, y) 偏移处。
    # 【参数】lines：render_lines 的产物（每行一个 Segment 列表）；x/y：起始列与行。
    # 【注意】同样要求备用屏已开启（否则抛 errors.NoAltScreen）；它把 ScreenUpdate 交给 render 变成 Segment
    # 写入缓冲，并立即刷出。
    def update_screen_lines(
        self, lines: List[List[Segment]], x: int = 0, y: int = 0
    ) -> None:
        # 【注意】清空发生在模板 format 之后、返回之前：即使用户的 code_format 触发了异常，
        # 【功能】export_text 的文件版：把结果写入指定路径。
        # 【参数】path：目标文件路径；clear/styles 语义同 export_text。
        # 【兼容】固定 encoding="utf-8"，不受运行环境默认编码影响（Windows 上默认是 GBK，
        """Update lines of the screen at a given offset.

#   clear 也不会执行（缓冲区内容保留，便于排查）。
        Args:
            lines (List[List[Segment]]): Rendered lines (as produced by :meth:`~rich.Console.render_lines`).
            x (int, optional): x offset (column no). Defaults to 0.
#   写中文会失败）；换行则交给平台默认（".\n" 在 Windows 上会被翻译成 CRLF）。
            y (int, optional): y offset (column no). Defaults to 0.

        Raises:
            errors.NoAltScreen: If the Console isn't in alt screen mode.
        """
        if not self.is_alt_screen:
            raise errors.NoAltScreen("Alt screen must be enabled to call update_screen")
        screen_update = ScreenUpdate(lines, x, y)
        segments = self.render(screen_update)
# 【兼容】统一用 with 打开并显式指定 utf-8：
#   1) 保证文件及时关闭（异常路径也关闭）；2) 避免 Windows 默认 ANSI 编码写中文失败。
# 【注意】未指定 newline：Windows 上会把 \n 翻译成 \r\n；
#   SVG/HTML 对换行不敏感，所以这里不影响正确性。
        self._buffer.extend(segments)
        self._check_buffer()
# 【功能】把录制内容导出为带样式的 HTML 片段/文档。
# 【参数】theme：TerminalTheme（颜色映射，默认 DEFAULT_TERMINAL_THEME）；
#   clear：导出后清空；code_format：外层模板（默认 CONSOLE_HTML_FORMAT，需含
#   {foreground}/{background}/{code}，非内联样式时还需要 {stylesheet}）；
#   inline_styles：True 把 CSS 直接写进 span 的 style 属性（文件更大但便于粘贴），
#   False 则生成 class 与 <style> 表（文件更小、复用度高）。
# 【返回】str（完整 HTML）。
# 【算法】1) assert record；取模板与主题；2) 加锁；3) Segment.simplify 合并相邻同样式段、
#   Segment.filter_control 去掉控制码；4) 逐段 html.escape 转义文本（防注入）；
#   5) 有样式时取 style.get_html_style(theme) 得到 CSS 规则；内联模式直接写 style 属性，
#      否则用 styles.setdefault 给每个不同规则分配递增编号 r1/r2/…；
#   6) 有 link 时用 <a href> 包裹（内联模式给 style 属性，普通模式给 class）；
#   7) 非内联模式把编号规则拼成样式表；8) 用模板 format 出最终 HTML；9) 按需清空缓冲区。
# 【注意】转义用标准库 html.escape（会把 & < > 变成实体），因此终端里的文本
#   不会被浏览器当\n 不在转义范围内，HTML 里靠模板的 <pre> 保留换行。
# 【易错】inline_styles=True 时 stylesheet 恒为空串，若自定义模板里仍写了 {stylesheet}，
#   会得到空样式表而不是报错。

    def print_exception(
        self,
# 【要点】save_svg 的参数与 export_svg 一一对应，只是把 clear 等原样透传；
#   这种"save 转调 export"的成对设计在 rich 中反复出现（text/html/svg 三组都是）。
        *,
        width: Optional[int] = 100,
        extra_lines: int = 3,
        theme: Optional[str] = None,
        word_wrap: bool = False,
        show_locals: bool = False,
        suppress: Iterable[Union[str, ModuleType]] = (),
        max_frames: int = 100,
    ) -> None:
# 【要点】自定义 code_format 时必须自洽：inline_styles=False 时模板要包含 {stylesheet}，
#   否则样式表会无处安放（不会报错，只是样式丢失）。
        # 【要点】函数内导入标准库：导出功能不是热路径，把 import 放进来可以让模块导入更快。
        """Prints a rich render of the last exception and traceback.

        Args:
            width (Optional[int], optional): Number of characters used to render code. Defaults to 100.
            extra_lines (int, optional): Additional lines of code to render. Defaults to 3.
            theme (str, optional): Override pygments theme used in traceback
            word_wrap (bool, optional): Enable word wrapping of long lines. Defaults to False.
            show_locals (bool, optional): Enable display of local variables. Defaults to False.
            suppress (Iterable[Union[str, ModuleType]]): Optional sequence of modules or paths to exclude from traceback.
            max_frames (int): Maximum number of frames to show in a traceback, 0 for no maximum. Defaults to 100.
        """
        from .traceback import Traceback

        traceback = Traceback(
            width=width,
            extra_lines=extra_lines,
# 【要点】参数默认值用 None 而不是直接写 CONSOLE_HTML_FORMAT：
#   这样可以在函数体内区分"未指定"与"显式指定"，也让默认模板的来源只有一处。
            theme=theme,
            word_wrap=word_wrap,
# 【算法】两种模式的取舍：内联模式每个 span 自带 style 属性，
#   自包含（复制到邮件/文档里不丢样式）但冗余；
#   非内联模式共用 class，体积小、可整体改主题，但必须带上 stylesheet。
            show_locals=show_locals,
# 【算法】Segment.simplify 先把相邻的同样式 Segment 合并（减少标签数量、压缩输出），
#   filter_control 再剔除控制码——控制码在 HTML 里没有意义，留下会变成空标签。
            suppress=suppress,
            max_frames=max_frames,
        )
        self.print(traceback)

    # 【功能】取调用方栈帧的文件名、行号与局部变量，供 log() 显示来源及 log_locals 使用。
    # 【参数】offset：距当前帧的层数（1 表示 log 的直接调用者）；currentframe：取当前帧的可调用对象，
    # 默认 inspect.currentframe()，测试时可注入桩函数。
    # 【返回】Tuple[str, int, Dict[str, Any]]：(文件名, 行号, 局部变量字典)。
    # 【注意】传入 currentframe 时用 f_back 循环走 offset 层，栈不够深会抛 RuntimeError；
    # 不传时改用 inspect.stack()（更慢，但可在不支持 currentframe 的实现上用）。
    @staticmethod
    def _caller_frame_info(
# 【要点】超链接在两种模式下的差别：内联模式用 style 属性承载颜色（<a> 自身不带上色），
#   非内联模式则给 <a> 加 class（见下一条分支），由样式表统一着色。
# 【易错】href 直接取自 style.link，未做 URL 转义：link 里含引号会破坏 HTML 结构，
#   因此 link 应由 Rich 的样式解析器给出（它不接受包含引号的链接字面量）。
        offset: int, currentframe: Optional[Callable[[], Optional[FrameType]]] = None
    ) -> Tuple[str, int, Dict[str, Any]]:
        # 【要点】用 (规则 → 编号) 字典做 CSS 去重：编号从 1 开始（len(styles) + 1），
        # 【易错】空规则（无任何样式）也会占一个编号，但生成样式表时被 `if style_rule` 跳过，
        """Get caller frame information.

        Args:
            offset (int): the caller offset within the current frame stack.
            currentframe (Callable[[], Optional[FrameType]], optional): the callable to use to
                retrieve the current frame. Defaults to None, which will use ``inspect.currentframe()``.

        Returns:
            Tuple[str, int, Dict[str, Any]]: A tuple containing the filename, the line number and
#   同一个 CSS 规则只生成一个 class，所以 r 编号的顺序取决于首次出现顺序。
#   因此编号可能出现不连续（无害，只是 r 号有洞）。
                the dictionary of local variables associated with the caller frame.

        Raises:
            RuntimeError: If the stack offset is invalid.
        """
        # Ignore the frame of this local helper
        offset += 1

# 【要点】样式表按 dict 插入顺序生成，因此 class 编号与 CSS 规则顺序一致，
#   便于阅读导出结果与调试样式映射。
        if currentframe is None:
            import inspect

            frame = inspect.currentframe()
# 【要点】模板占位符只有四个：code / stylesheet / foreground / background。
#   自定义模板必须名称一致；缺失的键会在 format 时抛 KeyError。
        else:
            frame = currentframe()
        if frame is not None:
            while offset and frame is not None:
                frame = frame.f_back
                offset -= 1
            assert frame is not None
            return frame.f_code.co_filename, frame.f_lineno, frame.f_locals
        else:
# 【功能】export_html 的文件版：把 HTML 写入路径。
# 【参数】与 export_html 相同，但 code_format 默认值写成了 CONSOLE_HTML_FORMAT 本身
#   （而不是 None）：因为这里的默认参数是"模板字符串"，而 export_html 用 None 表示"取默认"。
# 【兼容】同样显式指定 encoding="utf-8"，避免 Windows 默认编码写中文失败。
            from inspect import stack

            frame_info = stack()[offset]
            return frame_info.filename, frame_info.lineno, frame_info.frame.f_locals

    # 【功能】带时间戳与调用位置前缀的日志输出。
    # 【算法】收集渲染对象 → 可选整体样式包装 → 取调用方文件/行号 → 由日志渲染器生成
    # “时间 + 路径:行号 + 内容” → 渲染并按宽度裁剪写入缓冲。
    # 【参数】log_locals=True 时追加调用处的局部变量表（过滤掉双下划线开头的名字）；_stack_offset 调整“调用者”层数。
    # 【注意】文件名以 "<" 开头（交互式/exec）时不会生成可点击的链接路径。
    def log(
        self,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
        style: Optional[Union[str, Style]] = None,
        justify: Optional[JustifyMethod] = None,
        emoji: Optional[bool] = None,
        markup: Optional[bool] = None,
        highlight: Optional[bool] = None,
        log_locals: bool = False,
        _stack_offset: int = 1,
    ) -> None:
        # 【功能】把录制内容导出为自包含的 SVG 图片（模拟一个带标题栏的终端窗口）。
        # 【参数】title：窗口标题；theme：TerminalTheme（默认 SVG_EXPORT_THEME，深色背景）；
        # 【返回】str：完整 SVG 文本。
        # 【算法】见下方逐段注释：计算字号与行高几何 → 遍历裁剪后的每一行、每一段，
        # 【注意】SVG 是"按 cell 定位"的：x 用 cell_len(text) 累加，因此中文等宽字符不会错位；
        # 【易错】内容为空（没有任何 segment）时 y 保持 0，尺寸计算仍能给出一个空窗口，
        """Log rich content to the terminal.

        Args:
            objects (positional args): Objects to log to the terminal.
            sep (str, optional): String to write between print data. Defaults to " ".
            end (str, optional): String to write at end of print data. Defaults to "\\\\n".
            style (Union[str, Style], optional): A style to apply to output. Defaults to None.
            justify (str, optional): One of "left", "right", "center", or "full". Defaults to ``None``.
            emoji (Optional[bool], optional): Enable emoji code, or ``None`` to use console default. Defaults to None.
            markup (Optional[bool], optional): Enable markup, or ``None`` to use console default. Defaults to None.
#   clear：导出后清空录制缓冲；code_format：SVG 模板（默认 CONSOLE_SVG_FORMAT，
#   其中可用的占位符见函数末尾的 format 调用）；font_aspect_ratio：字体宽高比
#   （0.61 对应 Fira Code），用于把"字符数"换算成"像素宽度"；
#   unique_id：SVG 内元素/CSS 的 id 前缀，缺省时按内容哈希自动生成。
#   为样式生成 CSS class、为背景生成 rect、为文本生成 text 节点 → 生成每行的 clipPath
#   → 拼出窗口外框（chrome）与标题 → 用模板 format 组装。
#   由于 SVG 没有真正的等宽字体保证，文本节点同时给出 textLength 以强制拉伸对齐。
#   不会抛异常。
            highlight (Optional[bool], optional): Enable automatic highlighting, or ``None`` to use console default. Defaults to None.
            log_locals (bool, optional): Boolean to enable logging of locals where ``log()``
                was called. Defaults to False.
            _stack_offset (int, optional): Offset of caller from end of call stack. Defaults to 1.
        """
        if not objects:
            objects = (NewLine(),)
# 【参数】font_aspect_ratio 的物理含义是"字符宽 ÷ 字符高"，默认 0.61 对应 Fira Code；
#   若模板里换用其它等宽字体（例如 Source Code Pro 约 0.6、JetBrains Mono 约 0.6），
#   应同步调整该值，否则文字与背景矩形会横向错位。

        render_hooks = self._render_hooks[:]

        with self:
            renderables = self._collect_renderables(
                objects,
                sep,
                end,
                justify=justify,
                emoji=emoji,
# 【协作】想自定义 SVG 外观（字体、尺寸、配色）时，复制 CONSOLE_SVG_FORMAT 的模板字符串，
#   修改后通过 code_format 传入即可——本方法不解析模板内容，只负责 format 注入。
                markup=markup,
                highlight=highlight,
            )
            if style is not None:
                renderables = [Styled(renderable, style) for renderable in renderables]

            filename, line_no, locals = self._caller_frame_info(_stack_offset)
            link_path = None if filename.startswith("<") else os.path.abspath(filename)
# 【要点】zlib 只用于给 unique_id 算一个稳定哈希（adler32），
#   放在函数内导入可以让模块加载更快；cell_len 用于按单元格宽度累加 x 坐标。
            path = filename.rpartition(os.sep)[-1]
            if log_locals:
                from .scope import render_scope
# 【优化】样式 → CSS 规则字符串的缓存：同一份录制内容里样式重复率很高，
#   缓存可以避免对每个 Segment 都重新拼一遍字符串。

# 【功能】把一个 Style 转成 SVG 用的 CSS 规则串（并写入缓存）。
# 【算法】1) 前景色：样式未指定或为 default → 用主题前景色；否则 get_truecolor(theme)
#      把 8 位/标准色映射为真彩色（导出图片不受终端能力限制）；
#   2) 背景色同理；3) style.reverse 时前景/背景互换；
#   4) style.dim 时用 blend_rgb(color, bgcolor, 0.4) 把前景往背景混合 40% 模拟变暗
#      （SVG 没有 dim 属性，只能算颜色）；
#   5) 依次追加 fill、font-weight、font-style、text-decoration；
#   6) 拼成 CSS 串并缓存。
# 【返回】CSS 规则字符串（如 fill: #ff0000;font-weight: bold）。
# 【注意】underline 与 strike 都写 text-decoration，同时出现时后者会覆盖前者
#   （CSS 语义如此，严格来说应该合并成空格分隔的多个值）。
                locals_map = {
                    key: value
                    for key, value in locals.items()
                    if not key.startswith("__")
                }
# 【要点】is_default 表示"样式里显式写了 default 颜色"，与"没写颜色"一样都回落到主题色；
#   get_truecolor 负责把标准/8 位色号或颜色名解析成 RGB。
                renderables.append(render_scope(locals_map, title="[i]locals"))

            renderables = [
                self._log_render(
                    self,
                    renderables,
                    log_time=self.get_datetime(),
                    path=path,
                    line_no=line_no,
                    link_path=link_path,
                )
            ]
# 【要点】dim 的实现是颜色混合：向背景色靠拢 40%，视觉上就是"变暗"。
#   注意混合是在"前景/背景已互换之后"做的，因此 reverse+dim 组合也能得到正确结果。
            for hook in render_hooks:
                renderables = hook.process_renderables(renderables)
# 【要点】SVG 里文字着色只用 CSS 的 fill（不是 color）——color 在 SVG 中无效，
#   这是从终端样式映射到矢量图时最容易弄错的一点。
            new_segments: List[Segment] = []
            extend = new_segments.extend
            render = self.render
            render_options = self.options
            for renderable in renderables:
                extend(render(renderable, render_options))
            buffer_extend = self._buffer.extend
            for line in Segment.split_and_crop_lines(
                new_segments, self.width, pad=False
            ):
                buffer_extend(line)

# 【要点】与 export_html 不同，SVG 默认用深色主题（SVG_EXPORT_THEME），
#   因为导出图片通常嵌入深色背景的文档/幻灯片中。
# 【易错】注意 _theme 是在 get_svg_style 定义之后才赋值的：闭包在调用时读取它，
#   所以只要在第一次调用 get_svg_style 之前赋值就没问题（这里正是如此）。
    def on_broken_pipe(self) -> None:
# 【算法】几何换算：字符高度固定 20px，字符宽度 = 20 × font_aspect_ratio（等宽字体比例），
#   行高 = 20 × 1.22（留 22% 的行距）。所有 x 坐标都乘 char_width、所有 y 都乘 line_height，
#   因此改变 font_aspect_ratio 会整体横向缩放而不会改变行数。
# 【注意】width 取的是"当前 Console 宽度"，而不是录制时的宽度：
#   如果录制后终端被缩窄，导出的图会按新宽度重新裁行。
        # 【参数】这三个几何常量决定了导出 SVG 的整体观感：
        # 【易错】修改 font_aspect_ratio 只影响横向缩放，不会改变裁剪方式（裁剪按字符数发生在
        """This function is called when a `BrokenPipeError` is raised.
#   char_height=20px 是字号基准；char_width = char_height × font_aspect_ratio；
#   line_height = char_height × 1.22 保证行间有 22% 的余量，避免文字贴在一起。
#   split_and_crop_lines 阶段），因此不会导致字符被切掉。

        This can occur when piping Textual output in Linux and macOS.
        The default implementation is to exit the app, but you could implement
# 外边距与内边距：margin 是图片四周留白（同时决定阴影/描边的位置），
# padding_top=40 特别大，因为终端窗口的"标题栏"（含红黄绿三个圆点）画在这一带。
# 下面四个等式只是把成对的边距/内边距相加，供尺寸计算复用。
        this method in a subclass to change the behavior.

        See https://docs.python.org/3/library/signal.html#note-on-sigpipe for details.
        """
        self.quiet = True
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        raise SystemExit(1)

# ==== 【五·补、提交与输入】 ====
    def _check_buffer(self) -> None:
        # 【要点】三个输出容器：text_backgrounds 收集所有背景矩形、text_group 收集所有文本节点、
        """Check if the buffer may be rendered. Render it if it can (e.g. Console.quiet is False)
        Rendering is supported on Windows, Unix and Jupyter environments. For
#   classes 做"CSS 规则 → 编号"去重（与 export_html 同一思路）。
#   SVG 要求节点顺序固定（先画背景再画文字），所以背景与文字分两个列表收集。
        legacy Windows consoles, the win32 API is called directly.
        This method will also record what it renders if recording is enabled via Console.record.
        """
        if self.quiet:
# 【功能】HTML/SVG 转义文本并把空格换成 &#160;（不换行空格）。
# 【为什么】SVG 的 <text> 默认会折叠连续空格（XML 空白折叠规则），
#   用不换行空格才能保留 Rich 排版里的对齐空格——这是等宽终端画面能正确复刻的关键。
            del self._buffer[:]
            return

# 【功能】拼一个 XML 标签：有 content 生成 <name ...>content</name>，否则生成自闭合标签。
# 【参数】name：标签名；content：内部内容（None 或空串 → 自闭合）；**attribs：属性字典。
# 【算法】属性名去掉前导下划线并把内部下划线换成连字符（因为 Python 关键字/命名习惯
#   与 SVG 属性名不同，例如 _class → class、stroke_width → stroke-width）；
#   属性值经 stringify 处理：float 用 'g' 格式去掉多余的零（避免 1.2000000000000002 这种输出）。
# 【注意】属性值不做转义（调用方传的都是内部生成的安全值）；
#   content 为 "" 时也会走自闭合分支（空字符串为假）。
        try:
            self._write_buffer()
        except BrokenPipeError:
            self.on_broken_pipe()

# 【易错】isinstance 的第二个参数写成了单元素元组 (float) —— 它等价于 float（括号只是分组），
#   因此行为正确，但这种写法容易让人误以为还有别的类型被一起判断。
    def _write_buffer(self) -> None:
        """Write the buffer to the output file."""

        with self._lock:
            if self.record and not self._buffer_index:
                with self._record_buffer_lock:
                    self._record_buffer.extend(self._buffer[:])

            if self._buffer_index == 0:
                if self.is_jupyter:  # pragma: no cover
                    from .jupyter import display

                    display(self._buffer, self._render_buffer(self._buffer[:]))
                    del self._buffer[:]
                else:
                    if WINDOWS:
# 【算法】未指定 unique_id 时，用"所有 Segment 的 repr 拼接 + 标题"的 adler32 校验和
#   作为 id 前缀（形如 terminal-123456789）。
# 【为什么】同一份内容导出的 SVG id 稳定，多次导出可以比较/覆盖；不同内容不会撞 id，
#   从而避免把两份 SVG 粘进同一个 HTML 时 CSS class 与 clipPath 相互污染。
# 【注意】encode(..., "ignore") 保证遇到无法编码的字符时也不会抛异常。
                        use_legacy_windows_render = False
                        if self.legacy_windows:
                            fileno = get_fileno(self.file)
                            if fileno is not None:
                                use_legacy_windows_render = (
                                    fileno in _STD_STREAMS_OUTPUT
                                )

                        if use_legacy_windows_render:
                            from rich._win32_console import LegacyWindowsTerm
# 【要点】y 预先设为 0 是为了兜住"内容为空、循环一次都不执行"的情况（后面要用 y 算高度）；
#   循环体内 y 会被 enumerate 覆盖为真实行号。
                            from rich._windows_renderer import legacy_windows_render
# 【算法】按导出宽度把 Segment 流切行（不 pad：短行不需要补空格，因为背景矩形是逐段画的），
#   再对每行逐段处理。外层 y 是行号、内层 x 是当前单元格列位置。

                            buffer = self._buffer[:]
# 【算法】三段解构 Segment：text（内容）、style（样式，可能为 None）、control（控制码，
#   已被 filter_control 剔除，这里用不到）。style=None 时用空 Style 占位，
#   这样后面所有属性访问都不用判空。
# 【要点】样式相同的段会被归到同一个 class（classes 字典），因此相邻同色文本只生成一个 class。
# 【易错】控制码段已经被 Segment.filter_control 过滤，所以这里的 _control 恒为 None；
#   变量名加下划线前缀表示"有意不使用"。
                            if self.no_color and self._color_system:
                                buffer = list(Segment.remove_color(buffer))

# 【要点】按 CSS 规则串（而不是 Style 对象）去重：不同 Style 可能产生完全相同的 CSS，
#   合并后 class 数量更少、SVG 更小。style_no 从 1 递增。
                            legacy_windows_render(buffer, LegacyWindowsTerm(self.file))
                        else:
                            # Either a non-std stream on legacy Windows, or modern Windows.
# 【要点】class 名不含 unique_id（只有 r1/r2…），真正加前缀是在生成 CSS 与文本节点时
#   （见 styles 与 clip_path 处的 f-string），这样同一份导出内部的类名更短。
                            text = self._render_buffer(self._buffer[:])
# 【算法】背景矩形的关键：reverse 时背景其实是"前景色"（因为 get_svg_style 里已互换），
#   所以这里背景取的是 style.color（没写则用主题前景色）；非 reverse 时取 style.bgcolor。
# 【易错】has_background 只在"非 reverse 且 bgcolor 有效且不是 default"时为真，
#   所以 default 背景色不会生成矩形（由最外层终端的背景色统一负责）。
                            # https://bugs.python.org/issue37871
                            # https://github.com/python/cpython/issues/82052
                            # We need to avoid writing more than 32Kb in a single write, due to the above bug
                            write = self.file.write
                            # Worse case scenario, every character is 4 bytes of utf-8
                            MAX_WRITE = 32 * 1024 // 4
                            try:
                                if len(text) <= MAX_WRITE:
                                    write(text)
# 【要点】只有"显式且非 default"的背景色才画矩形：
#   默认背景由模板里最外层的 <rect fill=背景色> 统一提供，逐段再画一次纯属浪费。
# 【注意】text_backgrounds 与 text_group 分开收集，是为了保证 SVG 中"所有背景先画、
#   所有文字后画"的层叠顺序。
                                else:
                                    batch: List[str] = []
                                    batch_append = batch.append
                                    size = 0
                                    for line in text.splitlines(True):
                                        if size + len(line) > MAX_WRITE and batch:
# 【要点】用 cell_len 而不是 len：中文、emoji 等宽字符占 2 个单元格，
#   若用 len 会让背景矩形与 x 坐标逐渐错位。
                                            write("".join(batch))
# 【算法】背景矩形：位置 (x*char_width, y*line_height + 1.5)，尺寸按 text_length 计算；
#   shape_rendering="crispEdges" 关闭抗锯齿，避免相邻矩形之间出现细缝。
# 【注意】高度用 line_height + 0.25 稍微多出一点，也是为了防止行间露出底色缝隙。
                                            batch.clear()
# 【要点】背景矩形独立收集：SVG 的绘制顺序就是节点顺序，
#   把矩形统一放在文字之前可以避免"后画的背景盖住先画的文字"。
                                            size = 0
                                        batch_append(line)
                                        size += len(line)
                                    if batch:
                                        write("".join(batch))
                                        batch.clear()
                            except UnicodeEncodeError as error:
                                error.reason = f"{error.reason}\n*** You may need to add PYTHONIOENCODING=utf-8 to your environment ***"
                                raise
                    else:
                        text = self._render_buffer(self._buffer[:])
# 【优化】纯空格的文本节点不生成：它在视觉上完全由背景矩形体现，
#   跳过可以显著减小 SVG（表格/面板会产出大量空白段）。
# 【注意】判断用的是 len（不是 cell_len），对纯 ASCII 空格来说两者等价。
# 【要点】这里刻意跳过纯空格段：它们在视觉上完全由背景矩形表达，
#   不生成 <text> 可以让表格类输出的 SVG 体积显著减小。
                        try:
# 【要点】文本节点的 y 用 y*line_height + char_height：SVG 的文本坐标是基线位置，
#   加上一个字符高度后文字才落在该行的中线附近（而不是悬在行的上方）。
                            self.file.write(text)
                        except UnicodeEncodeError as error:
                            error.reason = f"{error.reason}\n*** You may need to add PYTHONIOENCODING=utf-8 to your environment ***"
                            raise

                    self.file.flush()
                    del self._buffer[:]
# 【要点】textLength 强制把文本拉伸到"字符数 × 字符宽"的像素长度——
#   SVG 渲染器无法保证所用字体等宽，不指定就会与背景矩形对不齐。
#   注意这里乘的是 len(text) 而不是 cell_len(text)，因此含有宽字符的段会被拉伸得
#   偏窄（文字可能挤在一起），这是默认模板的一个已知局限。
# 【易错】_class 会经 make_tag 变成 class 属性；clip_path 会变成 clip-path（下划线转连字符）。

# 【要点】每个文本节点按行 clipPath 裁剪：文字超出导出宽度时会被切掉，
#   而不是溢出到窗口外。这也是后面必须为每一行生成 clipPath 的原因。
    def _render_buffer(self, buffer: Iterable[Segment]) -> str:
        """Render buffered output, and clear buffer."""
        output: List[str] = []
# 【要点】x 用单元格宽度累加（与背景/裁剪保持同一度量），保证后续段的位置正确。
        append = output.append
# 【算法】clipPath 的 y 偏移与背景矩形保持一致（+1.5），共 y 行（range(y) 得到 0..y-1）。
# 【易错】这里的行数用 y（循环结束后的最后行号）而不是 y+1：
#   由于文本 y 坐标是 y*line_height + char_height（即行底部基线附近），
#   clip 区域从该行顶部开始覆盖到下一行顶部，配合 +1.5 的偏移刚好够用。
        color_system = self._color_system
# 【算法】为每一行生成一个 clipPath（rect 覆盖整行宽度、行高 + 0.25），
#   文本节点通过 url(#<id>-line-<n>) 引用它。
        legacy_windows = self.legacy_windows
        not_terminal = not self.is_terminal
        if self.no_color and color_system:
            buffer = Segment.remove_color(buffer)
        for text, style, control in buffer:
            if style:
# 【算法】把 classes 里的每条 CSS 规则写成 ".<unique_id>-r<n> { <css> }"；
#   加 unique_id 前缀是为了多份 SVG 内联到同一 HTML 时互不干扰。
# 【优化】backgrounds 与 matrix 用空串 join 而不是换行，直接拼成一大段（外层还有模板包裹）。
                append(
                    style.render(
                        text,
                        color_system=color_system,
                        legacy_windows=legacy_windows,
# 【算法】外框尺寸：宽 = 字符区像素宽 + 左右内边距（ceil 取整避免小数缝隙），
#   高 = (行数) × 行高 + 上下内边距。注意这里用 y+1 作为行数。
                    )
# 【要点】(y + 1) 是行数：y 是最后一行的下标（0 起），加 1 才是总行数；
#   内容为空时 y 仍是 0，于是高度等于"一行 + 内边距"，不会退化成 0。
                )
# 【算法】chrome 是窗口外框：一个圆角矩形（rx=8）+ 标题文字 + 三个装饰圆点。
#   圆点用固定颜色写死（macOS 红黄绿），并用 <g transform> 平移定位，
#   这是纯装饰，与主题无关。
            elif not (not_terminal and control):
                append(text)
# 【要点】外框用主题背景色填充并加一层半透明白描边（rgba(255,255,255,0.35)），
#   这样在浅色或深色文档背景上都能看出窗口边界。

        rendered = "".join(output)
        return rendered

    # 【功能】显示提示文本并读取一行用户输入。
    # 【参数】prompt：提示字符串或 Text（markup/emoji 只对 str 生效）；password=True 时用 getpass 隐藏输入；
    # stream：改从指定文本流读取而不是 stdin。
    # 【返回】str；用 stream 时包含行尾换行符，getpass 与内置 input() 会去掉换行。
    # 【注意】password=True 时提示仍由 Rich 打印，getpass 只负责读入；非交互环境下可能抛 EOFError。
    def input(
        self,
        prompt: TextType = "",
        *,
        markup: bool = True,
# 【要点】标题颜色固定取主题前景色（而不是标题栏常见的灰白），
#   目的是让导出图片在深色主题下保持足够的对比度。
        emoji: bool = True,
# 【要点】标题是可选的：空字符串（含默认以外的显式空值）时跳过标题节点，
#   但窗口外框与装饰圆点仍然保留，避免出现"只有三个圆点没有标题"的怪比例。
        password: bool = False,
        stream: Optional[TextIO] = None,
    ) -> str:
        # 【要点】三个装饰圆点用 f-string 直接内联在代码里：它们是纯装饰、不依赖主题，
        """Displays a prompt and waits for input from the user. The prompt may contain color / style.

        It works in the same way as Python's builtin :func:`input` function and provides elaborate line editing and history features if Python's builtin :mod:`readline` module is previously loaded.

        Args:
            prompt (Union[str, Text]): Text to render in the prompt.
            markup (bool, optional): Enable console markup (requires a str prompt). Defaults to True.
#   所以没有走 make_tag 的通用路径（也省下三次字典构造）。
            emoji (bool, optional): Enable emoji (requires a str prompt). Defaults to True.
            password: (bool, optional): Hide typed text. Defaults to False.
            stream: (TextIO, optional): Optional file to read input from (rather than stdin). Defaults to None.

        Returns:
            str: Text read from stdin.
        """
# 【算法】最后把几何量、样式表、三层内容（chrome/backgrounds/matrix）与 clipPath 定义
#   注入模板。各占位符含义：unique_id（id 前缀）、char_width/char_height/line_height
#   （字格几何，单位 px）、terminal_width/terminal_height（内层字符区尺寸）、
#   width/height（整图尺寸，含外边距）、terminal_x/terminal_y（字符区左上角偏移）、
#   styles（CSS）、chrome（窗口装饰）、backgrounds（背景矩形）、matrix（文本节点）、
#   lines（clipPath 定义）。
# 【易错】terminal_width 在这里又算了一次（char_width * width - 1，减 1 避免最右侧溢出），
#   与上面 chrome 用的 terminal_width（含 padding）不是同一个值，读代码时别混淆。
# 【注意】返回前不清空局部容器；需要清空的是 _record_buffer（已在锁内按 clear 处理）。
        if prompt:
# 【要点】save_svg 只透传参数、不加工内容：所有校验与默认值都在 export_svg 内完成，
#   因此自定义 unique_id 的稳定性由调用方负责（相同 id + 相同模板 → 覆盖式更新图片）。
            self.print(prompt, markup=markup, emoji=emoji, end="")
        if password:
            import getpass as _getpass_mod

# 【易错】这里的 terminal_width 与上面 chrome 用的同名局部量含义不同：
#   模板需要的是"字符区宽度"（不含内边距），减 1 是为了避免最右一列压到描边。
            result = _getpass_mod.getpass("", stream=stream)
        else:
            if stream:
                result = stream.readline()
            else:
                result = input()
        return result

# 【要点】模板变量名与局部变量一一对应，命名尽量自解释（styles/chrome/backgrounds/
#   matrix/lines）；其中 matrix 是文本节点集合，lines 是 clipPath 定义。

# ==== 【六·补、导出的共同约定】 ====
    def export_text(self, *, clear: bool = True, styles: bool = False) -> str:
        # 【功能】export_svg 的文件版：把 SVG 写入路径。
        # 【参数】与 export_svg 完全一致（title/theme/clear/code_format/font_aspect_ratio/unique_id）。
        # 【兼容】固定 encoding="utf-8"：SVG 里常含中文标题与文本，
        """Generate text from console contents (requires record=True argument in constructor).

        Args:
            clear (bool, optional): Clear record buffer after exporting. Defaults to ``True``.
#   不指定编码在 Windows 上会抛 UnicodeEncodeError。
            styles (bool, optional): If ``True``, ansi escape codes will be included. ``False`` for plain text.
                Defaults to ``False``.

        Returns:
            str: String containing console contents.

        """
        assert (
            self.record
        ), "To export console contents set record=True in the constructor or instance"

        with self._record_buffer_lock:
            if styles:
                text = "".join(
                    (style.render(text) if style else text)
                    for text, style, _ in self._record_buffer
                )
            else:
                text = "".join(
                    segment.text
                    for segment in self._record_buffer
                    if not segment.control
                )
            if clear:
                del self._record_buffer[:]
        return text
# 【要点】只是转调：所有逻辑都在 export_svg 里，save_* 系列方法都遵循这一模式
#   （export_* 负责生成字符串，save_* 负责编码写文件），便于调用方自己处理字符串。

    def save_text(self, path: str, *, clear: bool = True, styles: bool = False) -> None:
        # 【约束】pragma: no cover 表示不纳入覆盖率统计：
        """Generate text from console and save to a given location (requires record=True argument in constructor).

        Args:
            path (str): Path to write text files.
            clear (bool, optional): Clear record buffer after exporting. Defaults to ``True``.
            styles (bool, optional): If ``True``, ansi style codes will be included. ``False`` for plain text.
                Defaults to ``False``.

#   这段代码只在手工执行 python -m rich.console 时运行，测试套件不会触发它。
        """
# 【要点】构造时不强制任何能力，完全走自动探测：这就是 Rich 在真实终端里的默认表现。
        text = self.export_text(clear=clear, styles=styles)
# 【要点】演示 log 的两个常规用法：先输出一个带 markup 标签的 JSONRPC 请求（含各种类型），
#   再输出纯文本与 repr。可以观察到数字/布尔/None 的高亮、路径与时间戳的样式。
        with open(path, "w", encoding="utf-8") as write_file:
# 【要点】这一项演示 markup：方括号里的 [i]...[/i] 会被解析成斜体，
#   而不是作为字面文本输出——这正是 markup=True 的默认行为。
            write_file.write(text)

    # 【功能】把 record=True 录制下来的输出导出成 HTML 字符串。
    # 【算法】先用 Segment.simplify + filter_control 规整录制内容；inline_styles=True 时把样式内联到 span/a 标签，
    # 否则生成 .rN 类名并拼出样式表；最后按模板填充代码、样式表与前景/背景色。
    # 【返回】str：完整 HTML 文本。
    # 【注意】record=False 时断言失败（AssertionError），不会静默返回空串。
    def export_html(
        self,
        *,
        theme: Optional[TerminalTheme] = None,
        clear: bool = True,
        code_format: Optional[str] = None,
        inline_styles: bool = False,
    ) -> str:
        # 【要点】演示 Pretty：嵌套 dict/list 会被自动美化、着色并缩进，
        """Generate HTML from console contents (requires record=True argument in constructor).

        Args:
            theme (TerminalTheme, optional): TerminalTheme object containing console colors.
#   说明 _collect_renderables 里的 is_expandable 分支在实际使用中的效果。
            clear (bool, optional): Clear record buffer after exporting. Defaults to ``True``.
            code_format (str, optional): Format string to render HTML. In addition to '{foreground}',
                '{background}', and '{code}', should contain '{stylesheet}' if inline_styles is ``False``.
            inline_styles (bool, optional): If ``True`` styles will be inlined in to spans, which makes files
                larger but easier to cut and paste markup. If ``False``, styles will be embedded in a style tag.
                Defaults to False.

        Returns:
            str: String containing console contents as HTML.
        """
        from html import escape

        assert (
            self.record
        ), "To export console contents set record=True in the constructor or instance"
        fragments: List[str] = []
        append = fragments.append
        _theme = theme or DEFAULT_TERMINAL_THEME
        stylesheet = ""

        render_code_format = CONSOLE_HTML_FORMAT if code_format is None else code_format

        with self._record_buffer_lock:
            if inline_styles:
                for text, style, _ in Segment.filter_control(
                    Segment.simplify(self._record_buffer)
                ):
                    text = escape(text)
                    if style:
                        rule = style.get_html_style(_theme)
                        if style.link:
                            text = f'<a href="{style.link}">{text}</a>'
                        text = f'<span style="{rule}">{text}</span>' if rule else text
                    append(text)
            else:
                styles: Dict[str, int] = {}
                for text, style, _ in Segment.filter_control(
                    Segment.simplify(self._record_buffer)
                ):
                    text = escape(text)
                    if style:
                        rule = style.get_html_style(_theme)
                        style_number = styles.setdefault(rule, len(styles) + 1)
                        if style.link:
                            text = f'<a class="r{style_number}" href="{style.link}">{text}</a>'
                        else:
                            text = f'<span class="r{style_number}">{text}</span>'
                    append(text)
                stylesheet_rules: List[str] = []
                stylesheet_append = stylesheet_rules.append
                for style_rule, style_number in styles.items():
                    if style_rule:
                        stylesheet_append(f".r{style_number} {{{style_rule}}}")
                stylesheet = "\n".join(stylesheet_rules)

            rendered_code = render_code_format.format(
                code="".join(fragments),
                stylesheet=stylesheet,
                foreground=_theme.foreground_color.hex,
                background=_theme.background_color.hex,
            )
            if clear:
                del self._record_buffer[:]
        return rendered_code

    # 【功能】把 export_html 的结果写入文件。
    # 【参数】path：输出路径；其余参数与 export_html 相同。
    # 【返回】无。
    # 【注意】非内联模式下生成的是自带 <style> 表的单个文件，便于直接分发。
    def save_html(
        self,
        path: str,
        *,
        theme: Optional[TerminalTheme] = None,
        clear: bool = True,
        code_format: str = CONSOLE_HTML_FORMAT,
        inline_styles: bool = False,
    ) -> None:
        """Generate HTML from console contents and write to a file (requires record=True argument in constructor).

        Args:
            path (str): Path to write html file.
            theme (TerminalTheme, optional): TerminalTheme object containing console colors.
            clear (bool, optional): Clear record buffer after exporting. Defaults to ``True``.
            code_format (str, optional): Format string to render HTML. In addition to '{foreground}',
                '{background}', and '{code}', should contain '{stylesheet}' if inline_styles is ``False``.
            inline_styles (bool, optional): If ``True`` styles will be inlined in to spans, which makes files
                larger but easier to cut and paste markup. If ``False``, styles will be embedded in a style tag.
                Defaults to False.

        """
        html = self.export_html(
            theme=theme,
            clear=clear,
            code_format=code_format,
            inline_styles=inline_styles,
        )
        with open(path, "w", encoding="utf-8") as write_file:
            write_file.write(html)

# ==== 【七、SVG 导出：把 Segment 画成矢量图】 ====
    def export_svg(
        self,
        *,
        title: str = "Rich",
        theme: Optional[TerminalTheme] = None,
        clear: bool = True,
        code_format: str = CONSOLE_SVG_FORMAT,
        font_aspect_ratio: float = 0.61,
        unique_id: Optional[str] = None,
    ) -> str:
        """
        Generate an SVG from the console contents (requires record=True in Console constructor).

        Args:
            title (str, optional): The title of the tab in the output image
            theme (TerminalTheme, optional): The ``TerminalTheme`` object to use to style the terminal
            clear (bool, optional): Clear record buffer after exporting. Defaults to ``True``
            code_format (str, optional): Format string used to generate the SVG. Rich will inject a number of variables
                into the string in order to form the final SVG output. The default template used and the variables
                injected by Rich can be found by inspecting the ``console.CONSOLE_SVG_FORMAT`` variable.
            font_aspect_ratio (float, optional): The width to height ratio of the font used in the ``code_format``
                string. Defaults to 0.61, which is the width to height ratio of Fira Code (the default font).
                If you aren't specifying a different font inside ``code_format``, you probably don't need this.
            unique_id (str, optional): unique id that is used as the prefix for various elements (CSS styles, node
                ids). If not set, this defaults to a computed value based on the recorded content.
        """

        import zlib
        from html import escape

        from rich.cells import cell_len

        style_cache: Dict[Style, str] = {}

        # 【功能】为 export_svg 把一个 Style 转换成 SVG 属性字符串（fill / font-weight / text-decoration 等）。
        # 【参数】style：待转换样式。
        # 【返回】可直接放进 <text> 标签的属性串。
        # 【注意】颜色需先经 Color.get_truecolor 折算，因为 SVG 不接受终端调色板索引。
        def get_svg_style(style: Style) -> str:
            """Convert a Style to CSS rules for SVG."""
            if style in style_cache:
                return style_cache[style]
            css_rules = []
            color = (
                _theme.foreground_color
                if (style.color is None or style.color.is_default)
                else style.color.get_truecolor(_theme)
            )
            bgcolor = (
                _theme.background_color
                if (style.bgcolor is None or style.bgcolor.is_default)
                else style.bgcolor.get_truecolor(_theme)
            )
            if style.reverse:
                color, bgcolor = bgcolor, color
            if style.dim:
                color = blend_rgb(color, bgcolor, 0.4)
            css_rules.append(f"fill: {color.hex}")
            if style.bold:
                css_rules.append("font-weight: bold")
            if style.italic:
                css_rules.append("font-style: italic;")
            if style.underline:
                css_rules.append("text-decoration: underline;")
            if style.strike:
                css_rules.append("text-decoration: line-through;")

            css = ";".join(css_rules)
            style_cache[style] = css
            return css

        _theme = theme or SVG_EXPORT_THEME

        width = self.width
        char_height = 20
        char_width = char_height * font_aspect_ratio
        line_height = char_height * 1.22

        margin_top = 1
        margin_right = 1
        margin_bottom = 1
        margin_left = 1

        padding_top = 40
        padding_right = 8
        padding_bottom = 8
        padding_left = 8

        padding_width = padding_left + padding_right
        padding_height = padding_top + padding_bottom
        margin_width = margin_left + margin_right
        margin_height = margin_top + margin_bottom

        text_backgrounds: List[str] = []
        text_group: List[str] = []
        classes: Dict[str, int] = {}
        style_no = 1

        # 【功能】转义 XML 文本节点中的 & < >。
        # 【参数】text：原始文本。
        # 【返回】转义后的文本。
        # 【注意】SVG 是 XML，未转义的 & 或 < 会让整份文件解析失败。
        def escape_text(text: str) -> str:
            """HTML escape text and replace spaces with nbsp."""
            return escape(text).replace(" ", "&#160;")

        # 【功能】拼装一个带属性的 SVG 标签（如 <text ...>内容</text>）。
        # 【参数】**kwargs：标签属性，字段名中的下划线会转成连字符（text_anchor → text-anchor）。
        # 【返回】标签字符串。
        # 【算法】用列表推导拼接属性，再套上开闭标签，避免重复手写同类标签。
        def make_tag(
            name: str, content: Optional[str] = None, **attribs: object
        ) -> str:
            """Make a tag from name, content, and attributes."""

            # 【功能】把任意值转成 SVG 属性可用的字符串。
            # 【参数】value：任意对象。
            # 【返回】str。
            # 【注意】浮点会去掉多余小数位，减少 SVG 体积。
            def stringify(value: object) -> str:
                if isinstance(value, (float)):
                    return format(value, "g")
                return str(value)

            tag_attribs = " ".join(
                f'{k.lstrip("_").replace("_", "-")}="{stringify(v)}"'
                for k, v in attribs.items()
            )
            return (
                f"<{name} {tag_attribs}>{content}</{name}>"
                if content
                else f"<{name} {tag_attribs}/>"
            )

        with self._record_buffer_lock:
            segments = list(Segment.filter_control(self._record_buffer))
            if clear:
                self._record_buffer.clear()

        if unique_id is None:
            unique_id = "terminal-" + str(
                zlib.adler32(
                    ("".join(repr(segment) for segment in segments)).encode(
                        "utf-8",
                        "ignore",
                    )
                    + title.encode("utf-8", "ignore")
                )
            )
        y = 0
        for y, line in enumerate(Segment.split_and_crop_lines(segments, length=width)):
            x = 0
            for text, style, _control in line:
                style = style or Style()
                rules = get_svg_style(style)
                if rules not in classes:
                    classes[rules] = style_no
                    style_no += 1
                class_name = f"r{classes[rules]}"

                if style.reverse:
                    has_background = True
                    background = (
                        _theme.foreground_color.hex
                        if style.color is None
                        else style.color.get_truecolor(_theme).hex
                    )
                else:
                    bgcolor = style.bgcolor
                    has_background = bgcolor is not None and not bgcolor.is_default
                    background = (
                        _theme.background_color.hex
                        if style.bgcolor is None
                        else style.bgcolor.get_truecolor(_theme).hex
                    )

                text_length = cell_len(text)
                if has_background:
                    text_backgrounds.append(
                        make_tag(
                            "rect",
                            fill=background,
                            x=x * char_width,
                            y=y * line_height + 1.5,
                            width=char_width * text_length,
                            height=line_height + 0.25,
                            shape_rendering="crispEdges",
                        )
                    )

                if text != " " * len(text):
                    text_group.append(
                        make_tag(
                            "text",
                            escape_text(text),
                            _class=f"{unique_id}-{class_name}",
                            x=x * char_width,
                            y=y * line_height + char_height,
                            textLength=char_width * len(text),
                            clip_path=f"url(#{unique_id}-line-{y})",
                        )
                    )
                x += cell_len(text)

        line_offsets = [line_no * line_height + 1.5 for line_no in range(y)]
        lines = "\n".join(
            f"""<clipPath id="{unique_id}-line-{line_no}">
    {make_tag("rect", x=0, y=offset, width=char_width * width, height=line_height + 0.25)}
            </clipPath>"""
            for line_no, offset in enumerate(line_offsets)
        )

        styles = "\n".join(
            f".{unique_id}-r{rule_no} {{ {css} }}" for css, rule_no in classes.items()
        )
        backgrounds = "".join(text_backgrounds)
        matrix = "".join(text_group)

        terminal_width = ceil(width * char_width + padding_width)
        terminal_height = (y + 1) * line_height + padding_height
        chrome = make_tag(
            "rect",
            fill=_theme.background_color.hex,
            stroke="rgba(255,255,255,0.35)",
            stroke_width="1",
            x=margin_left,
            y=margin_top,
            width=terminal_width,
            height=terminal_height,
            rx=8,
        )

        title_color = _theme.foreground_color.hex
        if title:
            chrome += make_tag(
                "text",
                escape_text(title),
                _class=f"{unique_id}-title",
                fill=title_color,
                text_anchor="middle",
                x=terminal_width // 2,
                y=margin_top + char_height + 6,
            )
        chrome += f"""
            <g transform="translate(26,22)">
            <circle cx="0" cy="0" r="7" fill="#ff5f57"/>
            <circle cx="22" cy="0" r="7" fill="#febc2e"/>
            <circle cx="44" cy="0" r="7" fill="#28c840"/>
            </g>
        """

        svg = code_format.format(
            unique_id=unique_id,
            char_width=char_width,
            char_height=char_height,
            line_height=line_height,
            terminal_width=char_width * width - 1,
            terminal_height=(y + 1) * line_height - 1,
            width=terminal_width + margin_width,
            height=terminal_height + margin_height,
            terminal_x=margin_left + padding_left,
            terminal_y=margin_top + padding_top,
            styles=styles,
            chrome=chrome,
            backgrounds=backgrounds,
            matrix=matrix,
            lines=lines,
        )
        return svg

    # 【功能】把 Console 录制的内容导出成 SVG 图片文件。
    # 【参数】path：输出路径；title：SVG 标题；theme：终端主题；clear：导出后是否清空录制缓冲；
    # font_aspect_ratio：字符高宽比，用于推算字号；unique_id：DOM id 前缀，避免同页多张 SVG 冲突。
    # 【注意】需要 record=True（内部会断言）；固定以 utf-8 写文件。
    def save_svg(
        self,
        path: str,
        *,
        title: str = "Rich",
        theme: Optional[TerminalTheme] = None,
        clear: bool = True,
        code_format: str = CONSOLE_SVG_FORMAT,
        font_aspect_ratio: float = 0.61,
        unique_id: Optional[str] = None,
    ) -> None:
        """Generate an SVG file from the console contents (requires record=True in Console constructor).

        Args:
            path (str): The path to write the SVG to.
            title (str, optional): The title of the tab in the output image
            theme (TerminalTheme, optional): The ``TerminalTheme`` object to use to style the terminal
            clear (bool, optional): Clear record buffer after exporting. Defaults to ``True``
            code_format (str, optional): Format string used to generate the SVG. Rich will inject a number of variables
                into the string in order to form the final SVG output. The default template used and the variables
                injected by Rich can be found by inspecting the ``console.CONSOLE_SVG_FORMAT`` variable.
            font_aspect_ratio (float, optional): The width to height ratio of the font used in the ``code_format``
                string. Defaults to 0.61, which is the width to height ratio of Fira Code (the default font).
                If you aren't specifying a different font inside ``code_format``, you probably don't need this.
            unique_id (str, optional): unique id that is used as the prefix for various elements (CSS styles, node
                ids). If not set, this defaults to a computed value based on the recorded content.
        """
        svg = self.export_svg(
            title=title,
            theme=theme,
            clear=clear,
            code_format=code_format,
            font_aspect_ratio=font_aspect_ratio,
            unique_id=unique_id,
        )
        with open(path, "w", encoding="utf-8") as write_file:
            write_file.write(svg)


# ==== 【八、模块自测（python -m rich.console）】 ====
if __name__ == "__main__":  # pragma: no cover
    console = Console(record=True)

    console.log(
        "JSONRPC [i]request[/i]",
        5,
        1.3,
        True,
        False,
        None,
        {
            "jsonrpc": "2.0",
            "method": "subtract",
            "params": {"minuend": 42, "subtrahend": 23},
            "id": 3,
        },
    )

    console.log("Hello, World!", "{'a': 1}", repr(console))

    console.print(
        {
            "name": None,
            "empty": [],
            "quiz": {
                "sport": {
                    "answered": True,
                    "q1": {
                        "question": "Which one is correct team name in NBA?",
                        "options": [
                            "New York Bulls",
                            "Los Angeles Kings",
                            "Golden State Warriors",
                            "Huston Rocket",
                        ],
                        "answer": "Huston Rocket",
                    },
                },
                "maths": {
                    "answered": False,
                    "q1": {
                        "question": "5 + 7 = ?",
                        "options": [10, 11, 12, 13],
                        "answer": 12,
                    },
                    "q2": {
                        "question": "12 - 8 = ?",
                        "options": [1, 2, 3, 4],
                        "answer": 4,
                    },
                },
            },
        }
    )
