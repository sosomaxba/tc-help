# tc-help

Teamcenter 2506 产品帮助的离线检索技能：203 本书（202 本有正文）、24,334 页英文原文，另含 13,455 条首选项定义和 983 条现成查询。约 42 MB 文本装在 88 MB 的 SQLite 里（zip 约 24 MB），**不需要联网**。

## 两种拿法

- **同事拿 zip**：解压即用，库已随包（zip 不在此仓库，由维护者单独提供），跳过「从源码建库」。
- **从 GitHub 拿源码**：仓库**不含库**（Siemens 帮助原文有版权，不公开再分发），先按「从源码建库」生成 `data/2506/tc_help_2506.db`，再跑 `--selftest`。
- **需要现成的库（zip）**：发邮件到 `537499992@qq.com`，注明用途；库里的帮助原文版权归 Siemens，只在你自己的许可范围内使用。意见与问题请走仓库 Issues。

## 装法

把 `tc-help` 目录拷进 skills 目录（如 `C:\Users\<你>\.qoder-cn\skills\`）。要求 Python 3.9+，只用标准库；解压后约 88 MB（保留 zip 再加 24 MB）。

## 用起来

```bash
python <skills>/tc-help/scripts/help.py search "change ownership"
python <skills>/tc-help/scripts/help.py handler "EPM-set-property"
python <skills>/tc-help/scripts/help.py util "export administration data"
python <skills>/tc-help/scripts/help.py page <上一步的 href>
```

```
[1] Fundamentals > Active Workspace Fundamentals > Creating and revising data
    Change ownership of a data object
    ...Data objects are assigned an owner...
    src:  http://127.0.0.1:5000/documentation/external/.../xid1119706.html
```

## 命令

| 命令 | 用途 |
|---|---|
| `map` / `map --cat 16` | 分类一览 / 某分类下的书 |
| `topic 156` / `topic 156 --grep rule` | 书的目录 / 目录里搜 |
| `search "<英文词>"` | 全文检索；副本合并，每条带 `src:` |
| `search '"check out"'` | 引号内按短语匹配（默认按词 AND） |
| `search ... --client awc\|rc\|admin` | 只看某客户端 |
| `search ... --group` | 按书分组 |
| `search ... --cite` | 直接输出两行引用 |
| `handler "<名或英文词>"` | 316 个流程 handler：类型/放置/参数/摘要 |
| `util "<名或英文词>"` | 358 个命令行工具：参数/摘要 |
| `pref "<名或英文词>"` | 13,455 条首选项：分类/类型/范围/默认值/说明（弃用项标 `⚠`） |
| `pref --category "Active Workspace"` | 某分类下全部首选项（含子类） |
| `savedquery "<名或英文词>"` | 983 条现成查询：查询类/说明/条件原文 |
| `savedquery --class "Item"` | 精确列出某个查询类；输入是类名子串时给相近类名 |
| `page <href> --grep <词>` | 只回命中段落 |
| `page <href> --max-chars 0` | 读全文（默认 12000 字） |
| `--selftest` | 58 条判据，装完先跑 |

所有命令支持 `--json`；`--limit` 控制条数（`search`/`pref`/`savedquery` 默认 10，`handler`/`util` 默认 5）。换库用 `--db <路径>` 或环境变量 `TC_HELP_DB`；`page` 遇到重名标题会列候选，改用 `href` 打开。

`search`、`handler`、`util`、`pref`、`savedquery` 都只收**英文**，且用官方说法：`check out`（不是 lock）、`signoff`（不是 approval）、`list of values`（不是 dropdown）。中文问题由使用它的 AI 先翻译成官方英文词，工具不做翻译、也不内置中文词表。

## 边界

- 收产品帮助 + 首选项/查询清单；不收 ITK/RAC/SOA/Services API 文档（在各栈开发技能的符号库/契约库里）。
- 首选项与查询来自管理员数据报告的**快照**（出厂/样例），不是你们站点现状；站点实况查 `tc-biz`。
- 库里是扁平文本：表格、截图、代码缩进没有结构，遇到要看原文。
- "帮助里能做" ≠ "你们站上已经配了"。

## 原文链接

每条结果都带 `src:`：

- 本机镜像在跑 → `http://127.0.0.1:5000/documentation/external/...`，可直接打开；
- 镜像没跑 → 官网同路径 `https://docs.sw.siemens.com/documentation/external/...`，要 Siemens 账号；
- `--base package` → 包内相对路径 `<sku>/en-US/<loc>/<dir>/<href>`；
- PDF/Excel/单页 HTML → 页/行定位；SWF 页 → 只标 `SWF help (Active Workspace)`。

`--base auto|local|online|package`，默认 auto（镜像活着用镜像，否则官网）。`https://docs.sw.siemens.com/en-US/doc/282219420/PL….tc_doc_home` 只是文档集首页（iframe 外壳），拼页地址要用 `documentation/external/...`。`--json` 里 `url` 为空时看 `source`。

## 从源码建库（GitHub 版必读）

仓库不含 `data/2506/tc_help_2506.db`（版权原因），第一次用先建库。运行期只需 Python 3.9+ 和 SQLite（FTS5）；建库还需要：

| 输入 | 是什么 |
|---|---|
| `--release-root <dir>` | Teamcenter 2506 帮助包根目录（含 `HomePage/` 和 `tc_help` 等书目录），在本机帮助安装的 `documentation/external/PL…/en-US/` 下 |
| `--indexes <dir>` | 含 `Workflow_Handlers_Index.tsv` 与 `Utilities_Index.tsv` 的目录（帮助包旁的 `indexes/`） |
| `--admin-report <dir>` | 开发者参考里的 `AdminDataReport` 目录（其下 `Preferences/`、`SavedQueries/`） |
| `pdftotext` | 仅 PDF 书需要；构建机装 Poppler 即可，运行时不需要 |

```bash
python scripts/data/build_db.py --release-root "<帮助包根>" --indexes "<indexes 目录>" \
  --admin-report "<AdminDataReport 目录>" --out data/2506/tc_help_2506.db --version 2506
python scripts/help.py --selftest        # 58/58 才算建对
```

换版本改 `--release-root` / `--version` / `--release-sku`；SWF 手册用可选的 `--swf-dir`（不给就跳过这本）。没给 `--indexes` 或 `--admin-report` 时对应表为空，`--selftest` 会报出来。

只改索引或报告数据时不用重建整库（秒级）：

```bash
python scripts/data/build_db.py --indexes-only <db> --indexes <dir>
python scripts/data/build_db.py --extra-handlers-only <db> --release-root <根>
python scripts/data/build_db.py --prefs-only <db> --admin-report <dir>
python scripts/data/build_db.py --queries-only <db> --admin-report <dir>
python scripts/data/build_db.py --dedupe-only <db>
```

发版前跑两条判据，全绿再分发：

```bash
python scripts/help.py --selftest
python scripts/test/audit_source.py --release-root "<帮助包根>" --html audit.html
```

第二条用独立解析路径重比书、文章、章节、正文，缺页会报；`--html` 生成单文件检查页，浏览器直接打开。

盲测题库在 `scripts/test/questions_2506.json`（45 题 + 5 道边界题），答案表交 `python scripts/test/qa_check.py --answers <answers.json>` 判分（hit@1 / hit@3 / 边界通过率）。

## 打包

```bash
python scripts/pack.py    # 产物：tc-help-2506.zip（约 24 MB）
```

包里是 `tc-help/` 顶层目录：SKILL.md、README.md、`references/`、`scripts/help.py`、语料库、`MANIFEST.json`（版本、打包时间、文件大小、库 sha256）。`pack.py` 与 `scripts/data/`、`scripts/test/` 都不进包。

## 文件

```
SKILL.md              AI 协议（五步：翻译→拆分→查→总结→输出）
scripts/help.py       查询命令（唯一运行期脚本）
scripts/pack.py       打 zip（本身不进包）
scripts/data/         建库脚本（不随包）
scripts/test/         核对/判分脚本 + 题库（不随包）
references/           问句改写、语料口径、输出格式、书单
data/2506/            语料库
```
