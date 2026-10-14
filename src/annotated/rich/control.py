import time
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Union, Final

from .segment import ControlCode, ControlType, Segment

if TYPE_CHECKING:
    from .console import Console, ConsoleOptions, RenderResult

# ---------------------------------------------------------------------------
# 【模块定位】终端控制码的“生成与清理”层，包含两件事：
#   1) 把语义化的 ControlType（枚举定义在 segment.py）翻译成真实的 ANSI/ECMA-48 转义序列；
#   2) 提供剥离/转义文本中少量 C0 控制字符的工具函数。
#   调用方：console.py 的 bell()/home()/clear()/show_cursor()/set_alt_screen()/
#   set_window_title() 等便捷方法都会构造 Control 并交给 Console.control()；
#   text.py 在构造/更新/追加文本时用 strip_control_codes 清理输入（共 4 处）；
#   _inspect.py 用 escape_control_codes 把 docstring 里的控制字符转成可见写法。
#   ANSI 序列的“解析”（把终端里的 SGR/OSC 解回样式）不在本文件，而在 rich/ansi.py。
# 【关键数据结构】
#   - CONTROL_CODES_FORMAT：Dict[ControlType, Callable[..., str]]，枚举 → ANSI 序列的映射表；
#   - ControlCode：Union[(ControlType,), (ControlType, int | str), (ControlType, int, int)]，
#     定义在 segment.py，是 Segment.control 字段的元素类型；
#   - STRIP_CONTROL_CODES / CONTROL_ESCAPE：码位 → None / 替换串 的 str.translate 映射表。
# 【设计要点】
#   - 控制码不直接写标准输出，而是随 Segment（第三个字段 control）在渲染管线里流动，
#     最终由 Console 决定是否真的发给终端。这正是“同一份渲染结果既能输出到终端，
#     又能录制成纯文本/HTML”的基础。
#   - 用“字典 + lambda”做枚举到字符串的查表，取代一长串 if/elif：新增控制码只需加一行。
#   - 剥离与转义都基于 str.translate：一次 C 级扫描完成，是文本热路径上最省的写法。
#   - 重要：本文件**不做**任何 ANSI 解析。STRIP_CONTROL_CODES 只有 5 个 C0 控制字符
#     （BEL/BS/VT/FF/CR），ESC 引导的 CSI/OSC 序列不会被 strip_control_codes 去掉
#     （例如 strip_control_codes("a\x1b[31mb") 原样返回）。若课程材料提到“CSI/OSC 状态机”，
#     那部分在 rich/ansi.py（_ansi_tokenize / AnsiDecoder），而 Windows 旧版控制台的
#     API 翻译在 rich/_windows_renderer.py。
# 【阅读建议】先读三张常量表（尤其 CONTROL_CODES_FORMAT，它定义了 Rich 支持的全部控制码），
#   再读 Control 的类方法（语义化构造），最后读末尾两个 str.translate 工具函数。
# ---------------------------------------------------------------------------


# ==== 【常量表一：需要从文本中剥离的 C0 控制字符】 ====
# 【要点】只列出 5 个会破坏终端版面的 C0 控制码：BEL(7)、BS(8)、VT(11)、FF(12)、CR(13)。
#   刻意不含 ESC(0x1B) 与 TAB(0x09)：前者是 ANSI 序列的引导符（应由 ansi 层处理），
#   后者是文本对齐算法要保留的空白字符。
# 【约束】这份列表同时决定下面 _CONTROL_STRIP_TRANSLATE 的键集合，两者必须保持一致。
STRIP_CONTROL_CODES: Final = [
    7,  # Bell
    8,  # Backspace
    11,  # Vertical tab
    12,  # Form feed
    13,  # Carriage return
]

# 【要点】str.translate 的映射语义：值为 None 表示“删除该码位”，值为 str 表示“替换成该串”。
#   这里用字典推导从上面的码位列表生成，避免手写重复的键。
# 【优化】该字典是模块级常量，并且被用作 strip_control_codes 的默认参数值，
#   因此每次调用都复用同一个对象，不必重建映射表。
_CONTROL_STRIP_TRANSLATE: Final = {
    _codepoint: None for _codepoint in STRIP_CONTROL_CODES
}

# ==== 【常量表二：控制字符的“可见转义”写法】 ====
# 【要点】把不可见控制字符替换成它在源码里的转义写法（\r → "\\r"，即反斜杠加字母两个字符），
#   用于把 docstring、样式串之类的文本安全地展示给用户。
# 【约束】键集合与 STRIP_CONTROL_CODES 完全相同（同样的 5 个码位），只是目标是字符串而非 None；
#   两张表覆盖同一批字符，才能在“删掉”与“显示出来”两种策略之间自由切换。
CONTROL_ESCAPE: Final = {
    7: "\\a",
    8: "\\b",
    11: "\\v",
    12: "\\f",
    13: "\\r",
}

# ==== 【常量表三：ControlType → ANSI 序列】 ====
# 【要点】这是 Rich 里唯一“认识 ANSI 控制语法”的地方：
#   - 无需参数的用零参 lambda（BELL、CLEAR、备用屏开关、光标显隐）；
#   - 需要参数的用 *args lambda（CURSOR_UP/DOWN/FORWARD/BACKWARD、ERASE_IN_LINE）；
#   - 坐标类要 +1：ANSI 的 CUP/CHA 用 1 基坐标，而 Rich 内部一律用 0 基行列，
#     所以 CURSOR_MOVE_TO 写成 f"\x1b[{y+1};{x+1}H"、CURSOR_MOVE_TO_COLUMN 写成 param+1
#     （tests/test_control.py 里 Control.move_to(5, 10) 期望 "\x1b[11;6H" 正是这个原因）；
#   - SET_WINDOW_TITLE 用的是 OSC 序列（\x1b]0;...\x07），以 BEL 而不是 ST 结束。
# 【约束】字典的键必须覆盖 Control 各构造方法会用到的 ControlType；缺少键会在构造
#   Control 时直接抛 KeyError，而不是静默输出错误序列。
# 【兼容】Windows 旧版控制台不支持这些 ANSI 序列，那里改用 _windows_renderer.py
#   把 Segment.control 翻译成 Win32 API 调用；本表只服务“支持 ANSI 的终端”。
CONTROL_CODES_FORMAT: Dict[int, Callable[..., str]] = {
    ControlType.BELL: lambda: "\x07",
    ControlType.CARRIAGE_RETURN: lambda: "\r",
    ControlType.HOME: lambda: "\x1b[H",
    ControlType.CLEAR: lambda: "\x1b[2J",
    ControlType.ENABLE_ALT_SCREEN: lambda: "\x1b[?1049h",
    ControlType.DISABLE_ALT_SCREEN: lambda: "\x1b[?1049l",
    ControlType.SHOW_CURSOR: lambda: "\x1b[?25h",
    ControlType.HIDE_CURSOR: lambda: "\x1b[?25l",
    ControlType.CURSOR_UP: lambda param: f"\x1b[{param}A",
    ControlType.CURSOR_DOWN: lambda param: f"\x1b[{param}B",
    ControlType.CURSOR_FORWARD: lambda param: f"\x1b[{param}C",
    ControlType.CURSOR_BACKWARD: lambda param: f"\x1b[{param}D",
    ControlType.CURSOR_MOVE_TO_COLUMN: lambda param: f"\x1b[{param+1}G",
    ControlType.ERASE_IN_LINE: lambda param: f"\x1b[{param}K",
    ControlType.CURSOR_MOVE_TO: lambda x, y: f"\x1b[{y+1};{x+1}H",
    ControlType.SET_WINDOW_TITLE: lambda title: f"\x1b]0;{title}\x07",
}


# ==== 【Control：可渲染的控制码对象】 ====
# 【职责】把一组 ControlCode 打包成一个 Segment，使控制码能够像普通内容一样参与渲染管线
#   （可以被 yield、记录到 record 缓冲、也可以在导出时按需过滤掉）。
# 【关键属性】segment：唯一的实例属性（受 __slots__ 限制）。它的 text 是拼好的 ANSI 字符串，
#   style 恒为 None，control 字段是规范化后的控制码序列（注意这里运行期实际传入的是 list，
#   而类型标注写的是 Tuple，见 __init__ 的注释）。
# 【不变量】1) 实例创建后不可变（没有 setter，也没有其它属性）；
#   2) segment.text 等于按 CONTROL_CODES_FORMAT 依次格式化各控制码后拼接的结果；
#   3) 控制码序列为空时 text 为空串，此时 __rich_console__ 不产出任何 Segment。
# 【协作】Console.control() 负责把它交给渲染流程；Console 的 bell/home/clear/show_cursor/
#   set_alt_screen/set_window_title 等便捷方法内部都在构造它；
#   tests/test_control.py 直接断言 .segment 的文本与 control 元组。
class Control:
    """A renderable that inserts a control code (non printable but may move cursor).

    Args:
        *codes (str): Positional arguments are either a :class:`~rich.segment.ControlType` enum or a
            tuple of ControlType and an integer parameter
    """

    # 【要点】用 __slots__ 限制实例只能有 segment 这一个属性：既省内存（控制对象会被频繁创建），
    #   也顺便把“实例不可变”这件事写进语言层面（无法随手加属性）。
    __slots__ = ["segment"]

    # 【功能】把若干控制码打包成一个可渲染对象。
    # 【算法】1) 用列表推导做规范化：裸 ControlType 包成单元素元组，已是元组的（带参数）原样保留；
    #   2) 从 CONTROL_CODES_FORMAT 取出对应格式化函数，按 `code, *parameters` 解包调用，
    #      再把结果 join 成一条 ANSI 字符串；
    #   3) 组装 Segment(文本, None, 控制码列表)——样式为 None，控制码放进第三个字段。
    # 【参数】*codes (Union[ControlType, ControlCode])：要么是 ControlType，要么是
    #   (ControlType, 参数…) 元组，两种写法可以混用。
    # 【返回】None（构造函数）。
    # 【注意】1) Segment 的 control 字段名义类型是 Tuple，这里传入的是 list；NamedTuple 不做
    #      运行期校验，测试也是按相等比较（list 与 list 相等），因此不会有问题，但阅读时别被标注误导；
    #   2) 未在 CONTROL_CODES_FORMAT 注册的 ControlType 会在第 2 步抛 KeyError，
    #      这是刻意让“漏注册”立刻暴露，而不是静默崩溃在终端上。
    def __init__(self, *codes: Union[ControlType, ControlCode]) -> None:
        control_codes: List[ControlCode] = [
            (code,) if isinstance(code, ControlType) else code for code in codes
        ]
        _format_map = CONTROL_CODES_FORMAT
        rendered_codes = "".join(
            _format_map[code](*parameters) for code, *parameters in control_codes
        )
        self.segment = Segment(rendered_codes, None, control_codes)

    # 【功能】让终端响铃（写入 BEL，即 \x07）。
    # 【返回】Control 实例。
    # 【注意】是否真的发声取决于终端与用户配置，很多终端默认关闭或改为视觉提示；
    #   这是唯一一个“有副作用但不改变版面”的控制码。
    # 【协作】Console.bell() 的底层实现。
    @classmethod
    def bell(cls) -> "Control":
        """Ring the 'bell'."""
        return cls(ControlType.BELL)

    # 【功能】把光标移到屏幕左上角（\x1b[H，HOME 位置）。
    # 【返回】Control 实例。
    # 【注意】只移动光标，不擦除任何内容；要清屏请配合 clear()。
    # 【协作】Console.home()；Console.clear() 的实现里就是先 clear 再 home
    #   （见 console.py 1152-1154），这样清屏后光标回到原点。
    @classmethod
    def home(cls) -> "Control":
        """Move cursor to 'home' position."""
        return cls(ControlType.HOME)

    # 【功能】相对当前位置移动光标。
    # 【算法】内层生成器 get_codes() 按需产出控制码：x 非 0 时产出
    #   CURSOR_FORWARD(abs(x))（右移）或 CURSOR_BACKWARD(abs(x))（左移）；
    #   y 非 0 时产出 CURSOR_DOWN(abs(y))（下移）或 CURSOR_UP(abs(y))（上移）；
    #   两个轴都为 0 时不产出任何控制码。
    # 【参数】x (int)：水平偏移，右为正、左为负，默认 0；y (int)：垂直偏移，下为正、上为负，默认 0。
    # 【返回】Control 实例（控制码顺序固定为“先水平、后垂直”）。
    # 【注意】1) 方向由符号决定、数值取绝对值，因为 ANSI 的参数只能是非负数；
    #   2) 偏移为 0 的轴不会产生任何序列——tests/test_control.py 断言
    #      Control.move(0, 0).segment == Segment("", None, [])，即“无动作”而不是“移动 0 格”；
    #   3) x、y 都是关键字可传的普通参数（不是 keyword-only）。
    @classmethod
    def move(cls, x: int = 0, y: int = 0) -> "Control":
        """Move cursor relative to current position.

        Args:
            x (int): X offset.
            y (int): Y offset.

        Returns:
            ~Control: Control object.

        """

        # 【功能】生成移动光标所需的控制码序列（Control.move 的内层生成器，按需产出）。
        # 【算法】x 非 0 时产出 CURSOR_FORWARD(abs(x))（右移）或 CURSOR_BACKWARD(abs(x))（左移）；
        #   y 非 0 时产出 CURSOR_DOWN(abs(y))（下移）或 CURSOR_UP(abs(y))（上移）；两轴都为 0 时什么都不产出。
        # 【返回】Iterable[ControlCode]：元素是 (ControlType, 参数) 元组，正好是 Control.__init__ 接受的第二种写法。
        # 【注意】ANSI 参数不能为负，所以这里取绝对值、用方向枚举表达正负；偏移为 0 的轴不产生任何序列
        #   （tests/test_control.py 断言 Control.move(0, 0) 的控制码为空）。
        def get_codes() -> Iterable[ControlCode]:
            control = ControlType
            # 【要点】把 ControlType 绑定到局部名 control，省去循环里的属性查找，
            #   也让下面几行更短；这是 Rich 里常见的写法。
            if x:
                yield (
                    control.CURSOR_FORWARD if x > 0 else control.CURSOR_BACKWARD,
                    abs(x),
                )
            if y:
                yield (
                    control.CURSOR_DOWN if y > 0 else control.CURSOR_UP,
                    abs(y),
                )

        # 【要点】*get_codes() 把生成器展开成 Control 的位置参数，每个元素都是
        #   (ControlType, 参数) 元组，正好是 Control.__init__ 接受的第二种写法。
        control = cls(*get_codes())
        return control

    # 【功能】把光标移到第 x 列（绝对列位置），并可附带一个相对行偏移 y。
    # 【算法】用条件表达式分成两支：
    #   y 非 0 时同时产出 (CURSOR_MOVE_TO_COLUMN, x) 与 (CURSOR_DOWN/UP, abs(y))；
    #   y 为 0 时只产出列移动。
    # 【参数】x (int)：绝对列号（0 基，格式化时 +1 变成 ANSI 的 1 基）；y (int)：相对行偏移，
    #   下为正、上为负，默认 0。
    # 【返回】Control 实例。
    # 【注意】1) y 为 0 时刻意不产出垂直移动（避免生成无意义的 0 参数指令）；
    #   2) 列是绝对定位而行是相对偏移，这种不对称是本方法的语义，不要改成两端都绝对；
    #   3) docstring 里出现了两个 "Returns:" 小节，是上游遗留的笔误：前一个描述参数 x / y，
    #      后一个才描述返回值，阅读时按语义理解即可。
    @classmethod
    def move_to_column(cls, x: int, y: int = 0) -> "Control":
        """Move to the given column, optionally add offset to row.

        Returns:
            x (int): absolute x (column)
            y (int): optional y offset (row)

        Returns:
            ~Control: Control object.
        """

        return (
            cls(
                (ControlType.CURSOR_MOVE_TO_COLUMN, x),
                (
                    ControlType.CURSOR_DOWN if y > 0 else ControlType.CURSOR_UP,
                    abs(y),
                ),
            )
            if y
            else cls((ControlType.CURSOR_MOVE_TO_COLUMN, x))
        )

    # 【功能】把光标移到屏幕上的绝对位置 (x, y)。
    # 【算法】直接构造单个 (CURSOR_MOVE_TO, x, y) 控制码，由 CONTROL_CODES_FORMAT 里的
    #   lambda x, y: f"\x1b[{y+1};{x+1}H" 生成 CUP 序列。
    # 【参数】x (int)：列号（0 基）；y (int)：行号（0 基）。
    # 【返回】Control 实例。
    # 【注意】1) 参数顺序是 (x, y)，而 ANSI 的书写顺序是“行;列”，因此格式表里做了调换；
    #   2) 两处 +1 是因为 ANSI 坐标从 1 开始——tests/test_control.py 断言
    #      Control.move_to(5, 10) 生成 "\x1b[11;6H"；
    #   3) 该控制码带两个参数，是 ControlCode 联合类型里唯一用满三个元素的形态。
    @classmethod
    def move_to(cls, x: int, y: int) -> "Control":
        """Move cursor to absolute position.

        Args:
            x (int): x offset (column)
            y (int): y offset (row)

        Returns:
            ~Control: Control object.
        """
        return cls((ControlType.CURSOR_MOVE_TO, x, y))

    # 【功能】清屏（\x1b[2J，擦除整屏内容）。
    # 【返回】Control 实例。
    # 【注意】只擦除、不移动光标（光标停在原处），因此 Clear 后通常还要一个 home()；
    #   Console.clear() 正是“clear + home”的组合（见 console.py 1152-1154）。
    @classmethod
    def clear(cls) -> "Control":
        """Clear the screen."""
        return cls(ControlType.CLEAR)

    # 【功能】显示或隐藏光标。
    # 【算法】三元表达式在 SHOW_CURSOR / HIDE_CURSOR 之间二选一。
    # 【参数】show (bool)：True 显示、False 隐藏。
    # 【返回】Control 实例。
    # 【注意】隐藏光标是 Live、进度条、备用屏避免闪烁的常用手段；
    #   隐藏后必须在退出时恢复，否则用户终端会一直看不到光标
    #   （Console 的 screen() 上下文会负责恢复，见 console.py 1196 行附近）。
    @classmethod
    def show_cursor(cls, show: bool) -> "Control":
        """Show or hide the cursor."""
        return cls(ControlType.SHOW_CURSOR if show else ControlType.HIDE_CURSOR)

    # 【功能】开启或关闭备用屏（alt screen，终端里的“第二块画布”）。
    # 【算法】开启时一次发出两个控制码：ENABLE_ALT_SCREEN + HOME（即 "\x1b[?1049h\x1b[H"）；
    #   关闭时只发 DISABLE_ALT_SCREEN。
    # 【参数】enable (bool)：True 进入备用屏，False 退出。
    # 【返回】Control 实例。
    # 【为什么】进入备用屏后若不归位光标，新内容会从主屏遗留的光标位置开始写；
    #   而退出备用屏时终端会自行恢复主屏及其光标，所以不需要额外指令。
    # 【协作】Console.set_alt_screen() 调用它并维护 is_alt_screen 状态；
    #   Console.screen() / Live(screen=True) 在此之上提供上下文管理。
    @classmethod
    def alt_screen(cls, enable: bool) -> "Control":
        """Enable or disable alt screen."""
        if enable:
            return cls(ControlType.ENABLE_ALT_SCREEN, ControlType.HOME)
        else:
            return cls(ControlType.DISABLE_ALT_SCREEN)

    # 【功能】设置终端窗口标题。
    # 【算法】构造 (SET_WINDOW_TITLE, title) 控制码，格式化成 OSC 序列 "\x1b]0;{title}\x07"。
    # 【参数】title (str)：新的窗口标题。
    # 【返回】Control 实例。
    # 【注意】1) 这是 OSC（Operating System Command）而不是 CSI 序列，并且用 BEL(0x07) 作为
    #      结束符而不是标准的 ST；2) 若 title 自身含有 \x07，序列会被提前截断，
    #      甚至把后续文本当作终端指令执行——标题应来自可信来源或先清理；
    #   3) tests/test_control.py 里 Control.title("hello") 期望 "\x1b]0;hello\x07"。
    # 【协作】Console.set_window_title() 的底层实现。
    @classmethod
    def title(cls, title: str) -> "Control":
        """Set the terminal window title

        Args:
            title (str): The new terminal window title
        """
        return cls((ControlType.SET_WINDOW_TITLE, title))

    # 【功能】返回可直接写入终端的 ANSI 字符串（即内部 Segment 的 text）。
    # 【返回】str：拼接好的转义序列，可能为空串（当没有任何控制码时）。
    # 【注意】它包含不可见控制字符，不是给人阅读的文本；调试时建议看 segment.control 字段，
    #   那里保留了语义化的控制码。
    def __str__(self) -> str:
        return self.segment.text

    # 【功能】实现 Rich 的渲染协议，使 Control 能直接交给 Console 渲染。
    # 【算法】仅当 segment.text 非空时 yield 内部 Segment；空控制码（如 Control.move(0, 0)）
    #   不产出任何内容，因此不会在输出流里留下空段。
    # 【参数】console / options：渲染协议要求的上下文（本方法不使用它们）。
    # 【返回】RenderResult：零个或一个 Segment 的可迭代流。
    # 【注意】segment.style 恒为 None，控制码不参与着色；Console 会识别 segment.control
    #   并做特殊处理（例如导出纯文本时按 control 字段过滤掉这些段）。
    def __rich_console__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> "RenderResult":
        if self.segment.text:
            yield self.segment


# ==== 【文本级工具函数：剥离与转义控制字符】 ====
# 【功能】删除文本中的控制字符（BEL/BS/VT/FF/CR 五种）。
# 【算法】str.translate 单趟扫描：映射表中值为 None 的码位被直接丢弃。
# 【参数】text (str)：可能含控制码的字符串；_translate_table：内部参数，默认使用模块级常量
#   映射表，把它暴露出来的唯一目的是复用同一实现做别的映射（属于内部细节）。
# 【返回】str：删除控制字符后的新字符串（原字符串不可变，不会被就地修改）。
# 【注意】它**不会**去掉 ANSI 转义序列：'\x1b[31m' 中的 ESC(0x1B) 不在表里，
#   所以 strip_control_codes("a\x1b[31mb\x1b[0mc") 会原样返回整串；要处理终端颜色序列
#   请走 rich/ansi.py 的 AnsiDecoder。
# 【协作】text.py 在构造、更新与追加文本时调用它（156/413/982/1046 行），
#   防止用户数据里的 \r 把光标拉回行首而破坏版面；
#   tests/test_control.py 与 tests/test_text.py 都对其行为做了断言。
def strip_control_codes(
    text: str, _translate_table: Dict[int, None] = _CONTROL_STRIP_TRANSLATE
) -> str:
    """Remove control codes from text.

    Args:
        text (str): A string possibly contain control codes.

    Returns:
        str: String with control codes removed.
    """
    return text.translate(_translate_table)


# 【功能】把文本中的控制字符替换成它的“可见转义写法”（\r 变成反斜杠+r，\a 变成反斜杠+a 等）。
# 【算法】同样是 str.translate，只是映射表的值是替换字符串而不是 None；
#   一张字符 → 两三个字符，因此结果长度可能变长。
# 【参数】text (str)：可能含控制码的字符串；_translate_table：内部参数，默认 CONTROL_ESCAPE。
# 【返回】str：转义后的新字符串。
# 【注意】只处理那 5 个 C0 字符，不是通用的 repr()/unicode_escape：制表符、换行符、其它
#   不可打印字符都保持原样。
# 【协作】_inspect.py（237 行）用它渲染 docstring，避免文档里的 \r、\a 影响版面；
#   tests/test_control.py 断言 escape_control_codes("foo\rbar") == "foo\\rbar"。
def escape_control_codes(
    text: str,
    _translate_table: Dict[int, str] = CONTROL_ESCAPE,
) -> str:
    """Replace control codes with their "escaped" equivalent in the given text.
    (e.g. "\b" becomes "\\b")

    Args:
        text (str): A string possibly containing control codes.

    Returns:
        str: String with control codes replaced with their escaped version.
    """
    return text.translate(_translate_table)


# ==== 【作为脚本运行时的自测入口】 ====
# 【要点】`python -m rich.control` 会执行这里：先打印提示，再循环设置窗口标题（带 🚀 与递增的点），
#   用于手工观察 OSC 标题序列的效果（需要真实终端，且终端要允许设置标题）。
# 【注意】rich.console 的导入放在这个块内部：若放到模块顶层，就会让 control 与 console
#   互相导入（循环导入），因此这里必须保持“仅在作为脚本运行时才导入”。
if __name__ == "__main__":  # pragma: no cover
    from rich.console import Console

    # 【要点】这里显式新建 Console 而不是用全局单例，使脚本行为独立、可预测。
    console = Console()
    console.print("Look at the title of your terminal window ^")
    # console.print(Control((ControlType.SET_WINDOW_TITLE, "Hello, world!")))
    # 【注意】0.5 秒 × 10 次纯粹为了让人眼看清标题变化；sleep 会阻塞终端，别在生产代码里模仿。
    for i in range(10):
        console.set_window_title("🚀 Loading" + "." * i)
        time.sleep(0.5)
