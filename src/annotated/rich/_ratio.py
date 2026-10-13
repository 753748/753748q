from fractions import Fraction
from math import ceil
from typing import cast, List, Optional, Sequence, Protocol

# ---------------------------------------------------------------------------
# 【模块定位】本模块是 Rich 布局系统的“整数空间分配”工具库，位于渲染管线的**度量阶段**：
#   在真正把字符写进终端之前，先要把终端可用的行列空间（整数个字符单元）切分给各个子元素。
#   它是纯计算的叶子模块，只依赖 fractions（精确有理数）与 math.ceil，不导入 rich 的
#   任何其它子模块，因此可被无循环依赖地复用。
#   上游调用方：
#     - rich.layout.Layout 通过 ratio_resolve 把屏幕高度分给若干行（每行是一个 Edge）；
#     - rich.table.Table 通过 ratio_reduce / ratio_distribute 把表格宽度分给各列，
#       并用它们处理“超出/不足”的宽度增减。
# 【关键数据结构】没有自定义容器；核心是 Edge 协议描述的三个字段：
#     size（固定尺寸，None 表示“弹性/待分配”）、ratio（弹性权重）、minimum_size（下限）。
#   三个函数都只在 List[int] 上运算，返回与入参等长的整数列表。
# 【设计要点】
#   - 用 Fraction 而不是 float 做比例除法：要保证“各段之和恰好等于 total”，
#     float 的舍入误差会让最后一段少 1 个字符，从而在终端上留下错位。
#   - ratio_resolve 采用“逐轮收敛”而非一次性求解：某个弹性边一旦被判定必须取
#     minimum_size，它就变成固定尺寸并使剩余空间重算，所以外层必须 while 重来。
#   - ratio_reduce / ratio_distribute 用“边分边减”的滚动剩余量（total_remaining、
#     total_ratio 同步递减），最后一段自然拿走全部余数，从而严格守恒。
#   - 约束冲突时（最小尺寸之和 > total）不抛异常，而是返回“之和大于 total”的结果，
#     把裁剪责任交给调用方（Layout 会裁掉超出屏幕高度的行）。
# 【阅读建议】先读 Edge 掌握输入契约，再读 ratio_resolve（最复杂，含 while + for/else），
#   然后对照读 ratio_reduce 与 ratio_distribute——两者结构几乎对称，区别只在
#   min/round 与 max/ceil 的取舍方向，最后看文件末尾的 __main__ 自测。
#   注意：本文件没有模块 docstring，因此本注释块紧跟在导入之后。
# ---------------------------------------------------------------------------


# ==== 【输入契约：Edge 协议】 ====
# 【职责】结构化类型协议（typing.Protocol），描述 ratio_resolve 所要求的“边”的最小接口：
#   任何具备 size / ratio / minimum_size 三个属性的对象都能被接受，例如 Layout 的子节点，
#   或测试里用 dataclass 临时定义的结构体。
# 【关键属性】size：固定占用的大小，None 表示弹性可变；ratio：弹性权重（默认 1）；
#   minimum_size：分配下限（默认 1）。单位都是字符单元（cell），宽字符算 2。
# 【不变量】只有 size 为 None 的边才参与比例分配；ratio 是整数权重，0 表示该边可分到 0。
# 【协作】ratio_resolve 只读取这三个属性，从不写回；调用方（Layout）负责把结果落到自身。
# 【注意】Protocol 不参与运行时 isinstance 检查，它只是类型标注的载体；这里把三个属性
#   写成“带默认值的类属性”而非纯注解，是为了让结构体在运行期也能满足该协议。
# 【兼容】typing.Protocol 需要 Python 3.8+；更早版本由 typing_extensions 提供。
class Edge(Protocol):
    """Any object that defines an edge (such as Layout)."""

    size: Optional[int] = None
    ratio: int = 1
    minimum_size: int = 1


# ==== 【核心算法：按约束解析空间】 ====
# 【功能】把 total 个字符单元分配给 edges：先满足固定 size，再按 ratio 加权分配弹性空间，
#   并保证每条边至少拿到 minimum_size（除非空间不够）。
# 【算法】
#   1) 把所有 edge.size 抄进 sizes，None 表示“待定”。
#   2) 只要 sizes 里还有 None 就循环：
#      a) 收集弹性边（sizes 为 None）及其原始下标，便于回写；
#      b) remaining = total - 已固定边之和；
#      c) 若 remaining <= 0，说明固定边已吃满甚至超出空间，弹性边一律降级为 minimum_size
#         （此时结果之和可能 > total，交由调用方裁剪）；
#      d) 否则算 portion = remaining / 弹性边 ratio 之和（用 Fraction，精确不丢余数）；
#      e) 若某弹性边 portion*ratio <= minimum_size，就把它钉成 minimum_size 并 break，
#         让外层 while 重算（固定尺寸变了，portion 必须重算）；
#      f) 若没有任何边低于下限，则按比例切分：用 divmod(portion*ratio + remainder, 1)
#         把上一段累计的分数余数带入下一段，做到“各段之和精确等于 remaining”。
#   3) 此时 sizes 已全是整数，cast 后返回。
# 【参数】total：可分配的字符单元总数（单位是 cell，不是字符个数——宽字符占 2 cell）。
#   edges：按最终返回顺序排列的边序列。
# 【返回】与 edges 等长的 List[int]；正常情况下各元素之和等于 total；当最小尺寸之和
#   超过 total 时，之和会大于 total（不抛异常）。
# 【注意】返回值之和并不保证等于 total，调用方不可假定守恒，必要时自行裁剪。
# 【易错】`edge.size or None` 把 size=0 也归入 None（0 与“未设置”在该协议里不可区分），
#   因此想表达“固定 0 宽”的边不应使用 size=0。
# 【约束】ratio 为 0 或 None 时按 1 处理（`edge.ratio or 1`），以避免除零。
# 【优化】每轮 while 至少把一个 None 变成整数（或直接返回），循环次数上界是边数，
#   整体 O(E^2) 量级；对 Layout 这种个位数的边数完全够用。
def ratio_resolve(total: int, edges: Sequence[Edge]) -> List[int]:
    """Divide total space to satisfy size, ratio, and minimum_size, constraints.

    The returned list of integers should add up to total in most cases, unless it is
    impossible to satisfy all the constraints. For instance, if there are two edges
    with a minimum size of 20 each and `total` is 30 then the returned list will be
    greater than total. In practice, this would mean that a Layout object would
    clip the rows that would overflow the screen height.

    Args:
        total (int): Total number of characters.
        edges (List[Edge]): Edges within total space.

    Returns:
        List[int]: Number of characters for each edge.
    """
    # 【要点】sizes 与 edges 下标对齐；None 是“弹性/待分配”的哨兵值，整数则是已定尺寸。
    # Size of edge or None for yet to be determined
    sizes = [(edge.size or None) for edge in edges]

    # 【为什么】把 Fraction 绑定到局部名 _Fraction：它是本函数最热的构造调用，
    # 局部变量查找比全局名查找快，也让下面的长表达式更短。
    _Fraction = Fraction

    # 【注意】外层循环不可省：某轮把边钉成 minimum_size 后，分母与剩余空间都变了。
    # While any edges haven't been calculated
    while None in sizes:
        # 【要点】同时记下 index 与 edge，因为后面必须按原下标回写 sizes。
        # Get flexible edges and index to map these back on to sizes list
        flexible_edges = [
            (index, edge)
            for index, (size, edge) in enumerate(zip(sizes, edges))
            if size is None
        ]
        # 【要点】remaining 只减去“已确定”的尺寸；None 用 `or 0` 兜底避免 TypeError。
        # Remaining space in total
        remaining = total - sum(size or 0 for size in sizes)
        # 【为什么】空间被固定边占满时不能做比例分配（分母会变成 0 或负数），
        # 只能让所有弹性边取最小尺寸，并接受“总和 > total”的失真结果。
        if remaining <= 0:
            # 【易错】这里直接 return，不再走后面的比例分支；总和可能大于 total。
            # No room for flexible edges
            return [
                ((edge.minimum_size or 1) if size is None else size)
                for size, edge in zip(sizes, edges)
            ]
        # 【要点】portion 是“1 份 ratio 对应多少 cell”，用 Fraction 表示以避免累积误差。
        # Calculate number of characters in a ratio portion
        portion = _Fraction(
            remaining, sum((edge.ratio or 1) for _, edge in flexible_edges)
        )

        # 【算法】先扫描是否有人会低于下限：只要有，就把它钉住并跳出 for，
        # 借 for/else 的语义让 else 分支（真正的分配）不执行，回到 while 重算。
        # If any edges will be less than their minimum, replace size with the minimum
        for index, edge in flexible_edges:
            # 【要点】portion*ratio 是该边按比例应得的 cell 数（Fraction）；与下限比较即可判定。
            if portion * edge.ratio <= edge.minimum_size:
                sizes[index] = edge.minimum_size
                # 【为什么】固定尺寸一变，remaining 与 portion 都失效，故必须重来一轮。
                # New fixed size will invalidate calculations, so we need to repeat the process
                break
        else:
            # 【算法】没有任何边触底：逐条按比例切分。Fraction 与 int 的 divmod 得
            # (整数商, 分数余数)，把余数加到下一段的被除数上，最终精度不丢、总和守恒。
            # Distribute flexible space and compensate for rounding error
            # Since edge sizes can only be integers we need to add the remainder
            # to the following line
            remainder = _Fraction(0)
            for index, edge in flexible_edges:
                # 【要点】divmod(x, 1) 的商是向下取整的整数，余数 ∈ [0,1)，正好补偿给下一段。
                size, remainder = divmod(portion * edge.ratio + remainder, 1)
                sizes[index] = size
            # 【要点】分配完成后必须 break 出 while，否则 None in sizes 仍为 True 会死循环。
            break
    # 【要点】此刻 sizes 中已无 None；cast 只用于让类型检查器满意，运行期无开销。
    # Sizes now contains integers only
    return cast(List[int], sizes)


# ==== 【按比例“削减”：ratio_reduce】 ====
# 【功能】按 ratios 把 total 分摊到各个槽位，并从每个 values[i] 中**减去**自己那一份，
#   返回削减后的新值列表。它不返回“分到多少”，而返回“分完之后还剩多少”。
# 【算法】滚动分配：状态量 total_remaining（还剩多少总量没分）与 total_ratio（剩余权重和）
#   每轮同步递减，因此第 i 个槽位用“当前剩余”而非“初始总量”计算份额，
#   天然把前面取整留下的误差留给后面的槽位，最终总削减量不会超过 total。
#   每份再被 min(maximum, ...) 封顶，保证不会削减超过该槽位允许的上限。
# 【参数】total：本次要从所有槽位上削减掉的总量（正数）。
#   ratios：各槽位的权重；maximums：各槽位可分到的上限（为 0 表示该槽位不参与）；
#   values：各槽位的当前值（table.py 里就是列宽），返回值即 values[i] - 分到的削减量。
# 【返回】与 values 等长的 List[int]。各元素之和为 sum(values) - 实际削减总量；
#   由于封顶与取整，实际削减总量可能小于 total。
# 【注意】docstring 中“guaranteed to sum to total”的说法对“削减”语义并不成立：
#   返回的是剩余值而不是分配值，这里以代码行为准。
# 【易错】Python 内建 round 是“四舍六入五取偶”（banker's rounding），
#   单看一轮可能偏高或偏低，靠滚动剩余量抵消，不要改成 int() 或 math.floor。
# 【协作】调用方 rich.table.Table 用它把“表格总宽超出可用宽度”的多余量从各列扣掉。
def ratio_reduce(
    total: int, ratios: List[int], maximums: List[int], values: List[int]
) -> List[int]:
    """Divide an integer total in to parts based on ratios.

    Args:
        total (int): The total to divide.
        ratios (List[int]): A list of integer ratios.
        maximums (List[int]): List of maximums values for each slot.
        values (List[int]): List of values

    Returns:
        List[int]: A list of integers guaranteed to sum to total.
    """
    # 【要点】maximums 为 0 的槽位直接把权重清零：既不参与分配，也不会被削减。
    ratios = [ratio if _max else 0 for ratio, _max in zip(ratios, maximums)]
    total_ratio = sum(ratios)
    # 【为什么】所有槽位都被封顶为 0 时不能除以 0，原样返回 values 的副本（不可返回同一对象，
    # 否则调用方就地修改会污染入参）。
    if not total_ratio:
        return values[:]
    total_remaining = total
    result: List[int] = []
    append = result.append
    for ratio, maximum, value in zip(ratios, maximums, values):
        # 【易错】条件里同时有 ratio（该槽位是否参与）与 total_ratio > 0（是否还有剩余权重），
        # 少了后者就会在后面的轮次里除零。
        if ratio and total_ratio > 0:
            # 【要点】用“当前剩余总量/当前剩余权重”计算份额，再用 maximum 封顶。
            distributed = min(maximum, round(ratio * total_remaining / total_ratio))
            # 【要点】返回的是“削减后剩余的值”，不是分到的份额。
            append(value - distributed)
            total_remaining -= distributed
            total_ratio -= ratio
        else:
            # 【要点】不参与的槽位原样保留。
            append(value)
    return result


# ==== 【按比例“分配”：ratio_distribute】 ====
# 【功能】按 ratios 把 total 分摊到各个槽位，每个槽位至少拿到 minimums[i]；
#   与 ratio_reduce 相反，这里返回的就是“分到多少”，而不是“剩多少”。
# 【算法】与 ratio_reduce 对称的滚动分配，但取整方向相反：
#   - 份额用 ceil 向上取整（宁可多给当前槽位，把不足留给后面的槽位）；
#   - 下限用 max 生效（保证不低于 minimum）；
#   - total_ratio 与 total_remaining 同步递减，因此最后一个槽位的份额恰好等于
#     当时的 total_remaining（其 ratio 已被自己整除），从而把总账“抹平”。
# 【参数】total：要分配的总量（正数）；ratios：各槽位权重，为 0 表示不参与；
#   minimums：各槽位的下限，None 时视为全 0（可以纯按比例分配）。
# 【返回】与 ratios 等长的 List[int]；在最小值约束都能满足时各元素之和等于 total；
#   若前面的 minimum 已经吃掉超额，末尾槽位仍以 minimum 兜底，此时之和可能 > total。
# 【注意】若 ratios 之和为 0 会触发 assert 抛 AssertionError；而 `python -O` 会跳过
#   assert，因此在被优化运行时该保护失效，调用方必须自己保证权重非零。
# 【易错】`if minimums:` 判断的是列表真假：空列表与 None 都会走“不改 ratios”的分支；
#   而非空列表中 minimum 为 0 的槽位权重会被清零，即“下限为 0”等价于“不参与分配”。
# 【协作】rich.table.Table 用它把弹性宽度按列比例分给各列（含 flex 列的最小宽度）。
def ratio_distribute(
    total: int, ratios: List[int], minimums: Optional[List[int]] = None
) -> List[int]:
    """Distribute an integer total in to parts based on ratios.

    Args:
        total (int): The total to divide.
        ratios (List[int]): A list of integer ratios.
        minimums (List[int]): List of minimum values for each slot.

    Returns:
        List[int]: A list of integers guaranteed to sum to total.
    """
    # 【要点】下限为 0 的槽位权重清零：既保证不被“至少分 minimum”影响，也不参与比例分配。
    if minimums:
        ratios = [ratio if _min else 0 for ratio, _min in zip(ratios, minimums)]
    total_ratio = sum(ratios)
    # 【注意】这是本模块唯一主动抛错的地方；被 python -O 优化掉时不会抛。
    assert total_ratio > 0, "Sum of ratios must be > 0"

    total_remaining = total
    distributed_total: List[int] = []
    append = distributed_total.append
    # 【为什么】minimums 为 None 时也要有等长的下限序列，才能和 ratios 一起 zip；
    # 用 [0]*len(ratios) 而不是 []，避免 zip 提前截断导致结果长度不对。
    if minimums is None:
        _minimums = [0] * len(ratios)
    else:
        _minimums = minimums
    for ratio, minimum in zip(ratios, _minimums):
        # 【易错】不能用 `if ratio` 做判断：ratio 为 0 的槽位仍需要走 else 分支被赋值，
        # 否则结果长度会短于入参。这里判断的是“剩余权重是否还有”。
        if total_ratio > 0:
            # 【要点】ceil 向上取整 + max 保底，两者共同保证“不小于最小宽度”。
            distributed = max(minimum, ceil(ratio * total_remaining / total_ratio))
        else:
            # 【要点】剩余权重已耗尽：把剩下的额度全部塞给当前槽位，避免总额丢失。
            distributed = total_remaining
        append(distributed)
        total_ratio -= ratio
        total_remaining -= distributed
    return distributed_total


# ==== 【模块自测入口】 ====
# 【功能】直接 `python -m rich._ratio` 时跑一个最小示例：把 110 个字符单元
#   平均分给 3 条 ratio=1、minimum_size=1 的弹性边，并打印分配结果之和。
# 【注意】这只是开发期的冒烟测试，不属于对外 API；生产代码不会执行到这里。
# 【兼容】dataclass 的导入放在 if 内部，以免在正常 import 模块时增加额外开销。
if __name__ == "__main__":
    from dataclasses import dataclass

    # 【职责】演示用的最小 Edge 实现：让 ratio_resolve 有一个满足 Edge 协议的对象可用。
    # 【关键属性】size/ratio/minimum_size 与 Edge 协议完全对应。
    # 【要点】用 dataclass 在演示块里现场定义，避免为了自测而在模块中新增公开类型。
    @dataclass
    class E:
        size: Optional[int] = None
        ratio: int = 1
        minimum_size: int = 1

    resolved = ratio_resolve(110, [E(None, 1, 1), E(None, 1, 1), E(None, 1, 1)])
    print(sum(resolved))
