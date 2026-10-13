from __future__ import annotations

from functools import lru_cache
from operator import itemgetter
from typing import Callable, NamedTuple, Sequence, Tuple

from rich._unicode_data import load as load_cell_table

# ---------------------------------------------------------------------------
# 【模块定位】Rich 的终端“单元格（cell）宽度模型”基座：把 Python 字符串映射为它在终端上
#   占用的列数，并在此基础上提供切分、补齐、折行能力。凡是决定“一行能放多少字符”的地方都要
#   经过本模块：text.py 的折行/对齐/截断、table.py 的列宽分配、panel/rule/columns 的补齐、
#   segment.py 的行宽统计、console.py 的输出宽度校验，全部建立在 cell_len /
#   set_cell_size / split_text / chop_cells 之上。
#   唯一依赖是 rich/_unicode_data（按 Unicode 版本加载的宽度表），因此它是无循环依赖的底层模块。
# 【关键数据结构】
#   - CellSpan = Tuple[start, end, cell_length]：一个“字素簇”在字符串中的半开区间 [start, end)
#     以及它占用的列数（可能是 0、1 或 2）；
#   - CellTable(unicode_version, widths, narrow_to_wide)：宽度表。widths 是
#     (起始码位, 结束码位, 列宽) 的**有序**区间列表（供二分查找）；narrow_to_wide 是
#     “单独显示为窄、后面跟变体选择符 VS16 时才变宽”的字符集合（例如 ❤ U+2764）；
#   - _SINGLE_CELLS：常用单宽字符的冻结集合，_is_single_cell_widths 就是它的 issuperset，
#     用来做 O(1) 的“整串都是普通字符”判定。
# 【设计要点】
#   - 宽度模型：终端按字素簇渲染，每个字素簇占 0、1 或 2 列。控制字符与组合附加符
#     （如 U+0301）算 0 列；东亚宽字符（CJK、全角形式、多数 emoji）算 2 列；其余算 1 列。
#     Rich 只做线性累加，不依赖 ICU/regex 做完整字素簇切分——这是“够用且可预测”的取舍。
#   - 三级快速路径，把绝大多数真实文本挡在最便宜的判断上：
#     ① 全部字符都在 _SINGLE_CELLS 中 → 宽度直接等于 len(text)（纯 ASCII 走这里，零查表）；
#     ② 不含 ZWJ(\u200d) 与 VS16(\ufe0f) → 逐字符查表求和，无需维护字素簇状态；
#     ③ 含上述特殊字符 → 走带状态的扫描循环（见 _cell_len）。
#   - 两个 lru_cache 的存在理由：宽度计算出现在“每行 × 每列”的渲染循环里，重复率极高，
#     缓存把“逐字符查表 + 二分查找”降为一次字典命中。键中都带 unicode_version，
#     因此切换 Unicode 版本不会读到脏数据。
#   - cell_len 对长字符串刻意绕开缓存（>= 512 字符时直接计算）：既避免超长文本挤爆缓存，
#     也省掉为整串计算哈希的开销。
# 【阅读建议】按“测量 → 切分 → 排版”的顺序读：
#   1) _SINGLE_CELL_UNICODE_RANGES / _is_single_cell_widths 看清快速路径的边界；
#   2) get_character_cell_size（宽度表的二分查找与越界回退）；
#   3) _cell_len（ZWJ 与 VS16 的状态处理，本文件最需要小心的 20 行）；
#   4) split_graphemes（把字符串切成 CellSpan，是切分与折行的公共基础）；
#   5) 最后是 _split_text / split_text / set_cell_size / chop_cells 四个排版函数。
# ---------------------------------------------------------------------------

# 【要点】CellSpan 的语义是“半开区间 [start, end) + 该区间的列宽”：
#   start/end 是 Python 字符串下标（码位索引，既不是字节偏移也不是列位置），
#   cell_length ∈ {0, 1, 2}。之所以称它为字素簇区间，是因为 split_graphemes 会把组合符、
#   零宽字符、ZWJ 序列都归并进前一个区间。
CellSpan = Tuple[int, int, int]

# 【优化】用 operator.itemgetter(2) 代替 lambda span: span[2]：它是 C 实现，
#   在被 sum(map(...)) 反复调用的切片路径上更快，也避免每次调用创建新的函数对象。
# 【协作】_split_text 用 sum(map(_span_get_cell_len, spans[:offset])) 快速累加左侧列宽。
_span_get_cell_len = itemgetter(2)

# ==== 【单宽字符快速判定表】 ====
# 【要点】这些区间是“常见单宽字符”的近似集合，只求快、不求全：命中即认定宽度为 1，
#   使纯 ASCII 与常见欧洲文本完全免去宽度表查询。区间之外（CJK、emoji、组合符等）会走
#   精确的宽度表路径，所以“不完整”不会导致错误，只是退化为稍慢的路径。
# 【约束】区间是闭区间 [start, end]，构造集合时用 range(start, end + 1) 展开，
#   因此表中 end 值都需要“含端点”地理解；改区间时要留意这一点。
# 【兼容】0x20-0x7E 排除了控制字符（<0x20）与 DEL(0x7F)；0xA0 起刻意跳过 0xAD（软连字符，
#   零宽）以及 0x300-0x36F 组合附加符区段（所以下一段从 0x370 希腊字母继续），
#   这些字符都不能算作 1 列。
# Ranges of unicode ordinals that produce a 1-cell wide character
# This is non-exhaustive, but covers most common Western characters
_SINGLE_CELL_UNICODE_RANGES: list[tuple[int, int]] = [
    (0x20, 0x7E),  # Latin (excluding non-printable)
    (0xA0, 0xAC),
    (0xAE, 0x002FF),
    (0x00370, 0x00482),  # Greek / Cyrillic
    (0x02500, 0x025FC),  # Box drawing, box elements, geometric shapes
    (0x02800, 0x028FF),  # Braille
]

# 【要点】把上面的区间展开成一个字符集合，得到 O(1) 的成员判定结构（frozenset 哈希查找）。
# 【为什么】预先把所有单宽字符装进集合，而不是每次遍历区间做比较：判定发生在每一行文本上，
#   集合查表远快于区间扫描；代价只是一次性的启动内存（约两千个单字符对象）。
# 【优化】用 frozenset 而不是 set：不可变、可安全共享，元素哈希缓存后 issuperset 更快。
# A frozen set of characters that are a single cell wide
_SINGLE_CELLS = frozenset(
    [
        # 【算法】双层推导式：外层遍历区间，内层把每个码位转成字符，
        #   最终得到一个“平铺”的字符集合（区间本身在运行期不再需要）。
        character
        for _start, _end in _SINGLE_CELL_UNICODE_RANGES
        for character in map(chr, range(_start, _end + 1))
    ]
)

# 【要点】issuperset 的语义：字符串里**每一个**字符都必须在集合中才返回 True；
#   空串按集合论定义恒为 True（空集是任何集合的子集），所以 cell_len("") 走快速路径得到 0。
# 【易错】它只回答“是否全为窄字符”，不回答“宽度是多少”；返回 False 只意味着“需要走精确路径”，
#   并不表示串里一定有宽字符（组合附加符、零宽字符同样会让它返回 False）。
# 【优化】直接把 frozenset.issuperset 这个 C 方法绑定成函数对象，省掉一层 Python 包装函数。
# When called with a string this will return True if all
# characters are single-cell, otherwise False
_is_single_cell_widths: Callable[[str], bool] = _SINGLE_CELLS.issuperset


# ==== 【宽度数据结构】 ====
# 【职责】承载某个 Unicode 版本下的字符宽度数据，是 rich._unicode_data.load() 的返回值类型。
# 【关键属性】unicode_version：数据来源版本字符串（如 "17.0.0"）；
#   widths：按码位升序排列的 (start, end, width) 区间序列，供二分查找；
#   narrow_to_wide：默认按窄字符显示、但后面跟 VS16 时按宽字符渲染的字符集合。
# 【不变量】widths 必须按 start 升序且区间互不重叠——get_character_cell_size 的二分查找
#   依赖这个前提；narrow_to_wide 的成员在 widths 中的宽度应为 1，遇到 VS16 后再 +1。
# 【协作】rich/_unicode_data/__init__.py 的 load() 按版本导入对应的数据模块并返回本结构；
#   本模块的 get_character_cell_size、_cell_len、split_graphemes 都从它读取宽度信息。
class CellTable(NamedTuple):
    """Contains unicode data required to measure the cell widths of glyphs."""

    # 【关键属性】数据来源的 Unicode 版本（如 "17.0.0"），同时是 load() 的缓存键之一。
    unicode_version: str
    # 【关键属性】按码位升序的 (start, end, width) 区间序列；二分查找要求“有序且不重叠”。
    widths: Sequence[tuple[int, int, int]]
    # 【关键属性】默认窄、后接 VS16 时按宽字符渲染的字符集合（emoji 呈现形式的基础字形）。
    narrow_to_wide: frozenset[str]


# ==== 【宽度测量：单字符 → 列数】 ====
# 【功能】查询单个字符占用的终端列数，只可能返回 0、1 或 2。
# 【算法】
#   1) 取码位 ord(character)，先判控制字符：C0（< 32）与 DEL/C1（0x7F ≤ cp < 0xA0）一律返回 0
#      ——它们不显示、也不占列；
#   2) 加载当前 Unicode 版本的宽度表；若码位已超出表中最后一个区间的右端，返回 1
#      （未收录的字符按最保守的窄字符处理）；
#   3) 否则在这份有序区间列表上做二分查找：命中区间返回其宽度；落在区间空洞里也返回 1。
# 【参数】character (str)：单个字符（若传入多字符，ord() 会因长度不为 1 而抛 TypeError）；
#   unicode_version：Unicode 版本，"auto" 自动检测（可被 UNICODE_VERSION 环境变量覆盖，
#   默认用最新版），"latest" 表示最新版。
# 【返回】int：0（零宽/不可打印）、1（窄）或 2（宽）。
# 【注意】1) 第一处判断写成 `codepoint and codepoint < 32 or 0x07F <= codepoint < 0x0A0`，
#      因为 and 优先级高于 or，它等价于 (codepoint != 0 and codepoint < 32) or (0x7F ≤ cp < 0xA0)；
#      U+0000 走不到前一项，最终由宽度表给出 0，结果一致；
#   2) 组合附加符（如 U+0301）自身在宽度表里就是 0，所以 "e\u0301" 的总宽是 1；
#   3) 整个函数被 lru_cache(4096) 缓存，键是 (字符, 版本)，重复字符基本零成本；
#   4) 二分查找用显式 while 循环而不是 bisect，是为了在命中区间时能直接拿到 width。
@lru_cache(maxsize=4096)
def get_character_cell_size(character: str, unicode_version: str = "auto") -> int:
    """Get the cell size of a character.

    Args:
        character (str): A single character.
        unicode_version: Unicode version, `"auto"` to auto detect, `"latest"` for the latest unicode version.

    Returns:
        int: Number of cells (0, 1 or 2) occupied by that character.
    """
    codepoint = ord(character)
    # 【要点】控制字符一律零宽：C0 区（含 \t 之外的 \r、\n 等）与 DEL/C1 区都不可见。
    if codepoint and codepoint < 32 or 0x07F <= codepoint < 0x0A0:
        return 0
    # 【要点】只需宽度区间列表，不需要 narrow_to_wide，因此这里先取出 .widths；
    #   load_cell_table 自身带缓存（functools.cache），不会重复解析数据模块。
    table = load_cell_table(unicode_version).widths

    # 【要点】先做一次“越界快判”：宽度表只覆盖已收录码位，之后的码位（如新加入的 emoji）
    #   按窄字符 1 列处理，避免二分查找给出错误结论。
    last_entry = table[-1]
    if codepoint > last_entry[1]:
        return 1

    # 【算法】标准二分查找：区间按 start 升序排列，命中返回 width，
    #   落在两区间之间的空洞（表中未定义的码位）则跳出循环返回 1。
    lower_bound = 0
    upper_bound = len(table) - 1

    while lower_bound <= upper_bound:
        index = (lower_bound + upper_bound) >> 1
        start, end, width = table[index]
        if codepoint < start:
            upper_bound = index - 1
        elif codepoint > end:
            lower_bound = index + 1
        else:
            return width
    return 1


# 【功能】带缓存的整串宽度计算，是 cell_len 在短文本路径上的实现。
# 【算法】直接调用 _cell_len，但整个调用被 lru_cache(4096) 包住：相同 (文本, 版本)
#   只需一次哈希查找。
# 【参数】text (str)：待测文本；unicode_version：Unicode 版本，"auto" 自动。
# 【返回】int：文本占用的终端列数。
# 【注意】这是**公开**的缓存入口：docstring 明确提醒“它总是缓存、可能占用大量内存，
#   建议优先使用 cell_len”（后者对长字符串会绕开缓存）。
# 【为什么】把缓存放在这一层而不是 cell_len 里：让“是否缓存”的策略集中在 cell_len 的
#   一个分支上，同时保证直接调用本函数的用户也能得到一致的缓存行为。
@lru_cache(4096)
def cached_cell_len(text: str, unicode_version: str = "auto") -> int:
    """Get the number of cells required to display text.

    This method always caches, which may use up a lot of memory. It is recommended to use
    `cell_len` over this method.

    Args:
        text (str): Text to display.
        unicode_version: Unicode version, `"auto"` to auto detect, `"latest"` for the latest unicode version.

    Returns:
        int: Get the number of cells required to display text.
    """
    # 【要点】本函数只是“给 _cell_len 套一层 lru_cache”，逻辑一行都没有重复实现，
    #   因此 cached_cell_len 与 cell_len（长文本路径）的结果必然一致。
    return _cell_len(text, unicode_version)


# ==== 【宽度测量：整串 → 列数（对外主入口）】 ====
# 【功能】计算字符串在终端中占用的列数（判断“打印出来有多宽”的标准方法）。
# 【算法】按文本长度分流：短于 512 个字符走 cached_cell_len（享受 lru_cache），
#   否则直接调用 _cell_len 内核，避免超长文本污染缓存。
# 【参数】text (str)：待测字符串；unicode_version：Unicode 版本，"auto" 自动检测。
# 【返回】int：终端列数（宽字符算 2，组合符/零宽字符算 0）。
# 【注意】1) 阈值 512 是硬编码的启发式：它假设“长文本重复度低、缓存的收益小于代价”；
#   2) 本项目里所有布局度量都应以本函数为准，不要用 len() 代替——
#      len("中文") == 2 但 cell_len("中文") == 4，这正是终端排版出错的经典来源。
# 【协作】text.py（折行/对齐/裁剪）、table.py（列宽分配）、segment.py、console.py
#   等都在调用它。
def cell_len(text: str, unicode_version: str = "auto") -> int:
    """Get the cell length of a string (length as it appears in the terminal).

    Args:
        text: String to measure.
        unicode_version: Unicode version, `"auto"` to auto detect, `"latest"` for the latest unicode version.

    Returns:
        Length of string in terminal cells.
    """
    # 【要点】短文本才进缓存；512 这个界限让“缓存命中率”与“内存占用”取得平衡。
    if len(text) < 512:
        return cached_cell_len(text, unicode_version)
    # 【注意】长文本每次都完整重算——对反复测量同一段超长文本的场景（例如大表格），
    #   这是本模块主要的时间开销来源。
    return _cell_len(text, unicode_version)


# 【功能】cell_len 的无缓存内核：实现三级快速路径的宽度累加。
# 【算法】
#   1) 整串都是单宽字符（_is_single_cell_widths）→ 宽度就等于 len(text)，直接返回；
#   2) 既不含 ZWJ(\u200d) 也不含 VS16(\ufe0f) → 逐字符查宽度表求和（无状态，最简单）；
#   3) 否则走带状态的扫描循环：普通字符累加宽度并记入 last_measured_character；
#      遇到 ZWJ 时多跳一格（连同紧随其后的那个字符一起跳过），把整个 ZWJ 序列当作一个字素，
#      从而只计一次基础宽度；遇到 VS16 时，若前一个“已测量字符”属于 narrow_to_wide，
#      就给总宽 +1（表情呈现选择符把窄字形变宽），并清空 last_measured_character。
# 【参数】text (str)：待测字符串；unicode_version：Unicode 版本（必填，默认值由上层补好）。
# 【返回】int：总列数。
# 【注意】1) 状态里只保存“上一个宽度非 0 的字符”，因为零宽字符（组合符）不该被 VS16 影响；
#   2) ZWJ 分支先 index += 1、循环末尾又 += 1，一共跨过两个字符，这是本文件最容易读错的
#      地方，也是最关键的一步——"👩\u200d👩" 的宽度因此是 2 而不是 4；
#   3) 这是一套工程近似：多个连续 ZWJ、国旗序列、键帽序列等复杂 emoji 与真实终端可能
#      存在 ±1 的差异，Rich 选择用简单规则换取跨终端的一致性。
def _cell_len(text: str, unicode_version: str) -> int:
    """Get the cell length of a string (length as it appears in the terminal).

    Args:
        text: String to measure.
        unicode_version: Unicode version, `"auto"` to auto detect, `"latest"` for the latest unicode version.

    Returns:
        Length of string in terminal cells.
    """

    # 【快速路径 1】全部是常用单宽字符 → 列数等于字符数，零查表、零状态。
    if _is_single_cell_widths(text):
        return len(text)

    # "\u200d" is zero width joiner
    # "\ufe0f" is variation selector 16
    # 【快速路径 2】没有 ZWJ 与 VS16 时，字素簇与字符一一对应，逐字符求和即可。
    if "\u200d" not in text and "\ufe0f" not in text:
        # Simplest case with no unicode stuff that changes the size
        return sum(
            get_character_cell_size(character, unicode_version) for character in text
        )

    # 【快速路径 3】含 ZWJ / VS16：需要跨字符状态，改用显式下标循环。
    cell_table = load_cell_table(unicode_version)
    total_width = 0
    last_measured_character: str | None = None

    SPECIAL = {"\u200d", "\ufe0f"}

    index = 0
    character_count = len(text)

    while index < character_count:
        character = text[index]
        if character in SPECIAL:
            if character == "\u200d":
                # 【要点】ZWJ 表示“与下一个字符连成一个字形”，因此连它后面的那个字符一起跳过；
                #   下面的 index += 1 由循环末尾统一执行，这里只额外跳一格。
                index += 1
            elif last_measured_character:
                # 【要点】VS16 把前一个字符升级为 emoji 呈现形式；只有原本“窄”的字符
                #   （narrow_to_wide 成员）需要额外加 1 列，已经是宽字符的不再加。
                total_width += last_measured_character in cell_table.narrow_to_wide
                # VS16 之后状态清空，避免连续多个选择符重复加宽。
                last_measured_character = None
        else:
            # 【要点】海象运算符：先取宽度，非 0 才记录并累加；宽度为 0 的字符
            #   （组合附加符、零宽字符）不会覆盖 last_measured_character。
            if character_width := get_character_cell_size(character, unicode_version):
                last_measured_character = character
                total_width += character_width
        index += 1

    return total_width


# ==== 【字素簇切分：字符串 → CellSpan 列表】 ====
# 【功能】把文本切分成一组“字素簇”区间（CellSpan），同时返回整串的列宽。
# 【算法】单次线性扫描，用 spans[-1] 做“就地扩展上一个区间”：
#   - 普通字符：宽度非 0 就新开一个区间 (index, index+1, width) 并累加总宽；宽度为 0 则并入
#     上一个区间的 end（零宽字符跟随前一个字符一起显示）；
#   - 零宽字符出现在串首（没有上一个区间）：单独开一个 (0, 1, 0) 的零宽区间，
#     以保证区间覆盖所有下标、不留空洞；
#   - ZWJ(\u200d)：把它与紧随其后的一个字符一起并入上一个区间；若 ZWJ 已在串尾
#     （没有可连接的字符），则只并入它自己；
#   - VS16(\ufe0f)：并入上一个区间，并且当前一个已测字符属于 narrow_to_wide 时，
#     把该区间的 cell_length 与 total_width 各 +1；若串首就是 VS16（前面没有可影响的字符），
#     则只并入、不改宽度。
# 【参数】text (str)：待切分字符串；unicode_version：Unicode 版本，"auto" 自动。
# 【返回】tuple[list[CellSpan], int]：区间列表（覆盖 [0, len(text)) 的每个下标、无空隙）
#   与整串列宽。
# 【注意】1) 区间列宽允许为 0（连续零宽字符、串首 VS16 等），因此按列切分的调用方必须容忍
#      “零宽区间”；
#   2) 返回的总宽与 _cell_len 的结果一致，区别是这里给出显式区间，便于按列切分与折行；
#   3) 元组里的海象表达式按从左到右求值，所以 `(index, index := index + 1, cell_length)`
#      的第一个 index 是旧值（区间起点）、第二个是新值（区间终点）——这正是半开区间的写法，
#      也是本文件多个元组赋值能“顺手推进下标”的原因。
def split_graphemes(
    text: str, unicode_version: str = "auto"
) -> "tuple[list[CellSpan], int]":
    """Divide text into spans that define a single grapheme, and additionally return the cell length of the whole string.

    The returned spans will cover every index in the string, with no gaps. It is possible for some graphemes to have a cell length of zero.
    This can occur for nonsense strings like two zero width joiners, or for control codes that don't contribute to the grapheme size.

    Args:
        text: String to split.
        unicode_version: Unicode version, `"auto"` to auto detect, `"latest"` for the latest unicode version.

    Returns:
        A tuple of a list of *spans* and the cell length of the entire string. A span is a list of tuples
            of three values consisting of (<START>, <END>, <CELL LENGTH>), where START and END are string indices,
            and CELL LENGTH is the cell length of the single grapheme.
    """

    cell_table = load_cell_table(unicode_version)
    codepoint_count = len(text)
    index = 0
    # 【要点】只记录“上一个宽度非 0 的字符”：VS16 只对这样的字符生效，
    #   零宽组合符不应该被它影响。
    last_measured_character: str | None = None

    total_width = 0
    # 【要点】spans 是“只增不改结构、只改最后一个元素”的列表：所有合并操作都作用在
    #   spans[-1] 上，因此扫描过程只需 O(区间数) 的空间，且天然保持区间连续、无重叠。
    spans: list[tuple[int, int, int]] = []
    SPECIAL = {"\u200d", "\ufe0f"}
    while index < codepoint_count:
        if (character := text[index]) in SPECIAL:
            if not spans:
                # ZWJ or variation selector at the beginning of the string doesn't really make sense.
                # But handle it, we must.
                # 【易错】此处没有“上一个区间”可扩展，只能单开一个零宽区间；
                #   若不特殊处理，下面的 spans[-1] 会直接 IndexError。
                spans.append((index, index := index + 1, 0))
                continue
            if character == "\u200d":
                # zero width joiner
                # The condition handles the case where a ZWJ is at the end of the string, and has nothing to join
                # 【要点】正常情况下跨两格（ZWJ 自身 + 被连接的字符）；若 ZWJ 已在串尾，
                #   只跨一格，避免越界。
                index += 2 if index < (codepoint_count - 1) else 1
                # 【算法】把上一区间的终点推进到 index，列宽保持不变——
                #   于是整个 ZWJ 序列在外层看来仍是一个区间、一份列宽。
                start, _end, cell_length = spans[-1]
                spans[-1] = (start, index, cell_length)
            else:
                # variation selector 16
                index += 1
                if last_measured_character:
                    start, _end, cell_length = spans[-1]
                    # 【要点】只有 narrow_to_wide 成员（如 ❤ U+2764）需要因为 VS16 而 +1 列；
                    #   已经是宽字符的（如某些 emoji 基础字形）不再加宽。
                    # 【易错】加宽时要把 last_measured_character 置空，否则连续多个 VS16 会重复加 1。
                    if last_measured_character in cell_table.narrow_to_wide:
                        last_measured_character = None
                        cell_length += 1
                        total_width += 1
                    spans[-1] = (start, index, cell_length)
                else:
                    # No previous character to change the size of.
                    # Shouldn't occur in practice.
                    # But handle it, we must.
                    start, _end, cell_length = spans[-1]
                    spans[-1] = (start, index, cell_length)
            continue

        if character_width := get_character_cell_size(character, unicode_version):
            last_measured_character = character
            # 【要点】海象表达式同时推进 index 并作为区间终点，区间因此是 [index_旧, index_新)。
            spans.append((index, index := index + 1, character_width))
            total_width += character_width
        else:
            # Character has zero width
            if spans:
                # zero width characters are associated with the previous character
                # 【算法】零宽字符不新开区间，只把上一个区间的 end 向后推一格，
                #   使“组合符跟随基字符”呈现在同一个字素簇里。
                start, _end, cell_length = spans[-1]
                spans[-1] = (start, index := index + 1, cell_length)
            else:
                # A zero width character with no prior spans
                # 【边界】串首就是零宽字符：仍要产出一个（零宽）区间，
                #   保证区间列表覆盖整个字符串，供按列切分的算法安全索引。
                spans.append((index, index := index + 1, 0))

    # 【返回】区间列表 + 整串列宽；两者必须自洽（总宽等于各区间列宽之和）。
    return (spans, total_width)


# ==== 【按列切分：把文本在指定列位置锯开】 ====
# 【功能】按列位置把文本切成左右两段（内部实现，前提是已确认文本不是全单宽字符）。
# 【算法】
#   1) cell_position <= 0 时直接返回 ("", text)；
#   2) 先取字素簇区间与整串列宽，用按比例的初始猜测
#      offset = int((cell_position / cell_length) * len(spans)) 定位大致区间，
#      并累加出该猜测点左侧的列宽 left_size——在列宽分布均匀时这一步几乎已经命中；
#   3) 进入 while 循环按 left_size 与 cell_position 的关系左右微调 offset：
#      - 相等：切分点正好落在区间边界，用 spans[offset][0] 作为下标切成两个完整字符串
#        （若 offset 已越过末尾，说明切分点在整串之后，返回 (text, "")）；
#      - left_size < cell_position：向右走；若当前区间跨越了切分点
#        （left_size + cell_size > cell_position），说明切分点落在宽字符内部，
#        于是把这个宽字符替换成两个空格，左右各一个；
#      - left_size > cell_position：向左走，用 offset - 1 做对称的跨越判断与处理。
# 【参数】text (str)：待切分文本；cell_position (int)：以列为单位的切分位置；
#   unicode_version：Unicode 版本，"auto" 自动。
# 【返回】tuple[str, str]：左段与右段；宽字符被锯开时两侧各多一个空格。
# 【注意】1) 第 2 步会用 cell_length 做除数，因此 cell_length == 0（整串都是零宽字符）
#      会抛 ZeroDivisionError；split_text / set_cell_size 的调用路径保证了存在正宽度字符，
#      但直接调用本函数时需要自行保证；
#   2) “宽字符劈成两个空格”是刻意的取舍：终端无法只画半个汉字，用空格占位能保证
#      左右两段的列宽之和仍等于原宽，从而不破坏外层布局；
#   3) 循环没有“找不到解”的退出分支：left_size 每次至少变动 1，必然收敛。
def _split_text(
    text: str, cell_position: int, unicode_version: str = "auto"
) -> tuple[str, str]:
    """Split text by cell position.

    If the cell position falls within a double width character, it is converted to two spaces.

    Args:
        text: Text to split.
        cell_position Offset in cells.
        unicode_version: Unicode version, `"auto"` to auto detect, `"latest"` for the latest unicode version.

    Returns:
        Tuple to two split strings.
    """
    if cell_position <= 0:
        return "", text

    spans, cell_length = split_graphemes(text, unicode_version)

    # Guess initial offset
    # 【算法】按“列位置占总列数的比例”猜测它落在第几个区间；区间数远小于字符数时，
    #   这个猜测通常只差一两格，后面的 while 循环只需做少量线性修正。
    offset = int((cell_position / cell_length) * len(spans))
    # 【优化】用 map(_span_get_cell_len, ...) 累加前缀列宽：itemgetter 是 C 实现，
    #   比 Python 层循环求和快；切片只复制指针，代价很低。
    left_size = sum(map(_span_get_cell_len, spans[:offset]))

    while True:
        if left_size == cell_position:
            # 【要点】切分点恰好落在区间边界上：直接用该区间的起点做下标，
            #   左右两段都是完整字符串，不需要补空格。
            if offset >= len(spans):
                return text, ""
            split_index = spans[offset][0]
            return text[:split_index], text[split_index:]
        if left_size < cell_position:
            start, end, cell_size = spans[offset]
            # 【要点】当前区间从 left_size 起、到 left_size + cell_size 止；若切分点严格落在
            #   区间内部，说明这是一个宽字符（cell_size 为 2），只能替换成两个空格。
            if left_size + cell_size > cell_position:
                return text[:start] + " ", " " + text[end:]
            offset += 1
            left_size += cell_size
        else:  # left_size > cell_position
            start, end, cell_size = spans[offset - 1]
            # 【易错】向左走时必须看 spans[offset - 1]（即 left_size 真正覆盖的那个区间），
            #   判断条件也相应改成 left_size - cell_size < cell_position；两侧逻辑互为镜像。
            if left_size - cell_size < cell_position:
                return text[:start] + " ", " " + text[end:]
            offset -= 1
            left_size -= cell_size


# 【功能】按列位置切分文本（对外入口）。
# 【算法】先走快速路径：整串都是单宽字符时直接按 Python 切片切分（列位置与字符下标一一对应），
#   否则交给 _split_text 处理宽字符与组合符。
# 【参数】text (str)：待切分字符串；cell_position (int)：以列为单位的切分位置；
#   unicode_version：Unicode 版本，"auto" 自动。
# 【返回】tuple[str, str]：左段（列宽等于 cell_position）与右段。
# 【注意】1) 切分点落在宽字符内部时，该字符会被替换成两个空格，左右各一个
#      ——例如 split_text("中文", 1) == (" ", " 文")，两个空格合计 2 列，总宽不变；
#   2) 负的 cell_position 在两条路径下行为不同（快速路径按 Python 切片语义保留了 text[:-1]
#      这类结果，慢速路径则因 cell_position <= 0 直接返回 ("", text)），调用方应传非负值；
#   3) 文本首尾的空格不会被自动去掉，裁剪由调用方决定。
def split_text(
    text: str, cell_position: int, unicode_version: str = "auto"
) -> tuple[str, str]:
    """Split text by cell position.

    If the cell position falls within a double width character, it is converted to two spaces.

    Args:
        text: Text to split.
        cell_position Offset in cells.
        unicode_version: Unicode version, `"auto"` to auto detect, `"latest"` for the latest unicode version.

    Returns:
        Tuple to two split strings.
    """
    # 【要点】快速路径：全是单宽字符时，“列位置”就等于“字符下标”，可以直接切片，
    #   完全不需要构造字素簇区间。
    if _is_single_cell_widths(text):
        return text[:cell_position], text[cell_position:]
    return _split_text(text, cell_position, unicode_version)


# ==== 【固定宽度排版：补齐与裁剪】 ====
# 【功能】把字符串调整到恰好 total 列：不足则用空格补齐，超出则裁剪掉多余部分。
# 【算法】
#   1) 快速路径：整串单宽时直接比较长度并切片（补齐用 " " * (total - size)）；
#   2) 慢速路径：total <= 0 直接返回空串；cell_size == total 原样返回；
#      cell_size < total 补空格；否则调用 _split_text 只取左段作为裁剪结果。
# 【参数】text (str)：待调整字符串；total (int)：目标列数；unicode_version：Unicode 版本。
# 【返回】str：列宽等于 total 的新字符串（total <= 0 且走慢速路径时为 ""）。
# 【注意】1) 慢速路径里调用的是 cell_len(text)（使用默认的 "auto" 版本），并未把
#      unicode_version 透传进去——测量与切分理论上可能用不同版本的表，这是上游的一处小不一致；
#   2) 负数 total 在两条路径下结果不同：单宽文本会走 text[:total]，例如
#      set_cell_size("abc", -1) == "ab"（**不会**裁剪成空串），而含宽字符的文本因
#      total <= 0 分支返回 ""；调用方不应传负值；
#   3) 返回的永远是新的字符串对象（Python 字符串不可变），不会就地修改入参；
#   4) 补齐用的是普通空格而不是样式化的 Segment——着色由上层 Segment 负责。
# 【协作】rule.py（画分隔线）、text.py（pad/裁剪）都直接调用它。
def set_cell_size(text: str, total: int, unicode_version: str = "auto") -> str:
    """Adjust a string by cropping or padding with spaces such that it fits within the given number of cells.

    Args:
        text: String to adjust.
        total: Desired size in cells.
        unicode_version: Unicode version.

    Returns:
        A string with cell size equal to total.
    """
    # 【快速路径】全是单宽字符时无需查宽度表，长度即列数。
    if _is_single_cell_widths(text):
        size = len(text)
        if size < total:
            return text + " " * (total - size)
        return text[:total]
    # 【边界】目标列数为 0 或负数：含宽字符时一律视作“要一个空串”。
    if total <= 0:
        return ""
    # 【易错】这里用默认的 "auto" 版本测量，没有透传 unicode_version（见函数注释第 1 条）。
    cell_size = cell_len(text)
    if cell_size == total:
        return text
    if cell_size < total:
        return text + " " * (total - cell_size)
    # 【要点】需要裁剪：复用 _split_text 的左段，宽字符被锯开时左段末尾会自动补一个空格，
    #   于是结果列宽恰好等于 total。
    text, _ = _split_text(text, total, unicode_version)
    return text


# ==== 【按宽度折行：一行文本 → 多行】 ====
# 【功能】按可用列宽把一个字符串折成多行，使每一行的列宽都不超过 width。
# 【算法】
#   1) 快速路径：整串单宽时按 width 等距切片（range(0, len, width)）；
#   2) 否则先取字素簇区间，再线性扫描：累加 line_size，若再加入当前区间会超过 width，
#      就在该区间的起点处断行（把 text[line_offset:start] 收进结果），line_offset 指向本区间
#      起点、计数清零；随后把当前区间计入新行；
#   3) 扫描结束后若还有未收尾的列宽（line_size 非 0），把剩余文本作为最后一行。
# 【参数】text (str)：待折行文本；width (int)：每行可用的列数；unicode_version：Unicode 版本。
# 【返回】list[str]：折行结果，每行列宽 <= width。
# 【注意】1) 实际行宽可能**小于** width：断行只发生在字素簇边界，不会把宽字符拆开，
#      所以“折行”不等于“等宽切分”；
#   2) 收尾判断用的是 `if line_size:`（看列宽是否非 0），因此纯零宽字符的文本会得到空列表
#      ——例如 chop_cells("\u200b", 5) == []，原文被丢弃，调用方需要留意；
#   3) width 必须为正：0 或负数会导致每次迭代都断行，产出大量空行甚至死循环式增长；
#   4) 快速路径与慢速路径的分行位置可能略有不同（快速路径按字符数等分），
#      但两者都保证每行不超宽。
# 【协作】Text.wrap 的 fold 溢出策略、以及需要“先折行再逐行渲染”的场景会用到它。
def chop_cells(text: str, width: int, unicode_version: str = "auto") -> list[str]:
    """Split text into lines such that each line fits within the available (cell) width.

    Args:
        text: The text to fold such that it fits in the given width.
        width: The width available (number of cells).

    Returns:
        A list of strings such that each string in the list has cell width
        less than or equal to the available width.
    """
    # 【快速路径】单宽文本直接等距切片，一行 width 个字符。
    if _is_single_cell_widths(text):
        return [text[index : index + width] for index in range(0, len(text), width)]
    spans, _ = split_graphemes(text, unicode_version)
    line_size = 0  # Size of line in cells
    lines: list[str] = []
    line_offset = 0  # Offset (in codepoints) of start of line
    for start, end, cell_size in spans:
        # 【要点】“超过 width 才断行”意味着允许行宽恰好等于 width；断行点取当前区间的起点，
        #   因此宽字符绝不会被拆到两行。
        if line_size + cell_size > width:
            lines.append(text[line_offset:start])
            line_offset = start
            line_size = 0
        line_size += cell_size
    # 【易错】这里判断的是列宽而不是字符数：纯零宽字符的文本 line_size 仍为 0，
    #   于是最后一行不会被收进结果（该文本整体被丢弃）。
    if line_size:
        lines.append(text[line_offset:])

    return lines
