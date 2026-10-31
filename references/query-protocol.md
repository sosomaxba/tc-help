# 问句处理

库是英文原文，工具不翻译：中文由调用时的 AI 翻成官方英文词。
所有命令收到中文直接拒绝（退出码 2）；工具里没有任何中文词表。

## 拆三要素

动作 + 对象 + 上下文（客户端/模块/角色）。上下文决定选哪本书——同一功能在 AWC 与富客户端常是两本书。

## 用官方词

| 中文 | 别用 | 用 |
|---|---|---|
| 锁定 / 占用 | lock | `check out` |
| 解锁 | unlock | `check in`、`cancel check out` |
| 审批 / 会签 | approve | `signoff`、`approve` |
| 下拉框 | dropdown | `list of values` |
| BOM 树 | BOM tree | `structure`、`product structure` |
| 必填 | mandatory | `required` |
| 归档旧版 | archive old version | `archive`、`obsolete`、`retire` |
| 移交 / 转给 | transfer | `change ownership`、`reassign` |
| 权限 | privilege | `access`、`access manager`、`ACL` |
| 首选项 | option | `preference` |
| 数据集 | file | `dataset` |
| 类型 / 属性定义 | schema | `type`、`property`、`BMIDE` |
| 停用 | deactivate | `discontinue` |
| 映射表 | mapping list | `mapping table`、`selectively map attributes` |
| 默认规则 | default rule | `default rules`（别撞 `revision rule`） |
| 单位制 | unit type | `system of measurement`、`Nonmetric` |

改写的依据只能是库里实际命中的词。0 命中先换词性/换名词（`resume`→`start`、`size`→`dimension`），减词是最后一步。

## 客户端歧义

同一功能常有 AWC / 富客户端两套页，`search` 会在书名前标 `[AWC]` / `[RC]` / `[Admin]`。用户说了客户端就按标签挑；没说就两套都列，各给一句入口差异，别硬挑。

## 检索写法

- 2–4 个内容词，词间 AND；虚词会被丢掉。
- 界面原文/固定搭配用引号短语：`'"check out"'`、`'"list of values"'`。
- 标识符整体查：`tcxml_xfer_ownership`、`SCHMGT-approve`；前缀用 `*`：`workflo*`。
- 一个问题最多 3 次检索：主检索 + 两个换词/换客户端的辅助。

三轮收口：①直查改写词，看前 5 条书名是否集中；②按第 1 轮的书名换官方词 / 加客户端；③`map` 找片 → `topic` 看目录。三轮无线索就报缺口，不再硬猜。

## 缺口怎么写

试过哪些词、轮次、分类；给最接近的 2–3 条并标明相邻主题；指出下一步（换客户端版本、问管理员、转其它技能）。不允许只回"查不到"。

## handler

"这一步能不能自动做？"先查 handler。共 592 页：149 副本已折叠，443 = 316 结构化 + 127 同名变体；名字直接照抄搜。

| 前缀 | 覆盖 |
|---|---|
| `EPM-` | 流程动作、审批、归档、发布、条件 |
| `IPP-` | IPPE 流程模板、动态签核、任务属性 |
| `TCRS-` / `CM-` | 关系/结构/检入检出；变更管理 |
| `IPM-` / `LDM-` / `DOCMGT-` / `ERP-` / `SAP-` / `RDV-` | 制造、远程、文档、集成、可视化 |

常用词根：校验 `check`/`validate`、通知 `notify`/`mail`、归档 `archive`、改属性 `set-property`、签核 `signoff`。`EPM-set-property`、`epm_set_property`、`EPM set property` 等价；精确名自动置顶。读页看 `Syntax`（参数给法）与 `Arguments`（必填/可选）。

## 首选项 / 查询 / 工具

| 问什么 | 命令 |
|---|---|
| 某首选项的名字/默认值/范围/分类 | `pref "<英文名或词>"`（弃用/内部项标 `⚠`） |
| 某分类下全部首选项 | `pref --category "<分类>"`（含子类） |
| 有没有现成查询、条件是什么 | `savedquery "<英文名或词>"` |
| 某查询类下全部查询 | `savedquery --class "<类名>"`（精确；输入是类名子串时给相近类名） |
| 工具怎么调、参数是什么 | `util "<名或英文词>"` |

首选项与查询是报告快照，不是站点现状；站上配没配、值多少 → `tc-biz`。

## 不该用本技能

API 契约/版本差异（不在本库）；站点实况（`tc-biz`）；写代码（各 `tc-*-developer`）。

## 例子

"发布之后怎么自动把旧版本归档？"

1. 拆：归档 / 旧版本 / 发布流程；避开 `old version`。
2. 改写：`archive` + `revision` + `release`。
3. 查：`search "archive revision release"` → 命中 Workflow Handlers；再 `search "AR-mark-archive"` 精确定位。
4. 读：`page <href>` 看 Syntax / Arguments。
5. 答：用该类 handler 做配置 + 出处（书名 > 章节 > 页）+ 原文链接。
