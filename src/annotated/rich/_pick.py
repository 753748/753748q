from typing import Optional

# ---------------------------------------------------------------------------
# 【模块定位】Rich 处理“多层可选布尔配置”的合并规则工具：同一个开关往往同时存在于
#   实例属性、ConsoleOptions、方法参数等多个层次，用它按固定优先级挑出第一个显式设置的值。
#   调用方遍布渲染层：text.py（no_wrap/overflow）、table.py（safe_box、列样式）、
#   pretty.py（no_wrap）等，典型写法 `pick_bool(self.no_wrap, options.no_wrap)`。
# 【关键数据结构】无；输入输出都是 bool/None 序列。
# 【设计要点】
#   - 用 None 表示“未设置”，而不是用 False：只有这样才能区分“显式关闭”与“没指定”，
#     上层可以传 None 把决定权让给下一层，从而形成清晰的优先级链。
#   - 用 *values 变参而不是固定参数列表：调用点的层数会随版本增减，变参让签名保持稳定。
#   - 全部为 None 时返回 False：借助 for 循环变量的作用域规则兜底，写法简洁但对实现细节有依赖。
# 【阅读建议】本文件只有一个函数，读完后对照 tests/test_pick.py 的断言即可确认优先级语义。
# ---------------------------------------------------------------------------


# 【功能】按从左到右的优先级，返回第一个不为 None 的布尔值。
# 【算法】先 assert 保证至少有一个入参，然后线性扫描，遇到第一个非 None 元素立即返回；
#   若全部为 None，循环结束后 value 仍是最后一个元素（None），于是返回 bool(None) 即 False。
# 【参数】*values：任意个 Optional[bool]，越靠左优先级越高。
# 【返回】bool：首个显式设置的值；全部未设置时返回 False。
# 【注意】整段逻辑依赖“for 循环变量在循环结束后依然可见”这一 Python 作用域规则；
#   若改成生成器或推导式，全 None 分支的兜底就会失效。
#   另外断言只在未启用 -O 时生效：`python -O` 下传入空参数会因 value 未绑定而抛 UnboundLocalError。
def pick_bool(*values: Optional[bool]) -> bool:
    """Pick the first non-none bool or return the last value.

    Args:
        *values (bool): Any number of boolean or None values.

    Returns:
        bool: First non-none boolean.
    """
    assert values, "1 or more values required"
    for value in values:
        if value is not None:
            return value
    # 【易错】能走到这里说明 values 里全是 None：此时 value 仍是循环遗留的最后一个元素（None），
    #   bool(None) 即 False。这一行的正确性完全建立在“循环变量会泄漏到循环外”之上。
    return bool(value)
