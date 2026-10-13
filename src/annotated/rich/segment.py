from enum import IntEnum
from functools import lru_cache
from itertools import filterfalse
from operator import attrgetter
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

from .cells import (
    _is_single_cell_widths,
    cached_cell_len,
    cell_len,
    get_character_cell_size,
    set_cell_size,
)
from .repr import Result, rich_repr
from .style import Style

# ---------------------------------------------------------------------------
# 【模块定位】本模块是 Rich 渲染流水线的「最后一公里」：所有 Renderable（Text、Table、
#   Panel、Syntax……）最终都被渲染成一系列 Segment，再由 Console 把它们翻译成带 ANSI
#   转义序列的字符串写到终端。Segment = 一段文本 + 该文本的 Style + 可选的终端控制码
#   （光标移动、清行、窗口标题等），因此它是「内容」「外观」「终端指令」三者的最小载体。
#   本模块只做纯几何/文本处理：切分、按单元格宽度裁剪、补白、对齐、样式重写、去色、
#   去链接、合并；它不碰 IO，也不决定「渲染什么」。
# 【关键数据结构】
#   · ControlType(IntEnum)：16 种非打印控制语义；ControlCode 是控制码元组类型。
#   · Segment(NamedTuple)：字段 text / style / control，派生属性 cell_length。
#   · Segments、SegmentLines：把 Segment 序列（或行的列表）重新包装成 Renderable。
#   最重要的派生量是「单元格宽度」（cell length）：终端按格排版，一个汉字占 2 格、
#   一个 ASCII 字符占 1 格、组合附加符号占 0 格，它与 Python 的 len() 并不相等。
# 【设计要点】
#   · 用 NamedTuple 而不是普通类：构造开销小、支持元组解包、天然不可变，并且可哈希，
#     因此能直接作为 functools.lru_cache 的缓存键（见 Segment._split_cells）。
#   · 宽度计算一律委托给 cells 模块，绝不使用 len()。这是整个 Rich 布局算法的度量基准，
#     也是「中日韩文本对齐正确」的根本原因。
#   · 大量使用惰性生成器：split_lines、simplify、divide、apply_style 返回的都是生成器，
#     以便在一次终端输出中流式处理成千上万个 Segment，避免构造中间列表。
#   · 控制码 Segment 的 cell_length 恒为 0，并被各种裁剪/合并算法特殊对待：不参与宽度
#     统计、不参与样式合并、可以出现在任意位置。
#   · 热路径上有两道缓存：Segment._split_cells 的类级 lru_cache(16K)，以及
#     cells.cached_cell_len 的 lru_cache；“先估算再逐格修正”的算法也出自性能考虑。
# 【阅读建议】
#   1) 先读 Segment 的字段、cell_length、is_control，建立「宽度」与「控制码」两个概念；
#   2) 再读 _split_cells + split_cells：宽字符切分是全模块最难的一段；
#   3) 然后按 split_lines → split_and_crop_lines → adjust_line_length → set_shape →
#      align_top/middle/bottom 的顺序读布局算法；
#   4) 最后读 simplify / strip_links / strip_styles / remove_color / divide 这些流式变换；
#   5) Segments 与 SegmentLines 是适配器，可以最后看。
#   依赖：cells（宽度计算）、style（样式）、repr（__rich_repr__ 协议）；
#   被依赖：console、text、table、syntax、live_render、screen、tree、rule 等几乎所有
#   渲染相关模块——改这里几乎一定会影响整条渲染链路。
#   【约束】本文件与 cells.py 共享「单元格宽度」的定义；若 cells 的宽度语义变化，
#   本模块所有裁剪/对齐算法都必须同步复核。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
if TYPE_CHECKING:
    from .console import Console, ConsoleOptions, RenderResult


# ==== 【控制码类型定义】 ====
# ---------------------------------------------------------------------------
# 【职责】枚举出 Rich 需要下发给终端的「非打印控制语义」。这些语义与具体终端无关，
#   translation 发生在两处：control.py 生成 ANSI 序列，_windows_renderer.py 调用
#   Win32 控制台 API；本模块只负责给它们编号并放进 Segment.control。
# 【关键属性】每个成员的整数值就是它在元组控制码中的「类型码」，也是 Segment 中
#   区分不同控制指令的唯一标识。
# 【不变量】
#   1. 数值 1~16 连续递增，不得重复；
#   2. 具体数值必须与 control.py 的 @control 处理器表以及
#      _windows_renderer.py 的 if/elif 分支保持一致；
#   3. 新增控制类型要同时更新上述两个文件的映射，否则该控制码会被静默忽略。
# 【协作】control.py（ANSI 编码）、_windows_renderer.py（Windows 控制台 API）、
#   live_render.py（滚动刷新时按 control 字段定位光标）。
# ---------------------------------------------------------------------------
class ControlType(IntEnum):
    """Non-printable control codes which typically translate to ANSI codes."""

    # 【要点】每个成员有两层含义：（1）语义层——告诉渲染后端「要做什么」；
    #   （2）数据层——元组控制码的第一个元素就是这里的整数值，供消费方做 if/elif 分派。
    #   因此本文件只声明语义，具体字节序列一律交给 control.py / _windows_renderer.py。
    BELL = 1
    CARRIAGE_RETURN = 2
    HOME = 3
    # 【历史】CLEAR、ENABLE_ALT_SCREEN、DISABLE_ALT_SCREEN 这三个成员在当前 ANSI 与
    #   Windows 两条渲染路径上都还没有实现（属于上游遗留的占位定义），阅读时不必费力
    #   寻找它们的处理代码：Segment 允许携带它们，但现有后端会静默忽略。
    CLEAR = 4
    # 【易错】SHOW_CURSOR / HIDE_CURSOR 是一对布尔语义（由 Control.show_cursor 生成）；
    #   CLEAR 与 ERASE_IN_LINE 用途不同——前者清整屏，后者按模式清当前行。
    SHOW_CURSOR = 5
    HIDE_CURSOR = 6
    ENABLE_ALT_SCREEN = 7
    DISABLE_ALT_SCREEN = 8
    CURSOR_UP = 9
    CURSOR_DOWN = 10
    CURSOR_FORWARD = 11
    CURSOR_BACKWARD = 12
    CURSOR_MOVE_TO_COLUMN = 13
    CURSOR_MOVE_TO = 14
    # 【约束】16 是当前使用的最大编号；新增控制类型必须从这里往后取号，
    #   并同步更新 control.py 的映射表与 _windows_renderer.py 的分支，
    #   否则新控制码会被后端静默忽略。
    ERASE_IN_LINE = 15
    SET_WINDOW_TITLE = 16

# 【要点】ControlType 继承 IntEnum 而非 Enum，是为了让控制码可以参与整数比较、排序，
#   保持跨进程/序列化时的稳定性，并让 `int(control_type)` 这类转换随时可用。
# 【约束】成员顺序与数值今后只允许在文件末尾追加（取下一个未使用编号），
#   中间插入或改号会让已序列化的控制码失效。

# 【为什么】用 Union 而不是单一元组类型：不同控制码携带的载荷个数不同：
#   无载荷（BELL / CLEAR / 光标的上下左右移动）、一个参数（ERASE_IN_LINE 的模式 0/1/2、
#   CURSOR_MOVE_TO_COLUMN 的列号、SET_WINDOW_TITLE 的标题字符串）、两个参数
#   （CURSOR_MOVE_TO 的 x 与 y）。只有 CURSOR_MOVE_TO 会用到三元组那一种。
# 【易错】因为 ControlCode 是联合类型，消费方（如 _windows_renderer.py）必须用
#   cast + 解包断言来收窄类型，Python 运行时不做任何校验。
ControlCode = Union[
    Tuple[ControlType],
    Tuple[ControlType, Union[int, str]],
    Tuple[ControlType, int, int],
]


# ==== 【核心数据结构 Segment】 ====
# ---------------------------------------------------------------------------
# 【职责】Renderable → 终端文本之间的中间表示，同时也是整个渲染管线里最小的
#   「可排版单元」。本类既承载数据，也内置了一整套基于单元格宽度的静态算法库，
#   因此 segment.py 同时是「数据结构定义」与「文本排版工具集」。
# 【关键属性】
#   text    —— 显示文本，可能包含 \n（此时它跨越多行，需先切分再布局）；
#   style   —— 外观，None 表示「不设置，继承上层样式」而非「无样式」；
#   control —— 控制码序列，非 None 时本段不产生任何可见字符；
#   cell_length（派生）—— text 的单元格宽度之和，宽字符算 2，不看 control。
# 【不变量】
#   1. 一个 Segment 内的 text 与 style 严格一一对应，绝不把两种样式混在一段里；
#   2. control 非 None ⇒ cell_length == 0，且渲染时输出的是控制序列而不是 text；
#   3. 任何切分/裁剪操作都应保持「各段文本拼接 == 原文本」且「总格数不变」；
#   4. Segment 不可变（NamedTuple），所有变换返回新的 Segment 实例；
#   5. Segments/SegmentLines 之类的适配器必须用 list() 物化生成器后才能重放。
# 【协作】由 console/text/table/syntax/tree 等 Renderable 产出；由
#   Console._render_buffer 调用 Style.render 翻译成 ANSI 字符串；
#   在 legacy Windows 下由 _windows_renderer 走另一条 Win32 分支。
# ---------------------------------------------------------------------------
@rich_repr()
class Segment(NamedTuple):
    """A piece of text with associated style. Segments are produced by the Console render process and
    are ultimately converted in to strings to be written to the terminal.

    Args:
        text (str): A piece of text.
        style (:class:`~rich.style.Style`, optional): An optional style to apply to the text.
        control (Tuple[ControlCode], optional): Optional sequence of control codes.

    Attributes:
        cell_length (int): The cell length of this Segment.
    """

    text: str
    style: Optional[Style] = None
    control: Optional[Sequence[ControlCode]] = None

    # ==== 【尺寸、真值与调试表示】 ====
    # 【功能】返回该 Segment 在终端里占用的单元格数（而非字符数）。
    # 【算法】控制码 Segment 直接返回 0；否则调用 cells 模块的 cell_len 按 Unicode
    #   宽度表累加每个字符的格数。
    # 【返回】int，非负；空文本返回 0。
    # 【注意】1) 含 \n 的文本也会被统计（\n 的宽度是 0），所以布局代码必须先按行切分
    #   再统计宽度；2) 本方法是 adjust_line_length / get_line_length / get_shape 等
    #   所有布局算法的度量单位，语义变化会牵动整条链路。
    # 【优化】cell_len 内部对短字符串走 lru_cache（cached_cell_len），因此重复测量同一
    #   段文本的代价很低。
    @property
    def cell_length(self) -> int:
        """The number of terminal cells required to display self.text.

        Returns:
            int: A number of cells.
        """
        text, _style, control = self
        return 0 if control else cell_len(text)

    # 【功能】Rich 的调试表示协议（rich_repr）：决定 repr(segment) 打印成什么样子。
    # 【算法】先产出 text；无控制码时只在 style 非空时产出 style，有控制码时 style 与
    #   control 都产出。
    # 【为什么】调试输出保持精简：绝大多数 Segment 的 style 是 None，省略它可以显著
    #   提高日志与报错信息的可读性。装饰器 @rich_repr() 会把本方法的产出格式化成
    #   Segment('text', Style(...)) 这样的构造式。
    def __rich_repr__(self) -> Result:
        yield self.text
        if self.control is None:
            if self.style is not None:
                yield self.style
        else:
            yield self.style
            yield self.control

    # 【功能】真值判断，等价于「这段文本是否非空」。
    # 【注意】与 is_control 无关：一个只带控制码、text 为 "" 的 Segment 是「假」的，
    #   因此 `if segment:` 之类的写法会把控制段当成空内容跳过——需要区分二者时必须
    #   显式使用 is_control。
    def __bool__(self) -> bool:
        """Check if the segment contains text."""
        return bool(self.text)

    # 【功能】判断该 Segment 是否承载终端控制码。
    # 【返回】bool：等价于 control is not None。
    # 【注意】只看 control 字段是否为 None，不看内容；因此空元组 () 也算控制段。
    # 【协作】filter_control、simplify、split_lines 等都以本属性（或等价的字段判空）
    #   作为分支依据。
    @property
    def is_control(self) -> bool:
        """Check if the segment contains control codes."""
        return self.control is not None

    # ==== 【单元格切分（含双宽字符处理）】 ====
    # ---------------------------------------------------------------------------
    # 这一组是本文件的核心算法，回答同一个问题：
    # 「当裁剪点落在文本中间时，怎样切才能既不丢字符、又不破坏显示宽度？」
    # 双倍宽字符（汉字、全角标点、多数 emoji）使这个问题变得不平凡：切在它的「中间」在
    # 终端上无法表示，因此必须退化为两格空格。
    # ---------------------------------------------------------------------------
    # 【功能】把 Segment 按「单元格位置」切成前后两段：cut 之前归第一段、之后归第二段。
    # 【算法】text 的码点个数与格数并不相等，所以先用比例 cut / cell_length 估一个初始
    #   下标 pos，再逐格试探修正：算出 text[:pos] 的格数 cell_pos，然后比较 cell_pos 与 cut：
    #     · cell_pos == cut                      → 正好切在字符边界上，直接切；
    #     · out_by == -1 且 text[pos] 是双宽字符 → 切点落在该字符内部，用它两侧各一个空格；
    #     · out_by == +1 且 text[pos-1] 是双宽字符 → 同理，反向处理；
    #     · cell_pos < cut  → 下标后移一格，继续试探；
    #     · 否则            → 下标前移一格，继续试探。
    # 【参数】segment：待切分的 Segment（本方法之所以能带 lru_cache，正是因为 NamedTuple
    #   可哈希）；cut：切割点的单元格偏移，0 表示切在开头。
    # 【返回】Tuple[Segment, Segment]；两段的文本拼接等于原文本（宽字符被替换成空格的情形
    #   除外，那时总宽度仍然守恒）。
    # 【注意】1) 调用方需保证 0 <= cut < cell_length，本方法不校验越界；
    #   2) 控制码字段被原样复制到两段上，因此控制段也可能出现在两半里；
    #   3) 循环没有显式上界，靠 cell_len / get_character_cell_size 结果一致来收敛。
    # 【优化】lru_cache(1024 * 16)：列布局会把同一个长 Segment 反复按递增的 cut 切分，
    #   缓存命中率很高；代价是缓存键持有 Segment 引用，会延长文本对象的生命周期。
    @classmethod
    @lru_cache(1024 * 16)
    def _split_cells(cls, segment: "Segment", cut: int) -> Tuple["Segment", "Segment"]:
        """Split a segment in to two at a given cell position.

        Note that splitting a double-width character, may result in that character turning
        into two spaces.

        Args:
            segment (Segment): A segment to split.
            cut (int): A cell position to cut on.

        Returns:
            A tuple of two segments.
        """
        # 【要点】把元组一次性拆成三个局部变量，是为了让下面的 while 循环体只在局部命名空间
        #   里读写，避免每次迭代都做属性查找；_Segment 也做了同样的处理。这段代码在渲染热路
        #   径上被调用极频繁，微优化是刻意的。
        text, style, control = segment
        _Segment = Segment
        cell_length = segment.cell_length
        # 【要点】cut 落在末尾或越界时直接返回「原段 + 空段」，保证调用方总能拿到两段；
        #   空段沿用同样的 style / control，避免样式在切分处丢失。
        if cut >= cell_length:
            return segment, _Segment("", style, control)

        cell_size = get_character_cell_size

        # 【要点】先按「字符数 ≈ 格数」的比例估一个初始下标。对于等宽（ASCII/拉丁）文本一步
        #   命中；对中日韩全角文本误差可达 2 倍，但随后的 while 循环会逐格修正，
        #   所以估算质量只影响迭代次数，不影响正确性。
        # 【为什么】不直接用二分查找：宽字符的宽度分布不均匀，比例估算在真实文本上迭代次数
        #   更少，且实现简单。
        pos = int((cut / cell_length) * len(text))

        # 【易错】此循环没有迭代次数上界，理论上依赖 cell_len 与 get_character_cell_size 的
        #   结果一致来保证收敛。只要 pos 每次至少移动 1，就一定会扫到 out_by == 0 或 ±1 的
        #   分支；如果 Unicode 宽度表被替换成自相矛盾的数据，这里可能死循环。
        while True:
            before = text[:pos]
            cell_pos = cell_len(before)
            out_by = cell_pos - cut
            if not out_by:
                return (
                    _Segment(before, style, control),
                    _Segment(text[pos:], style, control),
                )
            # 【易错】out_by == -1 表示切片少算了半格，即 text[pos] 这个双宽字符被切在了中间。
            #   终端无法显示「半个汉字」，所以把它替换成一个空格，并在第二段的开头再补一个空格，
            #   这样两段的宽度之和不变，父容器的对齐不会被破坏。
            # 【约束】「切分不改变总宽度」是布局算法（table/panel 的边框对齐）正确性的前提。
            if out_by == -1 and cell_size(text[pos]) == 2:
                return (
                    _Segment(text[:pos] + " ", style, control),
                    _Segment(" " + text[pos + 1 :], style, control),
                )
            # 【易错】out_by == +1 是对称的情形：多算了一格，说明 text[pos-1] 这个双宽字符横跨
            #   了切点，同样用空格替换，第二段从 pos 开始（该字符的右半格已由空格顶替）。
            if out_by == +1 and cell_size(text[pos - 1]) == 2:
                return (
                    _Segment(text[: pos - 1] + " ", style, control),
                    _Segment(" " + text[pos:], style, control),
                )
            # 【要点】cell_pos < cut 说明当前下标过于保守（切得早了），向后扩大一格；
            #   否则说明切得晚了，向前收缩一格。两个分支必定推动 pos，故循环必有进展。
            if cell_pos < cut:
                pos += 1
            else:
                pos -= 1

    # 【功能】对外暴露的切分入口：在指定的「列」（单元格）位置把本 Segment 一分为二。
    # 【算法】先走单倍宽快路径（只用 len() 与切片，完全绕过 Unicode 宽度表）；
    #   只有遇到宽字符/组合字符时才回退到带缓存的 _split_cells 做精确切分。
    # 【参数】cut：切割处的单元格偏移，必须 >= 0（assert 会在 python -O 下被剥离，
    #   调用方仍需自行保证非负）。
    # 【返回】Tuple[Segment, Segment]；cut 超过文本长度时返回 (原段, 空段)。
    # 【注意】当切点落在双宽字符中间时，该字符会被替换成两个空格，以保证显示宽度不失真；
    #   这是「按格裁剪」而非「按字符截断」，是 Rich 能在中文环境下保持表格对齐的关键。
    # 【协作】Segment.divide 是它最重要的调用者（Text.divide → 按列切片富文本）。
    def split_cells(self, cut: int) -> Tuple["Segment", "Segment"]:
        """Split segment in to two segments at the specified column.

        If the cut point falls in the middle of a 2-cell wide character then it is replaced
        by two spaces, to preserve the display width of the parent segment.

        Args:
            cut (int): Offset within the segment to cut.

        Returns:
            Tuple[Segment, Segment]: Two segments.
        """
        text, style, control = self
        assert cut >= 0

        # 【优化】_is_single_cell_widths 是 cells 模块预先构造的 frozenset.issuperset，
        #   对纯 ASCII / 拉丁文本能在接近常数时间内判定，从而跳过整个宽度表查询。
        #   这是本方法在英文场景下几乎没有额外开销的原因。
        if _is_single_cell_widths(text):
            # Fast path with all 1 cell characters
            if cut >= len(text):
                return self, Segment("", style, control)
            return (
                # 【易错】这里传入的是自我转发的 _split_cells(self, cut)：_split_cells 是带 lru_cache
                #   的 classmethod，它的第一个业务参数必须是 Segment 实例本身，所以显式把 self 传进去。
                # 【要点】把带缓存的方法写成 classmethod 而不是实例方法，是为了让缓存挂在类上（与实例
                #   生命周期解耦）；又因为缓存键里包含 Segment 内容，缓存实际是「按内容」而非「按对象」的。
                Segment(text[:cut], style, control),
                Segment(text[cut:], style, control),
            )

        return self._split_cells(self, cut)

    # ==== 【构造与样式应用】 ====
    # 【功能】创建一个只含换行符的 Segment，作为「行终止符」的标准写法。
    # 【返回】Segment(text="\n")。
    # 【注意】这里创建的是普通文本段而不是控制码段，因此：
    #   1) 它的 cell_length 为 0（\n 的宽度为 0），不会影响行宽统计；
    #   2) 它的 __bool__ 为 True，依赖真值判断的代码要留意这一区别；
    #   3) 它会被 split_lines 之类的按行切分算法消费掉。
    # 【协作】Segments / SegmentLines 用它按行分隔输出。
    @classmethod
    # 【协作】Segments / SegmentLines 用它按行分隔输出；
    #   Console.render_lines(include_new_lines=True) 产出的行尾也带有它。
    def line(cls) -> "Segment":
        """Make a new line segment."""
        return cls("\n")

    # 【功能】给整条 Segment 流套上样式：结果样式为 style + 原样式 + post_style。
    # 【算法】分两趟惰性流水线：先按需叠加基础样式，再按需叠加后置样式；每趟都用生成器
    #   表达式重写 Segment。控制码段一律保持 style=None 原样透传。
    # 【参数】segments：待处理的 Segment 可迭代对象；
    #   style：底层样式（先应用，优先级低，通常来自组件自身的 style）；
    #   post_style：顶层样式（后应用，优先级高，用于标题/强调一类覆盖）。
    # 【返回】Iterable[Segment]，惰性生成器；若两个样式都为假值则原样返回入参对象本身
    #   （此时调用方拿到的是同一个可迭代对象，没有包装开销）。
    # 【注意】1) 返回生成器，一旦被消费就不能重放；若实参本身也是生成器，重复迭代会得到
    #   空结果；2) 控制码段不施加样式——样式与控制码在同一个 Segment 上互斥，
    #   这是 Segment 的不变量之一。
    # 【要点】apply = style.__add__ 把绑定方法提到循环外，避免每次迭代做属性查找；
    #   Style.__add__ 本身带 lru_cache，所以同一组样式叠加只会真正计算一次。
    # 【约束】样式叠加顺序必须与 markup / Text 中「外层先、内层后」的语义一致，
    #   否则 [bold] 套 [red] 的结果会与用户预期相反。
    @classmethod
    # ==== 【批量样式应用 apply_style】 ====
    # 【功能】给整条 Segment 流套上样式：结果样式为 style + 原样式 + post_style。
    # 【算法】分两趟惰性流水线：先按需叠加基础样式，再按需叠加后置样式；每趟都用生成器
    #   表达式重写 Segment。控制码段一律保持 style=None 原样透传。
    # 【参数】segments：待处理的 Segment 可迭代对象；
    #   style：底层样式（先应用、优先级低，通常来自组件自身的 style）；
    #   post_style：顶层样式（后应用、优先级高，用于标题/强调一类覆盖）。
    # 【返回】Iterable[Segment]，惰性生成器；两个样式都为假值时原样返回入参对象本身，
    #   此时调用方拿到的是同一个可迭代对象，没有包装开销。
    # 【注意】返回生成器，一旦被消费就不能重放；若实参本身也是生成器，重复迭代会得到空结果。
    # 【优化】apply = style.__add__ 把绑定方法提到循环外，避免每次迭代做属性查找；
    #   Style.__add__ 本身带 lru_cache，同一组样式叠加只会真正计算一次。
    def apply_style(
        cls,
        segments: Iterable["Segment"],
        style: Optional[Style] = None,
        post_style: Optional[Style] = None,
    ) -> Iterable["Segment"]:
        """Apply style(s) to an iterable of segments.

        Returns an iterable of segments where the style is replaced by ``style + segment.style + post_style``.

        Args:
            segments (Iterable[Segment]): Segments to process.
            style (Style, optional): Base style. Defaults to None.
            post_style (Style, optional): Style to apply on top of segment style. Defaults to None.

        Returns:
            Iterable[Segments]: A new iterable of segments (possibly the same iterable).
        """
        result_segments = segments
        if style:
            apply = style.__add__
            result_segments = (
                cls(text, None if control else apply(_style), control)
                for text, _style, control in result_segments
            )
        # 【要点】被应用样式的段若不携带控制码，则新样式为 style + 原样式（原样式为 None 时
        #   等价于直接取 style）；携带控制码的段必须保留 style=None，否则会破坏
        #   「control 与 style 互斥」的不变量。
        if post_style:
            # 【易错】post_style 分支里写作 `_style + post_style if _style else post_style`：
            #   原样式可能为 None，不能直接与 Style 相加（None + Style 会抛 TypeError），
            #   所以必须先判空。
            result_segments = (
                cls(
                    text,
                    (
                        None
                        if control
                        else (_style + post_style if _style else post_style)
                    ),
                    control,
                )
                for text, _style, control in result_segments
            )
        return result_segments

    # ==== 【过滤：按控制码/可见文本拆分】 ====
    # 【功能】按「是否携带控制码」过滤 Segment 流。
    # 【算法】用 operator.attrgetter("control") 作为 key，正向过滤用内置 filter，
    #   取反用 itertools.filterfalse；两者都是 C 层实现，比手写生成器快得多。
    # 【参数】is_control：True 只保留控制段，False 只保留可见文本段。
    # 【返回】Iterable[Segment]（惰性迭代器），不是列表。
    # 【协作】Console 在录制输出（export_text / export_html / export_svg）时用它剔除
    #   控制码，因为导出的文本不应包含光标移动等终端指令。
    @classmethod
            # 【易错】filterfalse 返回的是「不含控制码」的段，与 is_control=True 的分支恰好互补；
            #   两个分支都返回迭代器而不是列表，调用方需要注意一次性消费。
    # ==== 【按控制码过滤 filter_control】 ====
    # 【功能】按「是否携带控制码」过滤 Segment 流。
    # 【算法】用 operator.attrgetter("control") 作为 key：正向用内置 filter，
    #   反向用 itertools.filterfalse；两者都是 C 层实现，比手写生成器快得多。
    # 【参数】is_control：True 只保留控制段，False 只保留可见文本段。
    # 【返回】Iterable[Segment]（惰性迭代器），不是列表。
    # 【易错】filterfalse 与本分支恰好互补，但两者都是迭代器，只能消费一次。
    # 【协作】Console 在导出（export_text / export_html / export_svg）时用它剔除控制码，
    #   因为导出的文本不应包含光标移动等终端指令。
    def filter_control(
        cls, segments: Iterable["Segment"], is_control: bool = False
    ) -> Iterable["Segment"]:
        """Filter segments by ``is_control`` attribute.

        Args:
            segments (Iterable[Segment]): An iterable of Segment instances.
            is_control (bool, optional): is_control flag to match in search.

        Returns:
            Iterable[Segment]: And iterable of Segment instances.

        """
        if is_control:
            return filter(attrgetter("control"), segments)
        else:
            return filterfalse(attrgetter("control"), segments)

    # ==== 【按行切分】 ====
    # 【功能】把带有 \n 的 Segment 流切成「一行一个 Segment 列表」。
    # 【算法】逐段扫描；不含 \n 的段直接追加到当前行；含 \n 的段用 str.partition 逐段切开，
    #   每遇到一个换行就 yield 当前累积的行并开一个新列表（append 也一并重绑定）。
    # 【参数】segments：可能含换行的 Segment 可迭代对象。
    # 【返回】Iterable[List[Segment]]，每行一个列表；最后一行只有在非空时才产出，
    #   因此不会凭空多出一个尾随空行。
    # 【注意】1) 换行段被拆开后只继承 text 与 style，control 字段被丢弃；
    #   2) yield 出去的行列表是同一个对象被反复复用/重建的，消费方若需保留必须自己复制；
    #   3) 这是「不裁剪」的版本，需要固定宽度请用 split_and_crop_lines。
    # 【协作】tree.py / table.py 之类需要按行重排内容的组件会用到它。
    @classmethod
    # 【功能】把带有 \n 的 Segment 流切成「一行一个 Segment 列表」。
    # 【算法】逐段扫描；不含 \n 的段直接追加到当前行；含 \n 的段用 str.partition 逐段切开，
    #   每遇到一个换行就 yield 当前累积的行并重建一个新列表（append 也一并重绑定）。
    # 【参数】segments：可能含换行的 Segment 可迭代对象。
    # 【返回】Iterable[List[Segment]]，每行一个列表；最后一行非空时才产出，因此不会凭空
    #   多出一个尾随空行。
    # 【注意】1) 换行段被拆开后只继承 text 与 style，control 字段被丢弃；
    #   2) yield 出去的行列表会被后续操作复用，消费方若需保留必须自己复制；
    #   3) 这是「不裁剪」的版本，需要固定宽度请用 split_and_crop_lines。
    def split_lines(cls, segments: Iterable["Segment"]) -> Iterable[List["Segment"]]:
        """Split a sequence of segments in to a list of lines.

        Args:
            segments (Iterable[Segment]): Segments potentially containing line feeds.

        Yields:
            Iterable[List[Segment]]: Iterable of segment lists, one per line.
        """
        # 【优化】把 list.append 绑定成局部变量 append，并在每次重建行时重新绑定，避免在
        #   循环里反复做 `line.append` 的属性查找——这段代码每行都会执行，属于热路径。
        #   这是 Rich 代码库中反复出现的惯用手法（divide / adjust_line_length 等亦然）。
        line: List[Segment] = []
        append = line.append

        for segment in segments:
            if "\n" in segment.text and not segment.control:
                text, style, _ = segment
                while text:
                    _text, new_line, text = text.partition("\n")
                    if _text:
                        append(cls(_text, style))
                    if new_line:
                        yield line
                        line = []
                        append = line.append
            else:
                append(segment)
        if line:
            yield line

    # 【功能】与 split_lines 相同，但每行额外返回一个布尔量，表示这行是否由 \n 终止。
    # 【算法】与 split_lines 完全一致，只是 yield 的元素从「行列表」变为「(行列表, True)」，
    #   循环结束后的尾巴以 False 标记。
    # 【参数】segments：可能含换行的 Segment 可迭代对象。
    # 【返回】Iterable[Tuple[List[Segment], bool]]：(行, 是否有行终止符)。
    # 【注意】最后一行若没有以 \n 结尾，则以 False 产出；因此「文件末尾有无换行」在本方法
    #   中是可区分的，而 split_lines 无法区分。需要保留「行终止符」信息的渲染场景
    #   （如把终端内容导出为文本时保持原样）应使用本方法。
    @classmethod
    # 【为什么需要它】把终端内容导出为文本时，「末尾是否有换行」是必须保留的信息，
    #   split_lines 会丢掉这一区别，因此另立一个只多返回一个布尔量的方法。
    def split_lines_terminator(
        cls, segments: Iterable["Segment"]
    ) -> Iterable[Tuple[List["Segment"], bool]]:
        """Split a sequence of segments in to a list of lines and a boolean to indicate if there was a new line.

        Args:
            segments (Iterable[Segment]): Segments potentially containing line feeds.

        Yields:
            Iterable[List[Segment]]: Iterable of segment lists, one per line.
        """
        line: List[Segment] = []
        append = line.append

        for segment in segments:
            if "\n" in segment.text and not segment.control:
                text, style, _ = segment
                while text:
                    _text, new_line, text = text.partition("\n")
                    if _text:
                        append(cls(_text, style))
                    if new_line:
                        # 【易错】两个产出点必须同时维护：换行处是 (line, True)，循环结束后是 (line, False)；
                        #   漏改任意一处都会让「行终止符」信息与实际内容不一致。
                        yield (line, True)
                        line = []
                        append = line.append
            else:
                append(segment)
        if line:
            yield (line, False)

    # 【功能】先按行切分，再把每行裁剪（或补齐）到指定的单元格宽度。
    # 【算法】流式扫描：遇到 \n 就把当前累积行交给 adjust_line_length 处理，可选地追加
    #   一个换行 Segment，然后原地清空缓冲继续；循环结束后再处理最后一行（可能没有 \n）。
    # 【参数】length：目标行宽（单元格，不是字符数）；style：补白空格所用的样式；
    #   pad：行宽不足 length 时是否补空格；include_new_lines：产出的行尾部是否附一个
    #   "\n" Segment（用于后续按行拼接输出）。
    # 【返回】Iterable[List[Segment]]，每行一个列表。
    # 【注意】（1）控制码段被原样保留且不参与宽度统计，因此不适合需要纯粹可见文本的场景；
    #   （2）line.clear() 是原地复用同一个列表对象，每次 yield 出去的引用在下一轮会被改写，
    #   消费方必须立即消费或复制——Console 的渲染与导出流程正是依赖这一点来避免拷贝。
    # 【为什么】把补白/裁剪逻辑集中在 adjust_line_length 中，使「宽度调整」只有一处实现，
    #   本方法只负责「按行推进」这一件事。
    # 【协作】Console.render_lines 与 export_html 的主力工具；table/screen 均间接依赖它。
    @classmethod
    # ==== 【核心入口：按行切分并裁剪 split_and_crop_lines】 ====
    # 【功能】先按行切分，再把每行裁剪（或补齐）到指定的单元格宽度。
    # 【算法】流式扫描：遇到 \n 就把当前累积行交给 adjust_line_length 处理，可选地追加
    #   一个换行 Segment，然后原地清空缓冲继续；循环结束后再处理最后一行（可能没有 \n）。
    # 【参数】length：目标行宽（单元格，不是字符数）；style：补白空格所用的样式；
    #   pad：行宽不足 length 时是否补空格；include_new_lines：产出的行尾部是否附一个
    #   "\n" Segment（用于后续按行拼接输出）。
    # 【返回】Iterable[List[Segment]]，每行一个列表。
    # 【注意】1) 控制码段被原样保留且不参与宽度统计，因此不适合需要纯粹可见文本的场景；
    #   2) line.clear() 是原地复用同一个列表对象，每次 yield 出去的引用在下一轮会被改写，
    #      消费方必须立即消费或复制——Console 的渲染与导出流程正是依赖这一点避免拷贝。
    # 【协作】Console.render_lines、export_html、export_svg 的主力工具。
    def split_and_crop_lines(
        cls,
        segments: Iterable["Segment"],
        length: int,
        style: Optional[Style] = None,
        pad: bool = True,
        include_new_lines: bool = True,
    ) -> Iterable[List["Segment"]]:
        """Split segments in to lines, and crop lines greater than a given length.

        Args:
            segments (Iterable[Segment]): An iterable of segments, probably
                generated from console.render.
            length (int): Desired line length.
            style (Style, optional): Style to use for any padding.
            pad (bool): Enable padding of lines that are less than `length`.

        Returns:
            Iterable[List[Segment]]: An iterable of lines of segments.
        """
        line: List[Segment] = []
        append = line.append

        adjust_line_length = cls.adjust_line_length
        new_line_segment = cls("\n")

        for segment in segments:
            if "\n" in segment.text and not segment.control:
                text, segment_style, _ = segment
                while text:
                    _text, new_line, text = text.partition("\n")
                    if _text:
                        append(cls(_text, segment_style))
                    if new_line:
                        cropped_line = adjust_line_length(
                            line, length, style=style, pad=pad
                        )
                        if include_new_lines:
                            cropped_line.append(new_line_segment)
                        yield cropped_line
                        line.clear()
            else:
                append(segment)
        if line:
            yield adjust_line_length(line, length, style=style, pad=pad)

    # ==== 【宽度调整与对齐】 ====
    # 【功能】把「一行 Segment」调整到恰好 length 个单元格宽：短了补空格，长了裁剪。
    # 【算法】先求整行格数；短于目标时追加一个纯空格 Segment；长于目标时逐段累加格数，
    #   在越界的那一段上用 cells.set_cell_size 就地截断，然后立即中止循环。
    # 【参数】line：同一行内的 Segment 列表；length：目标单元格宽度；
    #   style：补白所用的样式（通常与内容样式一致，以保持背景色连续）；pad：是否补白。
    # 【返回】List[Segment]；恰好等宽时返回原列表的浅拷贝，任何情况下都不修改入参列表。
    # 【注意】1) 控制码段直接 append 且不计入宽度——它不占格，但必须保留在正确的顺序位置；
    #   2) 裁剪用的是 set_cell_size，它会把被截断的宽字符补成空格，故单行总宽仍是 length；
    #   3) 返回新列表但其中的 Segment 对象与入参共享（不可变，可安全共享）；
    #   4) 起点格数在此被重置为 0 并重新累加，与 line_length 的初值无关。
    @classmethod
    # ==== 【单行宽度调整 adjust_line_length】 ====
    # 【功能】把「一行 Segment」调整到恰好 length 个单元格宽：短了补白，长了裁剪。
    # 【算法】1) 先求整行格数 line_length；
    #   2) 若 line_length < length：pad 为真时追加一个纯空格 Segment（样式取 style），
    #      否则原样浅拷贝返回；
    #   3) 若 line_length > length：新建列表并逐段累加格数，同时保留全部控制码段，
    #      在越界的那一段上调用 cells.set_cell_size 就地截断，随后 break（后面的段全部丢弃）；
    #   4) 恰好相等：返回原列表的浅拷贝。
    # 【参数】line：同一行内的 Segment 列表；length：目标单元格宽度；
    #   style：补白所用样式（需要显式传入，否则带背景色的容器会露出无色空隙）；
    #   pad：行不足 length 时是否补白，默认 True。
    # 【返回】List[Segment]；不修改入参列表，但返回的列表与入参共享 Segment 对象。
    # 【注意】1) 控制码段必须原样保留（它不占格，但承担光标/清行等副作用）；
    #   2) 裁剪用 set_cell_size 而非字符串切片，避免把双宽字符切成半个；
    #   3) 只要有一段越界就停止处理，因此被丢弃的尾部内容不会产生任何副作用
    #      （这是有意的：终端只需要固定宽度的前 length 格）。
    # 【协作】split_and_crop_lines、set_shape、syntax.py 都依赖本方法。
    def adjust_line_length(
        cls,
        line: List["Segment"],
        length: int,
        style: Optional[Style] = None,
        pad: bool = True,
    ) -> List["Segment"]:
        """Adjust a line to a given width (cropping or padding as required).

        Args:
            segments (Iterable[Segment]): A list of segments in a single line.
            length (int): The desired width of the line.
            style (Style, optional): The style of padding if used (space on the end). Defaults to None.
            pad (bool, optional): Pad lines with spaces if they are shorter than `length`. Defaults to True.

        Returns:
            List[Segment]: A line of segments with the desired length.
        """
        line_length = sum(segment.cell_length for segment in line)
        new_line: List[Segment]

        if line_length < length:
            if pad:
                new_line = line + [cls(" " * (length - line_length), style)]
            else:
                new_line = line[:]
        elif line_length > length:
            new_line = []
            append = new_line.append
            line_length = 0
            for segment in line:
                segment_length = segment.cell_length
                if line_length + segment_length < length or segment.control:
                    append(segment)
                    line_length += segment_length
                else:
                    text, segment_style, _ = segment
                    text = set_cell_size(text, length - line_length)
                    append(cls(text, segment_style))
                    break
        else:
            new_line = line[:]
        return new_line

    # 【功能】统计「一行 Segment」的总单元格宽度。
    # 【参数】line：同一行内的 Segment 列表（约定不含 \n 字符）。
    # 【返回】int，所有非控制段 text 的格数之和。
    # 【注意】这里直接调用未缓存的 cell_len，而 get_shape 会对每一行调用本方法，
    #   因此在超长行较多的场景下是本模块的主要 CPU 开销来源之一。
    # 【优化】把 cell_len 绑定为 _cell_len 再在推导式里调用，省掉一次全局名查找。
    @classmethod
    # 【功能】统计「一行 Segment」的总单元格宽度。
    # 【参数】line：同一行内的 Segment 列表（约定不含 \n）。
    # 【返回】int，所有非控制段 text 的格数之和。
    # 【注意】这里直接调用未缓存的 cell_len，而 get_shape 会对每一行调用本方法，
    #   因此在超长行较多的场景下它是本模块的主要 CPU 开销来源之一。
    # 【优化】把 cell_len 绑定为局部名 _cell_len 再在推导式里调用，省掉一次全局名查找。
    def get_line_length(cls, line: List["Segment"]) -> int:
        """Get the length of list of segments.

        Args:
            line (List[Segment]): A line encoded as a list of Segments (assumes no '\\\\n' characters),

        Returns:
            int: The length of the line.
        """
        _cell_len = cell_len
        return sum(_cell_len(text) for text, style, control in line if not control)

    # 【功能】求一组行的外接矩形尺寸（最大行宽 × 行数）。
    # 【算法】对每行调用 get_line_length 取最大值；行数直接取 len。
    # 【返回】Tuple[int, int]：(最大单元格宽度, 行数)；空输入返回 (0, 0)。
    # 【注意】宽度取的是最宽行而不是每一行的实际宽度，所以它是「形状」而非精确尺寸；
    #   lines 为空时 max() 会抛 ValueError，因此代码里必须保留那个 if lines else 0。
    # 【协作】live_render 用它在刷新前算出需要重绘的区域大小。
    @classmethod
    # 【功能】求一组行的外接矩形尺寸（最大行宽 × 行数）。
    # 【算法】对每行调用 get_line_length 取最大值；行数直接取 len。
    # 【返回】Tuple[int, int]：(最大单元格宽度, 行数)；空输入返回 (0, 0)。
    # 【易错】lines 为空时必须短路成 0，否则 max() 会在空序列上抛 ValueError。
    # 【注意】宽度取的是最宽行而不是每一行的实际宽度，所以它是「形状」而非精确尺寸。
    # 【协作】live_render 用它在刷新前算出需要重绘的区域大小。
    # 【为什么需要形状】Live 显示与 Screen 铺屏都需要知道「内容占多大区域」，
    #   才能在刷新时正确地清行与重绘；宽高越准确，闪烁与残影越少。
    def get_shape(cls, lines: List[List["Segment"]]) -> Tuple[int, int]:
        """Get the shape (enclosing rectangle) of a list of lines.

        Args:
            lines (List[List[Segment]]): A list of lines (no '\\\\n' characters).

        Returns:
            Tuple[int, int]: Width and height in characters.
        """
        get_line_length = cls.get_line_length
        max_width = max(get_line_length(line) for line in lines) if lines else 0
        return (max_width, len(lines))

    # 【功能】把一组行统一补齐（或裁剪）成给定的宽高，必要时补充空白行。
    # 【算法】以 height 或 len(lines) 为目标高度，先构造一个空白行模板；
    #   再对传入的所有行做 adjust_line_length 得到等宽行，最后在行数不足时用空白行补齐。
    # 【参数】width：目标宽度（单元格）；height：目标高度，None 表示沿用现有行数；
    #   style：空白行的样式；new_lines：空白行是否自带换行符。
    # 【返回】List[List[Segment]]，新的行列表（原列表本身不被修改）。
    # 【易错】shaped_lines = lines[:_height] 之后，紧接着用列表推导对整个 lines（而不是
    #   shaped_lines）重建内容：当 len(lines) > height 时结果会多于 height 行，调用方
    #   （如 Screen）需要自行再截断。阅读与维护时不要贸然「修正」此处语义，
    #   否则会改变现有渲染行为。
    # 【注意】空白行是同一个 [blank] 列表对象的重复引用（用乘号复制），只读使用没有风险，
    #   但不要对它做原地修改。
    # 【协作】screen.py 的 Screen.__rich_console__ 直接调用它来铺满整个屏幕区域。
    @classmethod
    # ==== 【形状调整 set_shape（用于铺满整个屏幕）】 ====
    # 【参数】lines：待固定的行列表（不会被原地修改）；width：目标宽度（单元格）；
    #   height：目标高度，None 表示沿用现有行数；style：空白行的样式；
    #   new_lines：空白行是否自带换行符。
    # 【返回】List[List[Segment]]，新的行列表。
    # 【协作】screen.py 的 Screen.__rich_console__ 直接调用它把内容铺满终端视口。
    # 【协作】Screen 用它把任意渲染结果铺满整个可视区域，
    #   空白部分用 style 指定的样式填充（通常是屏幕背景色）。
    def set_shape(
        cls,
        lines: List[List["Segment"]],
        width: int,
        height: Optional[int] = None,
        style: Optional[Style] = None,
        new_lines: bool = False,
    ) -> List[List["Segment"]]:
        """Set the shape of a list of lines (enclosing rectangle).

        Args:
            lines (List[List[Segment]]): A list of lines.
            width (int): Desired width.
            height (int, optional): Desired height or None for no change.
            style (Style, optional): Style of any padding added.
            new_lines (bool, optional): Padded lines should include "\n". Defaults to False.

        Returns:
            List[List[Segment]]: New list of lines.
        """
        _height = height or len(lines)

        blank = (
            [cls(" " * width + "\n", style)] if new_lines else [cls(" " * width, style)]
        )

        adjust_line_length = cls.adjust_line_length
        shaped_lines = lines[:_height]
        shaped_lines[:] = [
            adjust_line_length(line, width, style=style) for line in lines
        ]
        if len(shaped_lines) < _height:
            shaped_lines.extend([blank] * (_height - len(shaped_lines)))
        return shaped_lines

    # 【功能】顶部对齐：行内容不足 height 时，在下方补空白行。
    # 【算法】算出缺少的行数 extra_lines；用 lines[:height] 先保证不超高，再在尾部追加空白行。
    # 【参数】width/style：空白行的宽度与样式；new_lines：空白行是否带 "\n"；
    #   lines 为待对齐的行列表，height 为目标高度。
    # 【返回】List[List[Segment]]；行数已足够时返回入参的浅拷贝（不做任何改动）。
    # 【协作】table.py 依据单元格的 vertical 属性在 align_top / align_middle /
    #   align_bottom 之间选择。
    @classmethod
    # 【功能】顶部对齐：行内容不足 height 时，在下方补空白行。
    # 【算法】算出缺少的行数 extra_lines；用 lines[:height] 先保证不超高，再在尾部追加空白行。
    # 【参数】width/style：空白行的宽度与样式；new_lines：空白行是否带 "\n"；
    #   lines 为待对齐的行列表，height 为目标高度。
    # 【返回】List[List[Segment]]；行数已足够时返回入参的浅拷贝（不做任何改动）。
    # 【协作】table.py 依据单元格的 vertical 属性在 align_top / align_middle /
    #   align_bottom 之间选择。
    # 【易错】三个对齐方法都先做 lines[:height] 再补空白行，
    #   因此当输入行数超过 height 时都会被截断；行为一致是有意为之。
    def align_top(
        cls: Type["Segment"],
        lines: List[List["Segment"]],
        width: int,
        height: int,
        style: Style,
        new_lines: bool = False,
    ) -> List[List["Segment"]]:
        """Aligns lines to top (adds extra lines to bottom as required).

        Args:
            lines (List[List[Segment]]): A list of lines.
            width (int): Desired width.
            height (int, optional): Desired height or None for no change.
            style (Style): Style of any padding added.
            new_lines (bool, optional): Padded lines should include "\n". Defaults to False.

        Returns:
            List[List[Segment]]: New list of lines.
        """
        extra_lines = height - len(lines)
        if not extra_lines:
            return lines[:]
        lines = lines[:height]
        blank = cls(" " * width + "\n", style) if new_lines else cls(" " * width, style)
        lines = lines + [[blank]] * extra_lines
        return lines

    # 【功能】底部对齐：行内容不足 height 时，在上方补空白行。
    # 【算法】与 align_top 完全对称，只是空白行加在列表头部。
    # 【返回】List[List[Segment]]；行数已足够时返回入参的浅拷贝。
    # 【要点】lines[:height] 保留的是「最上面」的 height 行；底部对齐的语义来自空白行补齐在
    #   上方，而不是来自对内容的裁剪方向——理解这一点才不会误改这段代码。
    @classmethod
    # 【功能】底部对齐：行内容不足 height 时，在上方补空白行。
    # 【算法】与 align_top 完全对称，只是空白行加在列表头部。
    # 【返回】List[List[Segment]]；行数已足够时返回入参的浅拷贝。
    # 【要点】lines[:height] 保留的是「最上面」的 height 行；底部对齐的语义来自空白行补齐在
    #   上方，而不是来自对内容的裁剪方向——理解这一点才不会误改这段代码。
    def align_bottom(
        cls: Type["Segment"],
        lines: List[List["Segment"]],
        width: int,
        height: int,
        style: Style,
        new_lines: bool = False,
    ) -> List[List["Segment"]]:
        """Aligns render to bottom (adds extra lines above as required).

        Args:
            lines (List[List[Segment]]): A list of lines.
            width (int): Desired width.
            height (int, optional): Desired height or None for no change.
            style (Style): Style of any padding added. Defaults to None.
            new_lines (bool, optional): Padded lines should include "\n". Defaults to False.

        Returns:
            List[List[Segment]]: New list of lines.
        """
        extra_lines = height - len(lines)
        if not extra_lines:
            return lines[:]
        lines = lines[:height]
        blank = cls(" " * width + "\n", style) if new_lines else cls(" " * width, style)
        lines = [[blank]] * extra_lines + lines
        return lines

    # 【功能】垂直居中：不足部分上下平分补空白行。
    # 【算法】extra_lines // 2 补在上方，剩余的（extra_lines - top_lines）补在下方，
    #   因此奇数行时多出的一行落在下方。
    # 【参数】width/style：空白行的宽度与样式；new_lines：空白行是否带 "\n"。
    # 【返回】List[List[Segment]]；行数已足够时返回入参的浅拷贝。
    # 【协作】table.py 依据单元格的 vertical 属性在 align_top / align_middle / align_bottom
    #   间选择，本方法即 vertical="middle" 的实现。
    @classmethod
    # 【功能】垂直居中：不足部分上下平分补空白行。
    # 【算法】extra_lines // 2 补在上方，剩余的（extra_lines - top_lines）补在下方，
    #   因此奇数行时多出的一行落在下方。
    # 【参数】width/style：空白行的宽度与样式；new_lines：空白行是否带 "\n"。
    # 【返回】List[List[Segment]]；行数已足够时返回入参的浅拷贝。
    # 【协作】table.py 依据单元格的 vertical 属性选择对齐方式，本方法即 vertical="middle"
    #   的实现。
    def align_middle(
        cls: Type["Segment"],
        lines: List[List["Segment"]],
        width: int,
        height: int,
        style: Style,
        new_lines: bool = False,
    ) -> List[List["Segment"]]:
        """Aligns lines to middle (adds extra lines to above and below as required).

        Args:
            lines (List[List[Segment]]): A list of lines.
            width (int): Desired width.
            height (int, optional): Desired height or None for no change.
            style (Style): Style of any padding added.
            new_lines (bool, optional): Padded lines should include "\n". Defaults to False.

        Returns:
            List[List[Segment]]: New list of lines.
        """
        extra_lines = height - len(lines)
        if not extra_lines:
            return lines[:]
        lines = lines[:height]
        blank = cls(" " * width + "\n", style) if new_lines else cls(" " * width, style)
        top_lines = extra_lines // 2
        bottom_lines = extra_lines - top_lines
        lines = [[blank]] * top_lines + lines + [[blank]] * bottom_lines
        return lines

    # ==== 【流式变换：合并 / 去链接 / 去样式 / 去色】 ====
    # 这一组方法都不改变「可见结果」，只改变 Segment 流的表示形式：合并可以显著减少 ANSI
    # 转义序列的数量，去样式/去色/去链接则服务于特定的输出目标（无颜色终端、纯文本导出）。
    # ---------------------------------------------------------------------------
    # 【功能】把样式相同且相邻的可见 Segment 合并成一个，减少后续 ANSI 转义序列的数量。
    # 【算法】经典的「单元素延迟输出」：缓存上一段；遇到样式相同且自身无控制码的段就把文本
    #   拼接进去，否则先吐出缓存段再换新缓存；循环结束后补吐最后一段。
    # 【参数】segments：任意 Segment 可迭代对象。
    # 【返回】Iterable[Segment]，生成器；输入为空时直接 return（产出 0 项，不是空列表）。
    # 【注意】1) 判断条件是「上一段与当前段样式相等」，控制码段永远不参与合并，
    #   并且会打断合并序列（它自己单独成为一段）；
    #   2) 生成器函数里 return 不带值即表示结束，调用方要留意空输入不会产出任何东西；
    #   3) 合并只改文本不改样式，渲染结果完全等价，只是更省字节、更快。
    # 【协作】Console 在输出前与导出时都会先 simplify，Windows 渲染分支同样受益。
    @classmethod
    def simplify(cls, segments: Iterable["Segment"]) -> Iterable["Segment"]:
        """Simplify an iterable of segments by combining contiguous segments with the same style.

        Args:
            segments (Iterable[Segment]): An iterable of segments.

        Returns:
            Iterable[Segment]: A possibly smaller iterable of segments that will render the same way.
        """
        iter_segments = iter(segments)
        try:
            last_segment = next(iter_segments)
        except StopIteration:
            return

        _Segment = Segment
        for segment in iter_segments:
            if last_segment.style == segment.style and not segment.control:
                last_segment = _Segment(
                    last_segment.text + segment.text, last_segment.style
                )
            else:
                yield last_segment
                last_segment = segment
        yield last_segment

    # 【功能】去掉所有 Segment 的链接（超链接 / OSC 8），保留颜色与属性。
    # 【算法】控制码段与无样式段直接透传；其余用 Style.update_link(None) 生成去链接的新样式。
    # 【参数】segments：任意 Segment 可迭代对象。
    # 【返回】Iterable[Segment]（生成器）。
    # 【注意】update_link 会重新生成 link_id，因此去链接后的样式在哈希意义上不同于原样式，
    #   不能指望对象同一性；只保证视觉一致。
    # 【协作】Console 在 no_color=False 但需要关闭超链接的场景会调用本方法。
    @classmethod
    def strip_links(cls, segments: Iterable["Segment"]) -> Iterable["Segment"]:
        """Remove all links from an iterable of styles.

        Args:
            segments (Iterable[Segment]): An iterable segments.

        Yields:
            Segment: Segments with link removed.
        """
        for segment in segments:
            if segment.control or segment.style is None:
                yield segment
            else:
                text, style, _control = segment
                yield cls(text, style.update_link(None) if style else None)

    # 【功能】剥掉全部样式，只保留文本与控制码。
    # 【算法】逐段解包 text / _style / control，用 cls(text, None, control) 重建。
    # 【参数】segments：任意 Segment 可迭代对象。
    # 【返回】Iterable[Segment]（生成器）。
    # 【注意】控制码被保留，因此输出仍然是「结构完整」的 Segment 流；若要连控制码一起
    #   去掉，需要配合 filter_control。
    # 【兼容】在 TERM=dumb 或明确不支持 ANSI 的输出目标上，Console 用它降级为纯文本。
    @classmethod
    def strip_styles(cls, segments: Iterable["Segment"]) -> Iterable["Segment"]:
        """Remove all styles from an iterable of segments.

        Args:
            segments (Iterable[Segment]): An iterable segments.

        Yields:
            Segment: Segments with styles replace with None
        """
        for text, _style, control in segments:
            yield cls(text, None, control)

    # 【功能】去掉颜色（前景与背景），保留粗体/下划线等属性、链接与元信息。
    # 【算法】用一个 {原样式: 无色样式} 的局部字典缓存映射结果，避免对相同样式反复调用
    #   Style.without_color（该属性每次都会新建 Style 对象）。
    # 【参数】segments：任意 Segment 可迭代对象。
    # 【返回】Iterable[Segment]（生成器）。
    # 【注意】缓存是每次调用新建的局部字典，所以只在单次遍历内去重，不跨调用累积（也不会
    #   造成内存泄漏）。
    # 【协作】Console(no_color=True) 走的就是 remove_color 而不是 strip_styles，
    #   以便在黑白终端上仍然保留加粗等语义信息。
    # 【要点】style 为 None 的段不必构造无色样式，直接透传，省一次对象分配。
    @classmethod
    def remove_color(cls, segments: Iterable["Segment"]) -> Iterable["Segment"]:
        """Remove all color from an iterable of segments.

        Args:
            segments (Iterable[Segment]): An iterable segments.

        Yields:
            Segment: Segments with colorless style.
        """

        cache: Dict[Style, Style] = {}
        for text, style, control in segments:
            if style:
                colorless_style = cache.get(style)
                if colorless_style is None:
                    colorless_style = style.without_color
                    cache[style] = colorless_style
                yield cls(text, colorless_style, control)
            else:
                yield cls(text, None, control)

    # ==== 【按列分割 divide】 ====
    # ---------------------------------------------------------------------------
    # divide 是 Segment 层面最复杂的算法：它把一条「横向流动」的 Segment 流，
    # 按若干单元格列位置切成若干「纵向切片」，用于把富文本按列拆开再重排（表格单元格、
    # Text.split、Justify 等）。理解它的关键是区分两个游标：pos（绝对列号）与 cut（下一个
    # 切点），以及一个缓冲 split_segments（当前切片已累积的内容）。
    # ---------------------------------------------------------------------------
    # 【功能】按给定的单元格列位置，把一条 Segment 流切成若干切片（每个切片是一个列表）。
    # 【算法】维护 split_segments（当前切片的累积缓冲）与 pos（当前段起点所在的绝对列号）。
    #   对每个 cut：段完全落在切点左侧就累积；正好落在切点上就吐出当前切片并前进；
    #   落在段中间就用 split_cells 把段一分为二，吐出一半、另一半留到下一次循环继续处理。
    #   开头的 cut == 0 会逐个吐出空切片；切点用尽（next 得到 -1）时立即结束，因此落在
    #   最后一个切点之后的尾部内容不会被吐出——调用方只应消费到最后一个切点为止。
    # 【参数】segments：Segment 可迭代对象；cuts：递增的、以单元格为单位的切割位置，
    #   自定义哨兵 -1 表示「没有更多切点」（因此调用方不能把 -1 当作合法切点）。
    # 【返回】Iterable[List[Segment]]，生成器：每个被走到的切点产出一个切片，流的末尾可能
    #   再产出一个尾巴切片（当切点恰好用尽于流末尾时）。
    # 【注意】1) 控制码段的格数按 0 计，因此它总是被并入当前切片；
    #   2) 用 cached_cell_len 而不是 cell_len，因为同一段文本会被反复测量；
    #   3) 结尾处 `if split_segments:` 判空是为了避免吐出多余的空列表；
    #   4) 该生成器不是完全惰性的：为了推进到下一个切点，它可能一次消费多段输入。
    # 【协作】text.py 的 Text.divide / Text.split 依赖它把一段富文本按列切成多块
    #   （例如把 Text 填进表格单元格或做 Justify 排版）。
    @classmethod
    # 【注释】divide 的详细算法说明见本文件顶部【按列分割 divide】一节的注释块；
    #   这里补充一条最容易被忽略的约束：cuts 必须严格递增，否则 pos 会回退，
    #   产生的切片会重叠。调用方（Text.divide）通过排序保证这一点。
    # 【返回】注意每个切片都是「已复制」的列表，调用方可以安全地就地修改。
    def divide(
        cls, segments: Iterable["Segment"], cuts: Iterable[int]
    ) -> Iterable[List["Segment"]]:
        """Divides an iterable of segments in to portions.

        Args:
            cuts (Iterable[int]): Cell positions where to divide.

        Yields:
            [Iterable[List[Segment]]]: An iterable of Segments in List.
        """
        # 【要点】split_segments 同时充当「当前切片的累积缓冲」与「即将产出的切片」；
        #   把 append / clear / copy 三个绑定方法提到循环外，避免在双重循环里反复做属性查找。
        split_segments: List["Segment"] = []
        add_segment = split_segments.append

        iter_cuts = iter(cuts)

        # 【要点】前导的 0 值切点表示「在流的最开始处切一刀」，此时还没有任何内容，
        #   因此直接产出空切片：这样「每个被真正走到的切点都对应一个产出」这一约定成立。
        # 【易错】`cut == -1` 直接 return（不产出任何切片），因此调用方必须保证 cuts 只包含
        #   合法列号，否则 -1 会被当成「没有更多切点」的哨兵而提前结束遍历。
        while True:
            cut = next(iter_cuts, -1)
            if cut == -1:
                return
            if cut != 0:
                break
            yield []
        pos = 0

        segments_clear = split_segments.clear
        segments_copy = split_segments.copy

        _cell_len = cached_cell_len
        for segment in segments:
            text, _style, control = segment
            while text:
                # 【要点】end_pos 用 pos + 段格数 表示该段结束于哪一列；控制码段格数为 0，
                #   于是 end_pos == pos，会自然落入 end_pos < cut 的分支被并入当前切片。
                end_pos = pos if control else pos + _cell_len(text)
                if end_pos < cut:
                    add_segment(segment)
                    pos = end_pos
                    break

                # 【要点】end_pos == cut 表示切点恰好落在段的边界上，无需切分该段；
                #   吐出切片后立刻读取下一个切点，若已无切点则把剩余缓冲吐出并结束。
                if end_pos == cut:
                    add_segment(segment)
                    yield segments_copy()
                    segments_clear()
                    pos = end_pos

                    cut = next(iter_cuts, -1)
                    if cut == -1:
                        if split_segments:
                            yield segments_copy()
                        return

                    break

                else:
                    before, segment = segment.split_cells(cut - pos)
                    text, _style, control = segment
                    add_segment(before)
                    yield segments_copy()
                    segments_clear()
                    pos = cut

                cut = next(iter_cuts, -1)
                if cut == -1:
                    if split_segments:
                        yield segments_copy()
                    return

        # 【要点】所有段处理完后，如果还有未产出的尾部内容就补吐一个切片；
        #   若最后一个切点恰好落在流的末尾，此处产出的是空列表，调用方需要能接受空切片。
        # 【易错】反过来并不成立：若切点在遍历中被用尽（next 返回 -1），函数会直接 return，
        #   落在最后一个切点之后的尾部内容不会被吐出——调用方只应消费到最后一个切点为止。
        yield segments_copy()

# ==== 【Renderable 适配器】 ====
# ---------------------------------------------------------------------------
# 【职责】把「一堆 Segment」重新包装成 Rich 的 Renderable，使其能被 console.print
#   直接输出，或在渲染中途作为普通对象参与组合。
# 【关键属性】segments：已物化的 Segment 列表；new_lines：是否在每个段后插入换行。
# 【不变量】构造时即 list(segments) 物化，因此可以重复消费（与生成器不同），
#   代价是失去惰性、多占一份内存。
# 【协作】syntax.py 用它把高亮结果交回渲染管线；普通用户代码可借此在
#   __rich_console__ 之外使用 Segment。
# ---------------------------------------------------------------------------

class Segments:
    """A simple renderable to render an iterable of segments. This class may be useful if
    you want to print segments outside of a __rich_console__ method.

    Args:
        segments (Iterable[Segment]): An iterable of segments.
        new_lines (bool, optional): Add new lines between segments. Defaults to False.
    """

    # 【功能】物化 Segment 迭代器，并记录是否要在每段之后补一个换行段。
    # 【参数】segments：任意可迭代的 Segment（生成器也会在此刻被消费完）；new_lines：True 时在每段后追加换行段。
    # 【注意】list(segments) 立即消费输入：大流会占内存，换来的是可重复渲染（生成器不行）；
    #   new_lines 的语义是“每个 Segment 一段”而不是“每行”——按行输出请用 SegmentLines。
    def __init__(self, segments: Iterable[Segment], new_lines: bool = False) -> None:
        self.segments = list(segments)
        self.new_lines = new_lines

    # 【功能】Rich 渲染协议入口：逐个产出 Segment，必要时在每段后补一个换行段。
    # 【参数】console / options 由 Console 在渲染时注入，本实现完全不使用它们。
    # 【返回】RenderResult（生成器）。
    # 【注意】new_lines=True 时换行是加在「每个 Segment」之后而非每行之后，
    #   因此它只适合展示「一段一段」的内容；需要按行输出请使用 SegmentLines。
    def __rich_console__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> "RenderResult":
        if self.new_lines:
            line = Segment.line()
            for segment in self.segments:
                yield segment
                yield line
        else:
            yield from self.segments


# ---------------------------------------------------------------------------
# 【职责】把「若干行的 Segment 列表」包装成 Renderable，是 Segments 的行版本。
# 【关键属性】lines：已物化的行列表（每行是一个 Segment 列表）；new_lines：是否在每行
#   之后插入换行段。
# 【不变量】与 Segments 一样在构造时物化，可重复渲染。
# 【协作】用于在渲染中途携带「已经是多行」的内容继续参与布局（例如把已排版好的
#   片段塞进更大的容器里）。
# ---------------------------------------------------------------------------
class SegmentLines:
    # 【功能】物化“行的列表”，并记录是否在每行之后插入换行段。
    # 【参数】lines：Iterable[List[Segment]]，每个元素是一行的 Segment 列表；new_lines：True 时每行后追加换行段。
    # 【注意】与 Segments 一样在构造时 list(...) 物化（可重复渲染、不再惰性）；
    #   行的宽度与行数都不做调整，对齐需要调用方自己保证。
    def __init__(self, lines: Iterable[List[Segment]], new_lines: bool = False) -> None:
        """A simple renderable containing a number of lines of segments. May be used as an intermediate
        in rendering process.

        Args:
            lines (Iterable[List[Segment]]): Lists of segments forming lines.
            new_lines (bool, optional): Insert new lines after each line. Defaults to False.
        """
        self.lines = list(lines)
        self.new_lines = new_lines

    # 【功能】Rich 渲染协议入口：逐行产出所有 Segment，可选地在每行后补一个换行段。
    # 【返回】RenderResult（生成器）。
    # 【注意】这里用 yield from 展开每行，因此行与行之间的换行完全由 new_lines 控制；
    #   行内部的 \n（如果存在）不会被特殊处理，需要换行请依赖 new_lines 或自行切分。
    def __rich_console__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> "RenderResult":
        if self.new_lines:
            new_line = Segment.line()
            for line in self.lines:
                yield from line
                yield new_line
        else:
            for line in self.lines:
                yield from line


# 【历史】文件末尾的 __main__ 块是 Rich 上游用于演示/自测的入口（pragma: no cover），
#   以 `python -m rich.segment` 运行可以看到 Segments 的生成与消费全过程：
#   console.print(fragments) 打印的是 Segment 对象本身，console.print(text) 打印的是
#   最终渲染结果，两者对照可以直观理解「Segment 是 ANSI 文本之前的最后一步」。
#   它不参与库的运行时行为，阅读时可以略过。
if __name__ == "__main__":  # pragma: no cover
    from rich.console import Console
    from rich.syntax import Syntax
    from rich.text import Text

    code = """from rich.console import Console
console = Console()
text = Text.from_markup("Hello, [bold magenta]World[/]!")
console.print(text)"""

    text = Text.from_markup("Hello, [bold magenta]World[/]!")

    console = Console()

    console.rule("rich.Segment")
    console.print(
        "A Segment is the last step in the Rich render process before generating text with ANSI codes."
    )
    console.print("\nConsider the following code:\n")
    console.print(Syntax(code, "python", line_numbers=True))
    console.print()
    console.print(
        "When you call [b]print()[/b], Rich [i]renders[/i] the object in to the following:\n"
    )
    fragments = list(console.render(text))
    console.print(fragments)
    console.print()
    console.print("The Segments are then processed to produce the following output:\n")
    console.print(text)
    console.print(
        "\nYou will only need to know this if you are implementing your own Rich renderables."
    )
