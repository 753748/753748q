# ---------------------------------------------------------------------------
# 【模块补充说明】Rich 的颜色子系统采用「一个值对象 + 三张调色板 + 若干缓存」的结构：
#   Color 只记录「这个颜色是什么」，至于「终端能显示成什么样」由 ColorSystem 描述、
#   由 downgrade 负责转换。因此在阅读任何一处颜色逻辑时，都应先问两个问题：
#     1) 这个 Color 是哪种 type（number 还是 triplet 有效）？
#     2) 当前目标的 ColorSystem 是哪一个（决定要不要降级）？
#   另外注意三张调色板的角色分工：
#     · STANDARD_PALETTE  —— 16 个 ANSI 标准色的「参考 RGB」，只用于降级时找最近邻；
#     · EIGHT_BIT_PALETTE —— 256 色的完整 RGB 表，用于 8 位色还原真彩色；
#     · WINDOWS_PALETTE   —— Windows 传统控制台的 16 色表，与标准调色板并不相同。
#   三张表都定义在 _palettes.py 中，本模块只负责在正确的时机查询它们。
# 【约束】颜色相关的缓存在本模块共有三处（parse / get_ansi_codes / downgrade），
#   它们的 maxsize 都是 1024；若某界面出现大量不同颜色（如图片转字符画），
#   可以考虑调大这些上限——但要注意 Color 是不可变对象，缓存不会导致状态不一致。
# ---------------------------------------------------------------------------
# 【附：常见误用与排查】
#   · 对真彩色取 .number 或对默认色取 .triplet 都会得到 None，读取前先看 .type；
#   · 标准色（0~15）的实际观感取决于终端主题，不要用固定 RGB 去断言它的十六进制；
#   · Color.parse 只认 #RRGGBB / color(N) / rgb(R,G,B) 三种写法，不接受 #RGB 简写；
#   · 降级是有损的：同一个真彩色在 256 色与 16 色终端上可能落到明显不同的色号；
#   · get_ansi_codes 输出的元组必须由调用方用 ";" 连接，它本身不是完整转义序列。
# ---------------------------------------------------------------------------
import re
import sys
from colorsys import rgb_to_hls
from enum import IntEnum
from functools import lru_cache
from typing import TYPE_CHECKING, NamedTuple, Optional, Tuple

from ._palettes import EIGHT_BIT_PALETTE, STANDARD_PALETTE, WINDOWS_PALETTE
from .color_triplet import ColorTriplet
from .repr import Result, rich_repr
from .terminal_theme import DEFAULT_TERMINAL_THEME

if TYPE_CHECKING:  # pragma: no cover
    from .terminal_theme import TerminalTheme
    from .text import Text
# ---------------------------------------------------------------------------
# 【模块定位】本模块是 Rich 颜色子系统的核心：把「颜色」抽象成一个不可变的 Color 值对象，
#   并提供三种终端颜色体系（STANDARD 16 色 / EIGHT_BIT 256 色 / TRUECOLOR 真彩色）
#   之间的解析、降级与 ANSI 编码能力。渲染链路中的位置是：
#   markup/Style → Style._make_ansi_codes → Color.downgrade(目标体系) → get_ansi_codes()
#   → 拼进 SGR 序列；导出 HTML 时则走 get_truecolor() → ColorTriplet.hex。
# 【关键数据结构】
#   · ColorSystem(IntEnum)：终端能力（STANDARD=1 / EIGHT_BIT=2 / TRUECOLOR=3 / WINDOWS=4）；
#   · ColorType(IntEnum)：颜色自身的类型（DEFAULT=0 / STANDARD=1 / EIGHT_BIT=2 /
#     TRUECOLOR=3 / WINDOWS=4），比 ColorSystem 多一个 DEFAULT；
#   · Color(NamedTuple)：name / type / number / triplet 四元组；
#   · ANSI_COLOR_NAMES：CSS 颜色名 → xterm-256 索引的大字典；
#   · ColorTriplet：RGB 三元组（定义在 color_triplet.py）。
# 【设计要点】
#   · Color 是 NamedTuple：不可变、可哈希，因此能安全地作为 Style 的字段与 lru_cache 的键；
#   · 「降级」是单向不可逆的近似：真彩色 → 8 位 → 16 色，每一步都在色板上找最近邻，
#     因此同一颜色在不同终端上可能显示不同，这是终端能力差异的必然结果；
# 【不变量】STANDARD=1 / EIGHT_BIT=2 / TRUECOLOR=3 这三个数值同时被 ColorType 复用，
#   因此 Color.system 才能用 `ColorSystem(int(self.type))` 直接转换；
#   给任一枚举插入新成员都会破坏这一对应关系。
#   · 调色板数据放在 _palettes.py（由 palette.Palette 提供最近邻匹配），本模块只做调度；
#   · parse 与 get_ansi_codes、downgrade 都带 lru_cache：颜色解析在渲染热路径上非常频繁，
#     且入参（字符串 / Color）天然可哈希。
# 【阅读建议】先读 ColorSystem 与 ColorType 的区别（一个描述终端、一个描述颜色本身），
#   再读 Color 的四个字段与 parse / from_* 工厂方法（颜色的构造方式），
#   然后重点读 get_truecolor（还原成 RGB）与 downgrade（按终端能力降级），
#   最后看 get_ansi_codes（编码成 SGR 参数）。
#   依赖：_palettes（三张调色板）、color_triplet、terminal_theme（默认终端主题）、repr。
#   被依赖：style（样式需要颜色）、console（颜色体系检测与 blend）、ansi（ANSI 解析）、
#   _win32_console、syntax、text 等。
# ---------------------------------------------------------------------------


# ==== 【平台常量】 ====
# 【要点】WINDOWS 在导入时求值一次，用于选择 Windows 传统控制台的颜色体系。
# 【注意】这里判断的是「运行平台」而不是「终端能力」：在 Windows 上运行但输出重定向到
#   文件时，颜色体系由 Console 另行决定，本常量只影响默认值的推断。
# 【兼容】除了这个平台常量，颜色系统还依赖 _palettes.WINDOWS_PALETTE 中的
#   16 个 RGB 值；它们取自 Windows 10 控制台的默认配色，与 ANSI 标准色并不相同，
#   因此在 Windows 传统控制台上做降级必须走 WINDOWS 分支而不是 STANDARD 分支。
WINDOWS = sys.platform == "win32"


# ==== 【颜色体系 ColorSystem】 ====
# ---------------------------------------------------------------------------
# 【职责】描述「终端能显示多少种颜色」，是颜色降级的目标与 ANSI 编码的依据。
# 【关键属性】成员值 1~4 与 ColorType 的前四个值一一对应（见 system 属性）。
# 【不变量】STANDARD=1 与 ColorType.STANDARD=1 必须保持相等，否则 Color.system 会把
#   颜色类型错当成颜色体系；两者是一组必须同步修改的常量。
# 【协作】Console 在启动时探测终端能力并保存一个 ColorSystem；
#   Style._make_ansi_codes 把它透传给 Color.downgrade / get_ansi_codes。
# ---------------------------------------------------------------------------
# 【兼容性背景】不同终端对颜色的支持可以粗略分成三档，Rich 用一个 ColorSystem
#   枚举把这三档（外加 Windows 传统控制台）编码下来：
#     · STANDARD  —— 8/16 色，几乎所有终端都支持（SGR 30-37 / 90-97）；
#     · EIGHT_BIT —— xterm-256（SGR 38;5;N），PuTTY、iTerm、Windows Terminal 均支持；
#     · TRUECOLOR —— 24 位色（SGR 38;2;R;G;B），现代终端支持，也是色值最精确的一档；
#     · WINDOWS   —— Windows 传统控制台的 16 色调色板，其 RGB 与标准调色板不同。
#   选择哪一档由 Console 在启动时探测（环境变量 COLORTERM、TERM 等），
#   并在渲染时透传给 Style 与 Color。
class ColorSystem(IntEnum):
    """One of the 3 color system supported by terminals."""

    # 【要点】STANDARD 指最基础的 8/16 色（SGR 30-37、90-97 与 40-47、100-107），
    #   几乎所有终端都支持，是最安全的降级目标。
    STANDARD = 1
    # 【要点】EIGHT_BIT 指 xterm-256 色（SGR 38;5;N / 48;5;N），
    #   前 16 个索引与 STANDARD 调色板一致，16-231 是 6×6×6 色立方，232-255 是灰阶。
    EIGHT_BIT = 2
    # 【要点】TRUECOLOR 指 24 位直接色（SGR 38;2;R;G;B），也就是常说的 16m 色；
    #   它不需要降级，是 Rich 内部的「规范表示」。
    # --- 8 位扩展色名（按色系大致分组，顺序并不严格） -------------------------
    # 【要点】从这里开始是 xterm-256 的扩展色名。它们与终端主题无关，
    #   因此 is_system_defined 为 False，导出 HTML 时可以直接算出十六进制值。
    TRUECOLOR = 3
    WINDOWS = 4

    # 【功能】让 repr(ColorSystem.TRUECOLOR) 显示为 "ColorSystem.TRUECOLOR"。
    # 【为什么】IntEnum 默认的 repr 是 "<ColorSystem.TRUECOLOR: 3>"，在调试输出里过于冗长；
    #   定制后与 Rich 其它枚举的显示风格一致。
    def __repr__(self) -> str:
        return f"ColorSystem.{self.name}"

    # 【功能】让 str(ColorSystem.TRUECOLOR) 显示为可读名字而不是数字。
    # 【返回】str：即 repr(self)，形如 “ColorSystem.TRUECOLOR”。
    # 【注意】Python 3.11 起 IntEnum 的 str 会返回数值（“3”），这里显式改为名字，
    #   避免日志/报告中出现无意义的数字；因此不要依赖 str() 做数值反序列化。
    def __str__(self) -> str:
        return repr(self)


# ==== 【颜色类型 ColorType】 ====
# ---------------------------------------------------------------------------
# 【职责】描述「一个 Color 对象本身是什么类型」，决定它的 number / triplet 哪个字段有效。
# 【关键属性】DEFAULT=0 表示「使用终端默认色」；其余四个值与 ColorSystem 对齐。
# 【不变量】1) type == STANDARD ⇒ number 在 0..15；
#   2) type == EIGHT_BIT ⇒ number 在 16..255；
#   3) type == TRUECOLOR ⇒ triplet 非空、number 为 None；
#   4) type == DEFAULT ⇒ number 与 triplet 均为 None；
#   5) 前四个成员的值必须与 ColorSystem 相同（system 属性依赖这一巧合）。
# 【协作】Color.from_ansi / from_triplet / parse 负责保证上述不变量。
# ---------------------------------------------------------------------------
# 【为什么要区分 type 与 system】
#   type 回答「这个颜色是怎么描述的」（用色号还是 RGB），
#   system 回答「终端能显示到什么程度」。
#   例如一个 ColorType.TRUECOLOR 的颜色在 ColorSystem.EIGHT_BIT 的终端上必须降级，
#   而一个 ColorType.EIGHT_BIT 的颜色在 TRUECOLOR 终端上无须改动即可使用
#   （8 位色号总能映射到确定的 RGB）。两者不可互换，这是本模块最重要的一对概念。
class ColorType(IntEnum):
    """Type of color stored in Color class."""

    # 【要点】DEFAULT 没有对应的 ColorSystem 成员：它不是一个「显示多少色」的概念，
    #   而是「不指定颜色」；Color.system 会对它做特殊处理（当作 STANDARD）。
    DEFAULT = 0
    STANDARD = 1
    EIGHT_BIT = 2
    TRUECOLOR = 3
    WINDOWS = 4

    # 【功能】让 repr(ColorType.TRUECOLOR) 显示为 "ColorType.TRUECOLOR"。
    # 【注意】本类只重写了 __repr__ 而没有 __str__：str(ColorType.X) 仍会走 IntEnum 的
    #   行为输出数字——这与 ColorSystem 不同，是有意的（parse 的调试信息里用 repr）。
    def __repr__(self) -> str:
        return f"ColorType.{self.name}"


# ==== 【颜色名表（CSS 风格名 → xterm-256 索引）】 ====
# ---------------------------------------------------------------------------
# --- 亮色与浅色系 --------------------------------------------------------
# 【易错】同一个色号常有多个名字（历史别名，如 grey/gray 拼写差异），
#   重复的键会被后写的值覆盖；新增别名不影响逻辑，但改动时要留意字典里的重复项。
# 【职责】把 W3C/CSS 风格的颜色名映射到 256 色终端的调色板索引，
#   让用户可以写 "red"、"deep_sky_blue1" 这样的名字而不必记数字。
# 【关键属性】键是颜色名（小写下划线），值是 0..255 的调色板索引。
# 【不变量】1) 0..15 是最基本的 ANSI 名（black/red/…/bright_white）；
#   2) 16 以上是 xterm-256 的扩展名，同一个索引常有多个别名
#      （grey/gray 拼写、不同历史来源的名字）；
#   3) 所有值必须落在 EIGHT_BIT_PALETTE 的范围内，否则 get_truecolor 会越界。
# 【易错】名字里用下划线而不是空格或连字符（"deep_sky_blue1" 而非 "deep sky blue"）；
#   未收录的名字会在 parse 里报 ColorParseError。
# 【协作】parse 用它做查表；_palettes.EIGHT_BIT_PALETTE 提供索引到 RGB 的映射。
# ---------------------------------------------------------------------------
ANSI_COLOR_NAMES = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
    "bright_black": 8,
    "bright_red": 9,
    "bright_green": 10,
    "bright_yellow": 11,
    "bright_blue": 12,
    "bright_magenta": 13,
    "bright_cyan": 14,
    "bright_white": 15,
    "grey0": 16,
    "gray0": 16,
    "navy_blue": 17,
    "dark_blue": 18,
    "blue3": 20,
    "blue1": 21,
    "dark_green": 22,
    "deep_sky_blue4": 25,
    "dodger_blue3": 26,
    "dodger_blue2": 27,
    "green4": 28,
    "spring_green4": 29,
    "turquoise4": 30,
    "deep_sky_blue3": 32,
    "dodger_blue1": 33,
    "green3": 40,
    "spring_green3": 41,
    "dark_cyan": 36,
    "light_sea_green": 37,
    "deep_sky_blue2": 38,
    "deep_sky_blue1": 39,
    "spring_green2": 47,
    "cyan3": 43,
    "dark_turquoise": 44,
    "turquoise2": 45,
    "green1": 46,
    "spring_green1": 48,
    "medium_spring_green": 49,
    "cyan2": 50,
    "cyan1": 51,
    "dark_red": 88,
    "deep_pink4": 125,
    "purple4": 55,
    "purple3": 56,
    "blue_violet": 57,
    "orange4": 94,
    "grey37": 59,
    "gray37": 59,
    "medium_purple4": 60,
    "slate_blue3": 62,
    "royal_blue1": 63,
    "chartreuse4": 64,
    "dark_sea_green4": 71,
    "pale_turquoise4": 66,
    "steel_blue": 67,
    "steel_blue3": 68,
    "cornflower_blue": 69,
    "chartreuse3": 76,
    "cadet_blue": 73,
    "sky_blue3": 74,
    "steel_blue1": 81,
    "pale_green3": 114,
    "sea_green3": 78,
    "aquamarine3": 79,
    "medium_turquoise": 80,
    "chartreuse2": 112,
    "sea_green2": 83,
    "sea_green1": 85,
    "aquamarine1": 122,
    "dark_slate_gray2": 87,
    "dark_magenta": 91,
    "dark_violet": 128,
    "purple": 129,
    "light_pink4": 95,
    "plum4": 96,
    "medium_purple3": 98,
    "slate_blue1": 99,
    "yellow4": 106,
    "wheat4": 101,
    "grey53": 102,
    "gray53": 102,
    "light_slate_grey": 103,
    "light_slate_gray": 103,
    "medium_purple": 104,
    "light_slate_blue": 105,
    "dark_olive_green3": 149,
    "dark_sea_green": 108,
    "light_sky_blue3": 110,
    "sky_blue2": 111,
    "dark_sea_green3": 150,
    "dark_slate_gray3": 116,
    "sky_blue1": 117,
    "chartreuse1": 118,
    "light_green": 120,
    "pale_green1": 156,
    "dark_slate_gray1": 123,
    "red3": 160,
    "medium_violet_red": 126,
    "magenta3": 164,
    "dark_orange3": 166,
    # --- 灰阶：232~255（24 级灰） -------------------------------------------
    # 【要点】greyN / grayN 里的 N 是灰度百分比（3% ~ 93%），色号从 232 递增到 255；
    #   它们与 16（纯黑）、231（纯白）共同构成降级时的灰阶目标。
    # 【约束】downgrade 中 gray = round(l*25) 的公式正是按这 26 个取值点设计的
    #   （0 与 25 需要单独钳位到 16 与 231），两处必须保持同步。
    "indian_red": 167,
    "hot_pink3": 168,
    "medium_orchid3": 133,
    "medium_orchid": 134,
    "medium_purple2": 140,
    "dark_goldenrod": 136,
    "light_salmon3": 173,
    "rosy_brown": 138,
    "grey63": 139,
    "gray63": 139,
    "medium_purple1": 141,
    "gold3": 178,
    "dark_khaki": 143,
    "navajo_white3": 144,
    "grey69": 145,
    "gray69": 145,
    "light_steel_blue3": 146,
    "light_steel_blue": 147,
    "yellow3": 184,
    "dark_sea_green2": 157,
    "light_cyan3": 152,
    "light_sky_blue1": 153,
    "green_yellow": 154,
    "dark_olive_green2": 155,
    "dark_sea_green1": 193,
    "pale_turquoise1": 159,
    "deep_pink3": 162,
    "magenta2": 200,
    "hot_pink2": 169,
    "orchid": 170,
    "medium_orchid1": 207,
    "orange3": 172,
    "light_pink3": 174,
    "pink3": 175,
    "plum3": 176,
    "violet": 177,
    "light_goldenrod3": 179,
    "tan": 180,
    "misty_rose3": 181,
    "thistle3": 182,
    "plum2": 183,
    "khaki3": 185,
    "light_goldenrod2": 222,
    "light_yellow3": 187,
    "grey84": 188,
    "gray84": 188,
    "light_steel_blue1": 189,
    "yellow2": 190,
    "dark_olive_green1": 192,
    "honeydew2": 194,
    "light_cyan1": 195,
    "red1": 196,
    "deep_pink2": 197,
    "deep_pink1": 199,
    "magenta1": 201,
    "orange_red1": 202,
    "indian_red1": 204,
    "hot_pink": 206,
    "dark_orange": 208,
    "salmon1": 209,
    "light_coral": 210,
    "pale_violet_red1": 211,
    "orchid2": 212,
    "orchid1": 213,
    "orange1": 214,
    "sandy_brown": 215,
    "light_salmon1": 216,
    "light_pink1": 217,
    "pink1": 218,
    "plum1": 219,
    "gold1": 220,
    # 【要点】四个字段的合法取值组合是受约束的（见类注释）：
    #   DEFAULT → number=None, triplet=None；
    #   STANDARD / EIGHT_BIT / WINDOWS → number 有值, triplet=None；
    #   TRUECOLOR → triplet 有值, number=None。
    #   因此读取 number 之前应先判断类型，否则会拿到 None（下游用 assert 兜底）。
    "navajo_white1": 223,
    "misty_rose1": 224,
    "thistle1": 225,
    "yellow1": 226,
    "light_goldenrod1": 227,
    "khaki1": 228,
    "wheat1": 229,
    "cornsilk1": 230,
    "grey100": 231,
    "gray100": 231,
    "grey3": 232,
    "gray3": 232,
    "grey7": 233,
    "gray7": 233,
    "grey11": 234,
    "gray11": 234,
    "grey15": 235,
    "gray15": 235,
    "grey19": 236,
    "gray19": 236,
    "grey23": 237,
    "gray23": 237,
    "grey27": 238,
    "gray27": 238,
    "grey30": 239,
    "gray30": 239,
    "grey35": 240,
    "gray35": 240,
    "grey39": 241,
    "gray39": 241,
    "grey42": 242,
    "gray42": 242,
    "grey46": 243,
    "gray46": 243,
    "grey50": 244,
    "gray50": 244,
    "grey54": 245,
    "gray54": 245,
    "grey58": 246,
    "gray58": 246,
    "grey62": 247,
    "gray62": 247,
    "grey66": 248,
    "gray66": 248,
    "grey70": 249,
    "gray70": 249,
    "grey74": 250,
    "gray74": 250,
    "grey78": 251,
    "gray78": 251,
    "grey82": 252,
    "gray82": 252,
    "grey85": 253,
    "gray85": 253,
    "grey89": 254,
    "gray89": 254,
    "grey93": 255,
    "gray93": 255,
}


# ==== 【异常】 ====
# 【功能】表示「颜色字符串无法解析」。
# 【注意】本异常继承 Exception 而不是 rich.errors.StyleError：color.py 刻意不依赖
#   errors 模块，以避免 style.py ↔ color.py 之间出现循环导入；Style.parse 捕获它并
#   转换成 errors.StyleSyntaxError 后再向上抛。
# 【协作】Style.parse 捕获本异常并改写为 errors.StyleSyntaxError，
#   并在消息里附上原始颜色串，从而让「样式定义串里的颜色写错了」也能得到清晰报错。
class ColorParseError(Exception):
    """The color could not be parsed."""


# ==== 【颜色定义串正则】 ====
# 【要点】一次性匹配三种写法，用捕获组区分：
#   1) #RRGGBB        → 组 1（六位十六进制）；
#   2) color(N)       → 组 2（0~255 的十进制）；
#   3) rgb(R,G,B)     → 组 3（逗号或空白分隔的数字，允许前后有空格）。
# 【注意】1) 整个模式锚定了 ^ 与 $，因此 input 两端多余的空格必须由调用方先 strip
#      （parse 里确实先做了 lower().strip()）；
#   2) 组 3 用的是字符类 [\d\s,]+ 而不是严格的数字列表，实际校验（个数、范围）
#      交给 parse 里的后续代码；
#   3) 不支持 #RGB 简写、rgb(100% 100% 100%) 与 hsl() 语法——这是有意的克制。
# 【兼容】re.VERBOSE 允许模式里出现空白与注释，使三种写法可以分行书写。
RE_COLOR = re.compile(
    r"""^
\#([0-9a-f]{6})$|
color\(([0-9]{1,3})\)$|
rgb\(([\d\s,]+)\)$
""",
    re.VERBOSE,
)


# ==== 【核心数据结构 Color】 ====
# ---------------------------------------------------------------------------
# 【职责】一个不可变的颜色值对象，同时充当「颜色定义串的解析结果」与「ANSI 编码的输入」。
# 【关键属性】name 原始名称（用于显示与 __str__）；type 颜色类型；
#   number 调色板索引（STANDARD / EIGHT_BIT / WINDOWS 时有效）；
#   triplet RGB 三元组（TRUECOLOR 时有效）。
# 【不变量】见 ColorType 的注释：type 与 number/triplet 的有效性严格对应；
#   name 只用于展示，不参与相等性判断（相等性由 NamedTuple 的四个字段共同决定）。
# 【协作】Style 持有 Color；ansi.py 解析 SGR 时构造 Color；palette 与 console 也使用它。
# ---------------------------------------------------------------------------
@rich_repr
# 【易错：最容易踩的坑】读取 number 前没有判断 type 是新手最常见的错误：
#   对真彩色取 number 会得到 None，对默认色取 triplet 也会得到 None。
#   正确的做法是先用 type 分派，或者直接调用 get_truecolor 让它替你分派。
# 【性能提示】Color 是 NamedTuple，构造与比较都很快；
#   但频繁用字符串构造 Color（例如在渲染循环里 Color.parse("red")）
#   仍应优先借助 lru_cache 或提前缓存到变量里。
class Color(NamedTuple):
    """Terminal color definition."""

    name: str
    """The name of the color (typically the input to Color.parse)."""
    type: ColorType
    """The type of the color."""
    number: Optional[int] = None
    """The color number, if a standard color, or None."""
    triplet: Optional[ColorTriplet] = None
    """A triplet of color components, if an RGB color."""

    # 【功能】实现 Rich 的 __rich__ 协议：在控制台里打印一个 Color 对象时，
    #   显示成「名称 + 类型 + 一个真正染成该颜色的圆点」。
    # 【算法】用 Text.assemble 拼接三段文本，中间那段用 Style(color=self) 上色。
    # 【返回】Text 对象（Rich 会继续渲染它）。
    # 【注意】方法内部才 import Style/Text，这是为了打破 color ↔ style ↔ text 的循环导入；
    #   调用频率低，导入开销可以忽略。
    # 【协作】Rich 的渲染协议 __rich__：任何对象只要有这个方法，console.print 就会
    #   调用它并把返回值当作可渲染对象；Color 用它实现「打印颜色即预览颜色」。
    # 【协作】颜色预览也是文档与调试的常用手段：
    #   在 REPL 里直接输入一个 Color 对象即可看到它对应的色块。
    def __rich__(self) -> "Text":
        """Displays the actual color if Rich printed."""
        from .style import Style
        from .text import Text

        return Text.assemble(
            f"<color {self.name!r} ({self.type.name.lower()})",
            ("⬤", Style(color=self)),
            " >",
        )

    # 【功能】Rich 的调试表示协议：省略默认值，只显示有意义的字段。
    # 【算法】name 与 type 直接产出；number 与 triplet 以 "字段名, 值, 默认值" 的形式产出，
    #   值为 None 时会被 rich_repr 自动省略。
    # 【注意】这就是 repr(Color.parse("red")) 只显示 name/type/number 而不显示 triplet 的原因。
    # 【要点】装饰器 @rich_repr 会读取本方法的产出拼出 repr 文本；这里四个字段中
    #   后两个带默认值 None，因此只有非空的字段才会出现在输出里。
    def __rich_repr__(self) -> Result:
        yield self.name
        yield self.type
        yield "number", self.number, None
        yield "triplet", self.triplet, None

    # ==== 【派生属性】 ====
    # 【功能】返回该颜色「原生」所属的颜色体系。
    # 【要点】三个捕获组恰好对应正则里三种写法之一，按组是否命中即可分派；
    #   不会出现两组同时命中的情况（模式之间用 | 分隔且整体锚定）。
    # 【算法】DEFAULT 类型特殊处理为 STANDARD；其余类型直接按数值转成 ColorSystem。
    # 【返回】ColorSystem。
    # 【易错】这个 int→enum 的转换依赖两个枚举的前四个值严格相等；
    #   如果将来给 ColorType 插入新成员，必须同步修改 ColorSystem。
    # 【注意】本属性的实现依赖 ColorType 与 ColorSystem 的数值对齐（见类注释的不变量 5）；
    #   它是「颜色类型」到「终端体系」的唯一桥接点。
    @property
    def system(self) -> ColorSystem:
        """Get the native color system for this color."""
        if self.type == ColorType.DEFAULT:
            return ColorSystem.STANDARD
        return ColorSystem(int(self.type))

    # 【功能】判断该颜色是否「由系统/终端定义」而非由调色板数据定义。
    # 【返回】bool：STANDARD、WINDOWS、DEFAULT 为 True；EIGHT_BIT 与 TRUECOLOR 为 False。
    # 【为什么】需要这个区分是因为：8 位与真彩色有确定的 RGB 值（可以在本地算出十六进制），
    #   而标准色与 Windows 色的实际 RGB 取决于用户终端的配色方案，只能靠终端主题去猜。
    # 【协作】Console 导出 HTML 时用它决定是否需要用 terminal_theme 解析颜色。
    @property
    def is_system_defined(self) -> bool:
        """Check if the color is ultimately defined by the system."""
        return self.system not in (ColorSystem.EIGHT_BIT, ColorSystem.TRUECOLOR)

    # 【功能】判断该颜色是否为「终端默认色」。
    # 【返回】bool：type == ColorType.DEFAULT。
    # 【协作】Style.transparent_background 依赖它判断背景是否应被视为透明。
    @property
    def is_default(self) -> bool:
        """Check if the color is a default color."""
        return self.type == ColorType.DEFAULT

    # ==== 【还原为真彩色】 ====
    # 【功能】把任意类型的颜色解析成 RGB 三元组（真彩色）。
    # 【算法】按 type 分派：
    #   · TRUECOLOR → 直接返回 triplet（断言非空）；
    #   · EIGHT_BIT  → 查 _palettes.EIGHT_BIT_PALETTE[number]；
    #   · STANDARD  → 查传入终端主题的 ansi_colors[number]（0~15 的实际 RGB 因终端而异）；
    #   · WINDOWS   → 查 _palettes.WINDOWS_PALETTE[number]；
    #   · DEFAULT   → 返回主题的前景色或背景色（由 foreground 参数决定）。
    # 【参数】theme：终端主题；None 表示使用 DEFAULT_TERMINAL_THEME。
    #   foreground：仅在 DEFAULT 类型时有意义，True 取前景色、False 取背景色。
    # 【返回】ColorTriplet（RGB 0-255）。
    # 【注意】1) 本方法没有缓存，但 EIGHT_BIT/WINDOWS 的查表很快，STANDARD 也只多一层列表索引；
    #   2) 各分支里的 assert 只是给类型检查器看的（运行时若数据被破坏会直接断言失败）；
    #   3) 这是「导出 HTML / 计算降级目标」时必须经过的一步，
    #      也是「标准色在不同终端上显示不同」这一现象的根源。
    # 【易错】foreground 参数只在 DEFAULT 类型时有意义；对其它类型传入它没有任何效果，
    #   因此调用时若想「取背景真彩色」必须确保颜色本身就是背景色或默认背景色。
    def get_truecolor(
        self, theme: Optional["TerminalTheme"] = None, foreground: bool = True
    ) -> ColorTriplet:
        """Get an equivalent color triplet for this color.

        Args:
            theme (TerminalTheme, optional): Optional terminal theme, or None to use default. Defaults to None.
            foreground (bool, optional): True for a foreground color, or False for background. Defaults to True.

        Returns:
            ColorTriplet: A color triplet containing RGB components.
        """

        if theme is None:
            theme = DEFAULT_TERMINAL_THEME
        if self.type == ColorType.TRUECOLOR:
            assert self.triplet is not None
            return self.triplet
        elif self.type == ColorType.EIGHT_BIT:
            assert self.number is not None
            return EIGHT_BIT_PALETTE[self.number]
        elif self.type == ColorType.STANDARD:
            assert self.number is not None
            return theme.ansi_colors[self.number]
        # 【易错】这里的 s 来自 colorsys.rgb_to_hls，取值是 0~1 的比例而不是 0~100 的百分数；
        #   若按百分数理解（把 0.15 当成 0.15%），灰阶判定几乎永远不会生效。
        elif self.type == ColorType.WINDOWS:
            assert self.number is not None
            return WINDOWS_PALETTE[self.number]
        else:  # self.type == ColorType.DEFAULT:
            assert self.number is None
            return theme.foreground_color if foreground else theme.background_color

    # ==== 【工厂方法】 ====
    # 【功能】用 8 位色号构造 Color。
    # 【算法】name 统一写成 "color(N)"（与 Style 定义串的写法一致，可往返解析）；
    #   N < 16 归为标准色，否则归为 8 位色。
    # 【参数】number：0~255 的调色板索引。
    # 【返回】Color。
    # 【要点】色立方分量量化：xterm 的六个等级是 0、95、135、175、215、255，
    #   因此分界点取 95，之后每 40 一档；round 保证落到最近的等级上。
    # 【注意】本方法不校验 number 的范围；越界会在 get_truecolor 查表时抛 IndexError。
    # 【易错】Windows 分支里对 number < 16 的 8 位色直接沿用索引，是因为两套 16 色的
    #   「语义位置」一致（0 黑、1 红……）；但它们的 RGB 定义不同，因此不能反过来把
    #   Windows 色号当成 ANSI 色号去查 STANDARD_PALETTE。
    @classmethod
    # 【协作】ansi.py 解析 SGR 序列时会用 8 位色号构造 Color；
    #   前 16 个号归为 STANDARD 是因为它们需要终端主题才能还原 RGB。
    # 【协作】ansi.py 的 AnsiDecoder 在解析 38;5;N 时正是用它构造 Color。
    def from_ansi(cls, number: int) -> "Color":
        """Create a Color number from it's 8-bit ansi number.

        Args:
            number (int): A number between 0-255 inclusive.

        Returns:
            Color: A new Color instance.
        """
        return cls(
            name=f"color({number})",
            type=(ColorType.STANDARD if number < 16 else ColorType.EIGHT_BIT),
            number=number,
        )

    # 【功能】用 RGB 三元组构造真彩色。
    # 【参数】triplet：ColorTriplet（0~255）。
    # 【返回】Color，name 取三重组的十六进制表示（如 "#ff0000"）。
    # 【注意】name 直接用 hex，因此对真彩色来说 name 与 triplet 是同一信息的两处表示，
    #   构造后不再校验两者一致。
    @classmethod
    # 【易错】不校验分量范围：ColorTriplet(300, 0, 0) 会被原样接受，
    #   直到 get_ansi_codes 输出非法的 SGR 参数时才在终端上表现为异常。
    def from_triplet(cls, triplet: "ColorTriplet") -> "Color":
        """Create a truecolor RGB color from a triplet of values.

        Args:
            triplet (ColorTriplet): A color triplet containing red, green and blue components.

        Returns:
            Color: A new color object.
        """
        return cls(name=triplet.hex, type=ColorType.TRUECOLOR, triplet=triplet)

    # 【功能】用三个浮点分量构造真彩色。
    # 【算法】int() 截断（不是四舍五入）后转交 from_triplet。
    # 【参数】red/green/blue：0~255 的分量，接受浮点数（便于与 colorsys 之类的库配合）。
    # 【返回】Color。
    # 【易错】int() 是向零截断：from_rgb(254.9, 0, 0) 得到 254 而不是 255；
    #   palette.py 的示例代码正是依赖这种「容忍浮点」的行为。
    @classmethod
# 【要点】两端点分量逐个线性插值，再用 int() 截断成整数；
#   返回新的 ColorTriplet，不改动入参。
    # 【协作】syntax.py 用它在语法高亮主题里做颜色混合；palette.py 的示例用它构造渐变。
    def from_rgb(cls, red: float, green: float, blue: float) -> "Color":
        """Create a truecolor from three color components in the range(0->255).

        Args:
            red (float): Red component in range 0-255.
            green (float): Green component in range 0-255.
            blue (float): Blue component in range 0-255.

        Returns:
            Color: A new color object.
        """
        return cls.from_triplet(ColorTriplet(int(red), int(green), int(blue)))
# 【说明】下面的演示代码用 Rich 自己的 Table/Text 渲染调色板，属于「库自己使用自己」的
#   典型例子，也是理解 Color + Style + Palette 协作的好材料：
#   它用 Text(" " * 10, style=f"on {name}") 直接以该颜色作为背景输出色块。

    # 【功能】返回代表「终端默认色」的 Color。
    # 【返回】Color(name="default", type=ColorType.DEFAULT)，注意 number 与 triplet 均为 None。
    # 【协作】解析样式串里的 "default" 关键字、以及 Style(meta) 之类的默认值都会用到。
    @classmethod
    # 【为什么】用工厂方法而不是模块级常量：Color 是不可变的，但上游选择保持「每次构造」
    #   的写法以强调 default 只是一个约定值；开销可以忽略（NamedTuple 构造很轻）。
    def default(cls) -> "Color":
        """Get a Color instance representing the default color.

        Returns:
            Color: Default color.
        """
        return cls(name="default", type=ColorType.DEFAULT)

    # ==== 【解析颜色定义串】 ====
    # 【功能】把颜色定义串解析成 Color（这是用户输入进入颜色系统的唯一入口）。
    # 【算法】按优先级依次尝试：
    #   1) 统一转小写并去首尾空白（保留原串用于报错信息）；
    #   2) "default" → 默认色；
    #   3) 查 ANSI_COLOR_NAMES（命中则按索引大小归档为 STANDARD 或 EIGHT_BIT）；
    #   4) 用 RE_COLOR 匹配 #RRGGBB / color(N) / rgb(R,G,B)：
    #      · 六位十六进制按每两位转成一个分量；
    #      · color(N) 校验 N <= 255；
    #      · rgb(...) 拆成三段并逐个 int()，校验共 3 个分量且都 <= 255；
    #   5) 全部失败则抛 ColorParseError。
    # 【参数】color：颜色定义串，大小写不敏感。
    # 【返回】Color。
    # 【注意】1) 带 lru_cache(maxsize=1024)——解析结果被缓存，因此它返回的 Color 对象是
    #      共享的（Color 不可变，共享安全）；
    #   2) 报错信息里用 original_color（保留用户原始大小写）而不是规范化后的串，
    #      便于用户定位问题；
    #   3) rgb() 的分量必须能 int() 成功，写成 rgb(1.5,0,0) 会抛 ValueError 而不是
    #      ColorParseError——这是一个已知的宽松边界。
    @classmethod
    @lru_cache(maxsize=1024)
    # 【易错】由于带 lru_cache，解析失败（抛异常）的结果也会占用缓存条目吗？
    #   不会：lru_cache 只缓存正常返回值，异常不会被缓存，因此错误的颜色串每次都会
    #   重新解析并重新抛错（对报错信息有利，对性能略不利）。
    # 【协作】调用者包括 Style.parse（样式定义串里的颜色）、
    #   markup 的 [red] 标记、以及用户直接调用的 Color.parse。
    def parse(cls, color: str) -> "Color":
        """Parse a color definition."""
        original_color = color
        color = color.lower().strip()

        if color == "default":
            return cls(color, type=ColorType.DEFAULT)

        color_number = ANSI_COLOR_NAMES.get(color)
        if color_number is not None:
            return cls(
                color,
                type=(ColorType.STANDARD if color_number < 16 else ColorType.EIGHT_BIT),
                number=color_number,
            )

        color_match = RE_COLOR.match(color)
        if color_match is None:
            raise ColorParseError(f"{original_color!r} is not a valid color")

        # 【要点】三个捕获组恰好对应正则里三种写法之一，因此这里只需按组是否命中来分派；
        #   不会出现两组同时命中的情况（模式之间用 | 分隔且整体锚定）。
        color_24, color_8, color_rgb = color_match.groups()
        if color_24:
            triplet = ColorTriplet(
                int(color_24[0:2], 16), int(color_24[2:4], 16), int(color_24[4:6], 16)
            )
            return cls(color, ColorType.TRUECOLOR, triplet=triplet)

        elif color_8:
            number = int(color_8)
            if number > 255:
                raise ColorParseError(f"color number must be <= 255 in {color!r}")
            return cls(
                color,
                type=(ColorType.STANDARD if number < 16 else ColorType.EIGHT_BIT),
                number=number,
            )

        else:  #  color_rgb:
            components = color_rgb.split(",")
            if len(components) != 3:
                raise ColorParseError(
                    f"expected three components in {original_color!r}"
                )
            red, green, blue = components
            triplet = ColorTriplet(int(red), int(green), int(blue))
            if not all(component <= 255 for component in triplet):
                raise ColorParseError(
                    f"color components must be <= 255 in {original_color!r}"
                )
            return cls(color, ColorType.TRUECOLOR, triplet=triplet)

    # ==== 【编码为 ANSI SGR 参数】 ====
    # 【功能】把颜色编码成 ANSI SGR 参数字符串元组（不含 ESC 前缀，最终由 Style 用 ";" 连接）。
    # 【算法】按类型分派：
    #   · DEFAULT   → ("39") 前景 / ("49") 背景（恢复默认色）；
    #   · WINDOWS   → 与 STANDARD 相同的编号规则（Windows 传统控制台只认这 16 色）；
    #   · STANDARD  → number<8 时前景 30+n / 背景 40+n；number>=8 时前景 82+n / 背景 92+n；
    #   · EIGHT_BIT → ("38"/"48", "5", str(number))；
    #   · TRUECOLOR → ("38"/"48", "2", R, G, B)。
    # 【参数】foreground：True 编码前景色，False 编码背景色。
    # 【返回】Tuple[str, ...]：需要由调用方用 ";" 连接（如 ("38","5","196") → "38;5;196"）。
    # 【易错】1) 高亮色用 82/92 而不是标准的 90/100：这是 ANSI 历史上 90-97 与 aixterm
    #      扩展不一致导致的兼容处理，改动它会破坏实测通过的终端行为；
    #   2) 本方法假定颜色已经 downgrade 到目标体系（TRUECOLOR 分支不会自己降级），
    #      调用方在 Style._make_ansi_codes 里已经做过这一步；
    #   3) 带 lru_cache(1024)：同一颜色在同一前景/背景角色下只编码一次。
    @lru_cache(maxsize=1024)
    # 【要点】返回元组而不是拼好的字符串，是为了让 Style 能把多个颜色的参数与属性参数
    #   统一用 ";" 连接；这也让 EIGHT_BIT/TRUECOLOR 的多段参数天然可表达。
    # 【速查：SGR 参数对照】
    #   前景  30-37 标准 8 色 / 90-97 亮色（Rich 生成 82-89 见下）；
    #   背景  40-47 标准 8 色 / 100-107 亮色（Rich 生成 92-99）；
    #   256 色 38;5;N（前景） / 48;5;N（背景）；
    #   真彩色 38;2;R;G;B（前景） / 48;2;R;G;B（背景）；
    #   默认色 39（前景） / 49（背景）。
    # 【易错】Rich 对「亮色」使用 82+n / 92+n 而不是 90+n / 100+n。
    #   这是为了兼容部分把 90-97 当作「加粗变亮」而非独立颜色的终端；
    #   改动这两个常量会直接影响实际观感，务必用真机验证后再改。
    def get_ansi_codes(self, foreground: bool = True) -> Tuple[str, ...]:
        """Get the ANSI escape codes for this color."""
        _type = self.type
        if _type == ColorType.DEFAULT:
            # 【要点】DEFAULT 类型输出 39/49 而不是直接省略参数：省略不会关闭上游可能已经打开的
            #   颜色，显式输出「恢复默认色」才能让终端回到未着色状态。
            return ("39" if foreground else "49",)

        elif _type == ColorType.WINDOWS:
            number = self.number
            assert number is not None
            fore, back = (30, 40) if number < 8 else (82, 92)
            return (str(fore + number if foreground else back + number),)

        elif _type == ColorType.STANDARD:
            number = self.number
            assert number is not None
            fore, back = (30, 40) if number < 8 else (82, 92)
            return (str(fore + number if foreground else back + number),)

        elif _type == ColorType.EIGHT_BIT:
            assert self.number is not None
            return ("38" if foreground else "48", "5", str(self.number))

        else:  # self.standard == ColorStandard.TRUECOLOR:
            assert self.triplet is not None
            red, green, blue = self.triplet
            return ("38" if foreground else "48", "2", str(red), str(green), str(blue))

    # ==== 【颜色降级】 ====
    # 【功能】把颜色降级到目标颜色体系（真彩色 → 8 位 → 16 色），必要时返回自身。
    # 【算法】
    #   · 已经是 DEFAULT 或本身就在目标体系 → 原样返回；
    #   · 目标 8 位且当前为真彩色：把 RGB 转成 HLS，
    #     饱和度 < 0.15 视为灰阶 → 映射到 232-255 的 24 级灰阶（0 与 25 分别钳到 16 与 231）；
    #     否则套 xterm 的 6×6×6 色立方公式：每个分量 < 95 时除以 95，
    #     否则 1 + (v-95)/40，再取整并组合成 16 + 36r + 6g + b；
    #   · 目标 16 色：先把当前颜色转成 RGB（真彩色直接用，8 位查调色板），
    #     再用 STANDARD_PALETTE.match 找最近的 ANSI 色；
    #   · 目标 WINDOWS：转成 RGB 后用 WINDOWS_PALETTE.match 找最近的 Windows 色；
    #     索引 < 16 的 8 位色可以直接复用索引；
    #   · 其它情况（例如目标是 TRUECOLOR）→ 原样返回。
    # 【参数】system：目标终端的颜色体系。
    # 【返回】Color：可能是新对象，也可能是 self。
    # 【注意】1) 降级是有损且不可逆的近似，同一真彩色在不同目标体系下会落到不同色号；
    #   2) 最近邻匹配在 palette.Palette.match 里用「加权欧氏距离」实现（对红色更敏感），
    #      那里有 lru_cache，因此重复降级同一颜色很快；
    #   3) 本方法带 lru_cache(1024)，键是 (self, system)；
    #   4) name 原样保留（不改成降级后的色号名），因此 name 反映的是「原始定义」而非最终色。
    @lru_cache(maxsize=1024)
    # 【易错】缓存的键包含 system，因此同一个颜色降级到不同体系会各缓存一份；
    #   maxsize=1024 在颜色种类很多的界面（如图片转字符画）上可能频繁失效。
    def downgrade(self, system: ColorSystem) -> "Color":
        """Downgrade a color system to a system with fewer colors."""

        if self.type in (ColorType.DEFAULT, system):
            return self
        # 【要点】若颜色已经是目标体系（或就是默认色），无需任何计算直接返回自身，
        #   这是绝大多数渲染调用的快路径（终端是 256 色时，8 位颜色零开销）。
        # Convert to 8-bit color from truecolor color
        if system == ColorSystem.EIGHT_BIT and self.system == ColorSystem.TRUECOLOR:
            assert self.triplet is not None
            # 【要点】rgb_to_hls 返回 (h, l, s)，这里只关心亮度 l 与饱和度 s；
            #   用 normalized 把 0~255 归一化到 0~1 正是 colorsys 要求的输入范围。
            _h, l, s = rgb_to_hls(*self.triplet.normalized)
            # If saturation is under 15% assume it is grayscale
            # 【要点】饱和度低于 15% 的视为灰阶：xterm 的 24 级灰阶（232-255）比 6×6×6 立方
            #   更贴近肉眼对灰度的感知，也避免灰色被量化成带色相的色号。
            # 【易错】gray = round(l * 25.0) 的结果范围是 0..25，因此两端需要单独钳位：
            #   0 → 16（纯黑）、25 → 231（纯白），其余映射到 231 + gray（232..255）。
            # 【易错】这里的 s 来自 colorsys.rgb_to_hls，取值为 0~1 的比例而不是 0~100 的百分数；
            #   若误按百分数理解（0.15 当作 0.15%），灰阶判定会几乎从不生效。
            if s < 0.15:
                gray = round(l * 25.0)
                if gray == 0:
                    color_number = 16
                elif gray == 25:
                    color_number = 231
                else:
                    color_number = 231 + gray
                return Color(self.name, ColorType.EIGHT_BIT, number=color_number)

            red, green, blue = self.triplet
            # 【要点】6×6×6 色立方的分量量化公式：xterm 的每一级对应 0、95、135、175、215、255；
            #   因此 95 以下按 95 分档，95 以上每 40 一档，正好是六个等级。
            six_red = red / 95 if red < 95 else 1 + (red - 95) / 40
            six_green = green / 95 if green < 95 else 1 + (green - 95) / 40
            six_blue = blue / 95 if blue < 95 else 1 + (blue - 95) / 40

            color_number = (
                16 + 36 * round(six_red) + 6 * round(six_green) + round(six_blue)
            )
            return Color(self.name, ColorType.EIGHT_BIT, number=color_number)

        # 【要点】16 色降级：先把颜色解析成 RGB，再用调色板做最近邻匹配；
        #   因为标准色的实际 RGB 由终端主题决定，这里的匹配结果是一个「平均意义下最接近」的选择。
        # Convert to standard from truecolor or 8-bit
        elif system == ColorSystem.STANDARD:
            if self.system == ColorSystem.TRUECOLOR:
                assert self.triplet is not None
                triplet = self.triplet
            else:  # self.system == ColorSystem.EIGHT_BIT
                assert self.number is not None
                triplet = ColorTriplet(*EIGHT_BIT_PALETTE[self.number])

            color_number = STANDARD_PALETTE.match(triplet)
            return Color(self.name, ColorType.STANDARD, number=color_number)

        # 【要点】Windows 传统控制台只有 16 色，且它的调色板与 ANSI 标准调色板并不相同
        #   （见 _palettes.WINDOWS_PALETTE 注释），因此必须用 Windows 专用的匹配表。
        # 【注意】索引 < 16 的 8 位色可以直接沿用索引，因为 xterm-256 的前 16 个索引
        #   与 Windows 控制台的 16 色是同一套语义（尽管 RGB 定义不同）。
        elif system == ColorSystem.WINDOWS:
            if self.system == ColorSystem.TRUECOLOR:
                assert self.triplet is not None
                triplet = self.triplet
            else:  # self.system == ColorSystem.EIGHT_BIT
                assert self.number is not None
                # 【易错】这里对 number < 16 的 8 位色直接沿用索引，是因为两套 16 色的「语义位置」
                #   一致（0 黑、1 红……）；但它们的 RGB 定义不同，因此不能反过来把 Windows 色号
                #   当成 ANSI 色号去查 STANDARD_PALETTE。
                if self.number < 16:
                    return Color(self.name, ColorType.WINDOWS, number=self.number)
                triplet = ColorTriplet(*EIGHT_BIT_PALETTE[self.number])

            color_number = WINDOWS_PALETTE.match(triplet)
            return Color(self.name, ColorType.WINDOWS, number=color_number)

        return self


# ==== 【模块级辅助函数】 ====
# 【功能】把六个十六进制字符解析成 RGB 三元组。
# 【参数】hex_color：不带 # 的六位十六进制串（如 "ff0000"）。
# 【返回】ColorTriplet。
# 【注意】用 assert 而不是抛异常来校验长度：在 python -O 下断言会被剥离，
#   届时长度不足会在切片/转换处抛别的错误。本函数只在内部受控路径上使用。
# 【协作】terminal_theme 与 syntax 主题解析 "#ff0000" 形式的配置值时使用。
def parse_rgb_hex(hex_color: str) -> ColorTriplet:
    """Parse six hex characters in to RGB triplet."""
    assert len(hex_color) == 6, "must be 6 characters"
    color = ColorTriplet(
        int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    )
    return color


# 【功能】在两个 RGB 颜色之间做线性插值（cross-fade）。
# 【算法】对每个分量计算 c1 + (c2-c1) * cross_fade 后取整。
# 【参数】color1/color2：两个 ColorTriplet；
#   cross_fade：混合比例，0 表示全取 color1、1 表示全取 color2，默认 0.5。
# 【返回】ColorTriplet。
# 【协作】Style.get_html_style 用 alpha=0.5 混合前景色与背景色来近似终端的 dim 效果；
#   console.py 也用它做降级时的颜色过渡。
# 【注意】结果用 int() 截断而不是四舍五入，因此 cross_fade=0.5 时不会严格取中值
#   （例如 (0,0,0) 与 (1,1,1) 混合得到 0）。
# 【要点】cross_fade 在 (0, 1) 之外没有钳位：取 -1 或 2 会得到超出范围的分量，
#   调用方需自行保证参数合理（现有调用点只用 0.5）。
def blend_rgb(
    color1: ColorTriplet, color2: ColorTriplet, cross_fade: float = 0.5
) -> ColorTriplet:
    """Blend one RGB color in to another."""
    r1, g1, b1 = color1
    r2, g2, b2 = color2
    new_color = ColorTriplet(
        int(r1 + (r2 - r1) * cross_fade),
        int(g1 + (g2 - g1) * cross_fade),
        int(b1 + (b2 - b1) * cross_fade),
    )
    return new_color


# 【历史】文件末尾的 __main__ 块是上游的演示/自测入口（pragma: no cover）：
#   它把 ANSI_COLOR_NAMES 里的颜色按色号排序后铺成一张表格，用真实的背景色显示色块，
#   并打印 8 位色的十六进制与 rgb 表示。以 `python -m rich.color` 可以运行，
#   用于人工核对调色板数据与降级结果。它不参与库的运行时行为。
# 【说明】下面的演示代码用 rich 自己的 Table/Text 渲染调色板，
#   属于「库自己使用自己」的典型例子，也是理解 Color + Style + Palette 协作的好材料：
#   它用 Text(" " * 10, style=f"on {name}") 直接以该颜色作为背景输出色块。
if __name__ == "__main__":  # pragma: no cover
    from .console import Console
    from .table import Table
    from .text import Text

    console = Console()

    table = Table(show_footer=False, show_edge=True)
    table.add_column("Color", width=10, overflow="ellipsis")
    table.add_column("Number", justify="right", style="yellow")
    table.add_column("Name", style="green")
    table.add_column("Hex", style="blue")
    table.add_column("RGB", style="magenta")

    colors = sorted((v, k) for k, v in ANSI_COLOR_NAMES.items())
    for color_number, name in colors:
        if "grey" in name:
            continue
        color_cell = Text(" " * 10, style=f"on {name}")
        if color_number < 16:
            table.add_row(color_cell, f"{color_number}", Text(f'"{name}"'))
        else:
            color = EIGHT_BIT_PALETTE[color_number]  # type: ignore[has-type]
            table.add_row(
                color_cell, str(color_number), Text(f'"{name}"'), color.hex, color.rgb
            )

    console.print(table)
