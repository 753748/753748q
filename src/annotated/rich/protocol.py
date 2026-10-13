from typing import Any, cast, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import RenderableType

# ---------------------------------------------------------------------------
# 【模块定位】Rich 的“可渲染对象”协议判定层，位于渲染管线最外层的分派之前：
#   Console.print() / Measurement.get() / Table.add_row() 都先用这里的函数把任意对象
#   归一化成真正能被 __rich_console__ 消费的东西。
#   依赖只有 typing；对 rich.console 仅在 TYPE_CHECKING 下导入，避免包内循环导入。
# 【关键数据结构】没有数据结构；协议本身是鸭子类型，包含三个可选接口：
#     str（直接当纯文本）、__rich__（把自身转换成另一个 renderable，可递归）、
#     __rich_console__（真正的渲染入口，返回 Segment 迭代器）。
# 【设计要点】
#   - 用 hasattr 而不是 isinstance/ABC 做判定：第三方对象无需继承任何 Rich 基类即可接入。
#     需要 isinstance 版本时用 rich.abc.RichRenderable（它靠 __subclasshook__ 做结构判定），
#     但注意两者的判定范围并不相同：is_renderable 接受 str，而 RichRenderable 不接受
#     （tests/test_protocol.py 里 `not isinstance("foo", RichRenderable)` 与
#      `console.print("foo")` 同时成立，就是这个差异的体现）。
#   - rich_cast 用 while 循环反复解包 __rich__，并用 rich_visited_set 记录“已见过的结果类型”，
#     遇到 A→B→A 这类互相转换时及时 break，而不是无限递归。
#   - _GIBBERISH 随机哨兵串用于识别“声称拥有所有属性”的对象（实现了 __getattr__ 就返回值的
#     Mock/代理类）：连这个随机名字都能取到属性，就放弃转换、退回 repr()。
#   - 类对象本身不参与 __rich__ 解包，因为类上取到的 __rich__ 通常是未绑定函数，缺少 self。
# 【阅读建议】先读 is_renderable（协议的最简定义），再读 rich_cast 的三层保护
#   （哨兵串、循环检测、class 排除），最后用 tests/test_protocol.py 的五个用例验证理解。
# ---------------------------------------------------------------------------

# 【要点】哨兵属性名：一旦某个对象的 __getattr__ 连这个名字都能返回属性，
#   就说明它在“假装什么属性都有”，对它调用 __rich__ 只会得到无意义的结果，
#   因此 rich_cast 直接退回 repr()。用随机串而不是 "abc" 之类的常见名，是为了避免误判。
_GIBBERISH = """aihwerij235234ljsdnp34ksodfipwoe234234jlskjdf"""


# 【功能】判断对象是否可能被 Rich 渲染：它是 str，或者实现了 __rich__ / __rich_console__ 之一。
# 【算法】三个 isinstance/hasattr 的短路或运算；只探测属性是否存在，不调用任何方法，
#   因此不会触发渲染副作用（但 hasattr 本身会触发 __getattr__，相关防护见 rich_cast）。
# 【参数】check_object：任意对象。
# 【返回】bool：True 表示“可以尝试交给 Console 渲染”，并不保证渲染一定成功。
# 【协作】measure.py（Measurement.get）与 table.py（add_row）用它做快速放行；
#   判定为 False 的对象由调用方走 repr() 兜底（Console 也是这样处理普通对象的）。
def is_renderable(check_object: Any) -> bool:
    """Check if an object may be rendered by Rich."""
    return (
        isinstance(check_object, str)
        or hasattr(check_object, "__rich__")
        or hasattr(check_object, "__rich_console__")
    )


# 【功能】把对象“转换”（cast）成一个 Rich renderable：反复调用 __rich__，直到对象不再提供它。
# 【算法】
#   1) 循环前先建立 rich_visited_set（已见过的结果类型集合），用于断环；
#   2) 只要对象还有 __rich__ 且自身不是类，就继续：
#      a) 先用 _GIBBERISH 探测“假装什么属性都有”的对象，命中则直接返回 repr(renderable)；
#      b) 调用 __rich__() 取回新对象；
#      c) 若新对象的类型已在集合中，说明出现了 A→B→A 的类型循环，break 退出；
#         否则把该类型记入集合后继续下一轮；
#   3) 最后用 typing.cast 做静态标注（运行期是恒等操作）。
# 【参数】renderable：任意对象（不要求已经满足 is_renderable）。
# 【返回】RenderableType：解包后的对象；调用方通常还会用 is_renderable 复核一次。
# 【注意】每一层 __rich__ 都会被真正执行，因此实现方要保证它无副作用、且不要返回同类型的对象；
#   函数只保证“同类型不重复解包”，对每次都产生新类型的实现没有深度上限。
#   另外它只处理 __rich__，不处理 __rich_console__——后者由 Console 直接消费。
def rich_cast(renderable: object) -> "RenderableType":
    """Cast an object to a renderable by calling __rich__ if present.

    Args:
        renderable (object): A potentially renderable object

    Returns:
        object: The result of recursively calling __rich__.
    """
    from rich.console import RenderableType

    # 【要点】集合里存的是“每次转换结果”的类型，而不是原始对象的类型；
    #   这正是能在第二轮就发现 A→B→A 的原因。
    rich_visited_set: Set[type] = set()  # Prevent potential infinite loop
    # 【约束】`not isinstance(renderable, type)`：类对象即使定义了 __rich__ 也不解包，
    #   因为从类上取到的是未绑定函数，直接调用会缺少 self。
    while hasattr(renderable, "__rich__") and not isinstance(renderable, type):
        # Detect object which claim to have all the attributes
        # 【易错】实现了 __getattr__ 的对象（Mock、代理、Fake）会让任何 hasattr 都返回 True；
        #   这里用随机哨兵串把它识别出来，直接 return repr()，避免调用到无意义的“__rich__”。
        if hasattr(renderable, _GIBBERISH):
            return repr(renderable)
        # 【注意】这一步会真正执行用户代码（__rich__），是本模块唯一可能有副作用的位置。
        cast_method = getattr(renderable, "__rich__")
        renderable = cast_method()
        renderable_type = type(renderable)
        # 【算法】结果类型重复出现即判定进入循环，break 后把“当前对象”交给调用方；
        #   tests/test_protocol.py::test_cast_recursive 断言的 "<B>" 就来自这个分支。
        if renderable_type in rich_visited_set:
            break
        rich_visited_set.add(renderable_type)

    # 【要点】cast 是 typing.cast，运行期恒等，只对类型检查器有意义。
    return cast(RenderableType, renderable)
