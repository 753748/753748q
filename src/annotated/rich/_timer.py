"""
Timer context manager, only used in debug.

"""

from time import time

import contextlib
from typing import Generator

# ---------------------------------------------------------------------------
# 【模块定位】开发期计时工具：一个 contextmanager，进入时记录时间，退出时把耗时打印到 stdout。
#   它完全不参与正式渲染路径——库内唯一引用是 table.py 的 `if __name__ == "__main__":`
#   基准入口（开发者也可以临时插桩），因此它可以被任何模块导入而不引入循环依赖。
# 【关键数据结构】无；只用 time.time() 做两次采样。
# 【设计要点】
#   - 用 contextlib.contextmanager 把 try/finally 式的收尾交给 with 语法，
#     调用方不必手写“开始计时/结束计时”两行，也不会因为提前 return 而漏掉打印。
#   - 用 time()（墙钟时间）而不是 perf_counter()：这里只求人眼可读的粗略耗时，
#     不追求单调性与高精度；代价是系统时钟被调整读数会失真，这也是它只能用于调试的原因。
#   - yield 一次且值为 None（类型标注 Generator[None, None, None]），所以 with 语句无法拿到耗时，
#     也不能 `as` 一个变量去读时间。
# 【阅读建议】整段只有四行；重点看函数注释里的异常路径与“打印到 stdout 会污染被测输出”两点。
# ---------------------------------------------------------------------------


# 【功能】测量 with 代码块的墙钟耗时，并在块结束时打印 `"{subject} elapsed {毫秒:.1f}ms"`。
# 【算法】进入时 start = time()；yield 交出控制权让 with 体执行；恢复后 elapsed = time() - start，
#   换算成毫秒并保留一位小数后 print。
# 【参数】subject (str)：打印前缀，用于区分多个计时点。默认 "time"。
# 【返回】上下文管理器；with 语句绑定到的是 None，而不是耗时数值。
# 【注意】1) yield 之后没有 try/finally：with 体内抛异常时，异常会在 yield 处抛回生成器并
#     直接结束它，因此**异常路径不会打印耗时**；
#   2) 用内置 print 直接写 sys.stdout，而不是 Rich 的 Console：既不参与标记渲染，
#     也不受 Console(file=...) 或 quiet 影响，可能混进被测程序的正常输出；
#   3) 计时基于 time.time()，受系统时钟调整影响，不保证单调递增。
@contextlib.contextmanager
def timer(subject: str = "time") -> Generator[None, None, None]:
    """print the elapsed time. (only used in debugging)"""
    start = time()
    yield
    elapsed = time() - start
    elapsed_ms = elapsed * 1000
    print(f"{subject} elapsed {elapsed_ms:.1f}ms")
