# 最优段落断行与两端对齐排版引擎 —— 接口与设计说明

本文档是本次维护功能（新增功能）的接口参考与设计说明。功能实现在
`rich/_hyphenation.py`、`rich/_layout.py`、`rich/_justify.py` 三个模块中，
演示程序在 `rich/_justify_demo.py`。

## 1. 快速开始

```python
from rich.console import Console

console = Console()

# 直接打印（宽度默认取终端宽度）
console.print_justified(article_text, width=76, mode="full")

# 拿到逐行结果自行处理
lines = console.print_justified(article_text, width=76, return_lines=True)
for line in lines:
    print(len(line.plain), line.hyphenated, line.demerits)
```

不依赖 Console 的函数式入口：

```python
from rich._justify import JustifyOptions, justify

lines = justify(
    text,
    options=JustifyOptions(
        width=80,
        mode="full",              # left / right / center / full
        hyphenate=True,           # 是否启用连字
        first_line_indent=4,      # 首行缩进（格）
        indent=2,                 # 每行缩进（格）
        last_line_left=True,      # 末行是否保持左对齐
    ),
)
```

命令行演示：

```
python -m rich._justify_demo demo      --width 76 --mode full --sample english
python -m rich._justify_demo compare   --width 52 --sample lorem
python -m rich._justify_demo hyphenate hyphenation justification
python -m rich._justify_demo patterns
python -m rich._justify_demo bench     --words 900
python -m rich._justify_demo svg       --out justify.svg --width 76
```

`--sample` 可选 `english` / `cjk` / `long-words` / `tech` / `lorem`，也可以直接传入任意文本。

## 2. 模块职责

| 模块 | 公开接口 | 说明 |
| --- | --- | --- |
| `rich._hyphenation` | `Hyphenator`、`load_tex_patterns`、`parse_pattern` | 连字：Liang 模式算法、精确词表、TeX 模式文件加载 |
| `rich._layout` | `LayoutOptions`、`layout`、`to_items`、`break_lines`、`BrokenLine`、`LayoutPlan` | 排版核心：项模型、Tokenizer、Knuth-Plass 动态规划 |
| `rich._justify` | `JustifyOptions`、`justify`、`justify_paragraph`、`reflow`、`render_lines`、`install` | 用户 API：四种对齐、段落切分、样式保持、Console 注入 |
| `rich._justify_demo` | `main` | 命令行演示（六个子命令） |

## 3. 数据模型

段落被建模为三种项的序列（`rich._layout`）：

| 项 | 字段 | 语义 |
| --- | --- | --- |
| `Box` | `width, offset, end, word, atomic` | 不可拆分的内容（单词、宽字符、原子串） |
| `Glue` | `width, stretch, shrink, nobreak` | 可伸缩空白（词间空格） |
| `Penalty` | `value, flagged, width, text` | 合法断点及其代价；`-10000` 为强制断行 |

断行结果 `BrokenLine` **只记录字符偏移量**，不复制文本：

| 字段 | 含义 |
| --- | --- |
| `start` / `end` | 该行对应源文本的字符区间 |
| `prepend` | 该行末尾要插入的字符（连字断行时为 `"-"`） |
| `trailing` | 属于源区间但不属于该行的尾随空白（拼接时须去掉） |
| `ratio` | 调整比，负值表示该行被压缩 |
| `natural` / `width` | 自然宽度 / 最终宽度（格） |
| `hyphenated` / `overfull` | 是否连字断行 / 是否超宽 |
| `demerits` | 该行的罚分 |

正因如此，样式保持是自动的：调用方用 `Text.divide` 按偏移量切分原对象，
任何样式区间、超链接、嵌套样式都不受影响。

## 4. 算法

### 4.1 调整比与松弛度

```
r = (L - W) / Y        需要拉伸时（W < L）
r = (L - W) / Z        需要压缩时（W > L）
badness = min(100 * |r|^3, 10000)
```

`badness` 的上限截断是必需的：没有它，一个 `r = 15` 的灾难性行会产生数千倍代价，
动态规划会宁可选择这一条极差的行也不肯多分几行。

### 4.2 罚分

```
普通断点：d = (line_penalty + badness)^2 + penalty^2 + (flagged ? flagged_penalty : 0)
强制断点：d = 0
适应度等级相差 ≥ 2 时额外加 fitness_penalty
```

适应度等级：`r < -0.5` → tight；`-0.5..0.5` → decent；`0.5..1.0` → loose；`> 1.0` → very loose。

### 4.3 动态规划

```
best[j] = min over 可行断点 i < j of ( best[i] + demerits(i, j) )
```

每个断点位置只保留每个适应度等级中代价最小的状态（最多 4 个），这是复杂度接近线性的关键。
段落先按强制断行切分为若干“段”，每段独立求解再拼接——这样硬换行不可能被跳过，
罚分也不会跨段泄漏。

### 4.4 两端对齐

终端无法拉伸字形，因此把该行富余的格数 `left` 尽可能均匀地分配到 `g` 个词间空隙：
每个空隙补 `left // g` 个空格，前 `left % g` 个空隙各多补一个。只有一个词时把富余格数补在行首。

> 注意：`left` / `right` / `center` 三种模式使用行的**自然文本**填充，
> 只有 `full` 模式才重写词间空隙。否则两端对齐已把行撑满，再叠加居中偏移会超出列宽。

### 4.5 中日韩禁则

`CJKClass.NO_START` 中的字符（如 `。，、）】`）不得出现在行首，
`NO_END` 中的字符（如 `（【“‘`）不得出现在行尾。实现方式是：逐字生成 `Box` 后，
若下一个字符属于禁则之首，则不插入断点 `Penalty`，从而在断点层面禁止该位置换行。

## 5. 连字

```python
from rich._hyphenation import Hyphenator, load_tex_patterns

# 默认：仅使用精确词表（未收录的词不做连字）
hyphenator = Hyphenator()
hyphenator.hyphenate("justification")     # [3, 5, 7, 9]
hyphenator.break_word("justification")     # ['jus-', 'ti-', 'fi-', 'ca-', 'tion']

# 加载真实 TeX 模式文件（专业级精度，适用于任意未见词）
patterns, exceptions = load_tex_patterns("hyph-en-us.pat.txt")
hyphenator = Hyphenator(patterns=patterns)
```

三个精度层次的对比：

| 层次 | 数据 | 精度 | 默认 |
| --- | --- | --- | --- |
| 精确词表 `_hyphenation_dict` | 1006 词 | 精确（人工校对） | 启用 |
| 近似模式表 `_hyphenation_patterns` | 1079 条模式 | 约 44%（推导所得） | **关闭**（需 `approximate=True`） |
| TeX 模式文件 | 完整 `hyph-utf8` | 专业级 | 需显式加载 |

近似模式表默认关闭是刻意的取舍：**断错位置比不断开对阅读体验的损害更大**。
生成器 `tools/gen_patterns.py` 会在生成文件头部写入实测精度，可用
`python -m tools.gen_patterns --report` 复核。

## 6. 性能

`python -m rich._justify_demo bench --words 900` 的实测结果（Windows，CPython 3.12）：

| 列宽 | 行数 | 动态规划访问的状态数 | 耗时 (ms) | 总罚分 |
| ---: | ---: | ---: | ---: | ---: |
| 40 | 161 | 1140 | 12.2 | 579267 |
| 60 | 110 | 1140 | 10.5 | 669316 |
| 80 | 84 | 1140 | 10.5 | 521168 |
| 100 | 68 | 1140 | 10.4 | 535050 |
| 120 | 57 | 1140 | 10.4 | 437307 |

被测文本是把同一段 90 词的段落重复 10 次（共 900 词，同一「段」内无硬换行），
因此状态数与列宽无关——它等于（可行断点数 × 活跃状态数），是段落规模的函数；
列宽只影响每行装多少内容，从而影响行数与罚分。900 词段落在 10 毫秒左右完成，
可以放心用于日志、帮助信息等高频场景。

如果段落包含硬换行，会被先切成多个段分别求解，此时状态数与各段长度相关。

## 7. 测试

```bash
cd src/maintained
python -m pytest tests/test_justify.py -q     # 95 passed
python -m pytest tests -q                     # 12 failed, 1063 passed（与基线一致，无回归）
```

95 项测试覆盖：连字的模式解析与词表行为、Tokenizer 的项生成与偏移正确性、
动态规划的列宽约束与文本守恒、四种对齐模式、样式与链接保持、
段落切分、Console 集成与全部命令行子命令。

## 8. 已知限制

1. 近似模式表精度有限，默认关闭（见第 5 节）。
2. 两端对齐通过插入空格实现，因此**不会**微调字符间距；这是终端渲染的固有限制。
3. 孤行寡行修正只在“合并断点后调整比仍在容差内”时生效，因此极端窄列下仍可能出现短行。
4. 同一段落内的宽字符与拉丁文混排时，宽度按单元格统一计算（中文 2 格），
   与 Rich 既有组件的行为完全一致。
