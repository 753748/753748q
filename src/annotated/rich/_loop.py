from typing import Iterable, Tuple, TypeVar

# ---------------------------------------------------------------------------
# 【模块定位】极小的迭代辅助工具集，位于渲染管线的“遍历/拼接”环节：把任意可迭代对象包装成
#   “带首/尾标记”的生成器，让渲染代码在拼接分隔符、决定是否换行/是否画边框时，
#   既不必事先知道序列长度，也不必把序列物化成 list。
#   被 box.py、pretty.py、table.py、traceback.py、tree.py、syntax.py、live_render.py、
#   screen.py 等大量模块复用，是 Rich 内部最通用的“小工具”之一。
# 【关键数据结构】只有类型变量 T；三个函数统一产出“标志位… + 元素”的元组流，
#   标志位顺序与函数名一致：loop_first → (first, value)、loop_last → (last, value)、
#   loop_first_last → (first, last, value)。
# 【设计要点】
#   - 全部用生成器 + “预读一个元素”的滞后技巧实现，内存占用 O(1)，也能处理惰性甚至
#     无限的可迭代对象；代价是只能单次遍历（single-pass），调用方不能再对原对象迭代。
#   - 空序列必须产出空流而不是抛异常，因此第一次 next() 用 try/except StopIteration 包住；
#     生成器内部也绝不能向外泄漏 StopIteration（PEP 479 会把它转成 RuntimeError），
#     所以这里用的是普通 `return`。
#   - loop_last 先把当前元素暂存、等读到下一个时才吐出，于是“最后一个元素”成为唯一
#     一个 last=True 的输出；这也意味着元素会延迟一拍才被消费方看到。
# 【阅读建议】先读 loop_last（Rich 里用得最多、技巧也最明显的一段），再对照 loop_first 与
#   loop_first_last 观察三种标志位的组合方式；配合 tests/test_tools.py 的断言理解返回顺序。
# ---------------------------------------------------------------------------

# 【要点】T 只出现在类型标注里，运行期不存在；保留它是为了让类型检查器能推断出元素类型。
T = TypeVar("T")


# 【功能】遍历 values，为每个元素附带“是否第一个”的标记。
# 【算法】先 next() 取出首个元素并单独 yield (True, value)，再对剩余部分逐个 yield
#   (False, value)；空序列在第一次 next() 抛 StopIteration 时直接 return，产出空流。
# 【参数】values：任意可迭代对象（不要求是 Sequence，也不需要 len）。
# 【返回】Iterable[Tuple[bool, T]]，标志位在前、元素在后。
# 【注意】生成器只消费一次；调用方若在遍历中途还去迭代同一个底层迭代器，会与这里“抢”元素。
def loop_first(values: Iterable[T]) -> Iterable[Tuple[bool, T]]:
    """Iterate and generate a tuple with a flag for first value."""
    iter_values = iter(values)
    try:
        value = next(iter_values)
    except StopIteration:
        # 【易错】必须用这种“普通 return”结束生成器，而不是让 StopIteration 冒泡：
        #   PEP 479 之后，生成器内未捕获的 StopIteration 会被转成 RuntimeError。
        return
    yield True, value
    for value in iter_values:
        yield False, value


# 【功能】遍历 values，为每个元素附带“是否最后一个”的标记。
# 【算法】先预读一个元素存入 previous_value；此后每读到一个新元素，就吐出上一个
#   （last=False）并把它换成新的暂存值；循环结束后再吐出最后暂存的那个（last=True）。
#   因此空序列不产出任何内容，单元素序列产出一次 (True, value)。
# 【参数】values：任意可迭代对象。
# 【返回】Iterable[Tuple[bool, T]]，标志位在前、元素在后。
# 【注意】判断“最后一个”的代价是延迟一拍产出：消费方拿到元素的时间点比源迭代慢一个元素；
#   对无限迭代器来说永远等不到 last=True，循环也永不结束。
# 【协作】live_render.py 用它决定“是否还要续写下一行”，box.py 用它决定分隔符放在哪一侧。
def loop_last(values: Iterable[T]) -> Iterable[Tuple[bool, T]]:
    """Iterate and generate a tuple with a flag for last value."""
    iter_values = iter(values)
    try:
        previous_value = next(iter_values)
    except StopIteration:
        return
    for value in iter_values:
        yield False, previous_value
        previous_value = value
    # 【要点】循环已耗尽 iter_values，previous_value 就是最后一个元素，此处才补上 last=True。
    yield True, previous_value


# 【功能】同时给出“是否第一个”和“是否最后一个”两个标记。
# 【算法】沿用 loop_last 的滞后暂存法，另加一个 first 布尔量：首个产出 first=True，
#   之后翻转为 False；循环结束后把最后暂存元素以 (first, True) 产出——
#   单元素序列会得到 (True, True) 这一个元组。
# 【参数】values：任意可迭代对象。
# 【返回】Iterable[Tuple[bool, bool, T]]，顺序固定为 (first, last, value)。
# 【注意】两个标志位都是“产出时才确定”，因此适合用来构造首行/末行形态不同的边框与缩进
#   （如 table.py 画行分隔线、traceback.py 给代码行加行号前缀）。
def loop_first_last(values: Iterable[T]) -> Iterable[Tuple[bool, bool, T]]:
    """Iterate and generate a tuple with a flag for first and last value."""
    iter_values = iter(values)
    try:
        previous_value = next(iter_values)
    except StopIteration:
        return
    first = True
    for value in iter_values:
        yield first, False, previous_value
        first = False
        previous_value = value
    # 【要点】first 会一直保持 False 直到循环结束——只有当序列只有一个元素时它才是 True，
    #   此时这个元组同时充当“第一个”和“最后一个”。
    yield first, True, previous_value
