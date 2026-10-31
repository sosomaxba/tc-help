# 语料口径

## 收了什么

2506 主产品帮助包 `PL20241125556497283` 的全部正文，加 AWC 的 SWF 手册：

| 来源 | 页数 |
|---|---:|
| tc_help | 22,028 |
| slm_tc_collection | 831 |
| ep_help | 553 |
| ewi_2312_en_US_help | 62 |
| awc_swf（官方 DITA html，不在主包） | 743 |
| PDF / 单页 HTML 补录 | 96 |
| Excel 补录（TEM→DC 映射表，每 40 行一页） | 21 |
| **合计** | **24,334** |

20 个分类 / 203 本书（202 本有正文）。第 20 类 `Release Notes` 装 What's New 与 Video Gallery；`Release Bulletin` 只有入口没有正文，作为 0 页占位（`map` 标 `no local content`）。另有 39 页不属任何书目录，归到各书 "Other pages" 章节。

## 没收录

| 内容 | 原因 |
|---|---|
| SDK 文档 10 本（ITK/C++/Data Model/Administration Data/Services Reference/SWF UI Pattern/What's Changed） | 开发文档，符号库按域分发。**例外**：Administration Data Documentation 的首选项（13,455）与现成查询（983）已入库 |
| Release Bulletin | 包内没有正文 |

## 库的形态

- 只存英文原文；建库剥掉旧 `> 来源:` 注解，**所有文本列**（pages 与四张结构化表）出现 CJK 就失败。
- 副本判定：标题相同 + 词集相似度 ≥0.98，当前 941 页。建库末尾再按"同名 + ≥0.95 + 以结构化 handler 页为正本"补标 111 个跨书 handler 副本（不降全局阈值，平台变体 0.85–0.95 保留）。830 个原有副本不存正文，111 个补标副本保留原文；搜索合并副本并在正本标 `also in:`。
- 扁平文本：表格、图片、代码缩进都不入库；这类问题必须看原文。
- PDF 书用 `pdftotext -layout` 按页抽，Excel 用标准库读单元格。构建机要 `pdftotext`，运行时不要。
- 检索 FTS5 `unicode61`：词级 AND + BM25，前缀用 `workflo*`，引号内按短语。库 88 MB（84 MiB），zip 约 24 MB，不做库内压缩。
- 四张结构化表：
  - `handlers` 316 行（274 人工索引 + 42 页面解析；Action 269 / Rule 47）。
  - `utilities` 358 行。
  - `prefs` 13,455 行（首选项定义：分类/说明/类型/保护范围/默认值）。
  - `saved_queries` 983 行（查询类/说明/条件原文）。
  后两张来自开发者参考里的 Administration Data Report，是**报告快照**（出厂/样例），不是站点实况。

## 已知边界

- handler 章节共 **592 页**：149 个跨书副本已折叠，443 个不同页 = **316 结构化 + 127 同名变体**。同名变体正文有实质差异（0.85–0.95），按规则保留；`314/314` 是旧口径，已作废。
- Saved Query 有 **108 条报告里没有说明**（源数据为空），clauses 全部完整。
- 同一本书可能挂在两个分类下：库里只收一次，归第一个分类，`note` 记 `also linked from ...`。
- 原始 html 与 topics.json 的页数已逐包对平（tc_help 22,028/22,028）。
- 原文地址：镜像 `http://127.0.0.1:5000/documentation/external/...` ↔ 官网同路径（需账号）；离线用 `--base package` 给包内相对路径。`/en-US/doc/…tc_doc_home` 只是 iframe 外壳，不能拼页地址。
- SWF 743 页官网取不到（403），`src:` 只标 "SWF help (Active Workspace)"。
