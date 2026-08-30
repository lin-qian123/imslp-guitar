# IMSLP 纯吉他总乐谱库设计

## 状态

- 日期：2026-08-30
- 状态：用户已批准最终落盘规格；分块实施计划已完成审查与修订，尚未执行迁移或下载
- 根目录：`/Volumes/PHILIPS/programs/muse-cache/imslp`
- 总入口：`file:///Volumes/PHILIPS/programs/muse-cache/imslp/index.html`
- 实施计划：`.agents/superpowers/specs/2026-08-30-imslp-guitar-library-implementation.md`

## 1. 目标

把现有的 `For 3 guitars (arr)` 单分类乐谱库扩展为一个可以完全离线浏览、可恢复构建、严格按 IMSLP 原分类整理的纯吉他总乐谱库。

最终系统必须：

1. 收录 IMSLP 中实际存在的古典/原声吉他、扩展弦数吉他及纯吉他合奏分类。
2. 同时收录原作为吉他编制的分类和 `(arr)` 改编分类。
3. 排除电吉他、贝斯吉他，以及吉他和其他乐器、声乐混合的分类。
4. 每个 IMSLP 分类保留独立、同风格的 HTML/Markdown/CSV/JSON 目录。
5. 在根目录提供跨分类搜索的总 `index.html`，并可直接通过 `file://` 打开。
6. 下载当前冻结运行快照内所有能够取得且经严格验证、确属目标吉他编制的 PDF。
7. 保存英文原名和中文参考译名，按音乐家排列并支持中英文检索。
8. 对跨分类重复 PDF 只保存一个底层实体，同时在各分类中提供清晰的本地路径。

## 2. 非目标

- 不收录录音、MIDI、MusicXML、MuseScore 源文件或其他非 PDF 附件。
- 不收录电吉他、贝斯吉他、曼陀林、鲁特琴或包含其他乐器/人声的混合编制。
- 不绕过 IMSLP 的访问限制、人机验证、地区版权限制或付费限制。
- 不把自动生成或参考音译的中文名表述为唯一权威译名。
- 不把约 9,800 个分类成员误称为同样数量的独立作品；跨分类重复必须单独统计。

## 3. 收录分类

目录名称必须逐字采用 IMSLP 分类名称。只为当前含至少一个页面的目标分类创建目录；分类数量变化时由配置和重新抓取更新。

### 3.1 标准吉他数量分类

| IMSLP 分类 | 2026-08-30 页面数快照 | 类型 |
|---|---:|---|
| `For guitar` | 4,911 | 原作 |
| `For guitar (arr)` | 1,939 | 改编 |
| `For 2 guitars` | 536 | 原作 |
| `For 2 guitars (arr)` | 1,089 | 改编 |
| `For 3 guitars` | 43 | 原作 |
| `For 3 guitars (arr)` | 456 | 改编 |
| `For 4 guitars` | 37 | 原作 |
| `For 4 guitars (arr)` | 611 | 改编 |
| `For 5 guitars` | 4 | 原作 |
| `For 5 guitars (arr)` | 125 | 改编 |
| `For 6 guitars` | 1 | 原作 |
| `For 6 guitars (arr)` | 14 | 改编 |
| `For 7 guitars` | 1 | 原作 |
| `For 8 guitars` | 4 | 原作 |
| `For 8 guitars (arr)` | 32 | 改编 |
| `For 9 guitars` | 2 | 原作 |
| `For 12 guitars` | 5 | 原作 |
| `For 12 guitars (arr)` | 3 | 改编 |
| `For 16 guitars` | 1 | 原作 |

### 3.2 扩展弦数与弹性纯吉他分类

| IMSLP 分类 | 页面数快照 | 总库分组 |
|---|---:|---|
| `For 6 string guitar (arr)` | 1 | 独奏吉他/扩展标签 |
| `For 7 string guitar (arr)` | 7 | 独奏吉他/扩展标签 |
| `For 7-string guitar (arr)` | 3 | 独奏吉他/扩展标签 |
| `For 8 string guitar (arr)` | 1 | 独奏吉他/扩展标签 |
| `For 8-string guitar (arr)` | 2 | 独奏吉他/扩展标签 |
| `For 10 string guitar (arr)` | 6 | 独奏吉他/扩展标签 |
| `For 10-string guitar (arr)` | 4 | 独奏吉他/扩展标签 |
| `For 2 and 3 guitars (arr)` | 1 | 弹性人数吉他合奏 |
| `For guitar ensemble (arr)` | 1 | 吉他合奏 |
| `For guitar orchestra (arr)` | 1 | 吉他乐团 |

这些快照合计 29 个分类、9,841 个分类成员。页面数会随 IMSLP 更新而变化，系统必须记录抓取时间和实际 API 数量，而不是把快照写死成通过条件。

### 3.3 明确排除

- `For electric guitar`、`For electric guitar (arr)`、`For 2 electric guitars`。
- 所有包含 `bass guitar` 的分类。
- 所有逗号分隔或文字表明包含 voice、piano、strings、woodwinds、percussion、mandolin、lute 等其他声部的分类。
- 名称损坏、零页面、搜索参数污染或明显不是 IMSLP 规范分类的条目。

### 3.4 分类发现、审批与漂移

`config/categories.json` 是已批准范围的唯一执行允许列表，但系统还必须提供只读分类发现任务：

1. 从 IMSLP 分类 API/Category Walker 获取包含 guitar 的非零分类候选。
2. 先用结构规则排除电吉他、贝斯和混合乐器，再与允许列表比较。
3. 输出 `metadata/category_drift_report.json`，分别列出新增候选、疑似更名、已清空、已删除和成员数变化。
4. 新分类不得自动进入下载范围；只有在人工确认仍符合本规格后才修改允许列表。
5. 分类更名必须保留旧名、目标名和迁移记录，不能静默创建第二份分类库。

因此“全库完成”只相对于某个已批准的允许列表版本和冻结运行快照成立；发现报告保证未来变化不会被静默遗漏。

## 4. 方案选择

采用“统一对象存储 + IMSLP 原分类视图”。

未采用的方案：

1. 每个分类独立保存完整 PDF：实现直观，但跨分类重复会浪费空间并增加校验负担。
2. 只做一个巨型目录：去重简单，但破坏用户要求的 IMSLP 原分类浏览体验。

推荐方案同时满足原分类目录、离线可浏览和物理去重。

## 5. 最终目录结构

```text
imslp/
├── index.html
├── README.md
├── AGENTS.md
├── TODO.md
├── catalog.csv
├── score_manifest.csv
├── config/
│   └── categories.json
├── docs/
├── metadata/
│   ├── categories.json
│   ├── category_drift_report.json
│   ├── runs/<run-id>.json
│   ├── migrations/<run-id>.json
│   ├── works.json
│   ├── score_files.json
│   ├── memberships.json
│   ├── path_map.json
│   ├── translations/
│   └── overrides/
├── objects/
│   └── <sha256-prefix>/<sha256>.pdf
├── backups/
├── quarantine/
├── _migration/
├── scripts/
│   ├── library.py
│   └── imslp_library/
├── For guitar/
│   ├── index.html
│   ├── README.md
│   ├── 乐谱库目录.md
│   ├── catalog.csv
│   ├── score_manifest.csv
│   ├── metadata/
│   └── scores/<Composer>/<Work>/<File>.pdf
├── For guitar (arr)/
└── <其他 IMSLP 原名分类>/
```

`objects/` 中保存按内部 SHA-256 命名的唯一实体，同时保留 IMSLP SHA-1 作为来源校验值。分类下的 `scores/` 优先使用硬链接指向对象，硬链接不可用时只允许使用根目录内的相对符号链接。若目标文件系统两者都不支持，环境检查必须在迁移或下载前阻塞，不能退化为复制或缺少分类本地路径。`storage_method` 属于每一条本地 `Membership` 路径，而不是全局文件记录。绝对符号链接和重复实体均不允许作为完成状态。

## 6. 组件边界

### 6.1 分类配置

`config/categories.json` 是唯一的收录边界来源。每项包含：

- IMSLP 分类原名和 URL；
- 显示分组、吉他数量或 `ensemble`；
- `original` 或 `arrangement`；
- 允许的乐谱小节标题模式；
- 明确排除模式；
- 是否属于扩展弦数分类。
- 允许列表版本和最后审批日期。

爬取器不得仅凭字符串中出现 `guitar` 自动扩大范围。

分类发现器只生成漂移报告，不直接写允许列表。配置加载器向其他组件提供稳定接口：按分类 ID 取得原名、类型、人数、完整锚定的标题匹配器和排除词；配置无效时在联网或写盘前失败。

### 6.2 IMSLP 数据访问层

职责限于：

- 分页读取分类成员；
- 缓存作品页 wikitext；
- 解析文件元数据、IMSLP 文件 ID、预计大小和 SHA-1；
- 识别人机验证、限流和地区限制；
- 使用有界重试和退避。

它不决定某文件是否属于目标编制。

每次全库运行开始时生成 `run_id`、配置哈希和 `snapshot_started_at`，先冻结所有分类的成员 page ID、页面 revision ID 与文件元数据，再进入下载。运行中 IMSLP 的新增、移除或修订不改变本次清单；结束时重新查询并形成漂移报告。完成声明针对该 `run_id`，下一次运行再处理漂移。

### 6.3 乐谱小节提取器

提取器先把作品页解析为带层级、顺序和原始标题的 heading tree，并且只读取 `FILES` 到 `WORK INFO` 之间的区域。标题匹配前执行 Unicode NFC、移除 MediaWiki 样式标记、折叠空白和大小写归一化；不得删除逗号、`and`、`or`、`with` 等可能暴露混合编制的符号或词。

原作分类必须同时满足：

1. page ID 属于冻结的目标原作分类。
2. `Instrumentation` 经同样归一化后与配置内纯吉他编制精确匹配；缺失或含其他乐器时进入人工审计。
3. 文件模板位于原始 `Scores`、`Scores and Parts` 或 `Parts` 分支，且不在 `Arrangements and Transcriptions`、`Source Files`、音频或商业分支中。

改编分类必须同时满足：

1. page ID 属于冻结的目标 `(arr)` 分类。
2. 文件模板位于 `Arrangements and Transcriptions` 分支下。
3. 编制标题与该分类的完整锚定模式匹配。标准标题仅允许精确的 `For Guitar`、`For N Guitars` 或配置列出的扩展弦数/合奏标题，并可带一个不含乐器名的编者括注。
4. 标题尾部或后代标题出现其他乐器、人声、`and`、`with`、逗号列举或 `or` 时拒绝；`For 2 and 3 Guitars` 只由其专用分类模式例外接受。

工作级文件是受控例外：当页面本身的 `Instrumentation` 精确等于目标纯吉他编制、没有目标改编子标题、文件位于工作级原始 Scores/Parts 分支，并且该 page ID 属于目标分类时可以收录。每条必须记录 `selection_reason=work_level_exact_instrumentation` 和原始 instrumentation 证据。现有 77 条此类记录来自 4 个作品页，迁移时必须逐页按该规则复核。

页面级混合集合只有在目标 PDF 位于可独立证明的纯吉他分支时才收录，否则写入人工审计/排除清单。`AUDIO`、`Source Files`、`Synthesized/MIDI`、商业下载和非 PDF 项始终排除。

### 6.4 规范化目录与去重层

全局数据模型至少包含：

- `Category`：原名、类型、人数、抓取时间和成员数；
- `Work`：IMSLP page ID、英文作品名、音乐家原名、来源 URL；
- `ScoreFile`：文件 ID、来源 revision、文件名、URL、大小、IMSLP SHA-1、内部 SHA-256、版权标签和不可变对象路径；
- `Membership`：作品/文件属于哪些分类、依据哪个页面小节、本地可读路径和 `storage_method`；
- `Translation`：中文参考名、来源类型和人工覆盖状态。

IMSLP 文件 ID 用于来源关联，内部 SHA-256 才是对象身份。相同内容但不同 IMSLP 文件 ID 只保存一个对象，同时保留全部来源记录。已验证对象不可原位覆盖：同一 IMSLP 文件 ID 后续出现不同大小、SHA-1 或内容时创建新的 source revision 并标记冲突，旧对象保持不变，未经审计不更新当前 membership。

路径映射必须确定且持久化到 `path_map.json`：输入先做 Unicode NFC，替换 `/` 和控制字符，清理尾随空格/点，实施组件长度上限；发生清理后重名或大小写折叠冲突时追加稳定的 page ID/file ID 后缀。不得依赖当前遍历顺序解决冲突。

### 6.5 下载器

- 先把内容写入同目录 `.part` 文件，完整校验后原子更名。
- 响应必须以 `%PDF-` 开始，匹配已知大小，并在 IMSLP SHA-1 可用时匹配该 SHA-1。
- HTML、验证码、登录页或错误页不得进入对象库。
- 支持按分类、批次、失败状态恢复。
- 外接盘断开、进程中断或网络失败时保留可审计状态，不把半成品计为成功。
- 特殊合法来源必须写入 `metadata/overrides/`，包括来源说明和校验值。

允许的来源仅包括 IMSLP API、作品页提供的规范静态文件端点，以及经记录的合法来源覆盖。不得绕过版权、地区、会员等待、登录或人机验证机制。

下载状态分为：

- 可重试：超时、连接中断、HTTP 429、临时 5xx；采用有界退避。
- 暂停等待用户：人机验证或 bot check；停止新请求并保留恢复点。
- 等待后重试：公开倒计时或普通会员延迟记为 `membership_wait_pending`，倒计时结束后必须重试，不能作为完成终态。
- 终态不可取得：`copyright_restricted`、`region_restricted`、`membership_required`、`login_required`、`commercial_only`、`deleted`；每项必须保存页面证据和判定时间。
- 需人工审计：元数据缺失、来源 SHA-1 缺失、来源内容冲突或页面结构无法唯一判断。
- 成功：`downloaded_verified` 或带完整覆盖证据的 `source_override_verified`。

来源 SHA-1 缺失时仍计算 SHA-256、验证 PDF 头、大小和可解析性，但记录 `source_hash_missing`，不得宣称通过了来源哈希校验。

对象写入和分类路径物化必须幂等：已存在对象先重算 SHA-256；内容错误的对象移动到 `quarantine/objects/<run-id>/`，不得覆盖。分类路径已存在且指向相同对象时视为成功，指向不同内容时阻塞并隔离。权限、跨设备、链接限制、磁盘不足或中断均保留结构化失败状态；只有对象和 membership 同时落盘后才提交成功状态。

### 6.6 翻译层

- 音乐家：复用现有 199 条人工映射；新增姓名优先使用可靠中文通行名，其次采用一致音译并保留原名。
- 作品：人工覆盖常见作品；规则化翻译调性、曲式、编号、作品号等结构；无可靠译法时明确显示“暂无通行中译（原题：…）”。
- 所有中文名均为检索用参考字段，不覆盖 IMSLP 原始字段。

### 6.7 静态页面生成器

根 `index.html` 必须是自包含静态页面，不依赖 HTTP 服务器、CDN 或在线 JavaScript。

搜索索引和分类数据必须内嵌在 HTML 中，不得在 `file://` 页面运行时使用 `fetch()` 读取本地 JSON。所有本地 URL 从当前页面到目标文件计算相对路径，逐路径组件百分号编码并进行 HTML 转义，统一使用 POSIX `/`。必须用含空格、`#`、`%`、撇号、非 ASCII、超长名称和清理后重名的固定样例验证链接。

总页提供：

- 分类卡片及作品、PDF、容量、下载进度；
- 按吉他数量、原作/改编、扩展弦数、合奏类别过滤；
- 跨分类的中英文音乐家和作品搜索；
- 重复作品的分类标签；
- 到各分类 `index.html` 和本地 PDF 的相对链接。

分类页延续现有 `For 3 guitars (arr)` 页面风格，按音乐家排列并显示英文名、中文名、IMSLP 来源和本地乐谱链接。

## 7. 现有三吉他库迁移

现有 1,451 份 PDF 已通过文件完整性和原清单哈希检查，但第一轮规格审查发现原提取正则只匹配标题前缀，误收了 `For 3 Guitars and Double Bass or Bass Guitar` 小节中的 6 份 PDF。它们属于同一作品页，包括总谱、贝斯/低音提琴声部和 3 个吉他声部，与新纯吉他边界不符。迁移必须把这 6 份文件保留在只读备份/隔离记录中，但不得进入新 `For 3 guitars (arr)` 清单；新严格清单的预期上限为 1,445 份，仍须在 77 条工作级记录复核后确定准确数字。

迁移采用有日志、可重复执行的两阶段切换：

1. 检查目标名称碰撞、PHILIPS 可用空间，并冻结旧清单、路径、大小、SHA-1 和整体 SHA-256 快照。
2. 保持 `for3guitars` 原树不变，在 `backups/for3guitars-pre-migration-<run-id>/` 创建只读可恢复副本或受验证的卷内克隆；备份包含原 metadata、logs、scripts 和全部 PDF，并由独立归档清单记录所有路径、大小和 SHA-256。
3. 在 `_migration/<run-id>/For 3 guitars (arr)/` 构建新 schema、严格清单、对象引用和全部静态页面；旧数据只读转换，不原地修改。
4. 导入合格 PDF 到对象库并验证 SHA-256；6 份混合编制文件写入独立隔离清单（路径、排除理由、大小和 SHA-256），77 条工作级记录逐页复核。
5. 物化分类路径，验证新清单/对象/路径、PDF、翻译和 `file://` 链接。
6. 只有验收全部通过后，才把原 `for3guitars` 原子移动为 `backups/for3guitars-retired-<run-id>/`，并把 staging 原子切换为 `For 3 guitars (arr)`。任何一步失败都保留旧入口或可一条命令回滚。
7. 切换后再次全量验证；旧树和迁移日志在用户明确同意清理前保留，不重新下载已经验证且仍符合范围的 PDF。

迁移 journal 固定写入 `metadata/migrations/<run-id>.json`，状态依次为 `planned`、`backup_verified`、`staging_verified`、`legacy_moved`、`category_activated` 和 `post_verified`；失败时另记 `rollback_required` 或 `rolled_back`。每次目录改名前先以临时文件、原子替换和 `fsync` 持久化预期动作；改名后对源父目录和目标父目录执行 `fsync`，再持久化已完成状态。CLI 每次启动先根据 journal 与四个精确路径（旧树、retired 树、staging 树、最终目标分类树）的实际存在状态恢复：在 `legacy_moved` 后可继续激活 staging，若激活条件不再成立则把 retired 树原子移回旧名；不得靠模糊目录扫描猜测。两个最终改名之间发生崩溃是强制回归场景。

旧入口会变为：

`file:///Volumes/PHILIPS/programs/muse-cache/imslp/For%203%20guitars%20(arr)/index.html`

## 8. 执行阶段

1. **基础设施和安全迁移**：建立根项目、配置和统一 CLI；迁移现有三吉他库。
2. **全分类元数据清单**：抓取 29 个分类，建立去重后的作品/文件清单，并在下载前报告准确文件数与预计容量。
3. **小分类试点**：选择页面数少的原作和改编分类，验证原作/改编提取规则、对象去重和分类硬链接。
4. **中型分类下载**：完成 3 把及以上分类，再处理二重奏。
5. **大型独奏分类下载**：最后处理 `For guitar` 和 `For guitar (arr)`，保持可恢复批次和进度报告。
6. **翻译和页面生成**：共享音乐家映射、作品参考译名、分类页和总页。
7. **全量审计**：校验分类纯度、PDF、对象去重、目录链接和清单一致性。

## 9. 错误处理与运行状态

- 每次抓取和下载写入结构化状态，不依赖终端日志判断完成度。
- IMSLP 页面数量变化不算错误；新增/移除成员必须形成差异报告。
- 人机验证出现时停止新的下载，不重复轰炸服务器，并保留恢复点。
- 分类小节无法唯一判断时进入人工审计队列，不猜测下载。
- 翻译缺失不阻塞乐谱下载，但阻止“中文目录完整”这一完成声明。
- 预计容量超过剩余空间的安全阈值时，下载阶段必须暂停并报告。
- 每个运行的配置哈希、成员 page/revision 集合、文件元数据和结束漂移必须保存在 `metadata/runs/<run-id>.json`。
- 对象冲突、路径冲突、权限错误、磁盘不足和迁移中断必须有明确恢复动作，不能只留下日志文本。
- 完成运行前人工审计队列必须清零：每项都要转为已验证下载、带证据的终态不可取得或带理由的明确排除；仅记录 `manual_review` 状态不算解决。

## 10. 验证策略

### 10.1 元数据

- 冻结运行快照中的 API 成员集合与本地分类成员集合一致；结束时漂移另行报告。
- 每个清单记录都有分类、作品、文件和提取小节证据。
- 不存在配置外分类。
- 分类发现报告已生成，新增/更名/清空/删除候选没有被静默忽略。

### 10.2 文件

- `objects/` 中 PDF 的 SHA-256 唯一集合必须等于全局对象清单所引用的 SHA-256 唯一集合；不存在未登记对象或缺失对象。
- 活跃分类本地路径集合必须等于活跃 `Membership.local_path` 集合；每条路径都按其 `storage_method` 指向对应 SHA-256，而一个对象允许被多个分类 membership/path 引用。
- 所有 PDF 通过文件头、大小、可用的 IMSLP SHA-1、内部 SHA-256 和 PDF 解析检查。
- 不存在 `.part`、HTML 伪 PDF、损坏文件或清单外 PDF。
- 活跃对象库中相同 SHA-256 只对应一个对象实体；所有分类硬链接或相对符号链接指向正确内容。
- 对象冲突、路径冲突和来源修订冲突均已解决或明确留在非完成状态。
- `backups/`、`quarantine/` 和 `_migration/` 不属于活跃清单/物理去重范围；它们分别由不可变归档清单记录路径、大小和 SHA-256，并单独验证无未登记文件。

### 10.3 分类纯度

- 原作分类不包含页面中的其他编制改编。
- 改编分类只包含目标 `For … Guitar(s)` 小节，或满足 6.3 全部证据要求并带 `selection_reason=work_level_exact_instrumentation` 的工作级受控例外。
- 不包含电吉他、贝斯或混合乐器分类。
- 旧库 6 份混合低音文件不在新清单，且仍有可审计隔离记录。
- 每个标题家族、工作级例外、混合乐器反例、损坏 PDF 和访问受限 HTML 都有固定回归样例。
- 迁移测试必须覆盖两个最终目录改名之间发生崩溃的情况，并证明能够从 journal 回滚或继续完成。

### 10.4 页面与翻译

- 所有作品和 PDF 均可从根页或所属分类页找到。
- 所有本地链接在 `file://` 下可访问。
- 页面不使用本地 `fetch()`；特殊字符、Unicode 和路径冲突样例的相对链接均可访问。
- 所有音乐家有中文参考名；作品无通行译名时有明确回退标记。
- CSV、JSON、Markdown、HTML 的核心字段和计数一致。

## 11. 完成标准

只有同时满足以下条件才能称为全库完成：

1. 指定 `run_id` 的已批准配置内所有非空 IMSLP 分类已抓取并生成目录，结束漂移已报告。
2. 冻结快照内所有允许取得的目标 PDF 已下载；版权/地区/永久会员/登录/商业/删除等终态不可取得项有明确证据，普通会员等待已完成并重试，人工审计队列为零。
3. 全部已下载文件通过严格校验，活跃清单与活跃本地文件集一致；备份、隔离和迁移树通过各自独立清单验证。
4. 没有配置外编制或其他乐器乐谱混入。
5. 全局对象库按 SHA-256 物理去重成立，分类本地路径完整且没有复制型重复实体。
6. 根总页和所有分类页可离线打开、搜索并访问 PDF。
7. 音乐家和作品中英文显示符合已声明的参考译名边界。
8. README、TODO 和运行说明反映真实完成状态及未闭环项。
9. 现有三吉他库的严格迁移完成，6 份混合编制文件未进入新清单，旧树仍可恢复直到用户批准清理。

## 12. 已确认决策

- 采用全范围方案 A。
- 最终目标是分阶段下载全部可取得 PDF。
- 使用 IMSLP 原分类名作为目录名。
- 现有 `for3guitars` 更名为 `For 3 guitars (arr)`。
- 收录古典/原声、扩展弦数和纯吉他合奏；排除电吉他、贝斯和混合编制。
- 总页使用与现有分类页一致的离线静态 HTML 体验。
