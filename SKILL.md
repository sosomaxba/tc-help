---
name: tc-help
description: Teamcenter 2506 产品帮助离线检索。
---

# Teamcenter 2506 帮助检索

> 拿到 zip 时库随包；从 GitHub 源码拿时仓库不含库，先按 README「从源码建库」生成 `data/2506/*.db`。库按脚本位置解析，不依赖 cwd，不联网。命令在技能根目录执行，别处调用时用 `scripts/help.py` 的绝对路径。
> 语料与缺口 [corpus](references/corpus.md)｜问句改写 [query-protocol](references/query-protocol.md)｜输出格式 [answer-format](references/answer-format.md)｜书单 [domain-map](references/domain-map.md)

**只答官方帮助 / 管理员数据报告里怎么说。** 库是英文原文，工具不翻译。

## 边界

- 功能怎么用、在哪配、管理员任务、概念 → **本技能**
- handler、命令行工具、首选项、现成查询 → **本技能的结构化命令**
- API 契约、跨版本差异 → 不在本库
- 站上配没配、对象真实数据 → `tc-biz`
- 写代码 → 各 `tc-*-developer`（技能路由以 `tc-ask` 为准）

## 五步

1. **翻译**：中文 → 2–4 个官方英文词。`锁定→check out`、`审批→signoff`、`下拉框→list of values`、`BOM 树→structure`、`必填→required`。不要整句塞进检索。
2. **拆分**：一个问句最多 3 次检索；标识符整体查（`tcxml_xfer_ownership`）；词根不全用 `*`（`workflo*`）。
3. **查**：先 `search`，看前 5 条的书名后缀与 `[AWC]`/`[RC]`/`[Admin]` 标；跑偏就换词，或用 `--cat`/`--book`/`--client` 收窄；第 3 轮 `map` 找片 → `topic` 看目录。handler / 工具 / 首选项 / 查询直接走结构化命令。
4. **总结**：`page` 读原文；多页综合；表格、截图类问题说明"库里是扁平文本，结构看原文"。
5. **输出**：**必须给参考原文，其余按题型。**
   - 每条依据两行：`《书名》> 章节 > 页`，下一行 `原文: <src: 原样抄>`。缺 `原文:` 行 = 不合格。
   - 事实题 = 一句话答案 + 依据；流程题 = 结论 + 编号步骤 + 依据 +（缺时）缺口；能力题 = 分档（原生 / 配置可行 / 要开发 / 证据不足）+ 依据 + 缺口。
   - 没有的段直接省略，不写"无"。界面名、配置项用英文原文。分清"帮助里说可以"与"你们站上配了没有"。

## 命令

```bash
python scripts/help.py map                              # 20 个分类
python scripts/help.py map --cat Fundamentals           # 分类下的书
python scripts/help.py search "change ownership"        # 默认 10 条，副本自动去重
python scripts/help.py search "signoff" --client awc --group   # 只看 AWC / 按书分组
python scripts/help.py search "constraint" --book "Product Configurator"
python scripts/help.py search '"check out"'             # 引号内按短语匹配（默认按词 AND）
python scripts/help.py search "change ownership" --cite # 直接给两行引用
python scripts/help.py handler "EPM-set-property"       # handler：类型/放置/参数
python scripts/help.py util "export administration data" # 工具：参数/摘要
python scripts/help.py pref "checkin"                   # 首选项：分类/类型/范围/默认值（英文）
python scripts/help.py pref --category "Active Workspace"
python scripts/help.py savedquery "BOM"                 # 现成查询：类/说明/条件（英文）
python scripts/help.py savedquery --class "Item"        # 按查询类精确列出
python scripts/help.py topic 156                        # 一本书的目录
python scripts/help.py topic 156 --grep rule
python scripts/help.py page <href|精确页标题> --max-chars 12000   # 默认 12000 字，0 = 全文；标题重名时用 href
python scripts/help.py page <href> --grep bypass        # 只回命中段落
python scripts/help.py --selftest                       # 58 条判据
```

- `search` 每条 4–7 行：分类 > 书名 > 章节、标题、`href`、命中段（最多两段）、`src:`；同页副本合并并标 `also in:`。`--json` 给结构化结果，零命中先给 `close:` 近似标题。
- `handler` / `util`：316 个 handler（274 人工索引 + 42 页面解析；127 个同名变体保留，149 个跨书副本已折叠）、358 个工具。handler 给类型/放置/参数，工具给参数/摘要，都带 `src:`。**只收英文**，查不到用精确名或描述词。
- `pref`：13,455 条首选项，给分类/类型/范围/默认值/说明；带 deprecated / internal 的会先标 `⚠`。只收英文；`--category` 含子类。
- `savedquery`：983 条现成查询，给查询类/说明/条件原文；只收英文；`--class` 精确匹配，输入是某类名的子串时会提示相近类名。
- 所有命令支持 `--json`。默认条数：`search`/`pref`/`savedquery` 10，`handler`/`util` 5。`--cite` 对 `search`/`handler`/`util`/`pref`/`savedquery`/`page` 可用。
- `src:` 五类：本机镜像 `http://127.0.0.1:5000/documentation/external/...`（可直接打开）；官网 `https://docs.sw.siemens.com/documentation/external/...`（同路径，要 Siemens 账号）；`--base package` 给包内相对路径 `<sku>/en-US/<loc>/<dir>/<href>`；PDF/Excel/单页 HTML 给页/行定位；SWF 页只标 `SWF help (Active Workspace)`。
- 退出码：0 正常，2 用法错 / 中文（所有命令），3 没找到，4 库不存在。

## 硬规矩

- **不许只回"查不到"**：给试过的词与 2–3 个相近候选。
- **不许自造术语**：界面名、配置项一律原文英文。
- **不许编出处**：书名/章节/页标题来自工具输出。
- **名称与参数照抄**：handler / 工具 / 首选项名、`参数` 行、savedquery 的 clauses 原样写，不改写。
- **站点实况不归本技能**：许可、模块、客户定制、当前值 → 点名未知并转 `tc-biz`。
