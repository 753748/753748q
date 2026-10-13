import re
from functools import partial, reduce
from math import gcd
from operator import itemgetter
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Pattern,
    Tuple,
    Union,
)

from ._loop import loop_last
from ._pick import pick_bool
from ._wrap import divide_line
from .align import AlignMethod
from .cells import cell_len, set_cell_size
from .containers import Lines
from .control import strip_control_codes
from .emoji import EmojiVariant
from .jupyter import JupyterMixin
from .measure import Measurement
from .segment import Segment
from .style import Style, StyleType

if TYPE_CHECKING:  # pragma: no cover
    from .console import Console, ConsoleOptions, JustifyMethod, OverflowMethod

# ---------------------------------------------------------------------------
# 【模块定位】本模块定义了 Rich 的核心数据类型 Text——“带样式的字符串”。它位于渲染管线
#   的**内容层**：上层（markup、syntax、table、markdown、traceback…）都先生成 Text，
#   再由 Console 通过 `__rich_console__` 协议驱动它输出 Segment。
#   依赖：rich._loop（loop_last）、rich._pick（pick_bool）、rich._wrap（divide_line，
#     换行断点计算）、rich.align（对齐方式字面量）、rich.cells（cell_len/set_cell_size，
#     终端单元格宽度）、rich.containers（Lines）、rich.control（strip_control_codes）、
#     rich.emoji、rich.jupyter（JupyterMixin）、rich.measure（Measurement）、
#     rich.segment（Segment）、rich.style（Style）。
#   被依赖：几乎全部渲染类模块，以及 console.py 的打印主流程。
# 【关键数据结构】
#   - Text：字符串本体（私有的 _text 分块列表）+ 一串 Span（样式区间）+ 若干排版属性
#     （justify/overflow/no_wrap/end/tab_size）+ 缓存长度 _length。
#   - Span：命名元组 (start, end, style)，区间为**字符下标**（不是 cell），左闭右开。
#   核心不变量：
#     (1) `len(text) == text._length == len(text.plain)`；
#     (2) Span 的 start/end 都落在 [0, len(plain)] 内，且不是所有 span 都保证有序；
#     (3) 所有 span 的样式在渲染时按“加入顺序”依次叠加，后加入者优先；
#     (4) 字符下标与终端 cell 是两套度量：宽字符（CJK/emoji）占 2 cell 但只占 1 个下标。
# 【设计要点】
#   - 不直接存一个 str，而是存 _text: List[str] 分块：append 只需 push 一个块（均摊
#     O(1)），直到有人读取 plain 时才 join 成整串（惰性合并），这让“大量小片段拼接”
#     的常见场景（markup、表格）避免 O(n²) 的字符串复制。
#   - 样式不内联进字符，而是外挂成 Span 列表：同一段文字可以叠加多层样式，
#     且裁剪/拼接时只需平移区间数字，不必重新解析字符串。
#   - 字符下标与 cell 宽度分离：spans/stylize/split 用字符下标，截断/对齐/换行用 cell；
#     这一分离是本文件最容易出错、也最值得先理解的地方。
#   - 不可变与可变之间的折中：Text 本身可变（stylize/append/truncate 就地修改），
#     但提供了 copy/blank_copy 来做“写时复制”式的安全切分。
# 【阅读建议】先读 Span（区间代数）与 Text 的构造/plain/_length 三者关系，再读
#   render（span 事件栈，理解“样式如何真正生效”），然后读 divide/split/wrap
#   （切分与换行，注意 line_range 的下标重定基），最后读 truncate/align/pad
#   （cell 度量）与 expand_tabs/with_indent_guides（排版辅助）。
#   本文件没有模块 docstring，因此本注释块紧跟在导入与常量声明之前。
# ---------------------------------------------------------------------------


# ==== 【模块常量与类型别名】 ====
# 【要点】两套默认值：justify 默认 "default"（等价于左对齐，但语义上允许 Console 覆写），
# overflow 默认 "fold"（超宽时折行而不是裁剪），它们是 options 缺省时的兜底值。
DEFAULT_JUSTIFY: "JustifyMethod" = "default"
DEFAULT_OVERFLOW: "OverflowMethod" = "fold"


# 【要点】预编译的正则，用于 rstrip_end：匹配“行尾的连续空白”，从而只裁掉多余的空格
# 而不是其它字符。预编译是必要的，因为它在渲染热路径上每次换行都会被用到。
_re_whitespace = re.compile(r"\s+$")

# 【要点】TextType 是“可以当作文本看待的东西”的联合：普通 str 或 Text 实例。
# 之所以要在模块级定义而不是写 Union[str, "Text"] 到处重复，是为了让各渲染类的签名统一。
TextType = Union[str, "Text"]
"""A plain string or a :class:`Text` instance."""

# 【要点】highlight_regex 允许传入的“样式回调”：接收匹配到的文本，返回样式或 None。
# 返回 None 表示“这个匹配不打样式”，因此不能把返回值直接当样式用。
GetStyleCallable = Callable[[str], Optional[StyleType]]


# ==== 【Span：样式区间】 ====
# 【职责】表示“某段文字使用某种样式”的标记，是 Text 唯一的样式载体。
# 【关键属性】start：区间起始字符下标（含）；end：结束字符下标（不含）；
#   style：样式定义，可以是 str（如 "bold red"）或已解析的 Style。
# 【不变量】
#   - 0 <= start <= end <= len(plain)（越界的 span 会在 plain 被赋新值时由 _trim_spans 清理）；
#   - 区间是左闭右开，因此相邻区间如 (0,3) 与 (3,5) 不重叠；
#   - 空区间（start == end）在逻辑上等价于“不存在”，见 __bool__；
#   - start/end 是**字符下标**而非 cell 宽度，宽字符在这里仍然只算 1 个下标。
# 【协作】由 Text.stylize/stylize_before 创建；被 divide 平移、被 right_crop 截断、
#   被 render 转成“进入/离开”事件。
# 【设计要点】用 NamedTuple 而不是普通类：它是不可变值对象，可以自由复制/比较，
#   还能直接按元组解包（文件里大量出现 `start, end, style = span`）。
class Span(NamedTuple):
    """A marked up region in some text."""

    start: int
    """Span start index."""
    end: int
    """Span end index."""
    style: Union[str, Style]
    """Style associated with the span."""

    # 【功能】给出便于调试的表示：`Span(0, 4, 'bold')` 而非默认的元组表示。
    # 【返回】字符串；style 用 !r 加引号，方便看出它是字符串样式名还是 Style 对象。
    # 【为什么】NamedTuple 自带的 repr 会把三要素平铺成元组，排查样式问题时不够醒目；
    #   自定义 repr 让 `text._spans` 打印出来可以直接与下标对照。
    # 【注意】只影响显示，不改变相等性比较（NamedTuple 的 == 仍按字段值比较）。
    def __repr__(self) -> str:
        return f"Span({self.start}, {self.end}, {self.style!r})"

    # 【功能】判断该 span 是否“非空”，即是否真的覆盖了至少一个字符。
    # 【返回】True 当且仅当 end > start。
    # 【为什么】把“空 span”定义成假值后，代码里可以直接写 `if span:` 来过滤；
    #   这与 NamedTuple 的默认语义（长度非 0 即为真）不同——默认语义下 (0,0,'x') 也是真，
    #   会让“零宽 span”意外地参与样式叠加。
    # 【易错】不要用 `span is not None` 判断“有样式”，空区间会被漏掉或被误算。
    def __bool__(self) -> bool:
        return self.end > self.start

    # 【功能】在给定偏移处把一个 span 切成两段，供 Text.divide 把样式分配到不同行。
    # 【算法】
    #   - 若 offset < start（切点在区间之前）：整段属于右侧，返回 (self, None)；
    #   - 若 offset >= end（切点在区间之后或正好在末尾）：整段属于左侧，返回 (self, None)；
    #   - 否则返回 (Span(start, offset, style), Span(offset, end, style))，
    #     两段首尾相接、共享同一 style，合起来恰好等于原区间。
    # 【参数】offset：切割位置的字符下标。
    # 【返回】(span1, span2) 二元组；span2 为 None 表示“没有切分”，此时 span1 就是自身
    #   （注意返回的是同一个对象，不是副本）。
    # 【注意】offset 落在边界上时不产生第二段，因此调用方需要处理 None。
    # 【易错】切分不检查 offset 是否为负：负数会走第一个分支并返回自身，行为上安全，
    #   但语义上“负偏移”是调用方的错误。
    def split(self, offset: int) -> Tuple["Span", Optional["Span"]]:
        """Split a span in to 2 from a given offset."""

        if offset < self.start:
            return self, None
        if offset >= self.end:
            return self, None

        start, end, style = self
        span1 = Span(start, min(end, offset), style)
        span2 = Span(span1.end, end, style)
        return span1, span2

    # 【功能】把整个区间平移一段距离，用于把样式从一个 Text 搬到另一个 Text
    #   （拼接、分行时都要重定基）。
    # 【参数】offset：要加到 start 与 end 上的字符数，可正可负。
    # 【返回】新的 Span；**不是**就地修改（NamedTuple 本来也不可变）。
    # 【要点】用解包 `start, end, style = self` 再重建对象，是本文件里反复出现的惯用法：
    #   它比 self._replace(...) 更快，也更容易读。
    def move(self, offset: int) -> "Span":
        """Move start and end by a given offset.

        Args:
            offset (int): Number of characters to add to start and end.

        Returns:
            TextSpan: A new TextSpan with adjusted position.
        """
        start, end, style = self
        return Span(start + offset, end + offset, style)

    # 【功能】从右侧把区间裁到不超过 offset，用于“文本被截断后同步收缩样式”。
    # 【算法】offset >= end 时无需裁剪，原样返回 self（省一次分配）；否则把 end 收窄为
    #   min(offset, end)——min 在这里是防御性写法，因为该分支已保证 offset < end。
    # 【参数】offset：裁切位置，应落在 [start, end] 之间。
    # 【返回】新的 Span 或自身。
    # 【注意】只动 end，不动 start：本方法用于右侧裁剪；需要裁左侧时用 split 取第二段。
    def right_crop(self, offset: int) -> "Span":
        """Crop the span at the given offset.

        Args:
            offset (int): A value between start and end.

        Returns:
            Span: A new (possibly smaller) span.
        """
        start, end, style = self
        if offset >= end:
            return self
        return Span(start, min(offset, end), style)

    # 【功能】把区间右端延长 cells 个字符，用于 expand_tabs：制表符被替换成空格后，
    #   覆盖该 tab 的样式区间需要跟着变长。
    # 【参数】cells：要加到 end 上的字符数（**这里实际是字符数而非 cell 宽度**，
    #   尽管参数名叫 cells；tab 展开时传入的正是“补几个空格”）。
    # 【返回】新的 Span；cells 为 0（或假值）时原样返回 self，避免无意义分配。
    # 【易错】只延长 end，不改变 start；若区间本来为空（start == end），
    #   延长后会变成一个有效区间——这正是给 tab 补样式时想要的效果。
    def extend(self, cells: int) -> "Span":
        """Extend the span by the given number of cells.

        Args:
            cells (int): Additional space to add to end of span.

        Returns:
            Span: A span.
        """
        if cells:
            start, end, style = self
            return Span(start, end + cells, style)
        else:
            return self


# ==== 【Text：带样式的字符串】 ====
# 【职责】Rich 的核心内容对象：保存纯文本 + 一组样式区间 + 排版参数，
#   并通过 `__rich_console__`/`__rich_measure__` 协议被 Console 渲染。
# 【关键属性】
#   _text：字符串分块列表（惰性合并，plain 读取时 join）；
#   _length：字符数缓存，必须与 len(plain) 保持一致（见不变量）；
#   _spans：Span 列表，**顺序即样式叠加顺序**（后面的 span 会覆盖前面的同名属性）；
#   style：整段文本的基础样式（在渲染时最先入栈，因此优先级最低）；
#   justify/overflow/no_wrap：排版策略，None 表示“跟随 ConsoleOptions 或默认值”；
#   end：渲染结束时追加的字符串（默认换行符），它不受样式影响；
#   tab_size：制表符宽度，None 表示跟随 console.tab_size。
# 【不变量】
#   - len(self) == self._length == len(self.plain)；
#   - self._spans 中每个 span 的区间都落在 [0, _length]；
#   - spans 不要求有序、也不要求互不重叠；叠加顺序由列表顺序决定；
#   - 字符下标与 cell 宽度是两套度量，绝不能混用（宽字符是两者差异的来源）。
# 【协作】被 markup/syntax/table/markdown 等构造；被 Console 渲染；
#   与 Lines 容器（split/divide/wrap 的返回类型）配合完成多行排版。
# 【设计要点】用 __slots__ 声明全部字段：Text 是渲染管线上分配最频繁的对象之一，
#   去掉 __dict__ 能显著降低内存与属性访问开销，同时防止用户误加属性。
class Text(JupyterMixin):
    """Text with color / style.

    Args:
        text (str, optional): Default unstyled text. Defaults to "".
        style (Union[str, Style], optional): Base style for text. Defaults to "".
        justify (str, optional): Justify method: "left", "center", "full", "right". Defaults to None.
        overflow (str, optional): Overflow method: "crop", "fold", "ellipsis". Defaults to None.
        no_wrap (bool, optional): Disable text wrapping, or None for default. Defaults to None.
        end (str, optional): Character to end text with. Defaults to "\\\\n".
        tab_size (int): Number of spaces per tab, or ``None`` to use ``console.tab_size``. Defaults to None.
        spans (List[Span], optional). A list of predefined style spans. Defaults to None.
    """

    # 【要点】__slots__ 里列出的名字就是本类的全部实例字段；下划线开头的是内部实现，
    #   不带下划线的是公开可配置属性。因为用了 __slots__，实例没有 __dict__，
    #   既省内存也禁止动态添加属性（拼错属性名会立刻报 AttributeError，而不是静默失效）。
    __slots__ = [
        "_text",
        "style",
        "justify",
        "overflow",
        "no_wrap",
        "end",
        "tab_size",
        "_spans",
        "_length",
    ]

    # 【功能】构造一个 Text，把原始字符串净化（去掉控制码）后按单块存入 _text，
    #   并记录排版参数与初始 span 列表。
    # 【参数】
    #   text：初始纯文本；**会被 strip_control_codes 过滤**，所以传入的 ANSI 转义序列
    #     不会保留（要保留转义序列的样式信息请用 Text.from_ansi）。
    #   style：整段文本的基础样式（优先级最低，渲染时最先入栈）。
    #   justify/overflow/no_wrap：排版策略，None 表示“留给 ConsoleOptions 或默认值决定”。
    #   end：渲染结束后追加的字符串，默认换行符；它不参与样式与宽度计算。
    #   tab_size：制表符宽度，None 表示跟随 console.tab_size（见 __rich_console__）。
    #   spans：预置的样式区间列表；为 None 或空列表时新建空列表。
    # 【要点】这里把文本存成「单元素列表」，与后续 append 的追加语义一致：
    #   _text 永远是“块的列表”，plain 读取时才合并。
    # 【易错】`spans or []` 在 spans 非空时**直接引用同一个列表对象**，不复制；
    #   若调用方之后修改该列表，Text 的样式也会跟着变（需要隔离请传 spans[:] 或 .copy()）。
    # 【易错】_length 统计的是“净化后的字符数”，不是 cell 宽度；宽字符仍只算 1。
    def __init__(
        self,
        text: str = "",
        style: Union[str, Style] = "",
        *,
        justify: Optional["JustifyMethod"] = None,
        overflow: Optional["OverflowMethod"] = None,
        no_wrap: Optional[bool] = None,
        end: str = "\n",
        tab_size: Optional[int] = None,
        spans: Optional[List[Span]] = None,
    ) -> None:
        # 【要点】在入口处过滤控制码，保证 Text 内部永远只有可打印文本；
        # 这样后续所有关于“下标 == 显示位置”的推理才成立。
        sanitized_text = strip_control_codes(text)
        self._text = [sanitized_text]
        self.style = style
        self.justify: Optional["JustifyMethod"] = justify
        self.overflow: Optional["OverflowMethod"] = overflow
        self.no_wrap = no_wrap
        self.end = end
        self.tab_size = tab_size
        self._spans: List[Span] = spans or []
        # 【要点】长度与文本同时初始化，建立 _length == len(plain) 这一不变量。
        self._length: int = len(sanitized_text)

    # ==== 【容器协议：长度、真假、比较】 ====
    # 【功能】返回文本的**字符数**（不是 cell 宽度）。
    # 【返回】缓存的 _length。
    # 【注意】判断显示宽度必须用 cell_len / self.cell_len；用 len() 会低估 CJK 文本的宽度。
    # 【为什么】不写 len(self.plain)：plain 可能触发分块合并，而 _length 是 O(1) 的缓存。
    def __len__(self) -> int:
        return self._length

    # 【功能】空判断：文本长度为 0 时为 False。
    # 【返回】bool(self._length)。
    # 【为什么】把“空文本”定义为假值，使 `if text:` 成为惯用的空值检查；
    #   注意它**不**考虑 spans——只有样式没有文字时同样是“空”。
    # 【易错】与本文件 Span.__bool__ 的语义不同：前者看长度，后者看区间是否非空。
    def __bool__(self) -> bool:
        return bool(self._length)

    # 【功能】转成普通字符串，便于与 str API 互操作（打印、拼接、日志）。
    # 【返回】self.plain（纯文本，丢掉全部样式信息）。
    def __str__(self) -> str:
        return self.plain

    # 【功能】调试用表示，包含纯文本、span 列表与基础样式。
    # 【返回】形如 `<text 'hi' [Span(0, 2, 'bold')] ''>` 的字符串。
    # 【为什么】把三要素都列出来，排查“样式没生效”时可以直接看到 spans 是否正确。
    def __repr__(self) -> str:
        return f"<text {self.plain!r} {self._spans!r} {self.style!r}>"

    # 【功能】支持 `Text + str` 与 `Text + Text` 的拼接。
    # 【算法】先复制自身（避免污染原对象），再把另一侧 append 进去。
    # 【参数】other：str 或 Text；其它类型返回 NotImplemented 让 Python 去尝试
    #   反向运算 __radd__，最终抛 TypeError。
    # 【返回】新的 Text（原对象不变）。
    # 【注意】与 list 的 += 语义不同：这里**不会**就地修改，因为没有 __iadd__。
    def __add__(self, other: Any) -> "Text":
        if isinstance(other, (str, Text)):
            result = self.copy()
            result.append(other)
            return result
        return NotImplemented

    # 【功能】相等性：纯文本与 span 列表都相同才算相等。
    # 【算法】先做类型守卫，再比较 plain 与 _spans（列表逐元素比较，因此顺序敏感）。
    # 【参数】other：任意对象；非 Text 时返回 NotImplemented。
    # 【返回】bool。
    # 【易错】比较是**顺序敏感**的：两个样式效果相同但 span 顺序不同的 Text 不相等。
    #   另外基础样式 self.style 不参与比较，只有 span 参与。
    # 【易错】未实现 __hash__（因为 __eq__ 被重写），所以 Text 不可放入 set/dict 的键。
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Text):
            return NotImplemented
        return self.plain == other.plain and self._spans == other._spans

    # 【功能】支持 `"abc" in text` 与 `Text("abc") in text` 的子串判断。
    # 【算法】统一取出纯文本后用 Python 的 in 运算符判断。
    # 【参数】other：str 或 Text；其它类型直接返回 False（而不是报错）。
    # 【返回】bool。
    # 【注意】只做“纯文本包含”，完全忽略样式；也不做大小写/宽度相关的任何归一化。
    def __contains__(self, other: object) -> bool:
        if isinstance(other, str):
            return other in self.plain
        elif isinstance(other, Text):
            return other.plain in self.plain
        return False

    # 【功能】按下标或切片取出子文本，并保留覆盖该范围的样式。
    # 【算法】
    #   - 整数下标：交给内层 get_text_at，取单个字符，并把所有覆盖该位置的 span
    #     收缩成 (0, 1) 区间（因为新 Text 的坐标系从 0 开始）；
    #   - 切片且 step == 1：用 slice.indices() 归一化（处理负数与省略端点），
    #     再调用 divide([start, stop]) 把它切成三段 [0,start)、[start,stop)、[stop,end)，
    #     取中间那段即是结果；
    #   - step != 1：不支持，抛 TypeError。
    # 【参数】slice：int 或 slice 对象；负下标与负数端点由 Python 的索引规则处理。
    # 【返回】新的 Text（样式随区间平移；原对象不变）。
    # 【注意】返回的对象是**新实例**，修改它不会影响原文本。
    # 【易错】两条分支保留的排版属性不一致：整数下标分支显式传 end=""，
    #   所以取出的单字符没有结尾换行；切片分支走 divide，保留 style/justify/overflow
    #   但丢掉 no_wrap/end/tab_size（divide 没有复制这些字段）。
    # 【优化】用 divide 而不是“先切片再重算 span”，可以复用同一套区间平移逻辑，
    #   避免在这里重复实现一套样式裁剪代码。
    def __getitem__(self, slice: Union[int, slice]) -> "Text":
        # 【职责】取单个字符并把它周围的样式压缩成一个 (0, 1) 区间。
        # 【要点】_Span = Span 是热路径上的局部别名，避免重复的全局查找。
        def get_text_at(offset: int) -> "Text":
            _Span = Span
            text = Text(
                self.plain[offset],
                # 【要点】列表推导筛出覆盖 offset 的 span，并统一改写成 (0, 1)：
                # 新 Text 只有一个字符，因此所有命中样式的区间都是整段。
                spans=[
                    _Span(0, 1, style)
                    for start, end, style in self._spans
                    if end > offset >= start
                ],
                # 【要点】单字符不应带结尾换行，故显式传 end=""。
                end="",
            )
            return text

        if isinstance(slice, int):
            return get_text_at(slice)
        else:
            # 【要点】slice.indices 把 None/负数归一化成 [0, len] 内的具体下标。
            start, stop, step = slice.indices(len(self.plain))
            if step == 1:
                # 【要点】divide([start, stop]) 返回三段，中间那段就是 [start:stop]。
                lines = self.divide([start, stop])
                return lines[1]
            else:
                # 【兼容】step != 1 的切片需要“逐字符取 + 重排 span”，实现成本高而
                # 实际使用极少，因此作者选择明确抛 TypeError 而不是给出慢实现。
                # This would be a bit of work to implement efficiently
                # For now, its not required
                raise TypeError("slices with step!=1 are not supported")

    # 【职责】以终端单元格为单位报告显示宽度，供布局/表格/对齐使用。
    # 【功能】返回渲染这段文本所需的 cell 数。
    # 【返回】cell_len(self.plain)：宽字符按 2 计，零宽字符按 0 计。
    # 【注意】它是属性而不是方法，但每次访问都会重新遍历整串计算（没有缓存），
    #   因此在循环里反复读同一个 Text 的 cell_len 有一定开销。
    # 【易错】不要与 len(self) 混用：len 是字符数，cell_len 是显示宽度；
    #   只有纯 ASCII 文本两者才恰好相等。
    @property
    def cell_len(self) -> int:
        """Get the number of cells required to render this text."""
        return cell_len(self.plain)

    # 【职责】把 Text 反向序列化成控制台标记（markup），是 from_markup 的逆操作。
    # 【功能】返回可再次被 Text.from_markup / Console 解析的标记字符串。
    # 【算法】
    #   1) 把“基础样式开始/结束”与“每个 span 的开始/结束”抽象成同一类事件：
    #      (偏移, 是否闭合, 样式)；基础样式的事件分别放在最前（0 处开启）与最后（末尾闭合）。
    #   2) 按 (偏移, 是否闭合) 排序：同一偏移上“开”在前（False < True），
    #      保证形如 a[bold]b[/bold]c 的输出是嵌套正确的。
    #   3) 顺序扫描事件，填充两件事件之间的纯文本（用 markup.escape 转义方括号），
    #      并在事件处输出 [style] 或 [/style]。
    # 【返回】markup 字符串；若没有任何样式则返回转义后的纯文本。
    # 【注意】样式为假值（空串/None）时不会生成标签，因此“默认样式”不会污染输出。
    # 【易错】同一偏移上多个样式同时开启/闭合时，输出依赖排序稳定性；
    #   Python 的 sort 是稳定的，配合 itemgetter(0, 1) 得到确定顺序。
    # 【兼容】这里依赖 markup 模块的 escape/render 约定（方括号需转义）；为避免循环导入，
    #   escape 在函数内部导入。
    @property
    def markup(self) -> str:
        """Get console markup to render this Text.

        Returns:
            str: A string potentially creating markup tags.
        """
        from .markup import escape

        output: List[str] = []

        plain = self.plain
        # 【要点】把基础样式也当成一对“事件”：在位置 0 开启、在文本末尾闭合，
        # 于是它自然成为最外层的标签。
        markup_spans = [
            (0, False, self.style),
            *((span.start, False, span.style) for span in self._spans),
            *((span.end, True, span.style) for span in self._spans),
            (len(plain), True, self.style),
        ]
        # 【要点】按 (偏移, 是否闭合) 排序：False < True，所以同一位置上先输出开启标签，
        # 保证 "[a][b]...[/b][/a]" 这种嵌套不会被写成非法顺序。
        markup_spans.sort(key=itemgetter(0, 1))
        position = 0
        append = output.append
        for offset, closing, style in markup_spans:
            # 【要点】两个事件之间的文字必须原样保留，并且要转义方括号，
            # 否则文本里的 "[" 会被误当成标记标签。
            if offset > position:
                append(escape(plain[position:offset]))
                position = offset
            # 【要点】空样式不产生标签，避免输出 "[][/]" 之类的噪声。
            if style:
                append(f"[/{style}]" if closing else f"[{style}]")
        markup = "".join(output)
        return markup

    # 【职责】从控制台标记（如 "[bold red]hi[/]"）构造 Text 的类方法。
    # 【功能】调用 rich.markup.render 解析标记，再把排版参数贴到结果上。
    # 【算法】解析与样式处理全部委托给 markup 模块（它负责把标签转成 span），
    #   本方法只负责补上 justify/overflow/end 这三个“排版意图”。
    # 【参数】text：含标记的字符串。style：整段基础样式。emoji/emoji_variant：
    #   是否替换 emoji 短代码及其变体。justify/overflow/end：排版参数。
    # 【返回】新的 Text 实例。
    # 【注意】解析失败（标签不闭合等）会抛 rich.errors.MarkupError，本方法不做捕获。
    # 【易错】这里的 `style` 是基础样式而不是 markup 标签；它会被 markup.render
    #   当作默认样式传下去（因此优先级最低）。
    # 【兼容】markup 在函数内部导入：markup 模块反向依赖 text，模块级导入会形成循环。
    @classmethod
    def from_markup(
        cls,
        text: str,
        *,
        style: Union[str, Style] = "",
        emoji: bool = True,
        emoji_variant: Optional[EmojiVariant] = None,
        justify: Optional["JustifyMethod"] = None,
        overflow: Optional["OverflowMethod"] = None,
        end: str = "\n",
    ) -> "Text":
        """Create Text instance from markup.

        Args:
            text (str): A string containing console markup.
            style (Union[str, Style], optional): Base style for text. Defaults to "".
            emoji (bool, optional): Also render emoji code. Defaults to True.
            emoji_variant (str, optional): Optional emoji variant, either "text" or "emoji". Defaults to None.
            justify (str, optional): Justify method: "left", "center", "full", "right". Defaults to None.
            overflow (str, optional): Overflow method: "crop", "fold", "ellipsis". Defaults to None.
            end (str, optional): Character to end text with. Defaults to "\\\\n".

        Returns:
            Text: A Text instance with markup rendered.
        """
        from .markup import render

        # 【要点】标记解析与样式区间生成全部由 markup.render 完成。
        rendered_text = render(text, style, emoji=emoji, emoji_variant=emoji_variant)
        # 【要点】排版参数在解析之后贴上去：它们不参与标记解析。
        rendered_text.justify = justify
        rendered_text.overflow = overflow
        rendered_text.end = end
        return rendered_text

    # 【职责】把含 ANSI 转义序列的字符串解析成带样式的 Text。
    # 【功能】用 AnsiDecoder 解码每一行，再用一个“换行 joiner”把它们连接起来，
    #   从而在保留颜色的同时得到多行文本。
    # 【算法】
    #   1) 先构造一个内容为 "\n" 的 Text 作为分隔符，并把所有排版参数挂在它身上；
    #   2) AnsiDecoder().decode(text) 逐行产出行 Text；
    #   3) 用 joiner.join(...) 连接：join 会把分隔符的排版参数带到结果上
    #      （blank_copy 复制了 justify/overflow/no_wrap/end/tab_size/style）。
    # 【参数】text：含 ANSI 转义码的字符串。style：基础样式。
    #   tab_size 默认 8（注意签名里是 8 而不是 None，与构造函数不同）。
    # 【返回】新的 Text 实例。
    # 【注意】ANSI 转义序列会被消费掉，不会残留在 plain 里；未被识别的序列会被丢弃。
    # 【易错】用 join 而不是 "\n".join(...) 是为了保留多行样式；直接拼接字符串会丢掉
    #   每行的 span。
    @classmethod
    def from_ansi(
        cls,
        text: str,
        *,
        style: Union[str, Style] = "",
        justify: Optional["JustifyMethod"] = None,
        overflow: Optional["OverflowMethod"] = None,
        no_wrap: Optional[bool] = None,
        end: str = "\n",
        tab_size: Optional[int] = 8,
    ) -> "Text":
        """Create a Text object from a string containing ANSI escape codes.

        Args:
            text (str): A string containing escape codes.
            style (Union[str, Style], optional): Base style for text. Defaults to "".
            justify (str, optional): Justify method: "left", "center", "full", "right". Defaults to None.
            overflow (str, optional): Overflow method: "crop", "fold", "ellipsis". Defaults to None.
            no_wrap (bool, optional): Disable text wrapping, or None for default. Defaults to None.
            end (str, optional): Character to end text with. Defaults to "\\\\n".
            tab_size (int): Number of spaces per tab, or ``None`` to use ``console.tab_size``. Defaults to None.
        """
        from .ansi import AnsiDecoder

        # 【要点】用“内容为换行符”的 Text 当分隔符：join 会把它携带的排版参数
        # （justify/overflow/no_wrap/end/tab_size/style）复制到结果上。
        joiner = Text(
            "\n",
            justify=justify,
            overflow=overflow,
            no_wrap=no_wrap,
            end=end,
            tab_size=tab_size,
            style=style,
        )
        decoder = AnsiDecoder()
        # 【要点】decode 是生成器：逐行解码 ANSI，再由 join 拼成单个 Text。
        result = joiner.join(line for line in decoder.decode(text))
        return result

    # 【职责】构造“整段预先着色”的 Text。
    # 【功能】先建空文本，再用 stylize 把样式作为 span 加上去。
    # 【算法】cls(text, ...) 构造后立即 stylize(style)，因此样式成为一条 span 而不是基础样式。
    # 【参数】text：纯文本（**不是 markup**，注意 docstring 里的描述与实际实现不一致，
    #   实际走的是 Text(text) 构造，不做标记解析）。style：要应用的样式。
    # 【返回】整段被一条 span 覆盖的 Text。
    # 【为什么】用 span 而不是 self.style：docstring 明确说明——以这种方式应用的样式
    #   在两端对齐（justify="full"）时**不会**被用来填充空白，因为它只覆盖正文区间。
    # 【易错】style 为空时 stylize 里的 `if style:` 会直接跳过，不产生任何 span。
    @classmethod
    def styled(
        cls,
        text: str,
        style: StyleType = "",
        *,
        justify: Optional["JustifyMethod"] = None,
        overflow: Optional["OverflowMethod"] = None,
    ) -> "Text":
        """Construct a Text instance with a pre-applied styled. A style applied in this way won't be used
        to pad the text when it is justified.

        Args:
            text (str): A string containing console markup.
            style (Union[str, Style]): Style to apply to the text. Defaults to "".
            justify (str, optional): Justify method: "left", "center", "full", "right". Defaults to None.
            overflow (str, optional): Overflow method: "crop", "fold", "ellipsis". Defaults to None.

        Returns:
            Text: A text instance with a style applied to the entire string.
        """
        # 【要点】先构造（不带 style），再 stylize：样式成为一条 span 而不是 base style，
        # 因此两端对齐补出来的空格不会继承它。
        styled_text = cls(text, justify=justify, overflow=overflow)
        styled_text.stylize(style)
        return styled_text

    # 【职责】按片段列表拼装一个 Text，是 markup.render 的实现基石。
    # 【功能】把若干 str / Text / (str, style) 元组依次追加，再挂上基础样式与元数据。
    # 【算法】先构造空的 Text（带着 style 与排版参数），再遍历 parts：
    #   str 或 Text 直接 append；二元组则拆成 append(text, style)。
    # 【参数】*parts：变长位置参数，元素为 str、Text 或 (str, style) 元组。
    #   style/justify/overflow/no_wrap/end/tab_size：挂在结果上的排版属性。
    #   meta：可选的元数据字典（用于 Textual 的事件处理器），非空时经 apply_meta
    #   转成带 meta 的 Style 并 stylize 到整段。
    # 【返回】新的 Text 实例。
    # 【注意】tab_size 的默认值是 8（构造函数的默认值是 None，两者不同）。
    # 【易错】`append(*part)` 依赖元组恰好是 (text, style) 两元；传三元组会因参数过多报错。
    # 【优化】用局部名 append/_Text 避免循环内的属性与全局查找，这是本文件一贯的微优化风格。
    @classmethod
    def assemble(
        cls,
        *parts: Union[str, "Text", Tuple[str, StyleType]],
        style: Union[str, Style] = "",
        justify: Optional["JustifyMethod"] = None,
        overflow: Optional["OverflowMethod"] = None,
        no_wrap: Optional[bool] = None,
        end: str = "\n",
        tab_size: int = 8,
        meta: Optional[Dict[str, Any]] = None,
    ) -> "Text":
        """Construct a text instance by combining a sequence of strings with optional styles.
        The positional arguments should be either strings, or a tuple of string + style.

        Args:
            style (Union[str, Style], optional): Base style for text. Defaults to "".
            justify (str, optional): Justify method: "left", "center", "full", "right". Defaults to None.
            overflow (str, optional): Overflow method: "crop", "fold", "ellipsis". Defaults to None.
            no_wrap (bool, optional): Disable text wrapping, or None for default. Defaults to None.
            end (str, optional): Character to end text with. Defaults to "\\\\n".
            tab_size (int): Number of spaces per tab, or ``None`` to use ``console.tab_size``. Defaults to None.
            meta (Dict[str, Any], optional). Meta data to apply to text, or None for no meta data. Default to None

        Returns:
            Text: A new text instance.
        """
        # 【要点】先建空 Text 并挂上排版属性，再把各片段追加进来。
        text = cls(
            style=style,
            justify=justify,
            overflow=overflow,
            no_wrap=no_wrap,
            end=end,
            tab_size=tab_size,
        )
        append = text.append
        _Text = Text
        for part in parts:
            # 【要点】str/Text 直接追加；否则视为 (内容, 样式) 二元组拆包。
            if isinstance(part, (_Text, str)):
                append(part)
            else:
                append(*part)
        # 【要点】meta 非空时经 apply_meta 转成一条元数据样式覆盖全文。
        if meta:
            text.apply_meta(meta)
        return text

    # 【职责】读写纯文本内容，同时维护 _length 与 span 的一致性。
    # 【功能】（读）把分块列表合并成一个字符串并缓存；（写）替换内容并同步长度与 span。
    # 【算法】读：若 _text 有多块则就地合并成单块（惰性 join）；写：先去控制码，
    #   再整体替换 `_text[:]`，更新 _length；若文本变短则调用 _trim_spans 裁掉越界 span。
    # 【返回】读属性返回 str；写属性返回 None。
    # 【为什么】读时用 `self._text[:] = [...]` 而不是 `self._text = [...]`：
    #   切片赋值保持**列表对象本身**不变，而 join()/append() 等地方可能已经持有
    #   该列表的绑定方法（如 extend_text），换掉对象会让那些引用失效。
    # 【注意】读取 plain 是“有副作用的缓存填充”（会改写 _text），但在语义上是幂等的。
    # 【易错】写入时只在“变短”的情况下裁剪 span：文本变长时旧 span 仍然合法，
    #   不需要处理；这一不对称是有意为之。
    # 【易错】赋值前先比较 new_text != self.plain，若相同则什么都不做（保留样式）。
    @property
    def plain(self) -> str:
        """Get the text as a single string."""
        if len(self._text) != 1:
            self._text[:] = ["".join(self._text)]
        return self._text[0]

    # 【职责】整体替换纯文本内容。
    # 【要点】属性名与方法名相同（plain），靠 @plain.setter 装饰器区分读写。
    @plain.setter
    def plain(self, new_text: str) -> None:
        """Set the text to a new value."""
        # 【要点】内容没变就提前返回：避免无谓的净化与（可能的）span 裁剪。
        if new_text != self.plain:
            sanitized_text = strip_control_codes(new_text)
            self._text[:] = [sanitized_text]
            old_length = self._length
            self._length = len(sanitized_text)
            # 【要点】只有当文本变短时，原有 span 才可能越界，需要裁剪。
            if old_length > self._length:
                self._trim_spans()

    # 【职责】暴露样式区间的读写接口。
    # 【功能】（读）返回内部的 span 列表引用；（写）用副本替换内部列表。
    # 【返回】读属性返回 List[Span]（**是内部对象本身，不是副本**）。
    # 【易错】读属性返回引用，因此 `text.spans.append(...)` 会直接改动 Text 的样式；
    #   这是有意提供的能力（方便批量处理），但也意味着调用方要自己承担副作用。
    # 【注意】写属性用 `spans[:]` 复制，与构造函数的 `spans or []` 不同：
    #   赋值时不会与调用方共享列表，改造顺序上的不一致属于历史包袱。
    @property
    def spans(self) -> List[Span]:
        """Get a reference to the internal list of spans."""
        return self._spans

    # 【要点】赋值时复制一份，避免外部列表后续被修改而影响本对象。
    @spans.setter
    def spans(self, spans: List[Span]) -> None:
        """Set spans."""
        self._spans = spans[:]

    # 【功能】返回一个“只复制排版元数据、不带文本与样式”的新 Text，可指定初始文本。
    # 【算法】重新调用构造函数，把 style/justify/overflow/no_wrap/end/tab_size 复制过去，
    #   但**不**复制 _spans。
    # 【参数】plain：新对象的初始文本，默认空串。
    # 【返回】新的 Text（样式区间为空）。
    # 【为什么】join/with_indent_guides 等需要“一个干净的目标对象 + 原对象的排版设置”，
    #   这个方法是它们的公共构造步骤。
    # 【注意】_length 由构造函数依据 plain 重新计算，无需手动同步。
    def blank_copy(self, plain: str = "") -> "Text":
        """Return a new Text instance with copied metadata (but not the string or spans)."""
        # 【要点】blank_copy 复制“排版元数据”而不复制文本与样式。
        copy_self = Text(
            plain,
            style=self.style,
            justify=self.justify,
            overflow=self.overflow,
            no_wrap=self.no_wrap,
            end=self.end,
            tab_size=self.tab_size,
        )
        return copy_self

    # 【功能】完整复制一个 Text：文本、span 与全部排版属性。
    # 【算法】用 self.plain 重新构造（顺带把分块合并成一块、并重新净化），
    #   再用 `copy_self._spans[:] = self._spans` 把 span 列表整体拷进去。
    # 【返回】独立的新 Text；修改它不影响原对象。
    # 【注意】复制时会走构造函数，因此 plain 会被 strip_control_codes 再处理一次——
    #   由于内部文本本已净化，这一步是幂等的。
    # 【易错】用切片赋值而不是 `copy_self._spans = self._spans`：后者会让两个 Text
    #   共享同一个列表对象，一次 stylize 就会“传染”。
    def copy(self) -> "Text":
        """Return a copy of this instance."""
        # 【要点】用 self.plain 重新构造：顺带把分块合并、并再次净化（幂等）。
        copy_self = Text(
            self.plain,
            style=self.style,
            justify=self.justify,
            overflow=self.overflow,
            no_wrap=self.no_wrap,
            end=self.end,
            tab_size=self.tab_size,
        )
        # 【易错】切片赋值而不是直接赋值属性：直接赋值会让两个 Text 共享同一个列表。
        copy_self._spans[:] = self._spans
        return copy_self

    # 【职责】给一段区间追加样式，是 Text 最主要的“上色”入口。
    # 【功能】把 (start, end, style) 作为一条**新 span 追加到列表末尾**。
    # 【算法】
    #   1) 空样式直接忽略（不产生无意义的 span）；
    #   2) 起止下标按 Python 习惯支持负数（-1 表示最后一个字符），统一换算成正下标；
    #   3) end 为 None 表示“到文本末尾”；
    #   4) 越界或非法区间（start >= length 或 end <= start）直接返回，不报错；
    #   5) 追加 Span(start, min(length, end), style)。
    # 【参数】style：样式名或 Style 对象（假值表示不做事）。start：起始字符下标。
    #   end：结束字符下标（不含），None 表示到末尾。
    # 【返回】None（就地修改）。
    # 【注意】区间用的是**字符下标**而非 cell 宽度；宽字符在这里仍只占一个下标。
    # 【易错】样式是“追加”而不是“覆盖”：后加的 span 在渲染时先入栈而优先，
    #   但旧 span 依旧存在，因此 stylize 多次会让 span 列表线性增长（不会合并）。
    # 【易错】越界区间是静默忽略的（不抛异常），拼错下标不会有任何提示。
    def stylize(
        self,
        style: Union[str, Style],
        start: int = 0,
        end: Optional[int] = None,
    ) -> None:
        """Apply a style to the text, or a portion of the text.

        Args:
            style (Union[str, Style]): Style instance or style definition to apply.
            start (int): Start offset (negative indexing is supported). Defaults to 0.
            end (Optional[int], optional): End offset (negative indexing is supported), or None for end of text. Defaults to None.
        """
        # 【要点】空样式视为“不做事”，避免生成永远无效的 span。
        if style:
            length = len(self)
            # 【要点】负下标换算：-1 → length-1，与 Python 序列语义一致。
            if start < 0:
                start = length + start
            if end is None:
                end = length
            if end < 0:
                end = length + end
            # 【易错】这里用 end <= start 判定“空区间”并直接返回，所以给同一位置
            # 上样式不会有任何效果（零宽 span 被主动丢弃）。
            if start >= length or end <= start:
                # Span not in text or not valid
                return
            # 【要点】min(length, end) 把超过末尾的 end 钳制住，保证不变量成立。
            self._spans.append(Span(start, min(length, end), style))

    # 【功能】与 stylize 相同，但把新 span 插到列表**开头**，使其优先级最低。
    # 【算法】下半段的区间归一化逻辑与 stylize 完全一致；唯一区别是
    #   用 self._spans.insert(0, ...) 而不是 append。
    # 【为什么】渲染时样式按 span 顺序入栈、后入栈者覆盖先入栈者；插到最前面意味着
    #   “这是一层底色，允许后面的样式覆盖我”。典型用途是 markup 里给外层标签打底。
    # 【注意】insert(0, ...) 需要移动整个列表，是 O(n) 操作；对 span 数量很大的文本
    #   反复调用会有性能影响（正常使用中 span 数量很小，可忽略）。
    # 【易错】插入顺序与调用顺序相反：连续两次 stylize_before 会让最后调用者排在最前，
    #   也即优先级最低。
    def stylize_before(
        self,
        style: Union[str, Style],
        start: int = 0,
        end: Optional[int] = None,
    ) -> None:
        """Apply a style to the text, or a portion of the text. Styles will be applied before other styles already present.

        Args:
            style (Union[str, Style]): Style instance or style definition to apply.
            start (int): Start offset (negative indexing is supported). Defaults to 0.
            end (Optional[int], optional): End offset (negative indexing is supported), or None for end of text. Defaults to None.
        """
        if style:
            length = len(self)
            if start < 0:
                start = length + start
            if end is None:
                end = length
            if end < 0:
                end = length + end
            if start >= length or end <= start:
                # Span not in text or not valid
                return
            self._spans.insert(0, Span(start, min(length, end), style))

    # 【职责】把元数据（如 Textual 的事件处理器）挂到文本上。
    # 【功能】用 Style.from_meta(meta) 造一个纯元数据样式，再作为 span 覆盖指定区间。
    # 【算法】完全委托给 stylize：元数据被编码进 Style 的 meta 字段，
    #   因此“元数据”与“样式”共用同一套区间机制。
    # 【参数】meta：元数据字典。start/end：与 stylize 相同的区间参数。
    # 【返回】None（就地修改）。
    # 【注意】元数据不产生任何视觉差异，只对理解 meta 的下游（Textual）有意义；
    #   它同样遵循“后加优先”的叠加顺序。
    def apply_meta(
        self, meta: Dict[str, Any], start: int = 0, end: Optional[int] = None
    ) -> None:
        """Apply metadata to the text, or a portion of the text.

        Args:
            meta (Dict[str, Any]): A dict of meta information.
            start (int): Start offset (negative indexing is supported). Defaults to 0.
            end (Optional[int], optional): End offset (negative indexing is supported), or None for end of text. Defaults to None.

        """
        # 【要点】元数据通过 Style.from_meta 转成样式对象，再交给 stylize 处理区间。
        style = Style.from_meta(meta)
        self.stylize(style, start=start, end=end)

    # 【职责】为整段文本注册事件处理器（Textual 项目的用法）。
    # 【功能】把关键字参数加上 "@" 前缀后写进 meta，并用它 stylize 整段文本。
    # 【算法】meta 缺省为空字典 → 把 handlers 转成形如 "@click": "..." 的键值对
    #   → Style.from_meta → stylize（覆盖全文）。
    # 【参数】meta：既有元数据字典（会被就地更新！）。**handlers：任意关键字参数，
    #   键名会被加上 "@" 前缀。
    # 【返回】self，以便链式调用（`text.on(click="x").stylize("bold")`）。
    # 【易错】当调用方传入 meta 时，本方法会 `meta.update(...)` **直接修改调用方的字典**，
    #   而不是复制一份；若不想被改动，请传 None 或自己传副本。
    # 【注意】它总作用于整段文本，没有区间参数，粒度比 apply_meta 粗。
    def on(self, meta: Optional[Dict[str, Any]] = None, **handlers: Any) -> "Text":
        """Apply event handlers (used by Textual project).

        Example:
            >>> from rich.text import Text
            >>> text = Text("hello world")
            >>> text.on(click="view.toggle('world')")

        Args:
            meta (Dict[str, Any]): Mapping of meta information.
            **handlers: Keyword args are prefixed with "@" to defined handlers.

        Returns:
            Text: Self is returned to method may be chained.
        """
        # 【易错】传入了 meta 时会就地更新调用方的字典（不是复制）。
        meta = {} if meta is None else meta
        # 【要点】关键字参数统一加 "@" 前缀，形成 Textual 约定的事件处理器键。
        meta.update({f"@{key}": value for key, value in handlers.items()})
        # 【要点】元数据被编码进 Style 的 meta 字段，因此复用样式区间机制。
        self.stylize(Style.from_meta(meta))
        return self

    # 【功能】如果文本以 suffix 结尾，就把它删掉（连同覆盖该后缀的样式空间）。
    # 【算法】先 endswith 判断，再复用 right_crop 裁掉相应字符数。
    # 【参数】suffix：要删除的后缀字符串。
    # 【返回】None（就地修改）；不匹配时什么都不做。
    # 【注意】只删一次（最末尾的那一次出现），不是全局替换。
    # 【为什么】复用 right_crop：它同时负责收缩 span 与更新 _length，
    #   避免在这里重复实现一遍“裁剪 + 平移”逻辑。
    def remove_suffix(self, suffix: str) -> None:
        """Remove a suffix if it exists.

        Args:
            suffix (str): Suffix to remove.
        """
        # 【要点】先判断再裁剪：right_crop 会连带收缩覆盖该后缀的 span。
        if self.plain.endswith(suffix):
            self.right_crop(len(suffix))

    # 【职责】查询“某个字符位置最终生效的样式”，供两端对齐（justify="full"）时
    #   给补出来的空格染色。
    # 【功能】把基础样式与所有覆盖该位置的 span 样式合并成一个 Style。
    # 【算法】
    #   1) 负偏移按 Python 习惯换算成正偏移；
    #   2) 取出基础样式并 **copy()**（关键：不能就地修改 self.style）；
    #   3) 遍历所有 span，凡是覆盖 offset 的就把其样式用 `+=` 叠加进来；
    #   4) 返回合并后的样式。
    # 【参数】console：用来解析样式名（console.get_style 带主题与缓存）。
    #   offset：字符下标，支持负数（-1 表示最后一个字符）。
    # 【返回】Style 实例（新建的，修改它不影响 Text）。
    # 【注意】合并顺序是 span 列表顺序，与 render 的“后入栈优先”语义一致。
    #   但 render 会按 (偏移, 是否离开) 排序后用**活动栈**合并，而这里是线性叠加所有
    #   覆盖该点的 span——两种实现只在 span 相互重叠时需对照理解。
    # 【性能】每次调用都要遍历全部 span，且 console.get_style 会解析样式字符串；
    #   调用方（containers.Lines.justify 的 full 分支）在每个词的两端各调一次，
    #   因此大文本的全对齐会比较慢（源码里的 TODO 注释也指出了这一点）。
    # 【兼容】default="" 表示 span 样式为空时不做任何改变（否则会被解析成空样式覆盖）。
    def get_style_at_offset(self, console: "Console", offset: int) -> Style:
        """Get the style of a character at give offset.

        Args:
            console (~Console): Console where text will be rendered.
            offset (int): Offset in to text (negative indexing supported)

        Returns:
            Style: A Style instance.
        """
        # 【注意】作者自己标注了性能问题：每次调用都遍历全部 span 并解析样式，
        # 目前仅被容器层的“两端对齐”使用（补空格时需要知道两侧的样式）。
        # TODO: This is a little inefficient, it is only used by full justify
        # 【要点】负偏移按 Python 习惯换算（-1 表示最后一个字符）。
        if offset < 0:
            offset = len(self) + offset
        get_style = console.get_style
        # 【易错】必须 .copy()：Style 是不可变对象的“可叠加值”，直接 += 会返回新对象，
        # 但这里要是漏掉 copy 而后续改用就地修改，就会污染 Text.style。
        style = get_style(self.style).copy()
        for start, end, span_style in self._spans:
            # 【要点】判断“该 span 是否覆盖 offset”，区间同样左闭右开。
            if end > offset >= start:
                # 【要点】default="" 表示解析失败时不改变已有样式（空样式与 null 等价）。
                style += get_style(span_style, default="")
        return style

    # 【职责】在文本末尾追加空格，并让这些空格继承“最后一个字符”的样式。
    # 【功能】按需延长 span 或直接追加文本，用于 expand_tabs 补足制表位。
    # 【算法】
    #   - spaces <= 0 直接返回；
    #   - 有 span 时：先把所有 end 到达原文本末尾的 span 用 extend 拉长 spaces，
    #     再把空格块 append 到 _text、把 _length 加 spaces；
    #   - 没有 span 时：走 plain 属性追加（会触发惰性合并）。
    # 【参数】spaces：要追加的空格数（字符数，同时也是 cell 数，因为空格宽度为 1）。
    # 【返回】None（就地修改）。
    # 【注意】只延长“end >= end_offset”的 span——即只影响延伸到文本末尾的那几层样式；
    #   中间位置结束的 span 不应被拉长，否则会在文本内凭空多出一段彩色空格。
    # 【易错】end_offset 必须在追加之前取（len(self)），追加后再算就会把新空格也算进去。
    def extend_style(self, spaces: int) -> None:
        """Extend the Text given number of spaces where the spaces have the same style as the last character.

        Args:
            spaces (int): Number of spaces to add to the Text.
        """
        # 【要点】非正数直接返回：没有任何空格需要补。
        if spaces <= 0:
            return
        spans = self.spans
        new_spaces = " " * spaces
        # 【要点】有 span 时走“延长 + 直接追加块”的路径，避免 plain 的合并与净化开销。
        if spans:
            # 【易错】end_offset 必须在追加空格**之前**取得，否则新追加的空格会被算进
            # “原文本末尾”，导致判定失真。
            end_offset = len(self)
            self._spans[:] = [
                # 【要点】只有延伸到原末尾的 span 才被拉长，中间的 span 保持不变。
                span.extend(spaces) if span.end >= end_offset else span
                for span in spans
            ]
            self._text.append(new_spaces)
            self._length += spaces
        else:
            # 【要点】没有 span 时直接拼字符串（会触发惰性合并），无需关心样式。
            self.plain += new_spaces

    # 【职责】用正则给文本批量加样式，是 syntax/traceback 等着色的基础原语。
    # 【功能】对正则的每个匹配（以及每个命名分组）追加一条 span。
    # 【算法】
    #   1) 若传入的是字符串，先 re.compile；
    #   2) 对每个 match：若给了 style，则对“整个匹配区间”加样式——
    #      style 可调用时用匹配到的文本求值（返回 None 表示该处不加样式）；
    #   3) 累加匹配计数；
    #   4) 遍历 match.groupdict()（**只处理命名分组**），把分组名当作样式名
    #      （可加 style_prefix 前缀）加到该分组覆盖的区间上。
    # 【参数】re_highlight：正则对象或字符串。style：整体匹配的样式，或
    #   (匹配文本) -> 样式 的回调。style_prefix：命名分组样式名的前缀，
    #   用于让分组名映射到已经注册好的样式（例如 "py." 前缀）。
    # 【返回】int：匹配到的次数（不含分组数）。
    # 【注意】未命名分组不会被处理，只有 groupdict() 里的命名分组才会生成 span。
    # 【易错】分组区间用 `start != -1` 判断“参与匹配”：未参与的可选分组
    #   match.span(name) 返回 (-1, -1)，必须过滤，否则会生成越界的 span。
    def highlight_regex(
        self,
        re_highlight: Union[Pattern[str], str],
        style: Optional[Union[GetStyleCallable, StyleType]] = None,
        *,
        style_prefix: str = "",
    ) -> int:
        """Highlight text with a regular expression, where group names are
        translated to styles.

        Args:
            re_highlight (Union[re.Pattern, str]): A regular expression object or string.
            style (Union[GetStyleCallable, StyleType]): Optional style to apply to whole match, or a callable
                which accepts the matched text and returns a style. Defaults to None.
            style_prefix (str, optional): Optional prefix to add to style group names.

        Returns:
            int: Number of regex matches
        """
        count = 0
        # 【优化】缓存 append 绑定方法与 Span 局部名，避免循环内的属性查找。
        append_span = self._spans.append
        _Span = Span
        plain = self.plain
        # 【兼容】允许直接传正则字符串：内部编译一次，避免调用方重复 compile。
        if isinstance(re_highlight, str):
            re_highlight = re.compile(re_highlight)
        for match in re_highlight.finditer(plain):
            # 【优化】把 bound method 取到局部，循环里多次调用（整体匹配 + 各分组）。
            get_span = match.span
            if style:
                start, end = get_span()
                # 【要点】style 可调用时按匹配文本求值；返回 None 表示“这次不打样式”。
                match_style = style(plain[start:end]) if callable(style) else style
                # 【易错】必须同时检查 style 非 None 与区间非空，否则会加入零宽 span。
                if match_style is not None and end > start:
                    append_span(_Span(start, end, match_style))

            count += 1
            # 【要点】只遍历 groupdict（命名分组）：分组名即样式名（可带前缀）。
            for name in match.groupdict().keys():
                start, end = get_span(name)
                # 【易错】未参与匹配的可选分组返回 (-1, -1)，必须过滤，
                # 否则会生成负下标的 span（后续切分时表现为越界）。
                if start != -1 and end > start:
                    append_span(_Span(start, end, f"{style_prefix}{name}"))
        return count

    # 【职责】把一组关键词标成同一种样式（例如给日志里的关键字加粗）。
    # 【功能】用 "|" 把关键词拼成正则，逐个匹配并加 span，返回命中次数。
    # 【算法】re.escape 逐个转义后 join 成 alternation，再用 finditer 扫描全串；
    #   case_sensitive 为假时加 re.IGNORECASE。
    # 【参数】words：要匹配的词序列。style：统一应用的样式。
    #   case_sensitive：是否区分大小写（对 Unicode 文本同样生效）。
    # 【返回】int：匹配到的次数（同一位置重复出现的关键词会各自计数）。
    # 【注意】模式没有加词边界，因此是**子串匹配**："cat" 也会命中 "concatenate"。
    # 【易错】words 为空（或全部转义后得不到有效分支）时 re_words 是空串，
    #   而空模式在 re.finditer 下会在每个位置都匹配一次，从而产生一堆零宽 span；
    #   调用方应自行保证 words 非空。
    def highlight_words(
        self,
        words: Iterable[str],
        style: Union[str, Style],
        *,
        case_sensitive: bool = True,
    ) -> int:
        """Highlight words with a style.

        Args:
            words (Iterable[str]): Words to highlight.
            style (Union[str, Style]): Style to apply.
            case_sensitive (bool, optional): Enable case sensitive matching. Defaults to True.

        Returns:
            int: Number of words highlighted.
        """
        # 【要点】关键词逐个转义后拼成 alternation，避免 "a.b" 里的点被当通配符。
        re_words = "|".join(re.escape(word) for word in words)
        add_span = self._spans.append
        count = 0
        _Span = Span
        for match in re.finditer(
            # 【要点】case_sensitive 为假时加 IGNORECASE：flags=0 表示“不加任何标志”。
            re_words, self.plain, flags=0 if case_sensitive else re.IGNORECASE
        ):
            start, end = match.span(0)
            # 【要点】只取第 0 组（整个匹配）；本方法不处理子分组。
            add_span(_Span(start, end, style))
            count += 1
        return count

    # 【功能】去掉文本末尾的空白字符。
    # 【算法】直接走 plain 的 setter：表达式求值顺序保证先算 self.plain.rstrip()，
    #   再由 setter 比较新旧文本、更新长度并在变短时裁剪 span。
    # 【返回】None（就地修改）。
    # 【注意】它使用 str.rstrip 的默认行为——去掉所有空白（空格、制表符、换行等），
    #   而不只是空格；这与 rstrip_end 的“只裁到指定宽度”不同。
    # 【易错】末尾空白被删掉后，覆盖这些空白的 span 会被 _trim_spans 收缩或清除，
    #   这是想要的行为（否则会留下悬空的样式区间）。
    def rstrip(self) -> None:
        """Strip whitespace from end of text."""
        self.plain = self.plain.rstrip()

    # 【职责】换行之后清理“超出可用宽度的行尾空白”。
    # 【功能】若文本长于 size，则至多裁掉超出部分那么多、且只裁空白字符。
    # 【算法】
    #   1) text_length 为字符数；不超过 size 就什么都不做；
    #   2) excess = text_length - size，即超出量；
    #   3) 用 _re_whitespace（r"\s+$"）找出末尾的连续空白块；
    #   4) 裁剪 min(空白块长度, excess) 个字符——既不超过空白块，也不超过超出量。
    # 【参数】size：期望的最大宽度（这里按**字符数**比较，与 wrap 传入的 cell 宽度
    #   在纯 ASCII 下一致；宽字符场景下两者会有差异）。
    # 【返回】None（就地修改）。
    # 【为什么】换行时保留行尾空格会影响后续 justify 的判断与终端上的软换行位置，
    #   所以只把“多出来的那段空白”去掉，而保留必要的对齐空格。
    # 【易错】用的是 search 而不是 match：r"\s+$" 需要靠 $ 定位到串尾，
    #   因此它找到的是末尾那段空白（而不是开头的空白）。
    def rstrip_end(self, size: int) -> None:
        """Remove whitespace beyond a certain width at the end of the text.

        Args:
            size (int): The desired size of the text.
        """
        # 【易错】这里用的是**字符数**与 size 比较；调用方 wrap 传的是 cell 宽度，
        # 纯 ASCII 下等价，含宽字符时行为会与“按宽度裁剪”略有差异。
        text_length = len(self)
        if text_length > size:
            excess = text_length - size
            # 【要点】找出文本末尾连续的空白块（r"\s+$"）。
            whitespace_match = _re_whitespace.search(self.plain)
            if whitespace_match is not None:
                whitespace_count = len(whitespace_match.group(0))
                # 【要点】取“空白块长度”与“超出量”的较小值：既不会删掉非空白字符，
                # 也不会删得比需要的更多。
                self.right_crop(min(whitespace_count, excess))

    # 【职责】把文本强行调整到指定长度（多退少补），用于把每行拉齐到固定宽度。
    # 【功能】比目标短就右补空格，比目标长就右侧裁剪。
    # 【算法】比较 len(self) 与 new_length，分别调用 pad_right / right_crop。
    # 【参数】new_length：目标长度（按**字符数**，不是 cell 宽度）。
    # 【返回】None（就地修改）。
    # 【注意】调用方 fit(width) 传入的 width 在 Rich 的语义里是 cell 宽度；
    #   由于本方法用字符数比较与补齐，含宽字符（CJK/emoji）的文本在 fit 之后
    #   实际显示宽度会小于 width——这是本文件“两套度量”不一致的一个已知落点。
    # 【易错】长度相等时不做任何事，因此不会产生多余的 span 或文本块。
    def set_length(self, new_length: int) -> None:
        """Set new length of the text, clipping or padding is required."""
        # 【易错】length 是**字符数**（len），而调用方 fit 传进来的 width 通常是
        # cell 数：含宽字符时两者不等，这是本方法的一个已知语义偏差。
        length = len(self)
        if length != new_length:
            if length < new_length:
                # 【要点】偏短则右侧补空格到目标长度。
                self.pad_right(new_length - length)
            else:
                # 【要点】偏长则从右侧裁掉多余字符（同时收缩 span）。
                self.right_crop(length - new_length)

    # ==== 【与 Console 的渲染协议】 ====
    # 【职责】实现 Rich 的渲染协议：把自己排版并输出为 Segment 流。
    # 【功能】确定 justify/overflow/tab_size/no_wrap 后换行，再用换行符把各行重新拼成一个
    #   Text，最后交给 render() 输出（这样跨行的样式能一次性处理）。
    # 【算法】
    #   1) tab_size：自身非 None 时优先，否则用 console.tab_size；
    #   2) justify/overflow：优先级为 自身 > options > 模块默认值（DEFAULT_JUSTIFY/
    #      DEFAULT_OVERFLOW）；
    #   3) no_wrap 用 pick_bool 在 自身/options/False 之间取第一个非 None；
    #   4) wrap 得到 Lines，用 Text("\n").join(...) 重新合成单个 Text，
    #      再 render（end 用 self.end，即渲染结束时的结尾串）。
    # 【参数】console：Console 实例（提供 tab_size 与样式解析）。options：ConsoleOptions，
    #   提供 max_width 与调用方的 justify/overflow/no_wrap 偏好。
    # 【返回】Iterable[Segment]（生成器）。
    # 【注意】tab_size or 8：即使 console.tab_size 为 0/None 也会兜底成 8，避免除零。
    # 【为什么】先按行 wrap 再 join 回整体：wrap 需要逐行处理断点，而 render 需要
    #   “一个连续坐标系的 Text”来保证样式跨行正确。
    def __rich_console__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> Iterable[Segment]:
        # 【要点】三级兜底：Text 自身 → Console → 8。
        tab_size: int = console.tab_size if self.tab_size is None else self.tab_size
        # 【要点】`or` 链天然实现了“None/空串都跳过”的优先级选择。
        justify = self.justify or options.justify or DEFAULT_JUSTIFY
        overflow = self.overflow or options.overflow or DEFAULT_OVERFLOW

        lines = self.wrap(
            console,
            options.max_width,
            justify=justify,
            overflow=overflow,
            tab_size=tab_size or 8,
            no_wrap=pick_bool(self.no_wrap, options.no_wrap, False),
        )
        # 【要点】用一个内容为换行的 Text 作为分隔符 join 各行，既保留每行样式，
        #   又把它们重新串成“一个 Text”，坐标连续、便于 render 统一处理。
        all_lines = Text("\n").join(lines)
        # 【要点】end=self.end 决定输出末尾是否追加换行（Text 默认 end="\n"）。
        yield from all_lines.render(console, end=self.end)

    # 【职责】实现测量协议，让父容器能在渲染前知道本 Text 的最小/最大宽度。
    # 【功能】返回 Measurement(最小宽度, 最大宽度)，单位是 cell。
    # 【算法】
    #   - 最大宽度：所有行里最宽的一行（按 cell_len 计算）；
    #   - 最小宽度：所有“词”里最长的词（即无法再折行的最窄宽度）；
    #   - 文本为空（splitlines 无结果）时最大宽度取 0；
    #   - 没有词（例如整段都是空白）时最小宽度退化为最大宽度。
    # 【参数】console/options：协议要求的位置参数，本实现**没有使用**（宽度与样式无关）。
    # 【返回】Measurement 实例。
    # 【注意】测量完全忽略 span 与样式：样式不改变显示宽度，因此无需考虑。
    # 【易错】min 用的是 text.split()（按任意空白切词），所以“一个很长且无空格的
    #   英文段落”最小宽度就等于这段文字本身——布局在窄容器里只能折行或溢出。
    def __rich_measure__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> Measurement:
        text = self.plain
        # 【要点】splitlines 拆出各逻辑行：最大宽度就是最宽的那一行。
        lines = text.splitlines()
        # 【易错】lines 为空（空文本）时 max 会报错，因此显式兜底为 0。
        max_text_width = max(cell_len(line) for line in lines) if lines else 0
        # 【要点】按任意空白切词：最小宽度即“最长单词”，也就是无法再折行的宽度。
        words = text.split()
        # 【易错】没有词（整段空白）时退回最大宽度，否则 max 会再次报错。
        min_text_width = (
            max(cell_len(word) for word in words) if words else max_text_width
        )
        # 【要点】返回 Measurement 而不是元组：它支持与其它测量值求并集（__rich_measure__ 协议）。
        return Measurement(min_text_width, max_text_width)

    # ==== 【Text → Segment：把 span 变成真正的样式】 ====
    # 【职责】把纯文本与 span 列表翻译成 Segment 流，是样式真正“生效”的地方。
    # 【功能】按位置扫描 span 的开启/关闭事件，维护一个活动样式栈，为每一段文字
    #   生成带当前合成样式的 Segment。
    # 【算法】
    #   1) 快速路径：没有任何 span 时只产出一个 Segment（无样式解析开销）；
    #   2) 给每条 span 分配一个从 1 开始的编号，样式先用 console.get_style 解析一次
    #      并存入 style_map；编号 0 保留给基础样式 self.style；
    #   3) 构造事件表：每条 span 产生“进入事件 (start, False, id)”与“离开事件
    #      (end, True, id)”，再加上基础样式在 0 处进入、在 len(text) 处离开；
    #   4) 按 (offset, 是否离开) 排序：同一偏移上“进入”排在“离开”之前，
    #      保证相邻 span 首尾相接时不会出现空档；
    #   5) 用 zip(spans, spans[1:]) 顺序遍历：先更新活动栈，再把
    #      text[offset:next_offset] 用当前合成样式输出为一个 Segment。
    # 【参数】console：用于解析样式名（带主题与缓存）。end：可选结尾串，作为**无样式**
    #   Segment 追加在末尾。
    # 【返回】Iterable[Segment]（生成器）；无 span 时也至少产出一个 Segment。
    # 【注意】样式合成顺序由 `sorted(stack)` 决定，即“span 的编号顺序（=加入顺序）”，
    #   基础样式编号为 0 因此在最底层；这与 stylize 文档所说的“后加优先”一致。
    # 【易错】活动栈用 list.remove(style_id) 出栈（局部别名却叫 stack_pop），
    #   按值删除而非按栈顶删除：因为 span 可以相互嵌套、重叠，离开事件的顺序未必
    #   与进入顺序相反。编号唯一，所以按值删除是安全的。
    def render(self, console: "Console", end: str = "") -> Iterable["Segment"]:
        """Render the text as Segments.

        Args:
            console (Console): Console instance.
            end (Optional[str], optional): Optional end character.

        Returns:
            Iterable[Segment]: Result of render that may be written to the console.
        """
        # 【要点】_Segment = Segment 是热路径别名：本方法是渲染调用最频繁的地方之一。
        _Segment = Segment
        text = self.plain
        # 【快速路径】没有 span 时完全跳过样式解析：直接产出一个无样式 Segment
        #（style 为 None 表示用 console 的当前样式），再按需追加结尾串。
        if not self._spans:
            yield Segment(text)
            if end:
                yield _Segment(end)
            return
        # 【要点】default=Style.null() 让“未定义的样式名”解析成空样式而不是报错；
        # partial 把 console 绑定进去，后续每次解析都不用再传参。
        get_style = partial(console.get_style, default=Style.null())

        # 【要点】编号从 1 开始：0 被保留给基础样式 self.style，
        # 这样基础样式一定是“最先进入”的那一层，优先级最低。
        enumerated_spans = list(enumerate(self._spans, 1))
        # 【优化】每条 span 的样式只解析一次并缓存，避免在下面的事件循环里重复解析字符串。
        style_map = {index: get_style(span.style) for index, span in enumerated_spans}
        style_map[0] = get_style(self.style)

        # 【要点】把每条 span 拆成“进入/离开”两个事件；基础样式在 0 处进入、
        # 在文本末尾离开，因此它覆盖全文。
        spans = [
            (0, False, 0),
            *((span.start, False, index) for index, span in enumerated_spans),
            *((span.end, True, index) for index, span in enumerated_spans),
            (len(text), True, 0),
        ]
        # 【要点】按 (偏移, 是否离开) 排序：同一偏移上 False(进入) 排在 True(离开) 之前，
        # 保证“先开后关”，相邻 span 之间不会出现样式空档。
        spans.sort(key=itemgetter(0, 1))

        # 【要点】活动样式栈里存的是 span 编号（而不是 Style 对象），便于按值移除。
        stack: List[int] = []
        stack_append = stack.append
        # 【易错】别名写成 stack_pop 但指向的其实是 list.remove（按值删除）：
        # 因为 span 可以嵌套/重叠，离开顺序与进入顺序未必相反，不能按栈顶弹。
        stack_pop = stack.remove

        # 【要点】样式组合的缓存：键是“当前活动样式的元组”，值是合成结果。
        style_cache: Dict[Tuple[Style, ...], Style] = {}
        style_cache_get = style_cache.get
        combine = Style.combine

        # 【职责】把当前栈里的所有样式按编号顺序合并成一个 Style。
        def get_current_style() -> Style:
            """Construct current style from stack."""
            # 【要点】sorted(stack) 保证合并顺序等于 span 的加入顺序（基础样式 0 在最前），
            # 从而与 stylize 的“后加优先”规则一致，且不受嵌套结构影响。
            styles = tuple(style_map[_style_id] for _style_id in sorted(stack))
            cached_style = style_cache_get(styles)
            if cached_style is not None:
                return cached_style
            current_style = combine(styles)
            style_cache[styles] = current_style
            return current_style

        # 【算法】成对遍历事件：当前事件决定栈的变化，(offset, next_offset) 决定
        # 本次要输出的文本区间。由于事件已排序，offsets 单调不减，
        # 因此 next_offset == offset 时区间为空（不产出 Segment）。
        for (offset, leaving, style_id), (next_offset, _, _) in zip(spans, spans[1:]):
            if leaving:
                stack_pop(style_id)
            else:
                stack_append(style_id)
            # 【要点】next_offset > offset 才输出：同一位置上的多个事件不产生文字。
            if next_offset > offset:
                yield _Segment(text[offset:next_offset], get_current_style())
        # 【要点】结尾串不带样式（样式应为 None 或默认），避免把正文颜色带到换行上。
        if end:
            yield _Segment(end)

    # ==== 【拼接：join / append 系列】 ====
    # 【职责】以自身为分隔符，把若干 Text 连接成一个 Text（类似 str.join）。
    # 【功能】在每两段之间插入 self 的内容与样式，并平移各段的 span 坐标。
    # 【算法】
    #   1) new_text 是 self 的 blank_copy（复制排版属性但不带内容）；
    #   2) 内层 iter_text 生成器按序产出“内容段 + 分隔符”：只有 self.plain 非空时
    #      才在段与段之间插入分隔符（用 loop_last 判断是否是最后一段）；
    #   3) 遍历时不走 append 方法，而是直接操作 _text/_spans（extend_text/extend_spans），
    #      并为每段自身的 style 生成一条 span；
    #   4) 所有 span 的下标都加上当前 offset——这就是“坐标重定基”；
    #   5) 最后手工设置 new_text._length。
    # 【参数】lines：要连接的 Text 可迭代对象。
    # 【返回】新的 Text。
    # 【为什么】直接操作私有列表而不调用 append：append 会做类型检查与净化，
    #   而这里每段都已经在构造函数里净化过，绕开可以省掉大量重复开销。
    # 【易错】分隔符为空文本时不会插入任何内容（且不会重复 yield self），
    #   但**排版属性仍以 self 为准**，因为 new_text 来自 blank_copy。
    # 【注意】本方法不复制 no_wrap/end/tab_size 的差异——它们已经被 blank_copy 复制，
    #   但各输入行自己的这些属性会被忽略（只取其文本、样式与 span）。
    def join(self, lines: Iterable["Text"]) -> "Text":
        """Join text together with this instance as the separator.

        Args:
            lines (Iterable[Text]): An iterable of Text instances to join.

        Returns:
            Text: A new text instance containing join text.
        """

        new_text = self.blank_copy()

        # 【职责】按“内容段 + 分隔符”交替产出，且只在确实需要时才插入分隔符。
        def iter_text() -> Iterable["Text"]:
            if self.plain:
                # 【要点】loop_last 提供“是否最后一段”的标志，避免在末尾多插一个分隔符。
                for last, line in loop_last(lines):
                    yield line
                    if not last:
                        yield self
            else:
                # 【要点】分隔符为空文本时退化为直接拼接，连 self 都不产出。
                yield from lines

        # 【优化】把三个绑定方法取到局部变量，避免在循环里反复做属性查找。
        extend_text = new_text._text.extend
        append_span = new_text._spans.append
        extend_spans = new_text._spans.extend
        offset = 0
        _Span = Span

        for text in iter_text():
            extend_text(text._text)
            # 【要点】把该段的 base style 物化成一条覆盖全段的 span（与 append 的规则一致）。
            if text.style:
                append_span(_Span(offset, offset + len(text), text.style))
            # 【要点】坐标整体平移 offset，完成各段 span 的重定基。
            extend_spans(
                _Span(offset + start, offset + end, style)
                for start, end, style in text._spans
            )
            offset += len(text)
        # 【要点】手工维护长度不变量：这里逐块拼接，没有走 append，因此必须自己累加。
        new_text._length = offset
        return new_text

    # ==== 【排版辅助：制表符与缩进】 ====
    # 【职责】把制表符就地展开成空格，使后续“字符下标 / cell 宽度”的推理在行内自洽。
    # 【功能】按 tab_size 计算制表位并替换每个 \t。
    # 【算法】
    #   1) 快速路径：没有 \t 直接返回；
    #   2) tab_size 为 None 时先用 self.tab_size，仍为 None 则用 8；
    #   3) 按行（split("\n", include_separator=True)）处理，逐行统计 cell_position；
    #   4) 行内再把 \t 当作分隔符切开（include_separator=True，让每个片段保留自己的 \t）：
    #      - 片段以 \t 结尾：把最后一个字符换成空格（即“先当成 1 列”），
    #        累加 cell_position 后用 `cell_position % tab_size` 算出还需补几个空格，
    #        调用 part.extend_style(spaces) 补齐样式并推进 cell_position；
    #      - 否则直接累加该片段的 cell_len；
    #   5) 用 Text("").join 拼回整体，再把结果写回 self._text/_length/_spans。
    # 【参数】tab_size：制表位宽度（cell 数）；None 表示依次回退到 self.tab_size → 8。
    # 【返回】None（就地修改）。
    # 【注意】位置是按 **cell** 累加的（用 cell_len 而非 len），因此含宽字符的行上
    #   制表位与终端实际对齐一致——这是本方法唯一使用 cell 度量的地方之一。
    # 【易错】step 4 的“先把 \t 变成空格再算余数”很重要：若不先替换，
    #   cell_len 会把 \t 算成 0 宽度，导致补空格数量偏多。
    # 【易错】最后用 `self._length = len(self.plain)` 而不是直接用 cell_position：
    #   长度必须是字符数，而 cell_position 是 cell 数。
    def expand_tabs(self, tab_size: Optional[int] = None) -> None:
        """Converts tabs to spaces.

        Args:
            tab_size (int, optional): Size of tabs. Defaults to 8.

        """
        # 【快速路径】没有制表符就不做任何事（本方法在渲染热路径上常被调用）。
        if "\t" not in self.plain:
            return
        # 【要点】两级回退：参数 → self.tab_size → 8。
        if tab_size is None:
            tab_size = self.tab_size
        if tab_size is None:
            tab_size = 8

        new_text: List[Text] = []
        append = new_text.append

        # 【要点】include_separator=True：把 "\n" 留在每行末尾，拼接后才能还原原文。
        for line in self.split("\n", include_separator=True):
            # 【要点】本行没有制表符时原样保留（不重建对象，省一次分配）。
            if "\t" not in line.plain:
                append(line)
            else:
                # 【要点】cell_position 记录当前行已推进的**显示宽度**（cell）。
                cell_position = 0
                # 【要点】同样带上分隔符，使每个以 \t 结尾的片段自含其制表符。
                parts = line.split("\t", include_separator=True)
                for part in parts:
                    if part.plain.endswith("\t"):
                        # 【要点】先把 \t 就地替换成一个空格：这样 part.cell_len
                        # 会把这一格算成 1，而不是把 \t 算成 0 宽。
                        part._text[-1] = part._text[-1][:-1] + " "
                        cell_position += part.cell_len
                        # 【要点】制表位计算：当前列对 tab_size 取余，余数为 0 表示
                        # 正好在制表位上，无需补空格。
                        tab_remainder = cell_position % tab_size
                        if tab_remainder:
                            spaces = tab_size - tab_remainder
                            # 【要点】补出来的空格要继承该 tab 所在的样式（extend_style
                            # 会把“延伸到末尾的 span”一并拉长）。
                            part.extend_style(spaces)
                            cell_position += spaces
                    else:
                        cell_position += part.cell_len
                    append(part)

        result = Text("").join(new_text)

        # 【要点】把拼接结果写回自身：_text 换成单块，_length 重新按字符数计算
        #（cell_position 是 cell 数，不能直接用作长度）。
        self._text = [result.plain]
        self._length = len(self.plain)
        self._spans[:] = result._spans

    # ==== 【裁剪与填充：cell 度量相关】 ====
    # 【职责】把文本裁到指定宽度（cell），可按 overflow 策略选择裁剪或省略号。
    # 【功能】若当前显示宽度超过 max_width，则按 overflow 处理；可选地补空格到指定宽度。
    # 【算法】
    #   1) _overflow = 参数 → self.overflow → DEFAULT_OVERFLOW（默认 "fold"）；
    #   2) "ignore" 时完全不做处理；
    #   3) 用 cell_len 量出当前宽度，超过 max_width 时：
    #      - "ellipsis"：先裁到 max_width - 1，再拼上 "…"（省略号本身占 1 cell）；
    #      - 其它（crop/fold）：直接 set_cell_size 裁到 max_width；
    #   4) pad 为真且当前宽度小于 max_width 时，右侧补空格到 max_width。
    # 【参数】max_width：最大显示宽度（cell）。overflow：覆盖 overflow 策略。
    #   pad：是否把短行补齐到 max_width。
    # 【返回】None（就地修改）。
    # 【注意】宽度全部按 cell 计算（cell_len/set_cell_size），因此截断 CJK 文本时
    #   不会把一个全角字符切成半个；被写入的是合法的更短字符串。
    # 【易错】"ellipsis" 分支先裁到 max_width - 1 再拼省略号，若 max_width 为 0
    #   会裁到 -1（set_cell_size 会退化成空串），最终得到只有一个省略号的文本。
    # 【易错】ellipsis 分支用 `self.plain = ...` 赋值（会触发 span 裁剪与净化），
    #   而 pad 分支直接改写 self._text 与 self._length；两条路径的副作用不同——
    #   后者不会重新净化，也不会裁剪 span（因为只是变长，span 仍合法）。
    def truncate(
        self,
        max_width: int,
        *,
        overflow: Optional["OverflowMethod"] = None,
        pad: bool = False,
    ) -> None:
        """Truncate text if it is longer that a given width.

        Args:
            max_width (int): Maximum number of characters in text.
            overflow (str, optional): Overflow method: "crop", "fold", or "ellipsis". Defaults to None, to use self.overflow.
            pad (bool, optional): Pad with spaces if the length is less than max_width. Defaults to False.
        """
        # 【要点】三级优先级：参数 → 自身 → 默认（"fold"）。
        _overflow = overflow or self.overflow or DEFAULT_OVERFLOW
        # 【要点】"ignore" 完全不处理：既不裁也不补（宽度可能溢出容器）。
        if _overflow != "ignore":
            # 【要点】用 cell_len 而不是 len：截断必须按显示宽度进行。
            length = cell_len(self.plain)
            if length > max_width:
                if _overflow == "ellipsis":
                    # 【要点】先裁到 max_width - 1 再拼 "…"：省略号自身占 1 cell，
                    # 这样总宽度恰好等于 max_width。
                    self.plain = set_cell_size(self.plain, max_width - 1) + "…"
                else:
                    # 【要点】crop/fold 都走这里：set_cell_size 会保证不切断宽字符。
                    self.plain = set_cell_size(self.plain, max_width)
            # 【易错】length 是截断**之前**量出来的：进入本分支时它必然 <= max_width，
            # 因此这里的判断仍按原宽度决定是否补齐（截断后不会重复补）。
            if pad and length < max_width:
                spaces = max_width - length
                # 【要点】直接改写 _text/_length（不走 plain 的 setter），
                # 因为只是变长，span 仍然合法、也无需重新净化。
                self._text = [f"{self.plain}{' ' * spaces}"]
                self._length = len(self.plain)

    # 【职责】在文本变短之后恢复“span 不越界”这一不变量。
    # 【功能】删除完全越界的 span，并把部分越界的 span 收缩到新的文本末尾。
    # 【算法】max_offset = len(plain)；对每条 span：
    #   - span.start >= max_offset 的直接丢弃（列表推导的 if 过滤）；
    #   - span.end < max_offset 的原样保留；
    #   - 其余（跨越新末尾的）改写成 Span(start, min(max_offset, end), style)。
    # 【返回】None（就地修改）。
    # 【注意】调用时机只有两处：plain 的 setter 在文本变短时、以及……不，仅此一处；
    #   其它裁剪路径（right_crop / _trim 逻辑）各自实现了等价逻辑，没有复用本方法。
    # 【易错】只按 start 过滤、只按 end 收缩：区间是左闭右开，因此 start == max_offset
    #   的 span 已经是空的，会被丢弃——这正是我们想要的（零宽 span 无意义）。
    def _trim_spans(self) -> None:
        """Remove or modify any spans that are over the end of the text."""
        # 【要点】新的文本末尾（开区间），span 的 end 超过它就需要收缩。
        max_offset = len(self.plain)
        _Span = Span
        # 【要点】用切片赋值整体替换 span 列表，保持列表对象身份不变。
        self._spans[:] = [
            (
                # 【要点】完全在范围内的 span 原对象返回（省一次分配）。
                span
                if span.end < max_offset
                # 【要点】跨越末尾的收缩到 max_offset；min 是防御性写法。
                else _Span(span.start, min(max_offset, span.end), span.style)
            )
            for span in self._spans
            # 【易错】start >= max_offset 的 span 已被丢弃——区间左闭右开，
            # 此时它必然是空区间，保留没有意义。
            if span.start < max_offset
        ]

    # 【职责】左右同时补字符（用于盒式布局的内边距）。
    # 【功能】在两侧各补 count 个 character，并把所有 span 右移 count。
    # 【算法】构造 pad_characters 字符串 → 用 plain 的 setter 拼成新文本
    #   → 把所有 span 的 start/end 各加 count（整段右移）。
    # 【参数】count：每侧补齐的**字符**个数。character：用于填充的单字符。
    # 【返回】None（就地修改）。
    # 【约束】assert len(character) == 1：填充字符必须是单个字符，否则宽度计算会失真。
    # 【注意】count 为 0 时不做任何事（连 span 都不动）。
    # 【易错】padding 用的是**字符数**而不是 cell 数：用宽字符（如 "　"）填充时
    #   实际显示宽度会是 count*2，与调用方按 cell 计算的目标不符。
    # 【易错】用 plain 的 setter 赋值会触发"变长不裁剪 span"的分支，因此随后必须
    #   手工平移 span；两处协作缺一不可。
    def pad(self, count: int, character: str = " ") -> None:
        """Pad left and right with a given number of characters.

        Args:
            count (int): Width of padding.
            character (str): The character to pad with. Must be a string of length 1.
        """
        # 【约束】填充字符必须是单个字符，否则宽度计算与 span 平移都会失准。
        assert len(character) == 1, "Character must be a string of length 1"
        # 【要点】count 为 0 时连 span 都不动。
        if count:
            pad_characters = character * count
            # 【要点】走 plain 的 setter：文本变长不会触发 span 裁剪。
            self.plain = f"{pad_characters}{self.plain}{pad_characters}"
            _Span = Span
            # 【要点】整体右移 count：因为左侧插入了 count 个字符。
            self._spans[:] = [
                _Span(start + count, end + count, style)
                for start, end, style in self._spans
            ]

    # 【职责】左填充（右对齐前的准备步骤）。
    # 【功能】在文本左侧插入 count 个 character，并把所有 span 右移 count。
    # 【算法】plain 前拼字符 → 所有 span 坐标 +count。
    # 【参数】count：左侧补齐的字符数。character：单个填充字符。
    # 【返回】None（就地修改）。
    # 【注意】pad 与 pad_left 的差别仅在于是否也补右侧；两者的 span 平移完全一样。
    # 【易错】同样以字符数为单位，宽字符填充字符会造成显示宽度多于预期。
    def pad_left(self, count: int, character: str = " ") -> None:
        """Pad the left with a given character.

        Args:
            count (int): Number of characters to pad.
            character (str, optional): Character to pad with. Defaults to " ".
        """
        assert len(character) == 1, "Character must be a string of length 1"
        if count:
            # 【要点】只在左侧拼接；右侧不动。
            self.plain = f"{character * count}{self.plain}"
            _Span = Span
            # 【要点】与 pad 相同的整体右移逻辑。
            self._spans[:] = [
                _Span(start + count, end + count, style)
                for start, end, style in self._spans
            ]

    # 【职责】右填充（把短行补到目标宽度）。
    # 【功能】在文本右侧插入 count 个 character。
    # 【算法】直接拼接到 plain 上（走 setter）。
    # 【参数】count：右侧补齐的字符数。character：单个填充字符。
    # 【返回】None（就地修改）。
    # 【注意】与 pad_left 不同，这里**不需要**平移 span：右侧追加不会改变已有下标。
    #   这是三个 pad 方法中唯一不用动 span 的。
    # 【易错】新补出来的空格不属于任何 span，因此不会被着色——这正是 Rich 期望的行为
    #   （填充的空白保持默认背景），若需要给填充着色请用 Lines.justify 里的
    #   get_style_at_offset 逻辑或 Text.extend_style。
    def pad_right(self, count: int, character: str = " ") -> None:
        """Pad the right with a given character.

        Args:
            count (int): Number of characters to pad.
            character (str, optional): Character to pad with. Defaults to " ".
        """
        assert len(character) == 1, "Character must be a string of length 1"
        if count:
            self.plain = f"{self.plain}{character * count}"

    # 【职责】把文本对齐到固定宽度（左/中/右）。
    # 【功能】先裁到 width，再按 align 在两侧补空格补足到 width。
    # 【算法】
    #   1) truncate(width)（不带 overflow 参数，因此用 self.overflow → 默认 "fold"）；
    #   2) excess_space = width - cell_len(plain)（现在必然 >= 0）；
    #   3) left：右侧补；center：左侧补 excess//2、右侧补余数；
    #      right（else 分支）：左侧补全部。
    # 【参数】align："left"/"center"/"right" 之一。width：目标显示宽度（cell）。
    #   character：填充字符，默认空格。
    # 【返回】None（就地修改）。
    # 【注意】只有 center 分支会把奇数余量分给右侧，因此居中的文本在差 1 列时靠右偏。
    # 【易错】else 兜底成右对齐：传入非法字符串（字面量类型在运行期不检查）不会报错，
    #   而是被当成 "right" 处理。
    # 【易错】excess_space 为 0 时 if 判断为假，什么都不做；若宽度比较出现浮点式
    #   负值也不可能发生（truncate 已保证不超宽）。
    def align(self, align: AlignMethod, width: int, character: str = " ") -> None:
        """Align text to a given width.

        Args:
            align (AlignMethod): One of "left", "center", or "right".
            width (int): Desired width.
            character (str, optional): Character to pad with. Defaults to " ".
        """
        # 【要点】先裁到目标宽度：这一步保证下面算出的剩余空间非负。
        self.truncate(width)
        # 【要点】按 cell 计算还差多少（不是字符数）。
        excess_space = width - cell_len(self.plain)
        if excess_space:
            if align == "left":
                # 【要点】左对齐：空格全部补在右侧。
                self.pad_right(excess_space, character)
            elif align == "center":
                # 【要点】居中：左半边取整，余数（奇数时为 1）留给右侧，
                # 因此差 1 列时文本会略微偏右。
                left = excess_space // 2
                self.pad_left(left, character)
                self.pad_right(excess_space - left, character)
            else:
                # 【易错】else 兜底为右对齐：任何非法 align 值都会走到这里（字面量类型
                # 在运行期不做检查），不会报错。
                self.pad_left(excess_space, character)

    # 【职责】在末尾追加 str 或 Text，是构建 Text 最常用的方法。
    # 【功能】追加内容，并把新内容对应的样式转成坐标平移后的 span。
    # 【算法】
    #   - 类型守卫：只接受 str/Text，其它类型抛 TypeError；
    #   - 空内容直接跳过（len(text) 为 0 时不产生任何 span）；
    #   - str 分支：净化控制码 → 作为新文本块 append 到 _text → 若给了 style，
    #     追加 Span(旧长度, 旧长度+新长度, style) → 累加 _length；
    #   - Text 分支：若传了 style 则抛 ValueError（避免语义含混）；
    #     另加一段文本 → 把对方的 base style 变成一条 span → 把对方的 span
    #     整体平移 text_length 后 extend 过来 → 累加 _length。
    # 【参数】text：str 或 Text。style：仅对 str 有效，表示这段新文本的样式。
    # 【返回】self，便于链式调用。
    # 【注意】追加 str 时 _length 在最后才更新，而 append Text 时用的是 len(text)，
    #   两者都保持 _length == len(plain) 的不变量。
    # 【易错】向 Text 追加时传 style 会直接报错，而不是默默忽略——这是刻意的设计，
    #   因为“另一种样式”应当通过 stylize 或先包一层 Text 来表达。
    # 【为什么】Text 分支用 `text._spans.copy()`：如果 text 就是 self（自我追加），
    #   不复制的话，一边遍历一边 extend 会让迭代器看见新加入的元素，导致无限循环。
    # 【易错】生成器里的循环变量名 `style` 遮蔽了同名参数：由于参数在 Text 分支里
    #   已被判定必须为 None，遮蔽不会造成实际错误，但阅读时容易误会。
    def append(
        self, text: Union["Text", str], style: Optional[Union[str, "Style"]] = None
    ) -> "Text":
        """Add text with an optional style.

        Args:
            text (Union[Text, str]): A str or Text to append.
            style (str, optional): A style name. Defaults to None.

        Returns:
            Text: Returns self for chaining.
        """

        # 【要点】类型必须显式校验：Text 与 str 的追加语义不同，混入其它类型
        # 只会在更晚的地方以更难诊断的形式出错。
        if not isinstance(text, (str, Text)):
            raise TypeError("Only str or Text can be appended to Text")

        # 【要点】空内容不产生任何副作用（包括不加样式）——空 span 没有意义。
        if len(text):
            if isinstance(text, str):
                # 【要点】与构造函数一致：入口处过滤控制码，保证下标语义成立。
                sanitized_text = strip_control_codes(text)
                self._text.append(sanitized_text)
                # 【要点】offset 必须在追加之后取吗？不必——此刻 _length 尚未更新，
                # 因此它正是“旧长度”，也就是新内容的起始下标。
                offset = len(self)
                text_length = len(sanitized_text)
                if style:
                    self._spans.append(Span(offset, offset + text_length, style))
                self._length += text_length
            elif isinstance(text, Text):
                _Span = Span
                # 【易错】给 Text 再指定 style 是语义冲突（样式应加在 Text 自身上）。
                if style is not None:
                    raise ValueError(
                        "style must not be set when appending Text instance"
                    )
                text_length = self._length
                # 【要点】对方的 base style 在这里降级为一条 span：因为 Text 只能有
                # 一个 base style，拼接后必须把它“物化”成区间样式。
                if text.style:
                    self._spans.append(
                        _Span(text_length, text_length + len(text), text.style)
                    )
                # 【要点】读取 text.plain 可能触发对方分块列表的惰性合并。
                self._text.append(text.plain)
                # 【要点】span 坐标整体平移 text_length，完成“重定基”。
                self._spans.extend(
                    _Span(start + text_length, end + text_length, style)
                    for start, end, style in text._spans.copy()
                )
                self._length += len(text)
        return self

    # 【职责】只针对 Text 的快速追加路径（docstring 说它比 append 更快）。
    # 【功能】与 append 的 Text 分支逻辑相同，但省掉了类型判断与 style 校验。
    # 【算法】取旧长度 → 物化对方的 base style 为 span → 追加 plain →
    #   平移并 extend 对方 span → 累加长度。
    # 【参数】text：要追加的 Text（必须已是 Text，本方法不做类型检查）。
    # 【返回】self，便于链式调用。
    # 【注意】它**不检查空文本**：追加空 Text 也不会产生 span（因为 style 为空、
    #   span 列表为空），因此行为上与 append 一致，只是少了几次判断。
    # 【易错】传 str 会在这里以 AttributeError（访问 .style）的形式失败，
    #   而不会像 append 那样给出清晰的 TypeError。
    # 【为什么】同样用 text._spans.copy() 防止自我追加时无限循环。
    def append_text(self, text: "Text") -> "Text":
        """Append another Text instance. This method is more performant than Text.append, but
        only works for Text.

        Args:
            text (Text): The Text instance to append to this instance.

        Returns:
            Text: Returns self for chaining.
        """
        _Span = Span
        # 【要点】用 _length 而非 len(self)：两者相等，但前者是 O(1) 的属性访问。
        text_length = self._length
        # 【要点】对方的 base style 物化成一条覆盖新内容的 span。
        if text.style:
            self._spans.append(_Span(text_length, text_length + len(text), text.style))
        self._text.append(text.plain)
        # 【要点】平移对方所有 span 后并入；.copy() 用于防止自我追加时无限迭代。
        self._spans.extend(
            _Span(start + text_length, end + text_length, style)
            for start, end, style in text._spans.copy()
        )
        self._length += len(text)
        return self

    # 【职责】批量追加“文本 + 样式”标记流，是语法高亮的输出落地口。
    # 【功能】遍历 tokens，把每段内容追加进 _text，并为其生成一条 span。
    # 【算法】offset 从当前长度开始；对每个 (content, style)：净化内容 → 追加块 →
    #   有样式则追加 Span(offset, offset+len(content), style) → offset 前进；
    #   最后一次性把 _length 设为 offset（而不是逐次累加）。
    # 【参数】tokens：可迭代的 (内容, 样式) 二元组，样式可以是 None。
    # 【返回】self，便于链式调用。
    # 【注意】与 append 的差别：这里用 `self._length = offset` 覆盖赋值，
    #   因此要求 offset 的起点就是进入时的长度——如果 _length 与实际内容不一致
    #   （理论上不该发生），本方法会把不一致“固化”下来。
    # 【易错】空 content 不会产生 span（因为区间宽度为 0），但会被追加进 _text
    #   （与 append 不同：append 对空串会整体跳过；这里 append_text(content)
    #   也只是加了个空块，效果等价）。
    # 【优化】缓存了 append_text/append_span 两个绑定方法，减少循环内的属性查找——
    #   语法高亮会产生成百上千个 token，这点优化是有意义的。
    def append_tokens(
        self, tokens: Iterable[Tuple[str, Optional[StyleType]]]
    ) -> "Text":
        """Append iterable of str and style. Style may be a Style instance or a str style definition.

        Args:
            tokens (Iterable[Tuple[str, Optional[StyleType]]]): An iterable of tuples containing str content and style.

        Returns:
            Text: Returns self for chaining.
        """
        # 【优化】缓存两个 append 绑定方法与 Span 局部名，循环内不再做属性查找。
        append_text = self._text.append
        append_span = self._spans.append
        _Span = Span
        # 【要点】offset 从当前长度开始：token 的样式区间据此平移到本对象的坐标系。
        offset = len(self)
        for content, style in tokens:
            # 【要点】与 append 一致地净化控制码，保证下标语义成立。
            content = strip_control_codes(content)
            append_text(content)
            # 【要点】空样式不产生 span（避免零宽无效区间）。
            if style:
                append_span(_Span(offset, offset + len(content), style))
            offset += len(content)
        # 【要点】一次性覆盖 _length：offset 的最终值就是新长度。
        self._length = offset
        return self

    # 【职责】把另一个 Text 的样式整体“贴”到本对象上（不复制文字）。
    # 【功能】把 text 的 span 列表 extend 到自己的 span 列表里。
    # 【算法】一次列表 extend，**不做任何坐标平移**。
    # 【参数】text：样式来源，docstring 要求它与本对象**等长**。
    # 【返回】None（就地修改）。
    # 【注意】本方法是纯追加：两个 Text 的 span 会共存并叠加，而不是替换。
    # 【易错】没有长度校验（docstring 只是约定）：若来源更长，会产生越界 span，
    #   渲染时 text[offset:next_offset] 就会切出错误的内容或空段。
    def copy_styles(self, text: "Text") -> None:
        """Copy styles from another Text instance.

        Args:
            text (Text): A Text instance to copy styles from, must be the same length.
        """
        # 【要点】纯追加：来源 span 的坐标**不做平移**，因为要求两者等长（同坐标系）。
        self._spans.extend(text._spans)

    # ==== 【分割：split / divide】 ====
    # 【职责】按分隔符把文本切成多个 Text（保留样式），是逐行排版的第一步。
    # 【功能】用 separator 切分，可选是否把分隔符本身留在行里、是否保留末尾空行。
    # 【算法】
    #   1) 断言分隔符非空；
    #   2) 文本里不含分隔符时，直接返回只含一个副本的 Lines；
    #   3) include_separator 为真：用每个匹配的 **end** 作为切分点，
    #      于是分隔符留在前一行的尾部；
    #   4) 否则：用每个匹配的 start 与 end 都作为切分点，从而把分隔符切成独立的一段，
    #      再用 `line.plain != separator` 把这些纯分隔符段过滤掉；
    #   5) 若文本以分隔符结尾且 allow_blank 为假，则弹出最后那个空行。
    # 【参数】separator：分隔字符串（默认 "\n"）。include_separator：把分隔符保留在行尾。
    #   allow_blank：保留结尾产生的空行。
    # 【返回】Lines 容器（内部是 Text 列表）。
    # 【注意】切分完全建立在 divide 之上，因此样式分配的正确性由 divide 保证。
    # 【易错】第 4 步的过滤条件是“整段等于分隔符”，因此文本中间连续出现两个分隔符时
    #   产生的空行（""）**不会**被过滤掉——只有分隔符本身才被丢弃。
    # 【注意】分隔符按字面处理（先 re.escape 再 re.finditer 定位），不是正则。
    # 【易错】Lines.pop() 只在“文本以分隔符结尾”时被调用，与 allow_blank 联动；
    #   若分隔符出现在中间，末尾不会多出空行，也就不会 pop。
    def split(
        self,
        separator: str = "\n",
        *,
        include_separator: bool = False,
        allow_blank: bool = False,
    ) -> Lines:
        """Split rich text in to lines, preserving styles.

        Args:
            separator (str, optional): String to split on. Defaults to "\\\\n".
            include_separator (bool, optional): Include the separator in the lines. Defaults to False.
            allow_blank (bool, optional): Return a blank line if the text ends with a separator. Defaults to False.

        Returns:
            List[RichText]: A list of rich text, one per line of the original.
        """
        # 【约束】空分隔符会让 split 产生无限多的空行，因此在入口处断言。
        assert separator, "separator must not be empty"

        text = self.plain
        # 【快速路径】不含分隔符时无需切分，直接返回一份副本。
        if separator not in text:
            return Lines([self.copy()])

        if include_separator:
            # 【要点】以每个匹配的 **结尾** 作为切分点：分隔符就落在上一行的末尾。
            lines = self.divide(
                match.end() for match in re.finditer(re.escape(separator), text)
            )
        else:

            # 【职责】产出“成对”的切分点：每个匹配的起点与终点各一个，
            # 于是分隔符本身被切成独立的一段，便于随后过滤掉。
            # 【注意】函数名叫 flatten_spans，但它产出的是**偏移量**而不是 span。
            def flatten_spans() -> Iterable[int]:
                for match in re.finditer(re.escape(separator), text):
                    start, end = match.span()
                    yield start
                    yield end

            lines = Lines(
                # 【要点】只丢弃“恰好等于分隔符”的那一段（空行 "" 会被保留）。
                line for line in self.divide(flatten_spans()) if line.plain != separator
            )

        # 【要点】文本以分隔符结尾时会多出一个空行；allow_blank 为假时把它弹出。
        if not allow_blank and text.endswith(separator):
            lines.pop()

        return lines

    # 【职责】按给定偏移把文本切成多行，并把每条 span 正确分配到各行（坐标重定基到 0）。
    # 【功能】返回 len(offsets)+1 个 Text 组成的 Lines。
    # 【算法】
    #   1) 切分点序列为 [0, *offsets, 文本长度]，两两配对得到每行的 [start, end) 区间；
    #   2) 为每个区间构造一个 Text，继承 base style/justify/overflow
    #      （**不继承** no_wrap/end/tab_size）；
    #   3) 没有 span 时到此结束（快速路径）；
    #   4) 否则缓存各行的 _spans.append 方法，并对每条 span 做两次二分查找：
    #      - 第一次找包含 span_start 的行号 start_line_no；
    #      - 第二次从该行继续向右找包含 span_end 的行号 end_line_no；
    #   5) 对 [start_line_no, end_line_no] 区间内的每一行，把 span 裁剪并平移到该行
    #      的局部坐标：new_start = max(0, span_start - line_start)，
    #      new_end = min(span_end - line_start, 行长)；仅当 new_end > new_start 时才追加。
    # 【参数】offsets：切分点（字符下标）的可迭代对象；空时返回整个文本的一份副本。
    # 【返回】Lines；每个新 Text 的 span 坐标都从 0 开始。
    # 【注意】切分点不校验顺序或范围：乱序/越界的 offsets 会产生空行或错位行，
    #   调用方（split/wrap/__getitem__）负责传入规范化的偏移。
    # 【易错】二分查找里用 `span_start > line_end` 判断“在右侧”，其中 line_end 是
    #   该行的**开区间右端**（也就是下一行的起点）；因此恰好落在行边界上的位置会被
    #   归给前一行，随后靠 `new_end > new_start` 把这类零宽结果过滤掉——
    #   这两处必须一起看，否则会以为边界处理有 bug。
    # 【性能】每条 span 两次 O(log L) 二分，加上遍历跨越的行；对每行很短的
    #   长文本（例如 markdown 渲染）这是必要的，否则每条 span 都要扫全部行。
    # 【易错】循环变量 style 遮蔽了上方保存 base style 的同名变量；
    #   由于 base style 在循环前已经用完，行为正确，但阅读时要留意。
    def divide(self, offsets: Iterable[int]) -> Lines:
        """Divide text into a number of lines at given offsets.

        Args:
            offsets (Iterable[int]): Offsets used to divide text.

        Returns:
            Lines: New RichText instances between offsets.
        """
        # 【要点】offsets 是“切分点”而不是“行数”：n 个切分点得到 n+1 行。
        _offsets = list(offsets)

        # 【要点】无切分点时退化为“整段文本的一份副本”。
        if not _offsets:
            return Lines([self.copy()])

        text = self.plain
        text_length = len(text)
        # 【要点】首尾补上 0 与文本长度，把切分点变成完整的行边界序列。
        divide_offsets = [0, *_offsets, text_length]
        # 【要点】相邻边界两两配对即得每行的 [start, end) 区间。
        line_ranges = list(zip(divide_offsets, divide_offsets[1:]))

        # 【要点】先把排版属性取到局部变量：生成器表达式里有屏蔽作用域，
        # 而且下面循环里的 style 是逐条 span 的样式，会遮蔽这里的 base style。
        style = self.style
        justify = self.justify
        overflow = self.overflow
        _Text = Text
        # 【要点】每个新行只带 base style/justify/overflow，不带 no_wrap/end/tab_size。
        new_lines = Lines(
            _Text(
                text[start:end],
                style=style,
                justify=justify,
                overflow=overflow,
            )
            for start, end in line_ranges
        )
        # 【快速路径】没有任何 span 时无需分配样式，直接返回。
        if not self._spans:
            return new_lines

        # 【优化】预先取出每行的 _spans.append 绑定方法，循环内直接调用，
        # 省掉“取属性 + 绑定”的开销（span 多、行多时收益明显）。
        _line_appends = [line._spans.append for line in new_lines._lines]
        line_count = len(line_ranges)
        _Span = Span

        for span_start, span_end, style in self._spans:
            # 【算法】二分查找第一条“可能与 span_start 相交”的行。
            lower_bound = 0
            upper_bound = line_count
            start_line_no = (lower_bound + upper_bound) // 2

            while True:
                line_start, line_end = line_ranges[start_line_no]
                if span_start < line_start:
                    upper_bound = start_line_no - 1
                # 【易错】用 > 而非 >=：恰好在行边界上的位置归给**前一行**，
                # 由此产生的零宽区间会在下面被 new_end > new_start 过滤掉。
                elif span_start > line_end:
                    lower_bound = start_line_no + 1
                else:
                    break
                start_line_no = (lower_bound + upper_bound) // 2

            # 【要点】若 span 的终点也落在同一行内，就只影响这一行。
            if span_end < line_end:
                end_line_no = start_line_no
            else:
                # 【算法】否则从当前行继续向右二分，找终点所在的行。
                # 这里把 lower_bound 也重置为 start_line_no，避免搜索窗口丢失左侧。
                end_line_no = lower_bound = start_line_no
                upper_bound = line_count

                while True:
                    line_start, line_end = line_ranges[end_line_no]
                    if span_end < line_start:
                        upper_bound = end_line_no - 1
                    elif span_end > line_end:
                        lower_bound = end_line_no + 1
                    else:
                        break
                    end_line_no = (lower_bound + upper_bound) // 2

            # 【要点】对跨越的每一行裁剪并平移 span，得到该行局部坐标系下的区间。
            for line_no in range(start_line_no, end_line_no + 1):
                line_start, line_end = line_ranges[line_no]
                # 【要点】max(0, ...) 处理“span 从本行之前开始”的情况；
                # min(..., 行长) 处理“span 在本行之后结束”的情况。
                new_start = max(0, span_start - line_start)
                new_end = min(span_end - line_start, line_end - line_start)
                # 【要点】只追加非空区间：这一步同时过滤掉“行边界上的零宽命中”。
                if new_end > new_start:
                    _line_appends[line_no](_Span(new_start, new_end, style))

        return new_lines

    # 【职责】从右侧删掉若干字符，同时收缩/丢弃越界的 span。
    # 【功能】把文本截短 amount 个字符，并重建 span 列表。
    # 【算法】max_offset = len(plain) - amount；列表推导对每条 span：
    #   - span.start >= max_offset 的丢弃；
    #   - span.end < max_offset 的原样保留；
    #   - 其余的改写成 Span(start, min(max_offset, end), style)；
    #   最后把 _text 换成截短后的单块，并把 _length 减去 amount。
    # 【参数】amount：要从末尾删除的**字符**数，默认 1。
    # 【返回】None（就地修改）。
    # 【注意】不做参数校验：amount 超过文本长度时 max_offset 为负，
    #   所有 span 都会被丢弃，而 self.plain[:-amount] 会得到空串，
    #   _length 可能变成负数——调用方必须保证 amount 合法。
    # 【易错】与 _trim_spans 的逻辑几乎相同（只是阈值不同），两者没有共用实现；
    #   修改其中一处时要注意另一处。
    # 【优化】这里直接操作 _text 而不是走 plain 的 setter，避免了对新文本再做一次
    #   控制码净化与整体替换。
    def right_crop(self, amount: int = 1) -> None:
        """Remove a number of characters from the end of the text."""
        # 【要点】裁剪之后的新末尾（amount 必须 <= 当前长度，本方法不校验）。
        max_offset = len(self.plain) - amount
        _Span = Span
        # 【要点】与 _trim_spans 同样的“保留 / 收缩 / 丢弃”三段逻辑，只是阈值不同。
        self._spans[:] = [
            (
                span
                if span.end < max_offset
                else _Span(span.start, min(max_offset, span.end), span.style)
            )
            for span in self._spans
            if span.start < max_offset
        ]
        # 【要点】文本换成截短后的单块；_text 直接赋值（不做净化，内容本已合法）。
        self._text = [self.plain[:-amount]]
        self._length -= amount

    # ==== 【换行：wrap / fit】 ====
    # 【职责】把一段文本按给定宽度折成多行（word wrap），并逐行应用对齐与溢出策略。
    # 【功能】返回排版好的 Lines。
    # 【算法】对 split 得到的每一行依次执行：
    #   1) 含制表符则先 expand_tabs（制表位依赖 cell 位置，必须在断行前展开）；
    #   2) 若 no_wrap：overflow == "ignore" 时原样保留该行（连 truncate 都不做）；
    #      否则把整行当作一行（不计算断点）；
    #   3) 否则用 _wrap.divide_line 计算断点（fold 决定长单词是否强行拆开），
    #      再用 line.divide(offsets) 切分，并对每个结果行 rstrip_end(width)
    #      去掉多余的行尾空白；
    #   4) 若 wrap_justify 非空，对这批新行执行 Lines.justify（对齐 + 补空格）；
    #   5) 最后逐行 truncate(width) 兜底，保证没有行超宽。
    # 【参数】console：用于样式解析（justify 时需要）。width：每行可用宽度（cell）。
    #   justify/overflow：覆盖本对象与模块默认值的排版策略。
    #   tab_size：展开制表符的宽度。no_wrap：禁用断行。
    # 【返回】Lines（每行一个 Text）。
    # 【注意】no_wrap 的取值用 pick_bool(参数, self.no_wrap, False)，优先级为
    #   “参数 > 自身属性 > False”。
    # 【易错】`or overflow == "ignore"` 比较的是**参数 overflow**而不是上面算好的
    #   wrap_overflow：只有在调用方显式传入 "ignore" 时才会强制 no_wrap；
    #   若仅通过 self.overflow 指定 "ignore"，本条不会生效（但 __rich_console__
    #   总是显式传 overflow，因此正常路径下没问题）。
    # 【易错】第 3 步的 `for line in new_lines` 复用了外层循环变量名，
    #   循环结束后 line 指向最后一条新行；由于外层变量此后不再使用，行为无害但易误读。
    def wrap(
        self,
        console: "Console",
        width: int,
        *,
        justify: Optional["JustifyMethod"] = None,
        overflow: Optional["OverflowMethod"] = None,
        tab_size: int = 8,
        no_wrap: Optional[bool] = None,
    ) -> Lines:
        """Word wrap the text.

        Args:
            console (Console): Console instance.
            width (int): Number of cells available per line.
            justify (str, optional): Justify method: "default", "left", "center", "full", "right". Defaults to "default".
            overflow (str, optional): Overflow method: "crop", "fold", or "ellipsis". Defaults to None.
            tab_size (int, optional): Default tab size. Defaults to 8.
            no_wrap (bool, optional): Disable wrapping, Defaults to False.

        Returns:
            Lines: Number of lines.
        """
        # 【要点】与 __rich_console__ 相同的三级优先级：参数 → 对象自身 → 模块默认。
        wrap_justify = justify or self.justify or DEFAULT_JUSTIFY
        wrap_overflow = overflow or self.overflow or DEFAULT_OVERFLOW

        # 【易错】这里的 overflow 是**原始参数**：若只想通过 self.overflow="ignore"
        # 来禁用换行，本行不会生效（见方法注释）。
        no_wrap = pick_bool(no_wrap, self.no_wrap, False) or overflow == "ignore"

        lines = Lines()
        # 【要点】allow_blank=True：保留原文里的空行（换行符之间的空段）。
        for line in self.split(allow_blank=True):
            # 【要点】制表符必须在断行**之前**展开：断点是按 cell 位置算的，
            # 而 \t 未展开时宽度为 0，会把断点算错。
            if "\t" in line:
                line.expand_tabs(tab_size)
            if no_wrap:
                # 【要点】"ignore" 表示彻底不管宽度：既不折行也不裁剪。
                if overflow == "ignore":
                    lines.append(line)
                    continue
                # 【要点】其余 no_wrap 情况把整行当成一行，后面仍会走 truncate 兜底。
                new_lines = Lines([line])
            else:
                # 【要点】divide_line 只算断点；fold=True 时允许在长单词内部断开。
                offsets = divide_line(str(line), width, fold=wrap_overflow == "fold")
                new_lines = line.divide(offsets)
                # 【易错】这里 line 重用为“新行”：循环结束后 line 不再是原始行。
                for line in new_lines:
                    # 【要点】去掉超出 width 的行尾空白，避免终端上的软换行错位。
                    line.rstrip_end(width)
            if wrap_justify:
                # 【要点】对齐会把行补齐到 width（含补空格），因此必须在 truncate 之前。
                new_lines.justify(
                    console, width, justify=wrap_justify, overflow=wrap_overflow
                )
            # 【要点】最后的兜底：无论走哪条分支，都保证每行不超过 width。
            for line in new_lines:
                line.truncate(width, overflow=wrap_overflow)
            lines.extend(new_lines)
        return lines

    # 【职责】把文本强行切成固定宽度的多行（按宽度硬切，不做单词换行）。
    # 【功能】对 split 得到的每一行调用 set_length(width) 补齐/裁剪，返回 Lines。
    # 【算法】先按换行符 split（默认不保留空行），再逐行 set_length。
    # 【参数】width：每行的目标长度（**set_length 内部按字符数处理**，见其注释）。
    # 【返回】Lines。
    # 【注意】与 wrap 的区别：fit 不计算断点、不做对齐，只做“拉齐到固定宽度”，
    #   常用于需要等宽网格输出的场景（例如导出）。
    # 【易错】width 的语义在这里是混的：调用方通常按 cell 传参，而 set_length 按字符数
    #   处理，含宽字符时会得到比预期更窄的显示宽度。
    def fit(self, width: int) -> Lines:
        """Fit the text in to given width by chopping in to lines.

        Args:
            width (int): Maximum characters in a line.

        Returns:
            Lines: Lines container.
        """
        lines: Lines = Lines()
        append = lines.append
        # 【要点】默认 split()：不保留空行、不保留分隔符，仅按换行切段。
        for line in self.split():
            # 【要点】把每行补齐/裁到 width，从而得到等宽的行。
            line.set_length(width)
            append(line)
        return lines

    # 【职责】猜测代码使用的缩进宽度（for with_indent_guides 的默认值）。
    # 【功能】返回最可能的“每级缩进空格数”。
    # 【算法】
    #   1) 用 r"^( *)(.*)$"（MULTILINE）取出每一行的**行首空格数**，构成集合；
    #   2) 只保留偶数缩进（`not indent % 2`），因为奇数缩进通常是对齐而非层级；
    #   3) 对这些值求最大公约数（reduce(gcd, ...)）；
    #   4) 空集合会让 reduce 抛 TypeError，捕获后返回 1；
    #   5) 若公约数为 0（例如全是 0 缩进）则 `or 1` 兜底为 1。
    # 【返回】int：缩进宽度，至少为 1。
    # 【注意】只统计**空格**：用制表符缩进的代码会被当成 0 缩进，返回值可能失真。
    # 【易错】`{0, 2, 4}` 的 gcd 是 2，而只有 {0} 时 gcd 为 0——这就是需要 `or 1` 的
    #   原因；集合里包含 0 是常态（顶层语句），所以这条兜底几乎总会用到。
    def detect_indentation(self) -> int:
        """Auto-detect indentation of code.

        Returns:
            int: Number of spaces used to indent code.
        """

        # 【要点】集合去重：只需要“出现过哪些缩进宽度”，重复出现无意义。
        # 【易错】正则只匹配**空格**（" *"），制表符缩进会被算成 0 缩进。
        _indentations = {
            len(match.group(1))
            for match in re.finditer(r"^( *)(.*)$", self.plain, flags=re.MULTILINE)
        }

        try:
            # 【要点】只保留偶数缩进（奇数通常是对齐而非层级），再求最大公约数。
            indentation = (
                reduce(gcd, [indent for indent in _indentations if not indent % 2]) or 1
            )
        except TypeError:
            # 【易错】列表为空时 reduce 会抛 TypeError（空序列无初值），
            # 这里把“没有任何偶数缩进”解释为 1 个空格一级。
            indentation = 1

        return indentation

    # 【职责】给代码文本加上缩进辅助线（形如 "│   " 的竖线），提升代码块可读性。
    # 【功能】返回一份新的 Text，每行的前导空格被替换成由 character 组成的缩进线。
    # 【算法】
    #   1) _indent_size 取参数，None 时调用 detect_indentation 猜；
    #   2) 复制文本并展开制表符（制表位必须展开才能算层级）；
    #   3) indent_line = character + (indent_size - 1) 个空格，即“一层缩进”的替换串；
    #   4) 逐行匹配行首空格：空行（无匹配或组 2 为空）先计数 blank_lines 并跳过；
    #      否则用 divmod(len(indent), _indent_size) 拆成完整层数与残余空格，
    #      拼出 new_indent，替换该行行首等长的前缀，并对这段新前缀 stylize(style)；
    #      若此前累计了空行，则插回同样数量的“只有缩进线”的行；
    #   5) 收尾把剩余的空行补上（用空的 Text 占位）；
    #   6) 用 blank_copy("\n") 作为分隔符 join 所有行。
    # 【参数】indent_size：缩进宽度；None 表示自动检测。
    #   character：竖线字符（默认 "│"）。style：缩进线样式（默认 "dim green"）。
    # 【返回】新的 Text（原对象不变）。
    # 【注意】空行中的缩进线是延迟插入的（blank_lines 计数）：只有遇到下一行有内容的
    #   代码时才补上，从而避免在文件末尾多出缩进线。
    # 【易错】`new_indent` 只替换行首的等长前缀（后面用 line.plain[len(new_indent):]），
    #   因此若残余空格数与层数之和不等于原缩进长度，行内容会被截短或错位——
    #   这里依赖 divmod 的数学关系：full_indents*indent_size + remaining == len(indent)。
    def with_indent_guides(
        self,
        indent_size: Optional[int] = None,
        *,
        character: str = "│",
        style: StyleType = "dim green",
    ) -> "Text":
        """Adds indent guide lines to text.

        Args:
            indent_size (Optional[int]): Size of indentation, or None to auto detect. Defaults to None.
            character (str, optional): Character to use for indentation. Defaults to "│".
            style (Union[Style, str], optional): Style of indent guides.

        Returns:
            Text: New text with indentation guides.
        """

        # 【要点】未指定缩进宽度时自动探测（只统计空格缩进）。
        _indent_size = self.detect_indentation() if indent_size is None else indent_size

        # 【要点】在副本上操作：本方法返回新 Text，不修改原对象。
        text = self.copy()
        # 【要点】必须先展开制表符，否则用 \t 缩进的代码无法按层级替换。
        text.expand_tabs()
        # 【要点】“一层缩进”的替换串：竖线 + (indent_size-1) 个空格，
        # 保证替换后该行的显示宽度不变。
        indent_line = f"{character}{' ' * (_indent_size - 1)}"

        re_indent = re.compile(r"^( *)(.*)$")
        new_lines: List[Text] = []
        add_line = new_lines.append
        # 【要点】空行计数：先攒着，等遇到下一条有内容的行时再补出对应数量的缩进线。
        blank_lines = 0
        for line in text.split(allow_blank=True):
            match = re_indent.match(line.plain)
            # 【要点】空行（无匹配）或只有空白（group(2) 为空）都算“空行”处理。
            if not match or not match.group(2):
                blank_lines += 1
                continue
            indent = match.group(1)
            # 【要点】把缩进空格拆成“完整层数 + 残余空格”，残余部分保留为空格。
            full_indents, remaining_space = divmod(len(indent), _indent_size)
            new_indent = f"{indent_line * full_indents}{' ' * remaining_space}"
            # 【要点】用等长替换：新前缀长度与原缩进长度相同（divmod 保证），
            # 因此后面的正文位置不变。
            line.plain = new_indent + line.plain[len(new_indent) :]
            # 【要点】只给新前缀上缩进线样式；正文样式保持不变。
            line.stylize(style, 0, len(new_indent))
            if blank_lines:
                # 【要点】补出之前攒下的空行：它们用“只有缩进线、没有正文”的 Text 表示。
                new_lines.extend([Text(new_indent, style=style)] * blank_lines)
                blank_lines = 0
            add_line(line)
        # 【要点】文件末尾的空行不补缩进线，用空 Text 占位（避免末尾多出竖线）。
        if blank_lines:
            new_lines.extend([Text("", style=style)] * blank_lines)

        # 【要点】用自带的换行分隔符 join：既保留每行样式，又把它们拼成单个 Text。
        new_text = text.blank_copy("\n").join(new_lines)
        return new_text


# ==== 【模块自测入口：justify 演示】 ====
# 【功能】直接 `python -m rich.text`（或在 rich 源码目录下执行本文件）时，
#   用同一段 Lorem ipsum 文本分别演示 left/center/right/full 四种对齐效果。
# 【要点】它同时用到了 highlight_words（给个别单词加粗/斜体）与 Console.print 的
#   style/justify 参数，是观察“Text 属性 vs ConsoleOptions 优先级”的现成例子：
#   文本自身的 justify 为 None，因此实际生效的是 print 传入的 justify。
# 【注意】本段属于开发期演示，依赖 Console（因此是延迟导入），不属于库 API。
if __name__ == "__main__":  # pragma: no cover
    from rich.console import Console

    text = Text(
        """\nLorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.\n"""
    )
    text.highlight_words(["Lorem"], "bold")
    text.highlight_words(["ipsum"], "italic")

    console = Console()

    console.rule("justify='left'")
    console.print(text, style="red")
    console.print()

    console.rule("justify='center'")
    console.print(text, style="green", justify="center")
    console.print()

    console.rule("justify='right'")
    console.print(text, style="blue", justify="right")
    console.print()

    console.rule("justify='full'")
    console.print(text, style="magenta", justify="full")
    console.print()
