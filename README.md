# IMSLP 纯吉他总乐谱库

本项目将现有三吉他改编谱库扩展为一个按 IMSLP 原始分类组织、可以通过 `file://` 离线浏览的纯吉他总乐谱库。

## 目标入口

```text
file:///Volumes/PHILIPS/programs/muse-cache/imslp/index.html
```

## 收录范围

- 独奏吉他及 2、3、4、5、6、7、8、9、12、16 把吉他分类。
- 对应的原作和 `(arr)` 改编分类。
- 六至十弦吉他、Guitar Ensemble 和 Guitar Orchestra 等纯吉他分类。
- 排除电吉他、贝斯吉他以及吉他与其他乐器或人声的混合编制。

## 当前状态

- 29 个批准分类均已建立独立双语目录，并汇总到根 `index.html`；总计 9,844 部作品、17,611 条纯吉他 PDF 记录。
- 其中 27 个分类完整下载并通过 PDF 头、目标大小、SHA-1、纯度、双语目录和本地链接验证。
- `For guitar`：4,914 部作品、7,382 条活动 PDF 记录、7,342 份严格有效本地 PDF；681 份其他乐器或混合编制附件已排除。
- `For guitar (arr)`：1,939 部作品、3,018 条活动 PDF 记录、3,008 份严格有效本地 PDF；4 份其他乐器附件已排除并保留在隔离区。
- 全库现有 17,561 份严格有效本地 PDF；另有 50 条记录受 IMSLP 版权复核、交互验证或旧文件版本替换影响，目录中标为待下载且不生成错误链接。
- `For 2 guitars` 重新执行文件级纯度检查后为 967 份纯二重奏 PDF；10 份钢琴、班卓琴、低音吉他、小提琴或混合总谱已从活动清单排除。
- 25 份 IMSLP 旧文件名/版本返回了另一条当前记录的内容；这些内容只有在同一作品、目标大小和 SHA-1 完全相符时才复制到正确记录，原隔离副本仍保留。
- 正式 `For 3 guitars (arr)` 含 456 部作品、1,445 份纯三吉他 PDF。旧 `for3guitars` 只作为历史兼容目录保留，不计入 29 个分类和总数。

完整设计见：

[`docs/superpowers/specs/2026-08-30-imslp-guitar-library-design.md`](docs/superpowers/specs/2026-08-30-imslp-guitar-library-design.md)

实施计划见：

[`.agents/superpowers/specs/2026-08-30-imslp-guitar-library-implementation.md`](.agents/superpowers/specs/2026-08-30-imslp-guitar-library-implementation.md)

## 实际结构

- 根 `index.html`：总分类导航与跨分类搜索。
- IMSLP 原名目录：每个分类独立 HTML、Markdown、CSV 和 JSON 目录。
- 分类 `scores/`：按音乐家/作品/文件保存 PDF。
- 分类 `metadata/`：分类成员、清单、翻译、缓存和下载覆盖记录。
- 根 `scripts/`：元数据、下载、翻译、迁移、渲染和验证工具。

## 证据边界

只有分类目录的下载、哈希和链接复验全部通过后才标记为完成。当前 27 个分类满足该条件；两个独奏分类的本地文件均已严格校验，但仍有 50 条 IMSLP 端暂不可取得的记录，因此不能称为全量下载完成。
