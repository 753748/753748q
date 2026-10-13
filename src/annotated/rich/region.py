from typing import NamedTuple

# ---------------------------------------------------------------------------
# 【模块定位】屏幕坐标系的最小数据契约：用四个整数描述终端里的一块矩形区域。
#   它是纯数据结构（NamedTuple），不含任何算法，充当 layout.py 与 console.py 之间的
#   “空间描述”通用货币：Layout 在把父区域切分给子节点时产出 Region，Console.update_screen()
#   再把 Region 解包成渲染尺寸与光标偏移。使用方：layout.py（_make_region_map 等）、
#   console.py（update_screen）、以及 tests/test_console.py。
# 【关键数据结构】Region：x、y 为区域左上角在屏幕上的 0 基坐标（列、行）；
#   width、height 为区域的宽高，单位都是终端单元格（cell，宽字符占 2 列）。
# 【设计要点】
#   - 用 NamedTuple 而不是普通类/dataclass：天然不可变、可哈希、可直接下标与解包，
#     调用方大量使用 `x, y, width, height = region` 这种写法，字段顺序因此成为硬契约。
#   - 刻意不提供任何裁剪、相交、偏移等运算方法：坐标语义与裁剪规则属于 Layout/Console 的
#     职责，这里保持“纯数据”，避免把屏幕模型耦合进布局算法。
#   - 不做任何校验：负坐标表示屏幕外的偏移、width/height 为 0 表示空区域，
#     有效性完全由调用方（Layout 切分逻辑、Console.update_screen）保证。
# 【阅读建议】先记住字段顺序（x, y, width, height）；随后去 layout.py 看
#   _make_region_map 如何为每个子 Layout 生成 Region，就能理解它在渲染链路中的作用。
# ---------------------------------------------------------------------------


# 【职责】描述屏幕上一块矩形区域（左上角坐标 + 宽高），用于布局切分与局部刷新。
# 【关键属性】x（列偏移，0 基）、y（行偏移，0 基）、width（宽，单位 cell）、height（高，单位 cell）。
# 【不变量】四个字段都是 int；调用方约定 width/height 非负；字段顺序不可更改——console.py 的
#   update_screen 依靠元组顺序解包：`x, y, width, height = region`。
# 【协作】layout.py 的 divide/_make_region_map 系列方法产出 Region；Console.update_screen(region=...)
#   消费它；tests/test_console.py 里直接构造 Region(2, 3, 8, 4) 做断言。
class Region(NamedTuple):
    """Defines a rectangular region of the screen."""

    x: int
    y: int
    width: int
    height: int
