from __future__ import annotations

from typing import IO, Callable

# ---------------------------------------------------------------------------
# 【模块定位】平台差异的隔离层：安全地从任意“类文件对象”上取出整数文件描述符（fd）。
#   上游只有一处调用者——console.py 的 _write_buffer()，而且只在 `if WINDOWS:` 分支里使用：
#   Windows 的“旧版控制台（legacy windows）”渲染路径必须先确认输出流是不是真正的
#   stdout/stderr（即 fd 是否属于 _STD_STREAMS_OUTPUT = (1, 2) 这一对），而
#   Console(file=...) 完全可能指向任何只实现了 write() 的鸭子类型对象——这类对象要么
#   没有 fileno，要么 fileno 会抛异常。POSIX 下不需要这个判断（Rich 直接用
#   os.isatty / os.get_terminal_size 探测终端），所以“为什么需要本模块”本质上是 Windows 问题。
# 【关键数据结构】无；返回 int 或 None。
# 【设计要点】
#   - 先 getattr 取方法、再 try/except Exception 全兜底：file-like 协议只保证 write()，
#     fileno() 的实现质量参差不齐（标准库只承诺可能抛 OSError，实际可能抛任何异常），
#     为了不让“把输出重定向到奇怪对象”导致渲染崩溃，这里吞掉一切异常并返回 None。
#   - 用 None 表示“取不到 fd”，与 fd 可能是 0/1/2 这类合法值区分开；调用方只需判断
#     `fileno is not None and fileno in _STD_STREAMS_OUTPUT` 就能决定是否走旧版渲染分支。
#   - 不做缓存：文件对象可能被 Console.file 的 setter 在运行期替换，缓存会读到过期的 fd。
# 【阅读建议】读完函数后立刻跳到 console.py 的唯一调用点（_write_buffer，约 2070-2080 行），
#   再对照 tests/test_getfileno.py 的三个用例（正常、没有 fileno、fileno 抛异常）。
# ---------------------------------------------------------------------------


# 【功能】取出类文件对象的整数文件描述符；任何失败都返回 None，绝不向上抛异常。
# 【算法】1) 用 getattr(file_like, "fileno", None) 探测该方法（用 getattr 而不是
#     hasattr + getattr 两步，既能直接拿到可调用对象，也避免两次属性查找）；
#   2) 存在就调用它，成功即返回结果；
#   3) 调用过程中抛出的任何异常（OSError、io.UnsupportedOperation、甚至 AttributeError）
#      都被捕获并返回 None；
#   4) 根本没有 fileno 方法时同样返回 None。
# 【参数】file_like (IO[str])：任何按文件协议实现的对象，如 sys.stdout、io.StringIO、
#   自制的鸭子类型包装器，或 rich.file_proxy.FileProxy。
# 【返回】int | None：真实 fd；无法取得时为 None。
# 【注意】返回值允许是负数——rich._null_file.NullFile.fileno() 固定返回 -1，
#   它不属于 _STD_STREAMS_OUTPUT，因此 NULL_FILE 天然被排除在旧版 Windows 渲染之外。
#   另外这里刻意不写具体异常类型，原因见模块头【设计要点】。
def get_fileno(file_like: IO[str]) -> int | None:
    """Get fileno() from a file, accounting for poorly implemented file-like objects.

    Args:
        file_like (IO): A file-like object.

    Returns:
        int | None: The result of fileno if available, or None if operation failed.
    """
    # 【要点】用 getattr(..., None) 一步拿到可调用对象或缺省值，等价于“先 hasattr 再 getattr”
    #   但少一次属性查找；对实现了 __getattr__ 的代理对象也更温和。
    fileno: Callable[[], int] | None = getattr(file_like, "fileno", None)
    if fileno is not None:
        try:
            return fileno()
        except Exception:
            # `fileno` is documented as potentially raising a OSError
            # Alas, from the issues, there are so many poorly implemented file-like objects,
            # that `fileno()` can raise just about anything.
            return None
    # 【约束】“对方没有 fileno 方法”与“调用 fileno 失败”对调用方不可区分，都是 None；
    #   这是刻意的简化：调用方只需要知道“能不能拿到可用于比较的 fd”。
    return None
