from typing import Optional, TYPE_CHECKING

from .segment import Segment
from .style import StyleType
from ._loop import loop_last


if TYPE_CHECKING:
    from .console import (
        Console,
        ConsoleOptions,
        RenderResult,
        RenderableType,
        Group,
    )

# ---------------------------------------------------------------------------
# 【模块定位】整屏渲染适配器：把任意 renderable 渲染成“恰好铺满终端的一屏”——
#   多余内容裁掉、不足的行列用空格补齐，并在行之间插入换行段。
#   两条使用路径：1) Console.screen() 返回的 ScreenContext（备用屏，见 console.py 的
#   ScreenContext：构造 Screen(style=style)，随后只替换 .renderable 做局部刷新）；
#   2) Live(screen=True) 的备用屏渲染（live.py 里 `Screen(renderable) if self._alt_screen`）。
#   依赖 segment.py（Segment.set_shape / Segment.line）与 _loop.py（loop_last）；
#   对 console 的引用只在运行期按需发生（__init__ 里导入 Group），因此不会形成循环导入。
# 【关键数据结构】Screen 实例保存三个字段：renderable（被包装成 Group 的渲染内容）、
#   style（可选的整体背景样式）、application_mode（换行策略开关）。
# 【设计要点】
#   - “整屏”语义由三步合成：render_lines(..., pad=True) 先按页宽分行、裁到页高并补齐行宽，
#     再用 Segment.set_shape 统一成 width × height 的矩形（幂等的保险），最后逐行拼接。
#   - 换行段由本类负责插入，而不是让 Segment 自带 "\n"：application_mode 为 True 时改用
#     "\n\r"，因为在应用模式下终端往往处于 raw 模式，LF 不会再自动回到行首。
#   - 用 loop_last 取得“最后一行”标志，避免在末尾多输出一个换行把屏幕内容顶上去一格。
#   - 传入 0 个、1 个或多个 renderable 都用 Group 统一包装，于是构造函数只有一条代码路径。
# 【阅读建议】先看 __init__ 的三个字段，再逐行读 __rich_console__ 的五步流程；
#   最后对照 tests/test_screen.py（width=20、height=5 时每行被补到 20 列、6 行输入只输出 5 行）。
# ---------------------------------------------------------------------------


# 【职责】把一个或多个子 renderable 渲染为一整屏内容（裁掉超出部分、补齐不足部分），
#   供备用屏（alt screen）与全屏刷新使用。
# 【关键属性】renderable：被 Group 包装后的子渲染对象（不传参数时是空 Group）；
#   style：整屏背景样式或 None；application_mode：是否使用 "\n\r" 作为行分隔。
# 【不变量】输出中每一行都恰好等于 options.size 的宽度，且总行数不超过 options.size 的高度；
#   这两个不变量来自 render_lines(pad=True, height=...) 与 set_shape 的配合。
# 【协作】console.ScreenContext 持有本类实例并通过 `self.screen.renderable = ...` 局部更新内容；
#   live.py 在备用屏模式下用它包住渲染结果；style 经 console.get_style 解析后同时交给
#   render_lines 与 set_shape，保证补白的空格也带上背景色。
class Screen:
    """A renderable that fills the terminal screen and crops excess.

    Args:
        renderable (RenderableType): Child renderable.
        style (StyleType, optional): Optional background style. Defaults to None.
    """

    # 【要点】类级注解只为类型检查器服务，运行期不产生任何代码；实例属性统一在 __init__ 中赋值。
    renderable: "RenderableType"

    # 【功能】构造整屏渲染对象：把位置参数收成 Group，并记录样式与应用模式开关。
    # 【算法】位置参数 *renderables 直接交给 Group（Group 是 console 里的轻量容器，
    #   渲染时按顺序依次产出，空 Group 产出 0 行）；style / application_mode 原样保存。
    # 【注意】1) 导入 Group 放在函数体内是为了避免 screen ↔ console 的循环导入；
    #   2) 即使不传任何 renderable，self.renderable 也是一个 Group 实例（恒为真值），
    #      所以 __rich_console__ 里 `self.renderable or ""` 的 "" 兜底实际永远不会生效，
    #      它只是防御性写法；
    #   3) style 与 application_mode 没有类级注解，属于约定俗成的实例属性。
    def __init__(
        self,
        *renderables: "RenderableType",
        style: Optional[StyleType] = None,
        application_mode: bool = False,
    ) -> None:
        from rich.console import Group

        self.renderable = Group(*renderables)
        self.style = style
        self.application_mode = application_mode

    # 【功能】按当前 ConsoleOptions 的宽高，把子 renderable 渲染成“正好一屏”的 Segment 流。
    # 【算法】
    #   1) 取外层 options 的 (width, height)，把可选样式解析成 Style；
    #   2) 用同一宽高渲染子对象：render_lines 内部会 split_and_crop_lines 裁到 max_width、
    #      用 islice 裁到 height，再补足到 height 行（pad=True 同时补齐每行宽度）；
    #   3) Segment.set_shape 再统一一次形状（补足行数、调整行宽），属于幂等的保险；
    #   4) 逐行 yield 内容，非最后一行之后再 yield 一个换行段。
    # 【参数】console (Console)：用于 get_style / render_lines；options (ConsoleOptions)：
    #   提供屏幕尺寸与颜色等渲染上下文。
    # 【返回】RenderResult（Segment 的可迭代流），由外层 Console 拼接后写终端。
    # 【注意】1) set_shape 在“行数多于 height”时并不会截断（它从原 lines 重建列表），
    #      这里能得到正好一屏完全依赖上一步 render_lines 已经裁到 height；
    #   2) 换行段是独立 Segment，不属于任何一行，因此不计入行宽统计；
    #   3) application_mode 目前没有任何调用方传 True（默认 False），它是为未来
    #      “终端进入应用/raw 模式”预留的开关。
    def __rich_console__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> "RenderResult":
        width, height = options.size
        style = console.get_style(self.style) if self.style else None
        # 【要点】把外层宽高原样传给子渲染：height 让 render_lines 负责“裁掉多余的行”，
        #   max_width(=width) 让它负责“补齐每行的列”，两者是整屏语义的主要实现者。
        render_options = options.update(width=width, height=height)
        lines = console.render_lines(
            self.renderable or "", render_options, style=style, pad=True
        )
        # 【注意】set_shape 只会向上补足到 height 行，并不会把多余的行裁掉
        #   （它用 lines 而非切片后的列表重建结果）；此处仍然安全，因为上一步已经裁过。
        lines = Segment.set_shape(lines, width, height, style=style)
        # 【为什么】应用模式下 LF 不再自动回车，必须显式写 "\n\r"；
        #   普通模式的 Segment.line() 只产出 "\n"，把回车交给终端做。
        new_line = Segment("\n\r") if self.application_mode else Segment.line()
        # 【要点】loop_last 提供“是否最后一行”，从而只在行与行之间插入换行，
        #   末尾不补换行，避免整屏内容被顶上去一行。
        for last, line in loop_last(lines):
            yield from line
            if not last:
                yield new_line
