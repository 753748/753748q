from typing import IO, Dict, List, Mapping, Optional

# 【附：本模块与 default_styles.py 的关系】
#   default_styles.py 里那张大字典（DEFAULT_STYLES）是 Rich 内置的「设计系统」：
#   它给 inspect/repr/json/markdown/progress/traceback 等所有内置渲染器定义了默认外观。
#   Theme(inherit=True) 以它的浅拷贝为基底，因此任何自定义主题都自动获得全部默认样式，
#   只需覆盖想改的那几个名字。这也解释了 Console 为什么在没有任何主题时也能工作：
#   themes.DEFAULT = Theme(DEFAULT_STYLES) 这个模块级实例就是兜底主题。
from .default_styles import DEFAULT_STYLES
# ---------------------------------------------------------------------------
# 【模块导读：一次样式查找的完整路径】
#   当用户写 console.print("[warning]磁盘即将写满[/warning]") 时，样式是如何落到终端的？
#     1) markup 把标记解析成样式名 "warning"（而不是直接解析成 Style）；
#     2) Console.get_style("warning") 先问 ThemeStack.get，也就是栈顶字典的 get；
#     3) 命中则拿到主题里注册好的 Style 对象（已是解析结果，无额外开销）；
#     4) 未命中则退化为 Style.parse("warning")——此时 "warning" 会被当成颜色名解析，
#        失败就抛 errors.MissingStyle（这正是「主题名拼错」的典型报错来源）；
#     5) 取到的样式与文本片段一起构成 Segment，最终由 Style.render 变成 ANSI 字节。
#   理解这条链路后就能明白本模块的定位：它只是第 2、3 步的「名字 → 样式」查表，
#   既不参与渲染，也不做任何宽度/布局计算。
# 【模块导读：主题栈为什么需要合并】
#   嵌套覆盖场景（例如 Live 里临时把 "info" 改成灰色）要求「内层看不到的样式仍能
#   从外层继承」。若查找时逐层回溯，代码会变复杂且每次查找都要走多级字典；
#   本实现选择在 push 时一次性合并成新字典，用一次字典拷贝换取每次查找只查一层。
# ---------------------------------------------------------------------------
# 【附：常见误用与排查】
#   · 主题名拼错不会在构造时报错，只会在 Console.get_style 解析失败时抛 MissingStyle；
#   · 注册了与颜色同名的样式（如 "red"）会覆盖内置颜色的解析结果；
#   · push_theme 必须与 pop_theme 成对（放 try/finally），否则主题会一直生效；
#   · pop_theme 弹出栈底会抛 ThemeStackError，栈底主题是不允许弹出的；
#   · Theme.read 的 encoding 默认跟随系统，中文 Windows 上建议显式传 utf-8；
#   · 直接修改 Theme.styles 只影响尚未并入栈的层，已 push 的合并层不受影响。
# ---------------------------------------------------------------------------
# 【附：主题与 Console 的接口】
#   Console 暴露了三个与主题相关的方法，都是对 ThemeStack 的薄封装：
#     · Console.get_style(name, default=...)  —— 按名字取样式（主题优先，其次解析）；
#     · Console.push_theme(theme, inherit=True) —— 压入一层临时主题；
#     · Console.pop_theme()                  —— 弹出栈顶主题。
#   此外 Console 还允许通过 console.get_theme() 直接拿到当前 Theme 对象，
#   或通过 console.get_theme().styles[name] = style 就地打补丁。
#   注意 get_style 对带链接的样式会返回 copy()（保证 link_id 唯一），
#   因此「从主题里取出来的样式」与「主题里存的那个对象」可能不是同一个实例。
# 【附：主题名与样式定义的优先级】
#   查找顺序是「先主题、后解析」。这意味着如果你给主题注册了一个叫 "red" 的样式，
#   它就会覆盖掉内置的同名颜色解析结果——注册名与颜色名撞车时要特别小心。
from .style import Style, StyleType
# ---------------------------------------------------------------------------
# 【模块定位】本模块实现 Rich 的「主题」机制：把用户可读的样式名（如 "warning"、
#   "table.header"）映射到 Style 对象，并提供在运行期临时覆盖样式的主题栈。
#   它是「样式定义」与「样式使用」之间的唯一间接层：
#   Console.get_style(name) 先查 ThemeStack.get(name)，查不到才退化为 Style.parse(name)，
#   因此主题只是「可选的一层名字到样式的映射」，而不是强制的样式来源。
# 【关键数据结构】
#   · Theme：一个 Dict[str, Style] 的包装，附带从 INI 文件读取与反向导出配置的能力；
#   · ThemeStack：主题栈，栈顶永远是一个「已合并好的」样式字典，get 属性指向它的 get；
#   · ThemeStackError：试图弹出栈底主题时抛出。
# 【设计要点】
#   · Theme 保存的是 Style 对象（构造时就把字符串解析掉），因此主题查找是纯粹的字典查询，
#     没有解析开销——这正是风格化渲染能够足够快的原因。
#   · ThemeStack 每次 push 都把「当前栈顶字典 + 新主题字典」合并成新字典，
#     而不是在查找时逐层回溯：以写换读，查找退化为一次 dict.get。
#   · self.get = self._entries[-1].get 把字典的 get 方法直接挂到实例上，
#     让 Console 的调用点写成 self._theme_stack.get(name)，省一次属性链查找；
#     代价是每次 push/pop 都必须重新绑定（见 push_theme / pop_theme 末尾）。
#   · 主题配置文件采用 configparser 的 INI 格式、固定 [styles] 段，
#     因此可以直接复用标准库，用户也能用现成的编辑器维护主题文件。
# 【阅读建议】先读 Theme.__init__ 与 styles 的构成（继承 DEFAULT_STYLES 的含义），
#   再读 config / from_file / read 这组「主题 ↔ 文件」的转换，
#   最后读 ThemeStack 的 push/pop，留意 self.get 的重新绑定与栈底保护。
#   依赖：default_styles（默认样式表）、style（Style 与 StyleType）。
#   被依赖：console（持有 ThemeStack 并暴露 push_theme / pop_theme）、
#   themes（构造默认主题 DEFAULT）、traceback 与 syntax（按名字取样式）。
# ---------------------------------------------------------------------------


# ==== 【Theme：名字到样式的映射表】 ====
# ---------------------------------------------------------------------------
# 【职责】持有一份「样式名 → Style 对象」的字典，并提供读写配置文件的往返能力。
# 【关键属性】styles：公开可变的字典（Console 允许直接改它来打补丁）。
# 【不变量】1) 键是不含空格的短名字；2) 值一律是 Style 实例（构造时就完成解析）；
#   3) inherit=True 时以 DEFAULT_STYLES 的副本为底，因此自定义主题是「增量覆盖」而非全量替换。
# 【协作】Console 构造时用 ThemeStack(主题) 包装它；themes.DEFAULT 是内置主题实例。
# ---------------------------------------------------------------------------
# 【类注释·协作】Theme 与 Style 的分工：Style 描述「长什么样」，Theme 描述「叫什么名字」。
#   因此同一个 Style 对象可以在多个主题里注册成不同名字，
#   也可以在同一主题里注册成多个名字（别名），互不影响。
# 【类注释·易错】主题只影响通过名字引用的样式；直接写在代码里的样式定义串
#   （如 Text("x", style="bold red")）不会经过主题查表。
# ---------------------------------------------------------------------------
# 【类注释·典型用法】
#   theme = Theme({"warning": "bold yellow", "error": "bold red"})
#   console = Console(theme=theme)
#   console.print("[warning]磁盘空间不足[/warning]")
#   注意右值既可以是样式定义串（延迟到构造时解析一次），也可以是已有的 Style 对象——
#   传对象可以避免重复解析，适合在程序里动态生成样式表。
# ---------------------------------------------------------------------------
# 【本模块在 Rich 架构中的位置】
#
#   用户输入                主题系统                    渲染输出
#   --------                --------                    --------
#   "[warning]…[/warning]" → 样式名 "warning"
#                              │
#                              ├─ Console.get_style(name)
#                              │     ├─ ThemeStack.get(name)  ← 本模块
#                              │     │     └─ 命中：返回注册好的 Style
#                              │     └─ 未命中：Style.parse(name)
#                              ↓
#                          Style 对象
#                              │
#                          Segment(text, style)
#                              │
#                          Style.render() → ANSI 字节 → 终端
#
#   所以本模块的价值在于「解耦」：组件代码里写的是语义化的名字
#   （table.header、logging.level.error、markdown.h1……），
#   而具体长什么样由主题决定。这带来两个直接好处：
#     1) 同一个组件在不同主题下有不同外观，无需修改组件代码；
#     2) 用户可以整体替换配色方案（例如适配深色/浅色终端）。
#
# 【本模块不做的事情】
#   · 不解析颜色（那是 color.py 的职责）；
#   · 不做样式合成（那是 Style.__add__ 的职责）；
#   · 不感知终端能力（那是 Console 与 Style.render 的职责）。
# ---------------------------------------------------------------------------
# 【类注释·设计取舍】把主题做成「一个字典 + 两个转换函数」而不是一套继承体系，
#   是为了让主题可序列化、可比较、可增量覆盖。若采用类继承（ThemeBase → MyTheme），
#   增量覆盖与运行期替换都会变得笨重，而 Rich 的使用场景恰恰强调「临时改一下」。
# 【类注释·边界】Theme 不负责校验样式名是否被任何组件使用：
#   拼错的名字不会在构造时报错，只会在真正用到时（Console.get_style 解析失败）
#   才以 MissingStyle 的形式暴露出来。

class Theme:
    """A container for style information, used by :class:`~rich.console.Console`.

    Args:
        styles (Dict[str, Style], optional): A mapping of style names on to styles. Defaults to None for a theme with no styles.
        inherit (bool, optional): Inherit default styles. Defaults to True.
    """
    # 【要点】styles 是类级注解（只声明类型），真正的实例字典在 __init__ 里创建；
    #   公开暴露而不是私有字段，是为了让高级用户可以 console.get_theme().styles[...] = ...
    #   直接打补丁——这是上游有意保留的扩展点。
    styles: Dict[str, Style]

    # 【功能】构造主题。
    # 【算法】1) 先按 inherit 决定基底：继承时用 DEFAULT_STYLES.copy()（浅拷贝：
    #      Style 对象本身不可变，共享安全），否则用空字典；
    #   2) 再把传入的 styles 逐个规范化后 update 进基底：值已经是 Style 就原样使用，
    #      否则当作样式定义串调用 Style.parse 解析。
    # 【参数】styles：名字到 Style 或样式定义串的映射；inherit：是否继承内置默认样式。
    # 【注意】1) 传进来的 Mapping 不会被修改（新建了字典并用 update 合并）；
    #   2) 同名键会覆盖默认样式——这是「主题定制」的主要手段；
    #   3) inherit=False 时主题里只有显式给出的样式，Console 找不到的名字会退化到
    #      Style.parse，因此拼写错误的名字不会报错而是被当成样式定义解析（可能抛语法错误）。
    def __init__(
        # 【参数】styles：可选的初始样式映射（值可以是定义串）；
        #   inherit：是否以 DEFAULT_STYLES 为基底，默认 True。
        self, styles: Optional[Mapping[str, StyleType]] = None, inherit: bool = True
    ):
        # 【为什么】用浅拷贝而不是深拷贝：Style 是不可变对象，浅拷贝已足以隔离「键的增删改」，
        #   同时省掉几十个 Style 的深拷贝开销。
        # 【为什么不做深拷贝】DEFAULT_STYLES 的值全部是 Style 实例，而 Style 逻辑上不可变
        #   （没有 __set__ 的位描述符、所有「修改」都返回新对象），因此共享这些 Style
        #   是安全的，也省去了几十次对象拷贝。任何试图往主题里塞「可变样式」的做法
        #   都会破坏这个前提，务必避免。
        self.styles = DEFAULT_STYLES.copy() if inherit else {}
        if styles is not None:
            self.styles.update(
                {
                    name: style if isinstance(style, Style) else Style.parse(style)
                    for name, style in styles.items()
                }
            )

    # ==== 【主题 ↔ 配置文件】 ====
    # 【功能】把当前主题序列化成 configparser 能读回来的配置文本。
    # 【算法】以固定的 "[styles]\n" 段落头开头，然后按名字排序输出 `名字 = 样式` 行。
    # 【返回】str：完整的 INI 文本。
    # 【注意】1) 排序是为了输出稳定（便于 diff 与快照测试）；
    #   2) 样式的右值来自 str(Style)，也就是 Style.__str__ 生成的规范定义串，
    #      因此 config → from_file → config 是一个稳定的往返；
    #   3) 空主题会得到只有段落头的文本，configparser 读取时不会有任何 styles 项。
    @property
    # 【易错】config 的输出格式是对外契约：Theme.from_file 必须能读回它。
    #   注意样式右值里可能带空格（"bold red on blue"），configparser 允许值含空格，
    #   因此这里不需要加引号——但也不要引入会破坏 INI 语义的字符（如换行）。
    # 【示例】config 的输出形如：
    #   [styles]
    #   bar.back = grey23
    #   bar.complete = rgb(249,38,114)
    #   bold = bold
    #   因此 theme.config 的结果可以直接写进 ~/.rich.theme 之类的文件，
    #   再用 Theme.read() 读回来——这就是「主题即配置」的设计意图。
    # 【实现细节：为什么用 sorted】
    #   dict 的迭代顺序是插入顺序，而插入顺序取决于 DEFAULT_STYLES 的定义顺序与用户
    #   覆盖的顺序。若不排序，同样的主题在不同构造路径下会输出不同的文本，
    #   这会给「导出主题 → 版本控制 → diff」带来大量噪音，也会让快照测试不稳定。
    #   排序的代价可以忽略（主题通常只有几十到几百个键）。
    # 【实现细节：为什么用 join 而不是多次 +=】
    #   字符串在 Python 中不可变，循环里反复拼接会产生 O(n²) 的临时对象；
    #   先用 join 把生成器展开成一次拼接是惯用的高效写法。
    # 【示例：往返验证】
    #   theme = Theme({"my.style": "bold red on blue"})
    #   text = theme.config          # "[styles]\n…\nmy.style = bold red on blue"
    #   again = Theme.from_file(io.StringIO(text))
    #   again.styles["my.style"] == theme.styles["my.style"]   # True
    #   这个往返性质是主题文件功能可用的前提，也是测试里最常断言的不变量之一。
    def config(self) -> str:
        """Get contents of a config file for this theme."""
        # 【要点】列表推导里 f"{name} = {style}" 会隐式调用 str(style)，
        #   这依赖 Style.__str__ 的输出可被 Style.parse 解析（往返性质）。
        config = "[styles]\n" + "\n".join(
            f"{name} = {style}" for name, style in sorted(self.styles.items())
        )
        return config

    # 【功能】从一个已打开的文本文件读取主题。
    # 【算法】用 configparser 读入，取 [styles] 段的全部键值对，逐个 Style.parse 后构造 Theme。
    # 【参数】config_file：以文本模式打开的文件对象；
    #   source：可选的文件名，仅用于 configparser 的报错信息（不含行号时便于定位）；
    #   inherit：是否继承内置默认样式（透传给 Theme 构造函数）。
    # 【返回】Theme：新实例。
    # 【易错】1) configparser 的键名会被自动转成小写（DEFAULT_INTERPOLATION 之前的行为），
    #      因此 "table.Header" 与 "table.header" 视为同一个键；
    #   2) 段落名必须是 styles，其它段落会被忽略；
    #   3) 这里在函数内部 import configparser，属于「按需导入」的写法，
    #      好处是没用到主题文件的程序不必付出导入成本（实测该模块导入开销很小，
    #      更多是历史/风格原因）。
    @classmethod
    # 【参数】source 只影响报错信息：configparser 解析失败时会用 source 拼出
    #   "文件名: 行号" 形式的错误，因此从文件读入时应当传 path（read 方法正是这么做的）。
    # 【易错】本方法要求 [styles] 段必须存在；配置文件缺少该段时 config.items("styles")
    #   会抛 configparser.NoSectionError（而不是返回空主题）。
# ==== 【ThemeStack：支持临时覆盖的主题栈】 ====
    # 【实现细节：为什么把 import configparser 放在函数内部】
    #   严格来说 configparser 的导入开销不大，放外面也完全可以。放在函数内部的效果是：
    #     1) 不用主题文件的应用（绝大多数）不会为它付出导入时间与内存；
    #     2) 阅读时一眼就能看出「这个模块只在读文件时才需要 configparser」。
    #   这是 Rich 代码库中常见的「按需导入」风格（color.py 的 __rich__ 也是同理，
    #   那里更多是为了打破循环导入）。
    # 【边界情况】
    #   · 文件里没有 [styles] 段 → configparser.NoSectionError；
    #   · 某一行缺少 "="       → configparser.ParsingError（带 source 与行号）；
    #   · 样式值无法解析         → errors.StyleSyntaxError（来自 Style.parse）；
    #   · 值为空串               → Style.parse("") 返回 NULL_STYLE，即该名字被显式清空。
    # 【易错】source 参数只用于报错定位，不参与任何解析逻辑；
    #   但把它传成错误的路径会让报错信息指向无关的文件，排查时容易误入歧途。
    # 【协作】Console 没有内置的「从文件加载主题」开关，
    #   需要时由用户显式 Theme.read(...) 后传给 Console(theme=...)。
    def from_file(
        cls, config_file: IO[str], source: Optional[str] = None, inherit: bool = True
    ) -> "Theme":
        """Load a theme from a text mode file.

        Args:
            config_file (IO[str]): An open conf file.
            source (str, optional): The filename of the open file. Defaults to None.
            inherit (bool, optional): Inherit default styles. Defaults to True.

        Returns:
            Theme: A New theme instance.
        """
        import configparser

        config = configparser.ConfigParser()
        config.read_file(config_file, source=source)
        # 【易错】configparser 默认会把键名转成小写，因此 "table.Header" 与 "table.header"
        #   会被视为同一个键；若主题需要大小写敏感的名字，这种 INI 格式就不适用了
        #   （可以改用 Theme(styles={...}) 直接构造）。
        styles = {name: Style.parse(value) for name, value in config.items("styles")}
        theme = Theme(styles, inherit=inherit)
        return theme

    # 【功能】按路径读取主题文件（from_file 的便捷封装）。
    # 【算法】用 open(path, encoding=encoding) 打开后交给 from_file。
    # 【参数】path：配置文件路径；inherit：是否继承默认样式；
    #   encoding：文本编码，None 表示用平台默认编码（locale 相关）。
    # 【返回】Theme：新实例。
    # 【注意】1) 本方法用 with 管理文件句柄，因此不会泄漏 fd；
    #   2) encoding=None 在中文 Windows 上可能是 GBK，包含非 ASCII 样式名的主题文件
    #      应显式传 encoding="utf-8"；
    #   3) 文件不存在时直接抛 FileNotFoundError，不做兜底。
    @classmethod
    # 【实现细节：为什么 read 只是一个薄封装】
    #   把「打开文件」与「解析内容」拆成两个方法，是为了支持调用方传入任意文件对象：
    #   例如 io.StringIO（测试时直接给字符串）、网络流、甚至是标准输入。
    #   这种「接受文件对象而不是路径」的接口设计让单元测试变得非常容易——
    #   tests 里大量出现的 StringIO 用法正得益于此。
    # 【兼容】本方法用 open(path, encoding=...) 而不是 pathlib.Path.read_text，
    #   原因是需要把「已打开的文件对象」交给 from_file（configparser 需要文件对象），
    #   同时也保证文件在 with 块结束时被关闭。
    def read(
        cls, path: str, inherit: bool = True, encoding: Optional[str] = None
    ) -> "Theme":
        """Read a theme from a path.

        Args:
            path (str): Path to a config file readable by Python configparser module.
            inherit (bool, optional): Inherit default styles. Defaults to True.
            encoding (str, optional): Encoding of the config file. Defaults to None.

        Returns:
            Theme: A new theme instance.
        """
        # 【易错】encoding=None 时使用 locale.getpreferredencoding()，在中文 Windows 上
        #   通常是 GBK；主题文件含中文（注释或非 ASCII 样式名）时应显式传 utf-8。
        with open(path, encoding=encoding) as config_file:
            return cls.from_file(config_file, source=path, inherit=inherit)

# ==== 【主题栈的异常类型】 ====
# 【功能】表示主题栈操作非法（目前只有「弹出栈底主题」这一种情况）。
# 【注意】它继承 Exception 而不是 rich.errors.ConsoleError，与 ThemeStackError 在
#   errors.py 中的 StyleStackError 是两个不同的异常，不要混淆。

# 【易错】不要把它与 rich.errors.StyleStackError 混为一谈：前者是本模块主题栈的异常，
#   后者是 errors.py 中「样式栈非法」的异常，两者没有继承关系。
# 【为什么需要专门的异常】
#   弹出栈底主题通常意味着 push/pop 不成对（常见于异常路径漏掉了 finally 中的 pop）。
#   若只抛 IndexError，用户很难判断是自己 pop 多了还是主题栈本身被破坏；
#   用专门的异常类型可以让调用方精确捕获并给出更有用的提示。
# 【注意】本异常没有任何附加属性（不携带栈深度等信息），上游保持最小实现。
# 【协作】Console.pop_theme 会把它原样抛出；调用方若不希望因 pop 过多而中断程序，
#   应在 finally 中先判断栈深度，或捕获本异常。
class ThemeStackError(Exception):
    """Base exception for errors related to the theme stack."""


# ==== 【ThemeStack：支持临时覆盖的主题栈】 ====
# ---------------------------------------------------------------------------
# 【职责】维护一串「已合并好的样式字典」，栈顶即当前生效的完整样式表；
#   为 Console 提供 get(name) 查找入口与 push/pop 的临时覆盖能力。
# 【关键属性】_entries：字典列表；get：当前栈顶字典的 get 方法（实例属性，每次变更重绑）。
# 【不变量】1) _entries 永远非空（栈底是构造时传入的主题，不允许弹出）；
#   2) 每个条目都是完整字典（继承时已与下层合并），因此查找只需看栈顶；
#   3) self.get 必须始终指向 _entries[-1].get——push/pop 后若忘记重绑，
#      查找会继续命中旧字典，表现为「覆盖样式不生效」。
# 【协作】Console._theme_stack 持有它；Console.push_theme / pop_theme 是它的门面，
#   常用于「临时把某个样式名改掉再恢复」的场景（如 Live 渲染、Syntax 主题切换）。
# ---------------------------------------------------------------------------
# 【类注释·性能】self.get 的绑定是「轻微越界优化」的典型例子：
#   它把「对象 → 列表 → 字典 → 方法」四级查找压缩成「对象 → 属性」两级，
#   代价是可读性下降与「忘记重绑」的风险。阅读时把它当作缓存看待即可。
# 【类注释·生命周期】ThemeStack 不持有 Console，因此它不知道渲染进度；
#   它只负责「当前生效的样式表是什么」，渲染状态由 Console 自己维护。
# ---------------------------------------------------------------------------
# 【示例：主题栈的生命周期】
#   stack = ThemeStack(Theme({"info": "blue"}))          # 栈底
#   stack.get("info")            -> Style(color=blue)      # 命中主题
#   stack.push_theme(Theme({"info": "dim blue"}))          # 合并入栈（inherit=True）
#   stack.get("info")            -> dim blue               # 新层覆盖旧层
#   stack.push_theme(Theme({"warn": "yellow"}))
#   stack.get("bold")            -> 仍可用（继承来的默认样式）
#   stack.pop_theme(); stack.pop_theme()                  # 回到栈底
#   stack.pop_theme()            -> 抛 ThemeStackError（不能弹出栈底）
# 【类注释·替代方案】若不使用主题栈，也可以让每个组件自己持有样式，
#   但那样「同一份内容在不同上下文里有不同配色」就无法实现——
#   例如 Table 在 Live 内部与独立显示时可能希望使用不同的边框颜色。
#   主题栈把这类临时覆盖变成了 push/pop 一对调用。
# 【类注释·只读视图】注意 ThemeStack 没有提供「遍历当前所有样式」的接口；
#   需要完整样式表时应从 Console.get_theme().styles 获取（它只反映栈底主题）。
# 【与 errors.StyleStackError 的区别（补充）】
#   StyleStack（style.py）管理的是「样式合成过程中的当前样式」，
#   ThemeStack（本模块）管理的是「名字到样式的映射表」。
#   前者服务于 markup 的嵌套标签，后者服务于主题切换；两者互不依赖。
class ThemeStack:
    """A stack of themes.

    Args:
        theme (Theme): A theme instance
    """

    # 【功能】用初始主题建立栈。
    # 【算法】把 theme.styles 作为唯一（栈底）条目；注意这里是引用而非拷贝，
    #   因此外部随后修改 theme.styles 会直接影响栈底内容。
    # 【参数】theme：栈底主题。
    # 【注意】self.get 是「实例属性」而不是方法：赋值语句把 dict.get 绑定到实例字典上，
    #   这样 self.get(name) 只做一次字典查找，比 self._entries[-1].get(name) 略快，
    #   这在每个 Segment 都要按名字解析样式时是可观的优化。
    def __init__(self, theme: Theme) -> None:
        # 【要点】_entries 只保存字典，不保存 Theme 对象：主题一旦入栈，
        #   后续对原 Theme.styles 的修改只会影响栈底那一层（它保存的是引用），
        #   而 push 出来的合并层是全新字典，不受影响。
        # 【不变量详解：为什么每层都必须是「完整字典」】
        #   设想查找走逐层回溯的方案：get(name) 需要从栈顶往下逐层 dict.get，
        #   直到命中或走到栈底。这要求每次查找做 O(层数) 次字典查询，
        #   而层数在使用 Console 的嵌套上下文时可能不小（Live、Progress、Table 都可能压层）。
        #   当前实现改为：push 时把下层与上层合并成新字典，查找永远只查一次。
        #   代价是每次 push 要做一次字典拷贝（O(样式数量)），但 push/pop 的次数远少于查找次数，
        #   这笔交易整体是赚的。
        # 【易错】正因为每层是完整字典，pop 之后无需做任何「撤销合并」的操作——
        #   直接丢弃栈顶即可，因为下层字典从未被修改过。这也是本设计的一个隐性收益。
        self._entries: List[Dict[str, Style]] = [theme.styles]
        self.get = self._entries[-1].get

    # 【功能】压入一个主题，形成临时覆盖层。
    # 【算法】inherit 为真时把「当前栈顶字典」与「新主题字典」合并（新主题优先）后入栈；
    #   为假时直接用新主题字典的一份拷贝入栈（完全替换）。最后重绑 self.get。
    # 【参数】theme：新主题；inherit：是否继承当前生效的样式。
    # 【注意】1) 合并用的是 {**current, **theme.styles}，因此新主题里没提到的名字仍然可用；
    #   2) inherit=False 时新层看不到任何下层样式，包括 DEFAULT_STYLES——
    #      Console.push_theme 的默认行为是 inherit=True，避免意外丢失基础样式；
    #   3) 入栈的是新字典，所以之后修改原 theme.styles 不会影响该层。
    # 【注意】push_theme 的 inherit 参数与 Theme 构造函数的 inherit 语义不同：
    #   前者表示「是否继承当前栈顶的样式」，后者表示「是否继承 DEFAULT_STYLES」，
    #   两者很容易混淆，阅读调用点时务必看清。
    # 【实现细节】本方法不修改传入的 Theme 对象，也不修改栈内已有字典，
    #   而是生成一个全新的合并字典入栈，因此 push 是「无副作用」的。
    def push_theme(self, theme: Theme, inherit: bool = True) -> None:
        """Push a theme on the top of the stack.

        Args:
            theme (Theme): A Theme instance.
            inherit (boolean, optional): Inherit styles from current top of stack.
        """
        # 【类型说明】styles 的局部注解写在赋值之前是为了让 mypy 知道合并表达式的类型；
        #   运行时它没有任何作用（Python 的局部变量注解不求值）。
        styles: Dict[str, Style]
        styles = (
            {**self._entries[-1], **theme.styles} if inherit else theme.styles.copy()
        )
        # 【易错】这一行之后的 self.get 重绑不可省略。若忘记，ThemeStack.get 仍指向
        #   上一层的字典，表现为「push_theme 明明成功了，但新样式查不到」——
        #   这是本类最容易引入、也最难一眼看出的 bug。
        self._entries.append(styles)
        self.get = self._entries[-1].get

    # 【功能】弹出栈顶主题，回到上一层。
    # 【算法】先判栈深：只剩栈底一层时抛 ThemeStackError；否则弹出并重绑 self.get。
    # 【注意】1) 用 len(self._entries) == 1 判断而不是捕获 IndexError，
    #      是为了给出明确的错误信息；
    #   2) 本方法与 Console.pop_theme 成对使用，通常放在 try/finally 里以保证恢复；
    #   3) 弹出后 self.get 必须重绑（见 step 2），否则查找仍指向被丢弃的字典。
    # 【协作】Console.pop_theme 是它的公开门面，通常与 push_theme 成对出现在
    #   try/finally 中，以保证异常路径下也能恢复原来的主题。
    # 【易错】pop_theme 不做任何返回值处理，也不触发重绘；
    #   若在 Console 正在渲染（例如 Live 内部）时弹出主题，
    #   已经生成的 Segment 不会改变外观，只有后续渲染才使用新主题。
    def pop_theme(self) -> None:
        """Pop (and discard) the top-most theme."""
        # 【要点】用显式长度判断给出友好报错，而不是让 self._entries.pop() 在空列表上失败：
        #   「不能弹出栈底主题」是一个明确的用户错误，值得单独的错误类型。
        if len(self._entries) == 1:
            raise ThemeStackError("Unable to pop base theme")
        # 【要点】直接丢栈顶字典即可，不需要做任何回滚：下层字典在整个过程中从未被改动过
        #   （push 时生成的是新字典）。这一点让 pop 的语义非常简单。
        self._entries.pop()
        self.get = self._entries[-1].get


# 【历史】文件末尾的 __main__ 块是上游的演示入口（pragma: no cover）：
#   创建一个默认主题并打印它的配置文本，用来人工检查默认样式表能否正确序列化。
#   以 `python -m rich.theme` 可以运行；它不参与库的运行时行为。
# 【说明】注意 if __name__ 的缩进是 0：它是一个模块级演示块，
#   与 Theme / ThemeStack 的定义同级。以 `python -m rich.theme` 运行即可看到
#   默认主题被序列化后的完整配置文本（内容就是 default_styles.py 里那张表）。
# 【历史】本演示块最早用于验证「默认样式表能否无损序列化」，
#   后来成为人工检查主题输出的便捷入口；它写完后没有任何副作用。
if __name__ == "__main__":  # pragma: no cover
    theme = Theme()
    print(theme.config)
