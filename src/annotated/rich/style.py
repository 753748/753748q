import sys
from functools import lru_cache
from itertools import count
from operator import attrgetter
from pickle import dumps, loads
from random import getrandbits
from typing import Any, Dict, Iterable, List, Optional, Type, Union, cast

from . import errors
from .color import Color, ColorParseError, ColorSystem, blend_rgb
from .repr import Result, rich_repr
from .terminal_theme import DEFAULT_TERMINAL_THEME, TerminalTheme
# ---------------------------------------------------------------------------
# 【模块定位】本模块定义 Rich 的「终端样式」模型，是渲染子系统的中间层：向下它把样式翻译
#   成 ANSI SGR 序列（经 color.py 的颜色降级），向上它被 markup、Text、Table、Console 等
#   几乎所有组件使用。Console.get_style(name) 先查主题（theme.py）再用 Style.parse 解析
#   字符串定义，最终都归到这里的 Style 对象。
# 【关键数据结构】
#   · Style：以 __slots__ 实现的轻量不可变对象，包含前景色 _color、背景色 _bgcolor、
#     13 个三态属性（用 _attributes 位域 + _set_attributes 掩码两个整数编码）、
#     超链接 _link / _link_id、以及任意元信息 _meta（pickle 后的 bytes）。
#   · _Bit：属性描述符，让 style.bold 这样的读取走位运算。
#   · StyleStack：样式栈，供 markup / tree 一类嵌套结构做「当前有效样式」的累积。
#   · NULL_STYLE：全局唯一的空样式单例，用对象同一性换取判空性能。
# 【设计要点】
#   · 三态属性：每个属性有「开 / 关 / 未设置」三种状态，未设置的属性在样式相加时不会
#     覆盖下层设置，这是 [bold]…[/bold] 嵌套语义的基础（详见 _add）。
#   · 用两个 int 位域而不是 13 个布尔字段：构造快、内存小、哈希/比较只需两次整数运算，
#     代价是位号与 _style_map / STYLE_ATTRIBUTES 必须严格同步。
#   · 不可变 + 缓存：Style 不支持原地修改，所有组合操作（+、copy、update_link）都产生新
#     对象；_add、parse、normalize、get_html_style、clear_meta_and_links 都带 lru_cache。
#   · _ansi 缓存的是「某个颜色体系下」的 SGR 字符串，只有 _ansi is None 时才会重新计算，
#     因此同一个 Style 对象不要混用不同 color_system 渲染。
# 【阅读建议】先读 __init__ 与 _Bit、_style_map、STYLE_ATTRIBUTES（理解三态位域方案），
#   再读 parse / __str__（字符串 ↔ 对象的双向转换），然后读 _add 与 __add__（样式合成的
#   核心语义），接着读 _make_ansi_codes / render（输出到终端），最后看 get_html_style
#   （导出为 HTML 的颜色与装饰规则）与 StyleStack。
#   依赖：color（颜色解析与降级）、terminal_theme（默认主题色）、errors（语法错误）、repr。
#   被依赖：console、text、markup、table、panel、syntax、tree、theme 等几乎所有模块。
# ---------------------------------------------------------------------------
# 【附：常见误用与排查】
#   · str(style) 得到 "none" 不等于「没有样式对象」——它表示一个什么都不改的空样式；
#   · 用 `if style:` 判断时，Style(bold=False) 会被判为假，但它与 NULL_STYLE 并不相等；
#   · `style_a + style_b` 中 style_b 会覆盖 style_a 的同名属性，包括用 False 显式关闭；
#   · 同一个 Style 对象不要用不同 color_system 渲染（_ansi 缓存会串味），需先 copy()；
#   · 从主题取到的样式可能已被 copy（带链接时），不要依赖对象同一性做缓存。
# ---------------------------------------------------------------------------
# 【易错】_hash_getter 用 attrgetter 而不是自己写 lambda：attrgetter 是 C 实现，
#   在 Style.__hash__ 这个热路径上更快；代价是字段名写错要到运行时才会暴露。
# 【注意】StyleType 是 typling.Union 的别名，运行时完全可以被字符串替代；
#   它的价值在于文档与静态检查，不影响任何运行时行为。
# 【说明】下面的注释是给阅读者的「速记表」，把英文关键词与中文含义对应起来：
#   bold 粗体 / dim 暗淡 / italic 斜体 / underline 下划线 / blink 闪烁 /
#   blink2 快速闪烁 / reverse 反显（前景背景互换） / conceal 隐藏 / strike 删除线 /
#   underline2 双下划线 / frame 边框 / encircle 环绕 / overline 上划线。
#   这些属性在真实终端上的支持程度差异很大，Rich 只负责生成 SGR 参数。
# 【要点】1 << bit_no 生成掩码；bit_no 从 0 开始，所以 bit 依次是 1、2、4、8……4096。
# 【注意】__slots__ 里只声明了 bit 一个属性，避免描述符对象自身也带字典。
# 【参数】obj：Style 实例；objtype：其类型（描述符协议会传入，本实现未使用）。
# 【说明】Style 是本模块唯一的核心类：下面按「构造 → 工厂 → 描述符 → 表示 → 缓存 →
#   不变性 → 解析 → 导出 → 合成 → 渲染」的顺序组织，阅读时可以按这个方法顺序推进。

# ==== 【哈希键与类型别名】 ====
# 【要点】_hash_getter 用 operator.attrgetter 一次性取出决定 Style 相等性的 6 个字段。
# 【为什么】刻意排除 _ansi、_style_definition、_link_id、_hash、_null 这几个「派生/缓存」
#   字段：它们只是加速用的缓存，两个内容相同的 Style 可能只在一方填了缓存，
#   若把它们纳入哈希，逻辑相等的样式就会「不相等」。
# 【约束】任何影响渲染结果的字段（颜色、属性、链接、元信息）都必须出现在这里，
#   否则 Console 的样式缓存会返回错误的渲染结果。
_hash_getter = attrgetter(
    "_color", "_bgcolor", "_attributes", "_set_attributes", "_link", "_meta"
)

# 【要点】StyleType 表示「样式还可以用字符串定义」：绝大多数 Rich API（Text.stylize、
#   Panel(style=...) 等）接受 "bold red" 这类定义串，由 Console.get_style 负责解析。
# 【为什么】用字符串别名而不是在每处调用点重复 Union：既提高可读性，也让类型检查器
#   在 API 边界上给出准确的提示。
# Style instances and style definitions are often interchangeable
StyleType = Union[str, "Style"]
# ==== 【链接 id 生成器】 ====
# 【要点】_id_generator 是进程内全局自增计数器，起点是 24 位随机数（避免多个进程/会话
#   产生的链接 id 撞车）。每次创建或复制带链接的 Style 时取一个新值，供 OSC 8 超链接
#   序列（ESC]8;id=...;URL）把链接的起止配对。
# 【注意】它只保证「同一进程内唯一」，不是全局唯一，也不参与序列化。


_id_generator = count(getrandbits(24))


# ==== 【属性描述符 _Bit】 ====
# ---------------------------------------------------------------------------
# 【职责】把「一个三态属性」实现为类属性描述符：读取 style.bold 时执行位运算，
#   返回 True / False / None 三态之一，而不是像普通属性那样存储一个值。
# 【关键属性】bit：该属性在 _attributes / _set_attributes 位域中对应的那一位。
# 【不变量】bit 的位号必须与 __init__ 中的移位量、__str__ 中的掩码、
#   _make_ansi_codes 中的 _style_map 索引完全一致；三者任一改动都要同步。
# 【协作】仅在 Style 上以类属性形式使用（bold / dim / italic / …共 13 个）。
# ---------------------------------------------------------------------------
# 【功能】把位号换算成掩码；用一个 2 的幂整数表示该属性在位域中的位置。
# 【参数】bit_no：位号（0 表示最低位，对应 bold）。
class _Bit:
    """A descriptor to get/set a style attribute bit."""

    __slots__ = ["bit"]

    # 【功能】把位号换算成位掩码并保存（self.bit = 1 << bit_no）。
    # 【参数】bit_no：位号，0 是最低位（对应 bold）。
    # 【注意】__slots__ 只允许 bit 一个属性；位号必须与 _attributes/_set_attributes 的位域、
    #   __str__ 的掩码以及 _make_ansi_codes 的 _style_map 索引保持一致，改动要同步。
    def __init__(self, bit_no: int) -> None:
        self.bit = 1 << bit_no

    # 【功能】读取三态属性值。
    # 【算法】先用 self._set_attributes & self.bit 判断该属性是否被显式设置过：
    #   未设置 → 返回 None（表示「不改变」）；已设置 → 返回 _attributes 中该位是否为 1。
    # 【返回】Optional[bool]：True / False / None。
    # 【注意】本方法只实现了 __get__，没有 __set__，所以 Style 实例上的同名属性是只读的；
    #   想「修改」必须重新构造一个 Style。
    # 【易错】obj 可能是 None（类属性访问，如 Style.bold），此处未做保护：
    #   直接访问 Style.bold 会抛 AttributeError，必须通过实例读取。
    def __get__(self, obj: "Style", objtype: Type["Style"]) -> Optional[bool]:
        if obj._set_attributes & self.bit:
            return obj._attributes & self.bit != 0
        return None


# ==== 【Style 主体】 ====
# ---------------------------------------------------------------------------
# 【职责】描述「一段终端文本应当如何显示」：前景色、背景色、13 个三态属性、超链接、
#   以及任意附加元信息；并负责把这些信息翻译成 ANSI SGR 序列或 CSS 规则。
# 【关键属性】
#   _color / _bgcolor —— 前景/背景色，None 表示不设置（继承），而非「默认色」；
#   _attributes       —— 13 位位域，置 1 表示该属性取值为 True；
#   _set_attributes   —— 13 位掩码，置 1 表示该属性被显式指定过（True 或 False）；
#   _link / _link_id  —— 超链接目标与进程内唯一标识（OSC 8 序列需要）；
#   _meta             —— 任意元信息（如 @click 处理器），pickle 成 bytes 保存；
#   _ansi / _style_definition / _hash —— 惰性缓存，不影响相等性。
# 【不变量】
#   1. 属性是否「生效」= _attributes & _set_attributes & bit；未设置的位一律当作不存在；
#   2. Style 逻辑上不可变：所有组合操作返回新对象，缓存字段只在首次访问时填充；
#   3. 相等性（__eq__ / __hash__）只看 _hash_getter 列出的 6 个字段；
#   4. _link 为空时 _link_id 必须是 ""，非空时必须是进程内唯一串；
#   5. 位号在 _Bit 声明、__init__、__str__、_style_map 四处必须一致。
# 【协作】被 Console/Text/Markup/Table 等几乎所有模块创建与合成；
#   通过 render() → _make_ansi_codes() → Color.get_ansi_codes() 落到终端字节。
# ---------------------------------------------------------------------------
@rich_repr
class Style:
    """A terminal style.

    A terminal style consists of a color (`color`), a background color (`bgcolor`), and a number of attributes, such
    as bold, italic etc. The attributes have 3 states: they can either be on
    (``True``), off (``False``), or not set (``None``).

    Args:
        color (Union[Color, str], optional): Color of terminal text. Defaults to None.
        bgcolor (Union[Color, str], optional): Color of terminal background. Defaults to None.
        bold (bool, optional): Enable bold text. Defaults to None.
        dim (bool, optional): Enable dim text. Defaults to None.
        italic (bool, optional): Enable italic text. Defaults to None.
        underline (bool, optional): Enable underlined text. Defaults to None.
        blink (bool, optional): Enabled blinking text. Defaults to None.
        blink2 (bool, optional): Enable fast blinking text. Defaults to None.
        reverse (bool, optional): Enabled reverse text. Defaults to None.
        conceal (bool, optional): Enable concealed text. Defaults to None.
        strike (bool, optional): Enable strikethrough text. Defaults to None.
        underline2 (bool, optional): Enable doubly underlined text. Defaults to None.
        frame (bool, optional): Enable framed text. Defaults to None.
        encircle (bool, optional): Enable encircled text. Defaults to None.
        overline (bool, optional): Enable overlined text. Defaults to None.
        link (str, link): Link URL. Defaults to None.

    """

    # 【要点】这 7 个类级注解（而非赋值）作用是声明实例字段类型，供类型检查器使用；
    #   __slots__ 才是真正内存布局的声明。
    # 【注意】_attributes / _set_attributes 是位域整数，不是布尔列表——
    #   「未设置」与「设为 False」的差别完全靠 _set_attributes 里对应位是否为 1 来表达。
    # 【重要区分】本模块中「None」几乎总是表示「不设置 / 不改变」，而不是「无」：
    #   _color=None 意味着「继承下层的前景色」，而不是「前景色为空」。
    #   真正表示「显式关闭某个属性」的是 False（配合 _set_attributes 的对应位）。
    #   理解这三态（True / False / None）是读懂样式合成 _add 的前提。
    _color: Optional[Color]
    # 【要点】两个同时出现的参数（color 与 bgcolor）都可能是字符串，用一个闭包统一处理，
    #   比在调用处写两遍条件表达式更清晰，也避免重复 isinstance 判断。
    _bgcolor: Optional[Color]
    _attributes: int
    _set_attributes: int
    _hash: Optional[int]
    _null: bool
    _meta: Optional[bytes]

    # 【约束】__slots__ 必须列出全部 11 个实例字段；漏掉任何一个都会让该字段变成普通实例
    #   属性（占用 __dict__，Style 就失去「小对象、无字典」的性能优势），
    #   而多了不存在的字段会在赋值时报 AttributeError。
    # 【为什么】用 __slots__ 而不是 dataclass：Rich 会创建海量 Style 对象（每个 Segment
    #   都可能持有一个），省掉 per-instance __dict__ 能显著降低内存占用与构造时间。
    __slots__ = [
        "_color",
        "_bgcolor",
        "_attributes",
        "_set_attributes",
        "_link",
        "_link_id",
        "_ansi",
        "_style_definition",
        "_hash",
        "_null",
        "_meta",
    ]

    # ==== 【位号 → SGR 参数映射表】 ====
    # 【要点】把位号（0~12）映射到 ANSI SGR 参数：1 粗体、2 暗淡、3 斜体、4 下划线、
    #   5 闪烁、6 快闪、7 反显、8 隐藏、9 删除线、21 双下划线、51 边框、52 环绕、53 上划线。
    # 【约束】键必须与 _Bit 声明顺序严格一致（_Bit(0) 是 bold，对应 "1"），
    #   且与 ansi.py 的 SGR_STYLE_MAP 反向对应——解析 ANSI 时靠那张表还原样式。
    # 【注意】这里的值是 SGR 参数的数字字符串，最终由 _make_ansi_codes 用 ";" 连接。
    # maps bits on to SGR parameter
    _style_map = {
        0: "1",
        1: "2",
        2: "3",
        3: "4",
        4: "5",
        5: "6",
        6: "7",
        7: "8",
        8: "9",
        9: "21",
        10: "51",
        11: "52",
        12: "53",
    }

    # ==== 【样式定义字符串的关键字表】 ====
    # 【要点】把用户在样式串里写的词（含简写）规范到规范的属性名：
    #   d→dim、b→bold、i→italic、u→underline、r→reverse、c→conceal、s→strike、
    #   uu→underline2、o→overline。parse 用它识别属性，__str__ 只输出规范名。
    # 【约束】这份表的「值」必须能在 Style.__init__ 的关键字参数中找到同名参数，
    #   因为 parse 最后会执行 Style(color=..., bgcolor=..., **attributes)。
    # 【易错】blink2 / frame / encircle 没有简写；"s" 是 strike 而不是 bold 之类的常见误记。
    # 【为什么】返回单例而不是每次都新建 Style()：空样式在无样式的 Segment 上出现极频繁，
    #   单例让「是否为空样式」退化成一次 `is` 比较，也避免反复分配对象。
    STYLE_ATTRIBUTES = {
        # 【注意】本方法不解析字符串：如果传入 "red"（str）会被原样存进 _color，
        #   之后调用 get_ansi_codes 之类的方法就会失败——它只服务于内部已经持有 Color 的调用点。
        "dim": "dim",
        "d": "dim",
        "bold": "bold",
        "b": "bold",
        "italic": "italic",
        "i": "italic",
        "underline": "underline",
        "u": "underline",
        "blink": "blink",
        "blink2": "blink2",
        "reverse": "reverse",
        "r": "reverse",
        "conceal": "conceal",
        "c": "conceal",
        "strike": "strike",
        "s": "strike",
        "underline2": "underline2",
        "uu": "underline2",
        "frame": "frame",
        "encircle": "encircle",
        "overline": "overline",
        "o": "overline",
    }

    # ==== 【构造】 ====
    # 【功能】构造 Style：解析颜色、把 13 个三态属性打包进两个位域、初始化派生字段与缓存位。
    # 【算法】1) 颜色：字符串交给 Color.parse，Color 对象则原样使用；
    #   2) _set_attributes：对每个参数判断 `is not None`，是则把对应位累加进去——
    #      这一步决定「该属性是否被显式指定」；
    #   3) _attributes：同样遍历，但只在参数为真（True）时置位，得到「实际生效的属性」；
    #      若 _set_attributes 为 0（一个属性都没指定），_attributes 直接取 0；
    #   4) 元信息用 pickle.dumps 冻结成 bytes，_link_id 在 link 或 meta 存在时才生成；
    #   5) _null 标记「完全空样式」，供 __bool__ 与多个快速路径使用。
    # 【参数】color/bgcolor：Color 或颜色定义串；其余 13 个是布尔三态（None 表示不设置）；
    #   link：超链接 URL；meta：任意可 pickle 的字典。
    # 【注意】构造器是关键字专用的（签名里只有一个 *），因此调用必须写 Style(color="red")，
    #   位置参数会被拒绝——这避免了 15 个参数写错顺序的低级错误。
    # 【约束】第 2、3 步中的移位量（1,2,4,8,…,4096）必须与 _Bit 的位号一一对应。
    # 【易错】手工构造意味着 __init__ 里的默认行为（如 _null 的计算、link_id 的分配）
    #   全部要在这里重现；新增实例字段时必须同步修改所有 _new 风格的工厂方法。
    def __init__(
        self,
        *,
        color: Optional[Union[Color, str]] = None,
        bgcolor: Optional[Union[Color, str]] = None,
        bold: Optional[bool] = None,
        dim: Optional[bool] = None,
        # 【要点】"@" 前缀是 Rich 约定的「元信息处理器」命名空间：Pretty 打印时把 @click
        #   一类键解释为可交互回调，普通键则只是附加数据。
        italic: Optional[bool] = None,
        underline: Optional[bool] = None,
        blink: Optional[bool] = None,
        blink2: Optional[bool] = None,
        reverse: Optional[bool] = None,
        conceal: Optional[bool] = None,
        strike: Optional[bool] = None,
        underline2: Optional[bool] = None,
        frame: Optional[bool] = None,
        encircle: Optional[bool] = None,
        overline: Optional[bool] = None,
        link: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ):
        self._ansi: Optional[str] = None
        self._style_definition: Optional[str] = None

        # 【要点】用一个内部函数统一「字符串 → Color」的转换，避免在 color/bgcolor 两处重复
        #   判断 isinstance；Color.parse 自身带 lru_cache，重复解析同一字符串几乎无成本。
        def _make_color(color: Union[Color, str]) -> Color:
            return color if isinstance(color, Color) else Color.parse(color)

        self._color = None if color is None else _make_color(color)
        self._bgcolor = None if bgcolor is None else _make_color(bgcolor)
        # 【要点】sum((bold is not None, dim is not None and 2, ...))：把每个参数的存在性
        #   折算成一个整数位；`is not None` 返回 bool，参与整数加法时等价于 0/1。
        # 【易错】这里累加的是「掩码」而不是「值」，因此 `bold=False` 也会让第 0 位置 1——
        #   这正是「显式关闭」能够覆盖下层样式的关键。
        self._set_attributes = sum(
            (
                bold is not None,
                dim is not None and 2,
                # 【易错】不要在类外部缓存 style.bold 的返回值：三态中的 None 表示「未设置」，
                #   与 False（显式关闭）语义不同，混用会让样式叠加出现难以排查的覆盖问题。
                italic is not None and 4,
                underline is not None and 8,
                blink is not None and 16,
                blink2 is not None and 32,
                reverse is not None and 64,
                # 【关键点】__str__ 的输出是「样式定义串」的一份规范形式：它同时被用作缓存键、
                #   repr 的输出内容、以及主题配置文件（Theme.config）中每一行的右值。
                #   因此它的输出格式是对外契约，不能随意调整（例如把 "not bold" 改成 "no-bold"
                #   会破坏已存在的配置文件与解析逻辑）。
                conceal is not None and 128,
                strike is not None and 256,
                underline2 is not None and 512,
                frame is not None and 1024,
                encircle is not None and 2048,
                overline is not None and 4096,
            )
        )
        # 【要点】`bold and 1 or 0` 是三元表达式的简写（Python 没有 C 的 ?:）；
        #   参数可能是 None，None 为假，于是该位保持 0。
        # 【为什么】仍需判断 self._set_attributes：若一个属性都没设置，_attributes 直接压成 0，
        #   避免出现「值位有 1 但掩码为 0」这种自相矛盾的状态。
        self._attributes = (
            sum(
                (
                    bold and 1 or 0,
                    dim and 2 or 0,
                    italic and 4 or 0,
                    underline and 8 or 0,
                    blink and 16 or 0,
                    blink2 and 32 or 0,
                    reverse and 64 or 0,
                    conceal and 128 or 0,
                    strike and 256 or 0,
                    underline2 and 512 or 0,
                    frame and 1024 or 0,
                    encircle and 2048 or 0,
                    overline and 4096 or 0,
                )
            )
            if self._set_attributes
            else 0
        )

        # 【要点】_meta 用 pickle 冻结：一是让 Style 可哈希（dict 不可哈希），
        #   二是防止外部拿到引用后原地修改导致哈希失效。
        # 【易错】pickle.dumps 要求 meta 里的值可序列化；放 lambda 或不可 pickle 的对象会在
        #   构造 Style 时抛异常（也就是说 Style(meta=...) 可能失败）。
        self._link = link
        self._meta = None if meta is None else dumps(meta)
        # 【要点】_link_id 只有在需要链接或元信息时才分配，且把 meta 的哈希拼进去，
        #   使「同一 URL 但不同 meta」也获得不同的 id。
        # 【注意】hash(self._meta) 依赖 Python 的哈希随机化，因此 _link_id 在不同进程中不同，
        #   这正是它「仅进程内唯一」的原因。
        self._link_id = (
            # 【易错】__str__ 里三段掩码（0b0000000001111 / 0b0000111110000 / 0b1111000000000）
            #   合计正好覆盖低 13 位；任何位被遗漏都会导致该属性不会出现在输出串里。
            f"{next(_id_generator)}{hash(self._meta)}" if (link or meta) else ""
        )
        self._hash: Optional[int] = None
        # 【要点】_null 是「样式完全没有内容」的判据：没有任何显式属性、没有前景/背景色、
        #   没有链接、没有元信息。__bool__ 直接返回 not self._null，省掉逐字段检查。
        # 【易错】一个 Style 可能有 _attributes 非零但 _null 为真（例如只写了 bold=False），
        #   因此 _null 只用于「快速判空」，不能当作「与空样式等价」使用。
        # 【要点】每组内先按名称顺序逐位判断，因此输出顺序固定为 bold、dim、italic、…，
        #   与 parse 的解析顺序无关（parse 是逐个词处理，与顺序天然无关）。
        self._null = not (self._set_attributes or color or bgcolor or link or meta)

    # ==== 【工厂方法：空样式 / 纯色 / 纯元信息】 ====
    # 【功能】返回全局唯一的空样式单例 NULL_STYLE。
    # 【返回】Style：等价于 Style() 但共享同一个对象，比较只需 is 判断。
    # 【为什么】空样式出现频率极高（每个无样式的 Segment），每次新建会浪费内存与时间；
    #   而 Style 不可变，共享实例绝对安全。
    # 【注意】不要对返回值做任何「修改」意图的操作——虽然本来就无法修改，
    #   但通过 + 组合得到的是新对象，不会污染这个单例。
    # 【易错】self._ansi is None 的缓存判断假定「同一个 Style 对象只被一种颜色体系渲染」。
    #   若调用方想用另一个 color_system 重新渲染，必须先 copy() 出一个新对象，
    #   否则会拿到上一体系的旧结果。
    @classmethod
    # 【协作】Style.parse("none")、Style.parse("") 以及所有「空样式」路径
    #   都返回这个单例；因此常量比较 `style is NULL_STYLE` 是可靠且高效的。
    def null(cls) -> "Style":
        """Create an 'null' style, equivalent to Style(), but more performant."""
        return NULL_STYLE

    @classmethod
            # 【约束】前景色必须在背景色之前追加，且两者都要先 downgrade 到同一 color_system，
            #   这样同一个 Style 在 256 色终端上得到的字节序列才是稳定可缓存的。
    # 【功能】快速构造一个「只有颜色、没有任何属性」的样式。
    # 【算法】绕过 __init__，直接用 cls.__new__ 分配对象并逐个填充 __slots__ 字段。
    # 【参数】color/bgcolor：Color 对象（注意本方法不做字符串解析）。
    # 【返回】Style 实例。
    # 【为什么】渲染热路径（如语法高亮的逐 token 着色）需要创建大量纯色样式，
    #   绕开 __init__ 里的循环与 pickle 调用可以省下可观的开销。
    # 【易错】必须显式设置所有 11 个 slot：漏设的字段在读取时会抛 AttributeError
    #   （__slots__ 没有默认值）。
    # 【要点】blink..strike 这 5 位与 underline2..overline 这 4 位用 for 循环批量处理，
    #   而前 4 位（bold/dim/italic/underline）单独展开——这段历史遗留的写法只是
    #   为了保留确定性的 SGR 输出顺序，便于与快照测试比对。
    def from_color(
        cls, color: Optional[Color] = None, bgcolor: Optional[Color] = None
    ) -> "Style":
        """Create a new style with colors and no attributes.

        Returns:
            color (Optional[Color]): A (foreground) color, or None for no color. Defaults to None.
            bgcolor (Optional[Color]): A (background) color, or None for no color. Defaults to None.
        """
        style: Style = cls.__new__(Style)
        style._ansi = None
        style._style_definition = None
        style._color = color
        style._bgcolor = bgcolor
        style._set_attributes = 0
        style._attributes = 0
        style._link = None
        style._link_id = ""
        style._meta = None
        # 【易错】normalize 对非法样式串不抛异常而是返回小写原文：调用点（标记解析、主题查找）
        #   需要容忍用户拼写错误，把错误推迟到真正解析样式时再报。
        style._null = not (color or bgcolor)
        style._hash = None
        return style

    @classmethod
    # 【功能】构造一个只携带元信息、不带任何外观的空样式。
    # 【算法】同样绕过 __init__ 手工填充字段；_meta 立即 pickle，_link_id 立即分配。
    # 【参数】meta：可 pickle 的字典，或 None。
    # 【返回】Style 实例。
    # 【注意】_null 取 `not meta`，因此 meta={} 得到的样式在布尔意义上是「空」的。
    def from_meta(cls, meta: Optional[Dict[str, Any]]) -> "Style":
        """Create a new style with meta data.

        Returns:
            meta (Optional[Dict[str, Any]]): A dictionary of meta data. Defaults to None.
        """
        style: Style = cls.__new__(Style)
        # 【注意】返回值可能是字符串：pick_first("bold", style) 会返回 "bold"，
        #   由调用方（通常紧跟 get_style）负责解析。
        style._ansi = None
        style._style_definition = None
        style._color = None
        style._bgcolor = None
        style._set_attributes = 0
        style._attributes = 0
        style._link = None
        # 【要点】产出三元组 (name, value, default) 时，rich_repr 会省略等于 default 的项，
        #   所以 repr(Style(color="red")) 里不会出现 bold=、dim= 这些噪音。
        style._meta = dumps(meta)
        style._link_id = f"{next(_id_generator)}{hash(style._meta)}"
        style._hash = None
        style._null = not (meta)
        return style

    # 【功能】创建一个带元信息的空白样式，并把关键字参数变成处理器。
    # 【算法】把 **handlers 的每个键前面加 "@"（如 click=fn → "@click": fn）合并进 meta，
    #   再交给 from_meta。
    # 【参数】meta：可选的基础元信息字典；**handlers：以关键字形式给出的回调。
    # 【返回】Style 实例。
    # 【注意】1) 当 meta 不为 None 时本方法会原地修改传入的字典（update）；
    #   2) 键冲突时 handlers 覆盖 meta 中已有的同名键；
    #   3) 回调用例：style = Style.on(click=self.on_click)。
    @classmethod
            # 【约束】__eq__ 与 __hash__ 必须成对定义并保持一致的判据（都依赖 _hash_getter），
            #   否则 Style 作为字典键时会出现「相等却落在不同桶」这类难以复现的问题。
    def on(cls, meta: Optional[Dict[str, Any]] = None, **handlers: Any) -> "Style":
        """Create a blank style with meta information.

        Example:
            style = Style.on(click=self.on_click)

        Args:
            meta (Optional[Dict[str, Any]], optional): An optional dict of meta information.
            **handlers (Any): Keyword arguments are translated in to handlers.

        Returns:
            Style: A Style with meta information attached.
        """
        meta = {} if meta is None else meta
        # 【优化】把 _hash 缓存在实例上，是因为 Style 常被用作 dict 键（Console 的样式缓存），
        #   而哈希计算本身要做属性提取与元组构造；缓存后重复查表几乎零开销。
        meta.update({f"@{key}": value for key, value in handlers.items()})
        return cls.from_meta(meta)

    # ==== 【属性描述符声明】 ====
    # 【约束】这 13 个类属性名与 _Bit 的位号必须一一对应；位号还要与 __init__ 里的移位量、
    #   __str__ 中的掩码常量（0b0000000001111 等）、_make_ansi_codes 中 _style_map 的索引
    #   全部保持一致。它们共同支撑「两个整数表示 13 个三态属性」这一方案。
    # 【易错】此处只是描述符：读取会调用 _Bit.__get__，赋值不会生效（未实现 __set__），
    #   因此 Style 事实上不可变——任何「修改」都必须通过 + 组合或 copy/update_link 产生新对象。
    # 【历史】属性顺序（bold→dim→italic→underline→blink→blink2→reverse→conceal→strike→
    #   underline2→frame→encircle→overline）自早期版本起就固定，新增属性只能追加在末尾。
    # 【说明】下面 6 个属性访问器（color/bgcolor/link/transparent_background/
    #   background_style/meta/without_color）都是只读派生量，没有任何副作用。
    bold = _Bit(0)
    dim = _Bit(1)
    italic = _Bit(2)
    underline = _Bit(3)
    blink = _Bit(4)
    blink2 = _Bit(5)
    reverse = _Bit(6)
    conceal = _Bit(7)
    strike = _Bit(8)
    underline2 = _Bit(9)
    # 【易错】ColorType.DEFAULT 的 is_default 为真，因此「显式写了 default 颜色」也被视为
    #   透明背景；这与终端的实际行为一致（39/49 号 SGR 就是「恢复默认」）。
    frame = _Bit(10)
    encircle = _Bit(11)
    overline = _Bit(12)

    # ==== 【链接与字符串表示】 ====
    # 【功能】返回该样式的超链接 id，供 OSC 8 序列配对使用。
    # 【返回】str：无链接时为空字符串（不是 None）。
    # 【注意】__slots__ 里保存的是 _link_id，link_id 只是一个只读访问器。
    # 【注意】本属性返回的是 Style(bgcolor=...) —— 一个只有背景色的新样式，
    #   它不携带原样式的属性，因此不要把两者相加后当作「原样式的背景部分」使用。
    @property
    def link_id(self) -> str:
        # 【注意】meta 的解码成本与字典大小相关；在高频渲染路径上反复读取 meta 会成为瓶颈。
        """Get a link id, used in ansi code for links."""
        return self._link_id

    # 【功能】把 Style 反向生成为样式定义字符串（例如 "bold red on blue link https://…"）。
    # 【算法】按位域分组检查：先处理 bold/dim/italic/underline（前 4 位），
    #   再处理 blink…strike，再处理 underline2/frame/encircle/overline，
    #   每个已设置的位都输出「属性名」或「not 属性名」；随后追加深色与背景色，
    #   最后追加 link；结果为空则返回 "none"。
    # 【返回】str：规范化的样式定义串，同时被缓存到 _style_definition。
    # 【注意】1) 只有「被显式设置」的属性才会出现（掩码为 0 的位一律跳过），
    #   因此 str(Style()) 是 "none" 而不是空串；
    #   2) parse(str(style)) 能还原出等价的样式，这个往返性质是 markup 与主题配置的基础；
    #   3) 分组用的掩码是硬编码常量，新增第 14 个属性时必须同时修改这里的三段掩码。
    # 【易错】返回的新样式 _null 被强制设为 False：即使原样式「看起来」只有颜色，
    #   去色之后也应被视为空样式，但这里为了保守（保留属性/链接）而显式置 False。
    def __str__(self) -> str:
        """Re-generate style definition from attributes."""
        if self._style_definition is None:
            attributes: List[str] = []
            append = attributes.append
            bits = self._set_attributes
            if bits & 0b0000000001111:
                if bits & 1:
                    append("bold" if self.bold else "not bold")
                if bits & (1 << 1):
                    append("dim" if self.dim else "not dim")
                # 【约束】parse 的关键字（bold/dim/…/overline）必须与 STYLE_ATTRIBUTES 的值集合一致，
                #   否则会出现「识别了词但构造 Style 时报 unexpected keyword」的错误。
                if bits & (1 << 2):
                    append("italic" if self.italic else "not italic")
                if bits & (1 << 3):
                    append("underline" if self.underline else "not underline")
            if bits & 0b0000111110000:
                if bits & (1 << 4):
                    append("blink" if self.blink else "not blink")
                if bits & (1 << 5):
                    append("blink2" if self.blink2 else "not blink2")
                if bits & (1 << 6):
                    append("reverse" if self.reverse else "not reverse")
                if bits & (1 << 7):
                    append("conceal" if self.conceal else "not conceal")
                if bits & (1 << 8):
                    # 【要点】"none" 与空串都映射到 NULL_STYLE：这两者是最常见的「无样式」写法，
                    #   直接短路可以省掉整个分词循环。
                    append("strike" if self.strike else "not strike")
            if bits & 0b1111000000000:
                if bits & (1 << 9):
                    append("underline2" if self.underline2 else "not underline2")
                if bits & (1 << 10):
                    append("frame" if self.frame else "not frame")
                if bits & (1 << 11):
                    append("encircle" if self.encircle else "not encircle")
                if bits & (1 << 12):
                    # 【兼容】分词只按空白切分，因此带空格的 URL（如 file:///a b）无法用 link 关键字表达；
                    #   这是样式定义串格式的既有局限，需要复杂链接时请直接构造 Style(link=...)。
                    append("overline" if self.overline else "not overline")
            if self._color is not None:
                append(self._color.name)
            if self._bgcolor is not None:
                append("on")
                append(self._bgcolor.name)
            if self._link:
                append("link")
                append(self._link)
            self._style_definition = " ".join(attributes) or "none"
        return self._style_definition

    # 【功能】布尔化：样式「非空」时为 True。
    # 【返回】bool：等价于 not self._null。
    # 【为什么】Rich 内部大量写 `if style:` 判断「是否需要渲染样式」，
    #   借助 _null 缓存可以让这个判断变成一次取反，而不是逐字段比较。
    # 【易错】`Style(bold=False)` 的 _null 为 True，因此它作为布尔值是 False，
    #   但它并不等于 Style()——两者哈希不同，相加效果也不同。
    def __bool__(self) -> bool:
        """A Style is false if it has no attributes, colors, or links."""
        return not self._null

        # 【要点】构造时把所有属性以关键字展开（**attributes）传入，因此 attributes 的键
        #   必须是 Style.__init__ 的合法参数名——这正是 STYLE_ATTRIBUTES 与构造器签名耦合之处。
    # ==== 【生成 ANSI 序列】 ====
    # 【功能】把本样式翻译成 ANSI SGR 参数字符串（不含 ESC 前缀与结尾的 m）。
    # 【算法】1) 只在 _ansi 为 None 时计算，结果缓存回 _ansi；
    #   2) 先按位域分组收集属性对应的 SGR 参数（顺序与 _style_map 一致）；
    #   3) 前景色与背景色各自 downgrade 到目标颜色体系后取 get_ansi_codes；
    #   4) 用 ";" 连接全部参数。
    # 【参数】color_system：目标终端能力，用于把真彩色降级为 8 位/标准色。
    # 【返回】str：如 "1;31"（可能为空串）。
    # 【易错】_ansi 一旦计算就被永久缓存，缓存的是「该 color_system 下」的结果；
    #   若同一个 Style 对象先后用不同 color_system 渲染，第二次会复用第一次的结果。
    #   实际调用路径（Console._render_buffer）对每个终端只使用一个体系，所以是安全的。
    # 【注意】attributes = self._attributes & self._set_attributes：先与掩码求交，
    #   确保未设置的位不会意外输出。
    def _make_ansi_codes(self, color_system: ColorSystem) -> str:
        """Generate ANSI codes for this style.

        Args:
            color_system (ColorSystem): Color system.

        Returns:
            str: String containing codes.
        """
# 【约束】本方法输出的 CSS 必须能被 export_html 的模板直接内联，因此不能包含
#   选择器、花括号或分号以外的结构；新增属性时不要破坏「以 ; 分隔的声明列表」这一格式。

        if self._ansi is None:
            sgr: List[str] = []
            append = sgr.append
            _style_map = self._style_map
            attributes = self._attributes & self._set_attributes
            if attributes:
                if attributes & 1:
                    append(_style_map[0])
                # 【要点】reverse 的语义是「前景背景互换」，因此在 CSS 里最自然的实现就是交换两个变量，
                #   而不是像终端那样输出 7 号 SGR 让终端去交换。
                if attributes & 2:
                    append(_style_map[1])
                # 【要点】dim 在 CSS 中没有对应属性，这里用「前景色向背景色混合 50%」近似终端上的
                #   暗淡效果。默认前景色为 None 时取主题的前景色作为起点，保证混合结果可见。
                if attributes & 4:
                    append(_style_map[2])
                if attributes & 8:
                    append(_style_map[3])
                if attributes & 0b0000111110000:
                    for bit in range(4, 9):
                        if attributes & (1 << bit):
                            append(_style_map[bit])
                if attributes & 0b1111000000000:
                    for bit in range(9, 13):
                        if attributes & (1 << bit):
                            # 【易错】背景色必须传 foreground=False，否则 get_truecolor 会把它当作前景色解析，
                            #   在 8 位色与 default 色的情况下会得到完全错误的颜色。
                            append(_style_map[bit])
            if self._color is not None:
                sgr.extend(self._color.downgrade(color_system).get_ansi_codes())
            if self._bgcolor is not None:
                sgr.extend(
                    self._bgcolor.downgrade(color_system).get_ansi_codes(
                        foreground=False
                    )
                )
            self._ansi = ";".join(sgr)
        return self._ansi

    # ==== 【规范化与选择】 ====
    # 【功能】把样式定义串规范成「等价写法统一」的形式，用作字典键与缓存键。
    # 【算法】尝试解析并 str() 回去；解析失败（语法错误）时退化为「去空白 + 转小写」。
    # 【参数】style：样式定义串。
    # 【返回】str：规范形式。
    # 【为什么】"Bold Red"、"red bold"、"b red" 都等价，规范化后只保留一种表示，
    #   从而让上游的样式缓存（Console._style_cache 等）命中率更高。
    # 【注意】带 lru_cache：对非法样式串也会缓存其退化结果，不会重复抛异常。
    # 【注意】combine 与 chain 的实现完全相同，差别只在调用形式（可迭代对象 vs 位置参数）；
    #   上游保留两者是为了向后兼容，新代码用哪个都可以。
    @classmethod
    @lru_cache(maxsize=1024)
        # 【要点】sum(iterator, start) 会从 start 开始逐个累加，因此起点是第一个样式，
        #   其余样式依次用 __add__ 叠加上去——叠加顺序即参数顺序，后面的覆盖前面的。
    def normalize(cls, style: str) -> str:
        """Normalize a style definition so that styles with the same effect have the same string
        representation.

        Args:
            style (str): A style definition.

        Returns:
            str: Normal form of style definition.
        """
        try:
            return str(cls.parse(style))
        except errors.StyleSyntaxError:
            return style.strip().lower()

    # 【功能】从若干候选样式中取第一个非 None 的。
    # 【参数】*values：Optional[StyleType] 候选值（可以是字符串或 Style）。
    # 【返回】StyleType：第一个非 None 的候选值；注意它不做解析，原样返回。
    # 【注意】全部为 None 时抛 ValueError；调用方需保证至少有一个非空候选。
    # 【为什么】copy() 不重新计算 _hash 与 _ansi：内容未变，缓存可以安全复用；
    #   但 _link_id 必须重算（见下一条注释），这也是 copy() 与「不可变」看似矛盾的地方。
    @classmethod
    # 【协作】Console.get_style(default=...) 与 Text.stylize 的「默认样式」语义
    #   都建立在「None 表示未指定」这一约定之上，本方法正是该约定的实现。
    def pick_first(cls, *values: Optional[StyleType]) -> StyleType:
        """Pick first non-None style."""
        for value in values:
            if value is not None:
                return value
        raise ValueError("expected at least one non-None style")

    # 【功能】Rich 的调试表示协议：决定 repr(style) 的输出内容。
    # 【算法】逐项 yield (名字, 值, 默认值) 三元组，rich_repr 会省略等于默认值的项；
    #   因此只有被设置的属性会出现在 repr 里。
    # 【注意】meta 单独处理：只在非空时输出，且直接给出解码后的字典便于阅读。
    def __rich_repr__(self) -> Result:
        yield "color", self.color, None
        yield "bgcolor", self.bgcolor, None
        yield "bold", self.bold, None,
        yield "dim", self.dim, None,
        yield "italic", self.italic, None
        yield "underline", self.underline, None,
        yield "blink", self.blink, None
        yield "blink2", self.blink2, None
        yield "reverse", self.reverse, None
        # 【注意】lru_cache 的键是 self，因此本方法与 style 的哈希绑定；
        #   但 Style 的 _hash 是惰性计算的，第一次调用会顺带填充缓存。
        yield "conceal", self.conceal, None
        yield "strike", self.strike, None
        yield "underline2", self.underline2, None
        yield "frame", self.frame, None
        yield "encircle", self.encircle, None
        yield "link", self.link, None
        if self._meta:
            yield "meta", self.meta

    # ==== 【相等性与哈希】 ====
    # 【功能】相等判断：委托给哈希比较。
    # 【算法】非 Style 类型返回 NotImplemented（让 Python 尝试反向比较），
    #   否则比较 self.__hash__() == other.__hash__()。
    # 【注意】用哈希相等代替逐字段比较，好处是实现简单、与 _hash 缓存天然一致；
    #   代价是理论上存在哈希碰撞导致「不等却相等」的极小概率。
    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Style):
            return NotImplemented
        return self.__hash__() == other.__hash__()

    # 【功能】不相等判断：与 __eq__ 对称，非 Style 返回 NotImplemented。
    # 【为什么】显式实现 __ne__ 是 Python 2 时代的遗留写法；在 Python 3 中
    #   默认会取 __eq__ 的反，这里保留只是为了明确语义与兼容旧行为。
    def __ne__(self, other: Any) -> bool:
        if not isinstance(other, Style):
            return NotImplemented
        return self.__hash__() != other.__hash__()

    # 【功能】计算并缓存哈希值。
    # 【算法】首次调用时用 _hash_getter 取 6 个决定相等性的字段组成元组求哈希，存入 _hash。
    # 【返回】int。
    # 【注意】_hash 是惰性缓存：Style 构造时不计算，只有被当作字典键或参与比较时才计算；
    #   这使「创建大量样式但只用少数字段」的场景更快。
    def __hash__(self) -> int:
        if self._hash is not None:
            return self._hash
        self._hash = hash(_hash_getter(self))
        return self._hash

    # ==== 【只读属性访问器】 ====
    # 【功能】前景色访问器。
    # 【返回】Optional[Color]：未设置时返回 None（表示「不改变」而不是「默认色」）。
    # 【注意】要得到「实际的终端颜色」需要调用 color.get_truecolor(theme)。
    @property
    def color(self) -> Optional[Color]:
        """The foreground color or None if it is not set."""
        # 【约束】ESC[0m 作为结束序列是硬编码的：它重置全部 SGR 状态，包括调用方在此之前
        #   设置的状态。Rich 之所以能这样做，是因为它总是「每个 Segment 独立渲染完整样式」。
        return self._color

    # 【功能】背景色访问器。
    # 【返回】Optional[Color]：未设置时返回 None。
    # 【协作】transparent_background / background_style 都基于它判断。
    @property
    def bgcolor(self) -> Optional[Color]:
        """The background color or None if it is not set."""
        return self._bgcolor

    # 【功能】超链接访问器。
    # 【返回】Optional[str]：链接 URL，未设置时为 None。
    # 【注意】与之配对的 link_id 是自动生成的，两者共同构成 OSC 8 序列。
    @property
    def link(self) -> Optional[str]:
        """Link text, if set."""
        return self._link

    # 【功能】判断该样式的背景是否「透明」（即不绘制背景）。
    # 【返回】bool：未设置背景色，或背景色是 ColorType.DEFAULT 时为 True。
    # 【协作】Console 决定是否需要用空格填充背景、以及 HTML 导出时是否输出 background-color。
    @property
    def transparent_background(self) -> bool:
        """Check if the style specified a transparent background."""
        return self.bgcolor is None or self.bgcolor.is_default

    # 【功能】返回一个「只保留背景色」的新样式。
    # 【返回】Style。
    # 【协作】用于把背景色从内容样式中剥离，单独应用到填充区域（如 Panel 的空白部分）。
    @property
    def background_style(self) -> "Style":
        """A Style with background only."""
        return Style(bgcolor=self.bgcolor)

    # 【功能】解码并返回元信息字典。
    # 【返回】Dict[str, Any]：始终返回字典，没有元信息时返回空字典（不是 None）。
    # 【注意】每次调用都会 pickle.loads 解密一份**新**字典，因此调用方修改返回值不会影响
    #   原样式；反过来也说明频繁调用有解码开销。
    # 【为什么】把合成逻辑集中到 _add 并加 lru_cache：样式组合在渲染中是热点
    #   （表格每行每列都要 style + get_style(...)），缓存后同一对样式只算一次。
    @property
    def meta(self) -> Dict[str, Any]:
        """Get meta information (can not be changed after construction)."""
        return {} if self._meta is None else cast(Dict[str, Any], loads(self._meta))

    # 【功能】返回去掉全部颜色、但保留属性/链接/元信息的新样式。
    # 【算法】绕过 __init__ 手工构造；属性位域原样复制，链接重新分配 link_id。
    # 【返回】Style；若自身是空样式则直接返回 NULL_STYLE。
    # 【协作】Segment.remove_color 依赖它，实现 Console(no_color=True) 的降级输出。
    @property
    def without_color(self) -> "Style":
        # 【易错】`self._attributes & ~style._set_attributes` 中的 ~ 是无限位取反，
        #   但 Python 的整数是无符号概念上的任意精度补码表示，与 & 结合使用是安全的
        #   （结果只保留 self 的非负位）。不要改成 ~style._attributes，那会丢掉「显式关闭」信息。
        """Get a copy of the style with color removed."""
        if self._null:
            return NULL_STYLE
        style: Style = self.__new__(Style)
        style._ansi = None
        style._style_definition = None
        style._color = None
        # 【要点】元信息合并只在两者都存在时进行，避免为了合并而 pickle/unpickle 空字典。
        style._bgcolor = None
        style._attributes = self._attributes
        style._set_attributes = self._set_attributes
        style._link = self._link
        style._link_id = f"{next(_id_generator)}" if self._link else ""
        style._null = False
        style._meta = None
        # 【注意】__add__ 不处理「style 为 None」的反向情形：None + style 会走 None.__add__，
        #   直接抛 TypeError。需要容忍 None 时请写成 `style_a + (style_b or NULL_STYLE)`。
        style._hash = None
        return style

    # ==== 【样式定义串解析】 ====
    # 【功能】把 "bold red on blue link https://example.com" 这类定义串解析成 Style。
    # 【算法】按空白分词后逐词处理：
    #   · "on"      → 取下一个词作为背景色（先用 Color.parse 校验）；
    #   · "not"     → 取下一个词作为属性名，把该属性显式置为 False；
    #   · "link"    → 取下一个词作为 URL；
    #   · 属性关键字 → 置为 True（支持 d/b/i/u/r/c/s/uu/o 等简写）；
    #   · 其他词     → 尝试作为前景色解析，失败则抛 StyleSyntaxError。
    #   最后调用 Style(color=..., bgcolor=..., link=..., **attributes) 构造对象。
    # 【参数】style_definition：样式定义串；空串或 "none" 直接返回空样式。
    # 【返回】Style 实例（带 lru_cache(4096) 缓存，重复解析同一串零成本）。
    # 【注意】1) 颜色一律延迟到 Color.parse 校验，因此非法颜色名会在这里报错；
    #   2) 后出现的同类词会覆盖先出现的（如 "red blue" 取 blue 作为前景色）；
    #   3) 本方法不负责查找主题名——"info" 这类主题样式名由 Console.get_style 先行查表。
    # 【约束】NULL_STYLE 必须在所有工厂方法之前定义好，因为 null() 等会引用它；
    #   如果把它移到文件末尾会产生 NameError（这些方法在调用时求值，所以顺序其实不影响
    #   定义，但保持在此处便于阅读）。
    # 【注意】default_style 不会被复制，因此调用方后续修改它（虽然不可变）不会影响栈；
    #   但传入同一个 Style 对象给多个栈是安全的，因为 Style 本身不可变。
    @classmethod
    @lru_cache(maxsize=4096)
# 【要点】push 立刻完成合成而不是延迟到 current 读取时，是「以写换读」的取舍：
#   嵌套结构的 push/pop 次数远少于读取 current 的次数（每行文本都要读）。
    def parse(cls, style_definition: str) -> "Style":
        """Parse a style definition.

        Args:
            style_definition (str): A string containing a style.

        Raises:
            errors.StyleSyntaxError: If the style definition syntax is invalid.

        Returns:
            `Style`: A Style instance.
        """
        if style_definition.strip() == "none" or not style_definition:
            # 【返回】返回弹出后的栈顶，方便调用方写 `prev = stack.pop()` 这样的一行式恢复。
            return cls.null()

        STYLE_ATTRIBUTES = cls.STYLE_ATTRIBUTES
        color: Optional[str] = None
        bgcolor: Optional[str] = None
        attributes: Dict[str, Optional[Any]] = {}
        link: Optional[str] = None

        words = iter(style_definition.split())
        for original_word in words:
            word = original_word.lower()
            if word == "on":
                word = next(words, "")
                if not word:
                    raise errors.StyleSyntaxError("color expected after 'on'")
                try:
                    Color.parse(word)
                except ColorParseError as error:
                    raise errors.StyleSyntaxError(
                        f"unable to parse {word!r} as background color; {error}"
                    ) from None
                bgcolor = word

            elif word == "not":
                word = next(words, "")
                attribute = STYLE_ATTRIBUTES.get(word)
                if attribute is None:
                    raise errors.StyleSyntaxError(
                        f"expected style attribute after 'not', found {word!r}"
                    )
                attributes[attribute] = False

            elif word == "link":
                word = next(words, "")
                if not word:
                    raise errors.StyleSyntaxError("URL expected after 'link'")
                link = word

            elif word in STYLE_ATTRIBUTES:
                attributes[STYLE_ATTRIBUTES[word]] = True

            else:
                try:
                    Color.parse(word)
                except ColorParseError as error:
                    raise errors.StyleSyntaxError(
                        f"unable to parse {word!r} as color; {error}"
                    ) from None
                color = word
        style = Style(color=color, bgcolor=bgcolor, link=link, **attributes)
        return style

    # ==== 【导出为 CSS】 ====
    # 【功能】把样式翻译成 HTML/CSS 声明串，用于 Console.export_html。
    # 【算法】1) reverse（反显）直接交换前景/背景色；
    #   2) dim（暗淡）无法在 CSS 表达，用「前景色与主题背景色各半混合」近似；
    #   3) 输出 color 与 text-decoration-color 两条声明（后者让下划线跟随文字颜色）；
    #   4) 背景色、粗体、斜体、下划线、删除线、上划线分别映射到对应 CSS 属性。
    # 【参数】theme：终端主题，用于把 8 位/标准色解析成真彩色；默认用 DEFAULT_TERMINAL_THEME。
    # 【返回】str：以 "; " 连接的 CSS 声明串（可能为空串）。
    # 【注意】1) 只有 self.bold 为真才输出粗体，三态中的 None 与 False 都不输出；
    #   2) 下划线/删除线/上划线都用 text-decoration，同时设置会互相覆盖——
    #      真实终端里它们可以叠加，导出成 HTML 时会退化，这是已知的表达能力差距；
    #   3) 带 lru_cache(1024)，因此同一 (样式, 主题) 组合只计算一次。
    @lru_cache(maxsize=1024)
    def get_html_style(self, theme: Optional[TerminalTheme] = None) -> str:
        """Get a CSS style rule."""
        theme = theme or DEFAULT_TERMINAL_THEME
        css: List[str] = []
        append = css.append

        color = self.color
        bgcolor = self.bgcolor
        if self.reverse:
            color, bgcolor = bgcolor, color
        if self.dim:
            foreground_color = (
                theme.foreground_color if color is None else color.get_truecolor(theme)
            )
            color = Color.from_triplet(
                blend_rgb(foreground_color, theme.background_color, 0.5)
            )
        if color is not None:
            theme_color = color.get_truecolor(theme)
            append(f"color: {theme_color.hex}")
            append(f"text-decoration-color: {theme_color.hex}")
        if bgcolor is not None:
            theme_color = bgcolor.get_truecolor(theme, foreground=False)
            append(f"background-color: {theme_color.hex}")
        if self.bold:
            append("font-weight: bold")
        if self.italic:
            append("font-style: italic")
        if self.underline:
            append("text-decoration: underline")
        if self.strike:
            append("text-decoration: line-through")
        if self.overline:
            append("text-decoration: overline")
        return "; ".join(css)

    # ==== 【样式合成】 ====
    # 【功能】把一组样式依次叠加成一个样式。
    # 【算法】sum(iter_styles, next(iter_styles))，即从第二个元素起逐个与累积值相加。
    # 【参数】styles：样式可迭代对象。
    # 【返回】Style：合成结果。
    # 【易错】next(iter_styles) 会先取走第一个元素；若传入空序列会抛 StopIteration。
    # 【注意】本方法只看第一个元素之后的项，第一个元素本身不参与「加」运算——
    #   这意味着它对空序列不安全，但对单元素序列返回该元素本身。
    @classmethod
    # 【易错】不要用 combine 把主题样式与内联样式合并——它的结果会是完整的 Style
    #   而非「可叠加的补丁」，正确做法是直接用 + 运算符。
    def combine(cls, styles: Iterable["Style"]) -> "Style":
        """Combine styles and get result.

        Args:
            styles (Iterable[Style]): Styles to combine.

        Returns:
            Style: A new style instance.
        """
        iter_styles = iter(styles)
        return sum(iter_styles, next(iter_styles))

    # 【功能】把位置参数给出的若干样式叠加成一个。
    # 【算法】与 combine 完全相同（先取第一个，再对剩余项求和）。
    # 【参数】*styles：样式列表。
    # 【返回】Style。
    # 【易错】不能传空参数（会抛 StopIteration）；这与 combine 是同一个实现细节，
    #   两处都保留是因为上游历史原因（combine 接受可迭代对象，chain 接受位置参数）。
    @classmethod
    def chain(cls, *styles: "Style") -> "Style":
        """Combine styles from positional argument in to a single style.

        Args:
            *styles (Iterable[Style]): Styles to combine.

        Returns:
            Style: A new style instance.
        """
        iter_styles = iter(styles)
        return sum(iter_styles, next(iter_styles))

    # ==== 【复制与派生】 ====
    # 【功能】复制一个样式。
    # 【算法】绕过 __init__ 手工复制全部字段；但 link_id 会重新分配（保证唯一），
    #   其余缓存（_ansi、_style_definition、_hash）直接复用。
    # 【返回】Style；若自身是空样式则返回 NULL_STYLE（保持单例语义）。
    # 【为什么】link_id 必须重新分配：OSC 8 超链接用 id 把「开始」与「结束」配对，
    #   两个对象共享同一个 id 会让终端把不同的链接错误地连成一片。
    # 【注意】Console.get_style 对带链接的样式会返回 copy()，正是出于这个原因。
    # 【协作】Console.get_style 在样式带链接时返回 copy()，
    #   这是 link_id 唯一性的唯一保障点，删除它会让嵌套超链接互相串扰。
    def copy(self) -> "Style":
        """Get a copy of this style.

        Returns:
            Style: A new Style instance with identical attributes.
        """
        if self._null:
            return NULL_STYLE
        style: Style = self.__new__(Style)
        style._ansi = self._ansi
        style._style_definition = self._style_definition
        style._color = self._color
        style._bgcolor = self._bgcolor
        style._attributes = self._attributes
        style._set_attributes = self._set_attributes
        style._link = self._link
        style._link_id = f"{next(_id_generator)}" if self._link else ""
        style._hash = self._hash
        style._null = False
        style._meta = self._meta
        return style

    # 【功能】返回去掉链接与元信息、其余不变的样式副本。
    # 【算法】同 copy，但 _link 置 None、_link_id 置空、_meta 置 None。
    # 【返回】Style；空样式直接返回 NULL_STYLE。
    # 【注意】带 lru_cache(128)：因为去元信息后的样式数量有限，缓存收益明显；
    #   缓存上限只有 128，样式组合极多时会频繁失效。
    # 【协作】用于把「可交互的样式」安全地交给不需要元信息的渲染路径。
    @lru_cache(maxsize=128)
    def clear_meta_and_links(self) -> "Style":
        """Get a copy of this style with link and meta information removed.

        Returns:
            Style: New style object.
        """
        if self._null:
            return NULL_STYLE
        style: Style = self.__new__(Style)
        style._ansi = self._ansi
        style._style_definition = self._style_definition
        style._color = self._color
        style._bgcolor = self._bgcolor
        style._attributes = self._attributes
        style._set_attributes = self._set_attributes
        style._link = None
        style._link_id = ""
        style._hash = None
        style._null = False
        style._meta = None
        return style

    # 【功能】返回一个除超链接外与自身完全相同的样式副本。
    # 【参数】link：新的链接 URL，None 表示去掉链接。
    # 【返回】Style：新的实例；link 非空时会分配新的 link_id。
    # 【注意】无论 link 是否与原来相同都会新建对象（因为 link_id 必须重算），
    #   所以不要把它当作幂等操作放在循环里频繁调用。
    # 【协作】Segment.strip_links 用它批量去除超链接。
    def update_link(self, link: Optional[str] = None) -> "Style":
        """Get a copy with a different value for link.

        Args:
            link (str, optional): New value for link. Defaults to None.

        Returns:
            Style: A new Style instance.
        """
        style: Style = self.__new__(Style)
        style._ansi = self._ansi
        style._style_definition = self._style_definition
        style._color = self._color
        style._bgcolor = self._bgcolor
        style._attributes = self._attributes
        style._set_attributes = self._set_attributes
        style._link = link
        style._link_id = f"{next(_id_generator)}" if link else ""
        style._hash = None
        style._null = False
        style._meta = self._meta
        return style

    # ==== 【渲染为终端字节】 ====
    # 【功能】把样式应用到一段文本上，返回带 ANSI 转义序列的字符串。
    # 【算法】1) 文本为空或 color_system 为 None 时原样返回文本（快速失败）；
    #   2) 取得（或计算）SGR 参数，套上 ESC[...m ... ESC[0m；
    #   3) 若存在超链接且不是 legacy Windows，再用 OSC 8 序列把整个结果包起来。
    # 【参数】text：待染色的文本；color_system：目标颜色体系，None 表示不输出任何颜色；
    #   legacy_windows：Windows 传统控制台不支持 OSC 8，置真时跳过超链接。
    # 【返回】str：可能原样返回 text。
    # 【注意】1) 结尾固定用 ESC[0m 全量重置，而不是逐个属性关闭——实现简单但会
    #      连带清掉终端里其它挂起状态；
    #   2) OSC 8 的结束序列是 ESC]8;;ESC\\（空参数即关闭链接）；
    #   3) 本方法不修改 _ansi 以外的任何状态。
    def render(
        self,
        text: str = "",
        *,
        color_system: Optional[ColorSystem] = ColorSystem.TRUECOLOR,
        legacy_windows: bool = False,
    ) -> str:
        """Render the ANSI codes for the style.

        Args:
            text (str, optional): A string to style. Defaults to "".
            color_system (Optional[ColorSystem], optional): Color system to render to. Defaults to ColorSystem.TRUECOLOR.

        Returns:
            str: A string containing ANSI style codes.
        """
        if not text or color_system is None:
            return text
        attrs = self._ansi or self._make_ansi_codes(color_system)
        rendered = f"\x1b[{attrs}m{text}\x1b[0m" if attrs else text
        if self._link and not legacy_windows:
            rendered = (
                f"\x1b]8;id={self._link_id};{self._link}\x1b\\{rendered}\x1b]8;;\x1b\\"
            )
        return rendered

    # 【功能】把带样式的文本直接写到标准输出。
    # 【参数】text：要显示的文本，None 表示用 str(self)（样式定义串本身）。
    # 【注意】仅供测试/调试使用；它直接操作 sys.stdout，绕过 Console，不进行宽度处理，
    #   也不受 no_color 等设置影响（总是按 TRUECOLOR 渲染）。
    def test(self, text: Optional[str] = None) -> None:
        """Write text with style directly to terminal.

        This method is for testing purposes only.

        Args:
            text (Optional[str], optional): Text to style or None for style name.

        """
        text = text or str(self)
        sys.stdout.write(f"{self.render(text)}\n")

    # ==== 【样式合成核心 _add】 ====
    # 【功能】实现「在 self 之上叠加 style」的语义，是所有样式组合的唯一实现。
    # 【算法】1) style 为 None 或空样式 → 返回 self；self 为空 → 返回 style；
    #   2) 颜色：style 的颜色优先，未设置则沿用 self 的（`or` 利用 None 的假值性）；
    #   3) 属性位：先用 `self._attributes & ~style._set_attributes` 清掉「style 显式设置过」
    #      的那些位，再 `| (style._attributes & style._set_attributes)` 填入 style 的值；
    #      这样 style 中被显式设为 False 的属性会真正覆盖掉 self 里的 True；
    #   4) 掩码取两者并集；链接与元信息同样以 style 优先，meta 则做字典合并；
    #   5) _null 直接采用 style._null（因为 self 与 style 都非空才会走到这里）。
    # 【参数】style：要叠加的样式，可为 None。
    # 【返回】Style：新实例（self 与 style 都不被修改）。
    # 【注意】1) 带 lru_cache(1024)，因此「Style 要可哈希」是本方法可用性的前提；
    #   2) 元信息合并用 {**self.meta, **style.meta}，即 style 的键覆盖 self 的同名键；
    #   3) 只在两者都有 meta 时才合并，否则直接取非空的那个（省一次 pickle 往返）。
    @lru_cache(maxsize=1024)
    def _add(self, style: Optional["Style"]) -> "Style":
        if style is None or style._null:
            return self
        if self._null:
            return style
        new_style: Style = self.__new__(Style)
        new_style._ansi = None
        new_style._style_definition = None
        new_style._color = style._color or self._color
        new_style._bgcolor = style._bgcolor or self._bgcolor
        new_style._attributes = (self._attributes & ~style._set_attributes) | (
            style._attributes & style._set_attributes
        )
        new_style._set_attributes = self._set_attributes | style._set_attributes
        new_style._link = style._link or self._link
        new_style._link_id = style._link_id or self._link_id
        new_style._null = style._null
        if self._meta and style._meta:
            new_style._meta = dumps({**self.meta, **style.meta})
        else:
            new_style._meta = self._meta or style._meta
        new_style._hash = None
        return new_style

    # 【功能】样式叠加运算符，实现 `style_a + style_b`。
    # 【算法】委托 _add；若结果带链接则再 copy 一次，使 link_id 保持唯一。
    # 【参数】style：要叠加的样式，允许为 None（此时按约定直接返回 self）。
    # 【返回】Style。
    # 【注意】只有 Style 在左边时才支持这种写法：None + style 会抛 TypeError。
    # 【协作】Panel/Table/Text 大量使用 `style + console.get_style(...)` 的组合形式。
    def __add__(self, style: Optional["Style"]) -> "Style":
        combined_style = self._add(style)
        return combined_style.copy() if combined_style.link else combined_style


# ==== 【全局空样式单例】 ====
# 【要点】NULL_STYLE 在模块导入时创建，是整个进程共享的空样式对象。
# 【约束】所有「样式为空」的代码路径（null()、copy()、clear_meta_and_links() 等）
#   都必须返回这个单例而不是新建对象，否则 `is NULL_STYLE` 这类快速判断会失效。
# 【易错】进程内共享意味着它携带的 link_id 为空串、meta 为 None——
#   任何试图往它上面挂状态的做法都是错误的。
NULL_STYLE = Style()


# ==== 【样式栈 StyleStack】 ====
# ---------------------------------------------------------------------------
# 【职责】维护一串「累积样式」：每次 push 都把新样式与栈顶样式相加后入栈，
#   因此栈顶永远是「从底到顶所有样式的合成结果」，供 markup / tree 这类嵌套结构
#   在进入子元素时获取当前有效样式。
# 【关键属性】_stack：Style 列表，_stack[-1] 即当前生效样式（current）。
# 【不变量】栈中每个元素都是「它之下所有样式的合成」，因此不能只弹栈不重算；
#   栈永不为空（构造函数至少放入 default_style）。
# 【协作】tree.py 用两个 StyleStack 分别跟踪节点样式与引导线样式。
# ---------------------------------------------------------------------------
class StyleStack:
    """A stack of styles."""

    __slots__ = ["_stack"]

    # 【功能】用初始样式初始化栈。
    # 【参数】default_style：栈底样式，通常来自 get_style(self.style)。
    # 【注意】__slots__ 只声明了 _stack，因此 StyleStack 也是紧凑对象。
    def __init__(self, default_style: "Style") -> None:
        self._stack: List[Style] = [default_style]

    # 【功能】调试用表示：直接显示整个样式栈。
    # 【返回】str：形如 <stylestack [Style(...), ...]>。
    # 【注意】列表末尾（_stack[-1]）才是当前生效样式，阅读 repr 时“越靠右越当前”。
    def __repr__(self) -> str:
        return f"<stylestack {self._stack!r}>"

    # 【功能】取栈顶（当前生效）样式。
    # 【返回】Style：累积后的当前样式。
    # 【注意】返回的是样式对象本身（不可变），无需复制。
    @property
    def current(self) -> Style:
        """Get the Style at the top of the stack."""
        return self._stack[-1]

    # 【功能】压入一个新样式。
    # 【算法】把「当前栈顶 + 新样式」的结果入栈，而不是把新样式原样入栈——
    #   因此栈里存的始终是累积结果。
    # 【参数】style：要与当前样式合成的样式。
    # 【注意】这意味着 push/pop 不是严格对称的（pop 只是丢弃栈顶），
    #   但也因此省去了每次读取时重新累加的开销。
    def push(self, style: Style) -> None:
        """Push a new style on to the stack.

        Args:
            style (Style): New style to combine with current style.
        """
        self._stack.append(self._stack[-1] + style)

    # 【功能】弹出栈顶样式并返回新的栈顶。
    # 【返回】Style：弹出后位于栈顶的累积样式，等价于 stack.current。
    # 【易错】本方法不检查栈深度；只要栈底默认样式还在，pop 就是安全的，
    #   但连续 pop 到空栈后再调用 current 会抛 IndexError。
    def pop(self) -> Style:
        """Pop last style and discard.

        Returns:
            Style: New current style (also available as stack.current)
        """
        self._stack.pop()
        return self._stack[-1]
