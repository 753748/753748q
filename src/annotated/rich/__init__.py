"""Rich text and beautiful formatting in the terminal."""

import os
from typing import IO, TYPE_CHECKING, Any, Callable, Optional, Union

from ._extension import load_ipython_extension  # noqa: F401

# ---------------------------------------------------------------------------
# 【模块定位】rich 包的入口与公共 API 表面（`from rich import print` 拿到的就是这里的函数）。
#   它只承担三件事：1) 维护全局 Console 单例的懒加载与重新配置（get_console / reconfigure）；
#   2) 暴露五个与内置 print/inspect 同名或近似的便捷函数；3) 记录“导入 rich 时的当前工作目录”，
#   供 traceback 显示源码路径时使用。
#   除 ._extension 之外，所有子模块（console、_inspect 等）都在函数体内按需导入，
#   目的是让 `import rich` 保持极轻——导入期不加载 console/text/style/table 这些重模块。
# 【关键数据结构】模块级可变全局状态：_console（Optional[Console]，全局单例）、
#   _IMPORT_CWD（str，导入时的绝对工作目录；取不到时为 ""）。
# 【设计要点】
#   - __all__ 只列出 5 个名字，把 Console/Table/Text 等类排除在 `from rich import *` 之外：
#     强制用户写明所属子模块，避免把庞大的命名空间灌进用户模块。
#   - get_console() 采用“首次调用才构造”的懒加载：既避免导入期就探测终端、构造 Console，
#     也让用户能在导入之后再通过 reconfigure() 调整全局配置。
#   - reconfigure() 不替换引用、而是原地置换 __dict__：这样早先拿到 Console 引用的代码
#     无需改动就能看到新配置（详见该函数注释）。
#   - 本版本**没有** __version__ 属性（旧版本曾提供 `rich.__version__`）：版本号来自打包元数据，
#     需要查询时用 importlib.metadata.version("rich")，不要写 rich.__version__。
#   - 本模块的 print / inspect 与内置同名：`from rich import print` 会遮蔽内置 print，
#     这正是官方推荐的用法；但在本模块内部，模块级 def print 定义之后，裸写 print(...)
#     调用的就是 Rich 版本（文件末尾的 __main__ 块即如此），要调用内置版本必须显式
#     使用 builtins.print，阅读时请留意这一点。
#   - _IMPORT_CWD 的取值用 try/except 兜住：工作目录可能已被删除（FileNotFoundError），
#     此时退化为空串，traceback 便不再拼绝对路径。
# 【阅读建议】先读 __all__ 与 get_console/_console 这对“全局状态”，再读 print（最薄的一层转发，
#   最能看清 Rich 与内置 print 的关系），最后读 inspect（参数最多，且内含 inspect(inspect)
#   这一自我引用特例）。
# ---------------------------------------------------------------------------


# ==== 【公共 API 导出清单】 ====
# 【要点】只有这 5 个名字参与 `from rich import *`；Console/Table/Text 等类必须显式
#   `from rich.console import Console`，这是刻意的“小出口”策略。
# 【注意】__all__ 只影响星号导入，不影响 `import rich` 后通过属性访问这些函数。
__all__ = ["get_console", "reconfigure", "print", "inspect", "print_json"]

# 【为什么】只在类型检查期导入 Console：运行期它由 get_console() 在函数体内导入，
#   这样包导入阶段就不会拉起重达数千行的 console 模块，也不会形成循环导入。
if TYPE_CHECKING:
    from .console import Console

# ==== 【全局状态】 ====
# 【要点】全局 Console 单例的唯一存储位置，用前向引用标注类型（字符串形式）以配合上面的
#   TYPE_CHECKING 导入。它同时被 get_console() 与 reconfigure() 读写。
# 【约束】“检查 None + 赋值”不是原子操作：多线程首次同时调用可能各自构造一个 Console，
#   后写者覆盖先写者。因为 Console 自身的输出有锁保护，最坏只是浪费一次构造，不会破坏输出。
# Global console used by alternative print
_console: Optional["Console"] = None

# 【要点】在“导入 rich 的那一刻”就记录工作目录，而不是等到渲染 traceback 时再取：
#   程序运行中 chdir 之后，traceback 依然能拼出源码文件的正确路径。
# 【协作】traceback.py 通过 `from rich import _IMPORT_CWD` 读取它（见 traceback.py 468/571 行）。
try:
    _IMPORT_CWD = os.path.abspath(os.getcwd())
except FileNotFoundError:
    # Can happen if the cwd has been deleted
    # 【兼容】工作目录已被删除时退化为空串；traceback 里 os.path.join("", filename) 得到
    #   相对路径，显示效果符合预期，且不会抛异常。
    _IMPORT_CWD = ""


# ==== 【全局 Console 的获取与重配置】 ====
# 【功能】获取全局 Console 实例；不存在时创建它（懒加载单例）。
# 【算法】先检查 _console 是否为 None，是则在函数体内导入 Console 并构造，再写回全局变量；
#   之后无论哪次调用都返回同一个对象。
# 【返回】Console：全局实例，也是 print / print_json / inspect 这些便捷函数的默认输出目标。
# 【注意】1) 函数体内导入 console 既避免导入期加载重模块，也避免包内循环导入；
#   2) 需要改配置请用 reconfigure()，直接给 rich._console 赋值属于内部实现细节；
#   3) 构造 Console 会探测终端（宽度、颜色系统），因此第一次调用可能比后续调用慢。
def get_console() -> "Console":
    """Get a global :class:`~rich.console.Console` instance. This function is used when Rich requires a Console,
    and hasn't been explicitly given one.

    Returns:
        Console: A console instance.
    """
    # 【要点】必须声明 global，否则下面的赋值只会创建一个同名局部变量，全局单例永远是 None。
    global _console
    # 【算法】标准的懒加载判定：只有从未构造过（None）才进入构造分支。
    if _console is None:
        # 【为什么】导入写在函数体内：包导入期就不会牵出 console → text → style 这条长链，
        #   同时避免 rich/__init__ 与 rich/console 之间的循环导入。
        from .console import Console

        # 【注意】构造 Console 会探测终端（宽度、颜色系统、是否支持超链接），
        #   所以首次调用的开销明显大于后续调用；探测失败时会退回保守默认值。
        _console = Console()

    return _console


# 【功能】用新的构造参数“重新配置”全局 Console。
# 【算法】先用传入参数构造一个临时 Console，再取回当前全局 Console，并把临时实例的
#   __dict__ 整体赋给旧实例——即原地置换内部状态，而不是替换全局引用。
# 【参数】*args / **kwargs：原样转发给 rich.console.Console 构造器
#   （如 file=、width=、theme=、force_terminal= 等）。
# 【返回】None。调用后 get_console() 返回的仍是同一个对象（身份不变），但状态已是新的。
# 【为什么】保持对象身份不变，可以让早先 `_console = get_console()` 或把 Console 存进
#   属性/live/进度条的代码自动看到新配置；若改成新建实例再赋值给 _console，
#   那些旧引用仍指向过期对象。
# 【易错】1) 注意此处没有 `global _console` 声明——只是修改对象的属性，并未重新绑定模块名，
#      函数内的 `_console` 其实是局部变量；
#   2) __dict__ 置换会丢弃旧实例上后来添加的状态（录制缓冲、live 栈、临时属性）；
#   3) 参数非法会在构造临时 Console 时抛出，此时全局实例尚未被改动，不会留下半成品配置。
def reconfigure(*args: Any, **kwargs: Any) -> None:
    """Reconfigures the global console by replacing it with another.

    Args:
        *args (Any): Positional arguments for the replacement :class:`~rich.console.Console`.
        **kwargs (Any): Keyword arguments for the replacement :class:`~rich.console.Console`.
    """
    # 【要点】这里用绝对导入（rich.console）而不是相对导入（.console），
    #   只是风格差异，效果相同；两种写法在 Rich 中并存。
    from rich.console import Console

    # 【算法】先按新参数构造一个“配置好的”Console……
    new_console = Console(*args, **kwargs)
    # 【易错】下面这行没有 global 声明，因此 _console 是**局部变量**：
    #   它的作用只是拿到全局实例的引用，而不会重新绑定 rich._console 这个名字。
    _console = get_console()
    # 【为什么】整体置换 __dict__ 而不是替换引用：已持有该 Console 的代码（包括 Rich 内部的
    #   Live、进度条、用户保存的引用）会立刻看到新配置；旧实例上的临时状态也随之被清空。
    _console.__dict__ = new_console.__dict__


# ==== 【与内置 print / 调试打印对应的便捷函数】 ====
# 【功能】按与内置 print 完全相同的签名打印对象，但由 Rich 渲染
#   （因此字符串里的标记、以及 Text/Table/Panel 等 renderable 都会被正确渲染）。
# 【算法】file 为 None 时使用全局 Console（get_console()）；否则为这次调用临时构造
#   Console(file=file)，再把 sep/end 原样转发给 Console.print。
# 【参数】*objects：任意个待打印对象（str 会按 Rich 标记解析，例如 "[bold]x[/]"）；
#   sep：对象间分隔符，默认 " "；end：结尾字符，默认 "\n"；
#   file：输出目标，None 表示标准输出；flush：为与内置 print 签名兼容而保留，
#   Rich 总是立即写出缓冲，因此该参数不起作用。
# 【返回】None（内置 print 同样返回 None）。
# 【注意】1) 传 file 时会新建 Console，颜色系统/宽度等会重新探测，且不继承全局 Console 的配置；
#   2) 局部变量叫 write_console 而不是 console，是为了让读者一眼看出“这次输出用哪个 Console”，
#      也避免与本模块的 print 语义混淆；
#   3) 本函数是 rich 最常用的入口，因此它保持“零额外逻辑”，一切行为都在 Console.print 里。
def print(
    *objects: Any,
    sep: str = " ",
    end: str = "\n",
    file: Optional[IO[str]] = None,
    flush: bool = False,
) -> None:
    r"""Print object(s) supplied via positional arguments.
    This function has an identical signature to the built-in print.
    For more advanced features, see the :class:`~rich.console.Console` class.

    Args:
        sep (str, optional): Separator between printed objects. Defaults to " ".
        end (str, optional): Character to write at end of output. Defaults to "\\n".
        file (IO[str], optional): File to write to, or None for stdout. Defaults to None.
        flush (bool, optional): Has no effect as Rich always flushes output. Defaults to False.

    """
    from .console import Console

    # 【要点】file 为 None 时复用全局 Console（保留其主题、宽度、录制设置）；
    #   一旦显式给了 file，就为这次调用单独造一个 Console——这正是“输出到文件”的语义。
    write_console = get_console() if file is None else Console(file=file)
    # 【注意】直接把 Console.print 的返回值 return 出去；它（以及内置 print）都返回 None，
    #   flush 参数在此被忽略，因为 Console 每次写完缓冲都会立即 flush。
    return write_console.print(*objects, sep=sep, end=end)


# 【功能】美化打印 JSON：接受 JSON 文本或 Python 数据，输出带缩进与语法高亮的合法 JSON。
# 【算法】本函数只做转发——所有参数原样交给全局 Console 的 print_json()，
#   真正的缩进/高亮/编码逻辑在 rich/json.py 的 JSON 类里。
# 【参数】json：JSON 文本；data：当 json 为 None 时要编码的对象（两者取其一）；
#   indent：缩进量，默认 2（int 或 str）；highlight：是否语法高亮，默认 True；
#   skip_keys：跳过非基本类型的键，默认 False；ensure_ascii：转义所有非 ASCII 字符，默认 False；
#   check_circular：检查循环引用，默认 True；allow_nan：允许 NaN/Infinity，默认 True；
#   default：无法序列化对象的转换回调；sort_keys：按字典键排序，默认 False。
# 【返回】None。
# 【注意】1) `*` 之后的参数都是 keyword-only，必须用关键字传参；
#   2) 输出保证是合法 JSON，但可能带上 Rich 的颜色转义码（非终端上颜色会自动关闭）；
#   3) json 与 data 同时给出时以 json 为准（由 Console.print_json 决定）。
def print_json(
    json: Optional[str] = None,
    *,
    data: Any = None,
    indent: Union[None, int, str] = 2,
    highlight: bool = True,
    skip_keys: bool = False,
    ensure_ascii: bool = False,
    check_circular: bool = True,
    allow_nan: bool = True,
    default: Optional[Callable[[Any], Any]] = None,
    sort_keys: bool = False,
) -> None:
    """Pretty prints JSON. Output will be valid JSON.

    Args:
        json (str): A string containing JSON.
        data (Any): If json is not supplied, then encode this data.
        indent (int, optional): Number of spaces to indent. Defaults to 2.
        highlight (bool, optional): Enable highlighting of output: Defaults to True.
        skip_keys (bool, optional): Skip keys not of a basic type. Defaults to False.
        ensure_ascii (bool, optional): Escape all non-ascii characters. Defaults to False.
        check_circular (bool, optional): Check for circular references. Defaults to True.
        allow_nan (bool, optional): Allow NaN and Infinity values. Defaults to True.
        default (Callable, optional): A callable that converts values that can not be encoded
            in to something that can be JSON encoded. Defaults to None.
        sort_keys (bool, optional): Sort dictionary keys. Defaults to False.
    """

    # 【要点】全部参数原样转发给全局 Console 的 print_json；真正的 JSON 编码与高亮在
    #   rich/json.py 中实现，本函数只是把“模块级便捷入口”接到 Console 上。
    get_console().print_json(
        json,
        # 【要点】json 与 data 二选一：json 非 None 时直接解析该 JSON 文本，否则序列化 data。
        data=data,
        # 【要点】indent 既可以是 int（空格数），也可以是 str（缩进前缀），默认 2。
        indent=indent,
        # 【要点】下面三参数决定“好不好看”与“要不要过滤键”；
        #   注意 ensure_ascii=False 与标准库 json.dumps 的默认值(True)相反——Rich 选择保留
        #   原字符，以便中文等文本直接可读。
        highlight=highlight,
        skip_keys=skip_keys,
        ensure_ascii=ensure_ascii,
        # 【要点】这三项与标准库 json.dumps 语义一致：循环引用检查、NaN/Infinity 放行、
        #   以及无法直接序列化对象时的转换回调。
        check_circular=check_circular,
        allow_nan=allow_nan,
        default=default,
        # 【要点】sort_keys 默认 False，保持原始插入顺序，便于对照输入观察结构。
        sort_keys=sort_keys,
    )


# 【功能】检查并打印任意 Python 对象的结构信息（类型、属性、方法、docstring、值等）。
# 【算法】
#   1) 选定输出目标：显式传入的 console 优先，否则用全局 Console；
#   2) 取特例标志 is_inspect = (obj is inspect)——即“检查 inspect 自己”；
#   3) 在函数体内导入 rich._inspect.Inspect，用各开关构造渲染对象
#      （is_inspect 为真时强制打开 help/methods/docs，好让用户看到完整形态）；
#   4) 交给 console.print 渲染。
# 【参数】obj：被检查的对象；console：输出目标，None 表示全局 Console；title：结果上方的标题；
#   help：显示完整帮助文本而非首段；methods：检查可调用对象；docs：渲染 docstring；
#   private：显示单下划线私有属性；dunder：显示双下划线属性；sort：按字母序排序
#   （可调用对象置顶，比较时忽略首尾下划线）；all：显示全部属性；value：美化打印属性值。
# 【返回】None。
# 【注意】1) 导入 Inspect 放在函数体内，避免包导入期拉入 _inspect/pretty/panel 等重模块；
#   2) 局部变量 `_console` 带下划线，以免与模块级全局单例同名而误读；
#   3) 只有 obj is inspect 时才走“自我检查”特例，其它对象的开关完全按参数生效。
def inspect(
    obj: Any,
    *,
    console: Optional["Console"] = None,
    title: Optional[str] = None,
    help: bool = False,
    methods: bool = False,
    docs: bool = True,
    private: bool = False,
    dunder: bool = False,
    sort: bool = True,
    all: bool = False,
    value: bool = True,
) -> None:
    """Inspect any Python object.

    * inspect(<OBJECT>) to see summarized info.
    * inspect(<OBJECT>, methods=True) to see methods.
    * inspect(<OBJECT>, help=True) to see full (non-abbreviated) help.
    * inspect(<OBJECT>, private=True) to see private attributes (single underscore).
    * inspect(<OBJECT>, dunder=True) to see attributes beginning with double underscore.
    * inspect(<OBJECT>, all=True) to see all attributes.

    Args:
        obj (Any): An object to inspect.
        title (str, optional): Title to display over inspect result, or None use type. Defaults to None.
        help (bool, optional): Show full help text rather than just first paragraph. Defaults to False.
        methods (bool, optional): Enable inspection of callables. Defaults to False.
        docs (bool, optional): Also render doc strings. Defaults to True.
        private (bool, optional): Show private attributes (beginning with underscore). Defaults to False.
        dunder (bool, optional): Show attributes starting with double underscore. Defaults to False.
        sort (bool, optional):  Sort attributes alphabetically, callables at the top, leading and trailing underscores ignored. Defaults to True.
        all (bool, optional): Show all attributes. Defaults to False.
        value (bool, optional): Pretty print value. Defaults to True.
    """
    # 【要点】局部变量 _console 遮蔽了模块级全局单例，避免阅读时误以为在读写全局状态。
    _console = console or get_console()
    # 【为什么】延迟到真正调用时才导入：inspect 是调试用函数，不该让 `import rich` 变重。
    from rich._inspect import Inspect

    # Special case for inspect(inspect)
    # 【易错】比较的是“函数对象本身”（obj 就是本模块的 inspect 函数），不是名字或字符串；
    #   命中后下面会强制打开 help/methods/docs，好让用户看到最完整的信息。
    is_inspect = obj is inspect

    _inspect = Inspect(
        obj,
        title=title,
        # 【要点】is_inspect 为真时把这些开关一律置为 True（`or` 短路，用户参数不再生效）。
        help=is_inspect or help,
        methods=is_inspect or methods,
        docs=is_inspect or docs,
        private=private,
        dunder=dunder,
        sort=sort,
        all=all,
        value=value,
    )
    # 【协作】Inspect 是 renderable，最终由 Console 渲染成属性面板（rich/_inspect.py）。
    _console.print(_inspect)


# ==== 【作为脚本运行时的自测入口】 ====
# 【要点】`python -m rich` 会执行这里：用本模块定义的 print 打印一句带标记的问候语，
#   同时也是“全局 Console 已就绪”的最小演示。pragma: no cover 表示不计入测试覆盖率。
if __name__ == "__main__":  # pragma: no cover
    print("Hello, **World**")
