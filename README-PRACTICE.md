# 开源软件阅读维护实践 —— Rich 终端富文本渲染库

本仓库是《软件工程》课程实践 1 的交付物：对开源软件 **Rich**（Textualize/rich v14.3.4，
Gitee 镜像 `https://gitee.com/mirrors/rich`）完成源码获取、编译安装、架构泛读、
关键模块精读标注，并提出与实现一项新功能。

## 一、仓库结构

```
src/
├── original-provided/          v1.0 初始代码版本：上游 v14.3.4 完整源码（未改动）
│   ├── rich/                   77 个 Python 文件，26564 行
│   └── tests/                  上游测试套件（用于建立回归基线）
├── annotated/                  v2.0 标注后的代码版本
│   └── rich/                   关键模块（终端渲染管线，8364 行原始规模）逐类逐方法中文标注
└── maintained/                 v3.0 维护后的代码版本
    ├── rich/                   在标注版本基础上新增排版引擎与连字引擎
    ├── tools/                  数据处理与校验工具（度量、连字数据生成、标注校验）
    ├── tests/                  含新增的 95 项排版回归测试
    └── docs/                   新增功能的接口与设计说明

docs/development/              阅读维护过程记录
├── 01-代码度量分析.md          规模与依赖度数分析（数据可复现）
├── 02-架构泛读.md              总体架构、模块职责、模块接口
├── 03-关键模块精读.md          精读结论与维护需求的推导
├── 04-标注规范.md              标注标记体系与校验方法
└── 04-标注产出统计.md          标注产出统计（脚本生成）
```

## 二、分支与版本

| 分支 | 提交数 | 内容 |
| --- | --- | --- |
| `master` | 3 | `v1.0-initial-source` → `v2.0-annotated` → `v3.0-maintained` |
| `personal` | **36** | 从勘察到验证的完整工作轨迹（不与 master 合并） |

```bash
git log --oneline --decorate --graph --all
git diff v1.0-initial-source v2.0-annotated --stat   # 标注改动
git diff v2.0-annotated   v3.0-maintained --stat     # 维护改动
```

## 三、编译安装与运行

```bash
# 1. 从源码安装（纯 Python 包，pip 会先构建 wheel）
python -m pip install -e src/maintained

# 2. 运行上游测试套件（基线：1063 passed / 12 环境相关失败 / 23 skipped）
cd src/maintained && python -m pytest tests -q

# 3. 运行新增功能的 95 项测试
python -m pytest tests/test_justify.py -q

# 4. 演示新功能
python -m rich._justify_demo demo      --width 76 --mode full
python -m rich._justify_demo compare   --width 52 --sample lorem
python -m rich._justify_demo demo      --width 44 --sample cjk
python -m rich._justify_demo hyphenate hyphenation justification algorithm
python -m rich._justify_demo patterns
python -m rich._justify_demo bench     --words 900
```

## 四、新增功能：最优段落断行与两端对齐排版引擎

上游 `Text.wrap` 是贪心首次适配换行，不回头调整，右边缘参差不齐；且没有连字，
也不处理中日韩禁则。新功能实现了出版级的段落排版：

* **Knuth-Plass 总代价最优断行**：盒子—胶水—罚分模型 + 松弛度截断 + 适应度等级剪枝，
  最小化整段罚分之和，而不是逐行贪心；
* **连字（hyphenation）**：Liang 模式算法 + 1006 词精确词表，可加载真实 TeX 模式文件；
* **中日韩禁则**：标点不出现在行首，开引号不出现在行尾，逐字可断且按 2 格计宽；
* **四种对齐**：left / center / right / full，full 模式把富余格数均匀分配到词间空隙；
* **样式完整保留**：断行结果以字符偏移表示，用 `Text.divide` 切分，任意样式区间、
  超链接、嵌套样式都不受影响，因此免费获得 HTML/SVG 导出能力；
* **孤行寡行修正**：在调整比允许的前提下合并断点，修正后整体校验不会产生超宽行。

```python
from rich.console import Console

console = Console()
console.print_justified(text, width=76, mode="full", hyphenate=True)
```

接口与设计细节见 `src/maintained/docs/justification.md`。

## 五、验证结论

| 项目 | 结果 |
| --- | --- |
| 上游测试套件（维护后） | 1063 passed / 12 failed / 23 skipped，与基线完全一致，**无回归** |
| 新增功能测试 | 95 passed |
| 上游测试套件（标注后） | 968 passed / 12 failed / 23 skipped，与原始源码**逐项相同**，标注未改变任何行为 |
| 标注结构校验 | 22 个关键模块文件（8364 行）标注后 AST 与上游一致（去掉文档字符串后完全相等） |
| 标注规模 | 关键模块 8364 → 14775 行（1.77 倍） |

> 说明：12 项失败均为环境相关（Pygments 版本导致高亮配色快照不一致、
> Windows 权限受限导致写入失败），在原始源码上运行也得到完全相同的 12 项失败，
> 因此与本次改动无关；基线数据在项目开始时即已记录。

## 六、当前状态

本仓库的代码与文档已完成；报告已生成（`开源软件阅读维护报告.docx` / `.pdf`）。
尚未完成的一步是 **git 版本历史的构建**：`_tools/build_history.ps1` 已就绪并做过语法
与机制验证，但尚未对仓库执行，因此目前 master 上只有 v1.0 一个版本，
personal 分支尚未创建。执行方式：

```powershell
pwsh -File _tools/build_history.ps1
```

该脚本会构建 master 的 3 个版本、personal 的 35 个版本，并推送到 `origin`。

