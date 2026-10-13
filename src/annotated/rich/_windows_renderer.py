from typing import Iterable, Sequence, Tuple, cast

from rich._win32_console import LegacyWindowsTerm, WindowsCoordinates
from rich.segment import ControlCode, ControlType, Segment

# ---------------------------------------------------------------------------
# 【模块定位】本模块是 Rich 的“legacy Windows 渲染适配器”，位于渲染管线的**最后一步**：
#   Console.render 平时把 Segment 序列化成 ANSI 转义字符串写给文件，但在不支持 VT 的
#   老 Windows 控制台上，转义序列会被当成普通字符显示。此时 Console.render 改为调用
#   legacy_windows_render，把同一批 Segment 翻译成等价的 Win32 控制台 API 调用。
#   它依赖 rich._win32_console（LegacyWindowsTerm、WindowsCoordinates）与
#   rich.segment（Segment、ControlType、ControlCode）；自身没有任何下游依赖，
#   是一个纯粹“翻译层”叶子模块。
#   调用方：rich.console.Console.render 中 `if use_legacy_windows_render:` 分支
#   （并且要求输出文件是真实的 std 流，见 console.py 的 _STD_STREAMS_OUTPUT 判断）。
# 【关键数据结构】
#   - Segment：(text, style, control) 三元组。control 为 None 表示普通文本；
#     否则是一个 ControlCode 元组序列，形如 (ControlType,)、(ControlType, int)
#     或 (ControlType, int, int)。
#   - WindowsCoordinates：行/列坐标（Y 在前），0 基；而 ANSI/ControlCode 里的坐标
#     是 (x=列, y=行) 且 1 基，转换时两个方向都要调整。
# 【设计要点】
#   - 用“消息翻译”而不是“字符串替换”：每个 ControlType 直接映射到一个 Win32 API
#     包装方法，避免解析 ANSI 文本，也避免了老控制台不认转义序列的问题。
#   - 无样式的 Segment 走 term.write_text（只写文件），有样式的才走 term.write_styled
#     （写文件 + SetConsoleTextAttribute + 恢复默认属性），从而减少昂贵的 API 往返。
#   - 故意“静默忽略”无法映射的控制码（BELL、CLEAR、备用屏幕等），而不是抛异常：
#     渲染路径的健壮性优先于功能完整，这也是与 POSIX 路径最重要的行为差异。
#   - 坐标统一在调用 term 之前做 ±1 偏移，让 LegacyWindowsTerm 的接口保持纯粹的 0 基。
# 【阅读建议】本文件只有一个函数，按 for 循环的顺序从上往下读即可：先看普通文本分支
#   （有/无 style 两条路），再逐个看 ControlType 分支，并把每个分支与 ANSI 序列对照
#   （MOVE_TO ↔ CSI row;colH、CARRIAGE_RETURN ↔ \r、ERASE_IN_LINE ↔ CSI nK 等）。
#   `cast` 只服务于类型检查器，运行期没有开销，读的时候可以忽略。
# ---------------------------------------------------------------------------


# ==== 【Segment → Win32 API 的翻译循环】 ====
# 【功能】遍历 Segment 缓冲区，把普通文本按样式写到 Windows 控制台，把控制码翻译成
#   对应的光标移动/擦除/标题设置等 Win32 调用。
# 【算法】单层 for 遍历 buffer：control 为假 → 纯文本输出（有 style 用 write_styled，
#   否则用 write_text）；control 为真 → 再遍历其中的控制码元组，按 ControlType
#   逐一派发到 LegacyWindowsTerm 的对应方法。
# 【参数】buffer：Segment 的可迭代对象（Console 传入的是缓冲区列表的副本，
#   必要时已用 Segment.remove_color 去掉颜色）。term：已绑定输出文件句柄的
#   LegacyWindowsTerm 实例，负责真正的 API 调用与文件写入。
# 【返回】None；副作用是写终端。
# 【注意】本函数没有返回值，也不会把异常吞掉——如果底层 API 调用失败，异常会向上传播。
# 【易错】控制码里的坐标是 1 基且为 (x, y) 顺序，而 WindowsCoordinates 是 0 基且
#   (row, col)，任何一处忘记 -1 或写反行列都会让光标跑到错误位置。
# 【兼容】BELL / CLEAR / ENABLE_ALT_SCREEN / DISABLE_ALT_SCREEN 没有分支，
#   在 legacy Windows 路径下会被静默忽略——这是与 POSIX 终端行为的已知差异。
def legacy_windows_render(buffer: Iterable[Segment], term: LegacyWindowsTerm) -> None:
    """Makes appropriate Windows Console API calls based on the segments in the buffer.

    Args:
        buffer (Iterable[Segment]): Iterable of Segments to convert to Win32 API calls.
        term (LegacyWindowsTerm): Used to call the Windows Console API.
    """
    # 【要点】Segment 是三元组解包：text 是要写的字符串，style 是样式或 None，
    # control 是控制码元组或 None。三者互斥：有 control 时 text 通常为空串。
    for text, style, control in buffer:
        # 【易错】这里用“假值”判断而不是 `control is None`：空元组 () 也应视为“没有控制码”。
        if not control:
            # 【为什么】无样式时走 write_text，只调用 file.write，不做 SetConsoleTextAttribute
            # 往返；在老 Windows 上每次属性切换都要额外两次 API 调用，这个分支能显著提速。
            if style:
                term.write_styled(text, style)
            else:
                term.write_text(text)
        else:
            # 【要点】一个 Segment 的 control 可以包含多个控制码（Sequence[ControlCode]）。
            control_codes: Sequence[ControlCode] = control
            for control_code in control_codes:
                # 【要点】所有 ControlCode 元组的第一项都是 ControlType（IntEnum），
                # 因此可以按枚举成员逐个比较分发。
                control_type = control_code[0]
                # 【要点】CSI row;colH 等价物：控制码里是 (x=列, y=行) 且 1 基，
                # WindowsCoordinates 需要 0 基的 (row, col)，故两个分量都要 -1 并交换位置。
                if control_type == ControlType.CURSOR_MOVE_TO:
                    _, x, y = cast(Tuple[ControlType, int, int], control_code)
                    term.move_cursor_to(WindowsCoordinates(row=y - 1, col=x - 1))
                # 【注意】这是唯一用“写文本”实现的移动：直接把 \r 交给文件层，
                # 让控制台自己处理回车，而不是调用 SetConsoleCursorPosition。
                elif control_type == ControlType.CARRIAGE_RETURN:
                    term.write_text("\r")
                # 【要点】HOME 等价于移动到屏幕左上角；WindowsCoordinates(0, 0) 的两个
                # 位置参数依次是 row 和 col，恰好都是 0 所以看不出顺序问题，别被误导。
                elif control_type == ControlType.HOME:
                    term.move_cursor_to(WindowsCoordinates(0, 0))
                # 【要点】以下四个单步移动在 legacy 路径里需要手工换行/回绕，
                # 因为 Win32 的 SetConsoleCursorPosition 不像终端那样自动 wrap。
                elif control_type == ControlType.CURSOR_UP:
                    term.move_cursor_up()
                elif control_type == ControlType.CURSOR_DOWN:
                    term.move_cursor_down()
                elif control_type == ControlType.CURSOR_FORWARD:
                    term.move_cursor_forward()
                elif control_type == ControlType.CURSOR_BACKWARD:
                    term.move_cursor_backward()
                # 【要点】CSI nG 等价物：只移动列，行保持不变，同样要把 1 基转成 0 基。
                elif control_type == ControlType.CURSOR_MOVE_TO_COLUMN:
                    _, column = cast(Tuple[ControlType, int], control_code)
                    term.move_cursor_to_column(column - 1)
                # 【要点】隐藏/显示光标通过 CONSOLE_CURSOR_INFO.bVisible 实现，
                # 保留原有 dwSize（光标厚度），否则会意外改变光标形状。
                elif control_type == ControlType.HIDE_CURSOR:
                    term.hide_cursor()
                elif control_type == ControlType.SHOW_CURSOR:
                    term.show_cursor()
                # 【要点】CSI nK 等价物，n 的三种取值语义与 ANSI 完全一致：
                # 0=擦到行尾、1=擦到行首、2=擦整行。
                elif control_type == ControlType.ERASE_IN_LINE:
                    _, mode = cast(Tuple[ControlType, int], control_code)
                    if mode == 0:
                        term.erase_end_of_line()
                    elif mode == 1:
                        term.erase_start_of_line()
                    elif mode == 2:
                        term.erase_line()
                # 【要点】设置窗口标题；控制码里携带的是 str，与其它分支的 int 负载不同，
                # 这也是 control_code 需要用 cast 收窄类型的原因。
                elif control_type == ControlType.SET_WINDOW_TITLE:
                    _, title = cast(Tuple[ControlType, str], control_code)
                    term.set_title(title)
