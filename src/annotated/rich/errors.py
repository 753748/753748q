# ---------------------------------------------------------------------------
# 【模块定位】Rich 的异常类型清单（属于公共 API 的一部分）：把“渲染/使用方式出错”的原因
#   编码成不同异常类，使调用方既能按大类捕获（ConsoleError / StyleError），也能精确捕获某一原因。
#   这些类都是零行为的“标签型”异常（只有 docstring），不引入任何依赖，因此可以被任意子模块导入。
# 【关键数据结构】两棵互不相交的继承树：
#     ConsoleError → StyleSyntaxError / StyleStackError / NotRenderableError /
#                    MarkupError / LiveError / NoAltScreen
#     StyleError   → MissingStyle
#   注意 MissingStyle 继承的是 StyleError 而**不是** ConsoleError，这是一处易被忽略的差异：
#   写 `except errors.ConsoleError` 捕不到 MissingStyle。
# 【设计要点】
#   - 用“类别基类 + 具体子类”而不是“单一异常 + 错误码”，让调用点能写出自解释的
#     `except errors.MissingStyle:`，也让日志里的异常类型自带语义。
#   - 一律不定义 __init__、不附加字段：异常消息由 raise 处传入，异常类型与消息彻底解耦。
#   - 只在“用户输入/调用契约被破坏”时抛出；正常渲染流程不靠异常控制流程
#     （唯一的例外是 console.get_style 把 StyleSyntaxError 转成 MissingStyle，见下）。
# 【阅读建议】先通读类名与继承关系，再用 grep 搜索 `raise errors.` 找出每个异常的真实抛出点
#   （NotRenderableError 来自 console.py/measure.py/table.py，MarkupError 来自 markup.py，
#   StyleSyntaxError 来自 style.py，NoAltScreen 来自 console.update_screen*）。
# ---------------------------------------------------------------------------


# 【职责】最通用的“控制台操作错误”基类：所有与 Console 使用方式相关的异常都继承它，
#   调用方可以只写 `except errors.ConsoleError` 兜住绝大多数 Rich 异常。
# 【不变量】库内没有任何地方直接 raise 这个基类，它只作为类别存在，抛出的永远是具体子类。
# 【协作】子类见本模块下方：StyleSyntaxError / StyleStackError / NotRenderableError /
#   MarkupError / LiveError / NoAltScreen。注意 MissingStyle 属于另一棵树（StyleError）。
class ConsoleError(Exception):
    """An error in console operation."""


# 【职责】样式相关错误的基类，与 ConsoleError 平行、互不继承。
# 【不变量】当前只有 MissingStyle 继承它（StyleSyntaxError 虽同属“样式问题”，却挂在 ConsoleError 下）。
# 【协作】这样划分的目的是让“只想处理样式名查不到”的调用方可以 `except StyleError`，
#   而不会顺手把渲染类错误一起吞掉。
class StyleError(Exception):
    """An error in styles."""


# 【职责】表示一段样式定义（如 "bold red on blue"、"link https://..."）语法不合法。
# 【协作】由 style.py 的 Style.parse 在解析失败处抛出（例如 "color expected after 'on'"）；
#   console.get_style 会捕获它并转换成 MissingStyle 再抛给上层（见 console.py 1492-1497），
#   因此直接用 Console 的调用方通常看到的是 MissingStyle，只有直接调用 Style.parse 才看到本异常；
#   style.py:401 附近也捕获它用于“把非法颜色当作无颜色处理”的兼容分支。
class StyleSyntaxError(ConsoleError):
    """Style was badly formatted."""


# 【职责】表示请求的样式名既不在主题栈里，也无法解析成样式定义（“没有这个样式”）。
# 【协作】console.get_style 在 theme_stack 查不到且 Style.parse 失败时抛出；
#   它继承 StyleError（而不是 ConsoleError），所以 `except errors.ConsoleError` 捕不到它。
class MissingStyle(StyleError):
    """No such style."""


# 【职责】表示 StyleStack（样式入栈/出栈栈）状态非法，例如弹出次数多于压入次数。
# 【注意】在当前 rich 版本里全库已没有任何 raise 点（只剩本定义与 console.set_live 的
#   一处文档字符串提及），属于保留给下游代码与历史版本的公共异常类型；
#   阅读源码时不要误以为存在活跃的触发路径。
class StyleStackError(ConsoleError):
    """Style stack is invalid."""


# 【职责】表示对象既不是 str，也没有 __rich__ / __rich_console__，因此无法被渲染。
# 【协作】measure.py（Measurement.get）与 table.py 在“加进来的东西不可渲染”时抛出；
#   console.py 在 render/render_str 遇到不可渲染对象时同样抛它（见 console.py 1325/1333）。
class NotRenderableError(ConsoleError):
    """Object is not renderable."""


# 【职责】表示 Rich 标记（BBCode 风格的方括号标签）格式错误，例如标签未闭合、属性为空。
# 【协作】markup.py 的 render 在解析标记的过程中抛出（见 markup.py 167-196 的四处 raise）；
#   它与 StyleSyntaxError 的区别是：前者是“标记文本”的问题，后者是“样式定义串”的问题。
class MarkupError(ConsoleError):
    """Markup was badly formatted."""


# 【职责】表示 Live 实时显示相关的非法用法（例如在一个 Console 上重复进入 Live 上下文）。
# 【注意】与 StyleStackError 一样，当前源码中只有本定义和 console.set_live 文档字符串里
#   的 “Raises: errors.LiveError”，没有实际抛出点；它是为下游/历史用法保留的公共类型。
class LiveError(ConsoleError):
    """Error related to Live display."""


# 【职责】表示调用方在未开启备用屏（alt screen）的情况下，调用了只在备用屏下有效的接口。
# 【协作】console.update_screen / console.update_screen_lines 在 is_alt_screen 为 False 时抛出
#   （见 console.py 1835/1863）；进入备用屏要用 Console.screen() 或 Live(screen=True)。
class NoAltScreen(ConsoleError):
    """Alt screen mode was required."""
