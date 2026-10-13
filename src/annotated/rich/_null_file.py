from types import TracebackType
from typing import IO, Iterable, Iterator, List, Optional, Type

# ---------------------------------------------------------------------------
# 【模块定位】“黑洞”文件对象：所有写入被静默丢弃，所有读取都得到空内容，并且自称非终端。
#   它是 Console.file 属性的最后兜底——console.py 中 `file = self._file or (sys.stderr if ...)`
#   之后若仍为 None（典型场景是 Windows 的 pythonw.exe：没有控制台，sys.stdout/stderr 都是 None），
#   就改用 NULL_FILE（见 console.py 约 760 行）。下游还会依赖它的“类型”做判断：
#   logging.py 用 `isinstance(self.console.file, NullFile)` 识别 pythonw 场景，
#   照常生成日志记录但不写任何字节。
# 【关键数据结构】NullFile：IO[str] 的一个完整但全是空操作的实现；模块级单例 NULL_FILE。
# 【设计要点】
#   - 提供一个“可用对象”而不是 None：把 None 交给 self.file.write 会直接 AttributeError，
#     而 NullFile 让所有写入路径天然无害，于是 Console.print 在没有标准输出的环境里
#     依然能完整跑完渲染管线（错误处理、记录、导出等功能都不受影响）。
#   - isatty() 返回 False 是语义核心：Console 据此认定“不是终端”，于是关闭颜色、
#     不发送光标/清屏等控制码，也不去探测终端尺寸。
#   - fileno() 返回 -1 而不是抛 io.UnsupportedOperation：既满足需要整数 fd 的调用方，
#     又因为 -1 不属于 _STD_STREAMS_OUTPUT，天然被 console.py 的 Windows 旧版渲染分支排除
#     （该分支正是通过 _fileno.get_fileno 取得 fd 再比较的）。
#   - 完全无状态、无缓冲：整个进程共用一个实例，因此天然线程安全，也不必反复构造。
#   - 继承 IO[str] 主要是为了类型标注与“协议齐全”的自我约束，并未复用 io 的缓冲实现。
# 【阅读建议】先读 isatty / writable / fileno 这三个“语义开关”，其余方法都是同一模式的
#   空实现（返回 0、""、[] 或 False，或直接 pass）；最后看文件末尾的单例定义。
# ---------------------------------------------------------------------------


# ==== 【NullFile：丢弃一切写入的空文件对象】 ====
# 【职责】充当可安全写入的“黑洞”输出流：读操作返回空内容、写操作被静默丢弃，并自称非终端。
# 【关键属性】没有实例属性、没有内部缓冲；所有方法要么返回常量，要么 pass，
#   因此实例不可变，可以全局共享而无需加锁。
# 【不变量】1) isatty() 恒为 False；2) 任何写操作都不产生副作用也不抛异常；
#   3) fileno() 恒为 -1；4) 所有方法都无状态，调用顺序不影响结果。
# 【协作】console.py 的 Console.file 属性在取不到任何真实流时返回 NULL_FILE；
#   logging.py 用 isinstance(console.file, NullFile) 判断“当前没有可写终端”；
#   _fileno.get_fileno(NULL_FILE) 得到 -1，用于 Windows 旧版渲染分支的排除判断。
class NullFile(IO[str]):
    # 【功能】关闭文件——空实现，不置任何“已关闭”标志。
    # 【注意】关闭后所有方法仍可继续调用且行为不变，这正是它作为“永远可写的兜底流”的前提。
    def close(self) -> None:
        pass

    # 【功能】回答“是否连接到终端”，恒为 False。
    # 【要点】这是 Console 判定“非终端”的唯一依据，直接决定颜色系统、控制码与终端探测的取舍。
    def isatty(self) -> bool:
        return False

    # 【功能】读取若干字符，恒返回空字符串。
    # 【参数】__n：被忽略；双下划线前缀是让位置参数不与子类/关键字冲突的惯用写法。
    def read(self, __n: int = 1) -> str:
        return ""

    # 【功能】回答“是否可读”，恒为 False（读操作只是不报错，并不表示真的可读）。
    def readable(self) -> bool:
        return False

    # 【功能】读一行，返回空字符串（表示“立即到达文件末尾”）。
    def readline(self, __limit: int = 1) -> str:
        return ""

    # 【功能】读取所有行，恒返回新的空列表。
    # 【注意】每次都新建列表，调用方就地修改返回值不会污染其它调用者。
    def readlines(self, __hint: int = 1) -> List[str]:
        return []

    # 【功能】定位文件指针，恒返回 0（当前位置永远不变）。
    # 【注意】seekable() 为 False，因此标准 io 协议不会真的调用它；它只是让协议完整。
    def seek(self, __offset: int, __whence: int = 1) -> int:
        return 0

    # 【功能】回答“是否可定位”，恒为 False。
    def seekable(self) -> bool:
        return False

    # 【功能】返回当前文件位置，恒为 0。
    def tell(self) -> int:
        return 0

    # 【功能】截断文件，恒返回 0——没有任何内容可截断。
    def truncate(self, __size: Optional[int] = 1) -> int:
        return 0

    # 【功能】回答“是否可写”，恒为 False：写入会被静默丢弃，而不是真的落盘。
    # 【注意】返回 False 的同时 write() 并不抛异常，这是 NullFile 刻意保留的“宽容”行为。
    def writable(self) -> bool:
        return False

    # 【功能】写入多行——空实现，逐行丢弃。
    def writelines(self, __lines: Iterable[str]) -> None:
        pass

    # 【功能】迭代器协议：返回下一个“元素”，恒为空字符串。
    # 【注意】它**不会**抛 StopIteration，因此直接 next(NULL_FILE) 可以无限取到 ""；
    #   但 `for x in NULL_FILE` 走的是下面被重写的 __iter__（只产出一个 ""），
    #   所以两种遍历方式的语义并不一致，阅读使用时不要混淆。
    def __next__(self) -> str:
        return ""

    # 【功能】返回一个只含单个空字符串的迭代器，使 `for line in NULL_FILE` 立即结束。
    def __iter__(self) -> Iterator[str]:
        return iter([""])

    # 【功能】上下文管理器入口：返回自身，因此支持 `with NULL_FILE as f:`。
    def __enter__(self) -> IO[str]:
        return self

    # 【功能】上下文管理器出口：什么都不做。
    # 【返回】隐式返回 None，表示**不吞掉**异常，with 体内的异常会继续向上传播。
    def __exit__(
        self,
        __t: Optional[Type[BaseException]],
        __value: Optional[BaseException],
        __traceback: Optional[TracebackType],
    ) -> None:
        pass

    # 【功能】写入文本，恒返回 0（“写了 0 个字符”），内容被丢弃。
    # 【注意】返回值是 int 而不是 None，因为部分调用方（以及 IO 协议）会检查写入长度；
    #   返回 0 表示“什么都没有写”，但不会报错。
    def write(self, text: str) -> int:
        return 0

    # 【功能】刷新缓冲——空实现，因为这里没有任何缓冲可刷。
    def flush(self) -> None:
        pass

    # 【功能】返回文件描述符，恒为 -1。
    # 【为什么】返回 -1 而不是抛 io.UnsupportedOperation：既让需要整数 fd 的调用方拿到合法类型，
    #   又保证它与 _STD_STREAMS_OUTPUT（真实的标准输出/错误 fd）不相等，
    #   于是 console.py 的 Windows 旧版控制台渲染分支会正确地跳过这个假文件。
    def fileno(self) -> int:
        return -1


# 【要点】模块级单例：NullFile 无状态且不可变，全进程共享一个实例即可，
#   既省去重复构造，也让 logging.py 的 isinstance 判断与身份比较都成立。
NULL_FILE = NullFile()
