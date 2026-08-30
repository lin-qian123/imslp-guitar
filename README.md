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

- 总体设计已由用户批准。
- 第三轮书面规格审查发现的问题正在修订；尚未启动全分类抓取或下载。
- 现有 `for3guitars` 库仍保持原位，含 456 个作品页和 1,451 份通过文件完整性/哈希检查的 PDF。
- 新的严格编制审计发现其中 6 份来自“3 把吉他 + 低音提琴/贝斯吉他”的混合小节；迁移时会保留隔离证据，但不会把它们带入纯吉他新清单。
- 实施时会保持旧库不变，先在 staging 中完成严格清单、文件复用和验收；只有验收通过后才把旧树移入 `backups/`，并把 staging 原子切换为 `For 3 guitars (arr)`。

完整设计见：

[`docs/superpowers/specs/2026-08-30-imslp-guitar-library-design.md`](docs/superpowers/specs/2026-08-30-imslp-guitar-library-design.md)

## 计划结构

- 根 `index.html`：总分类导航与跨分类搜索。
- IMSLP 原名目录：每个分类独立 HTML、Markdown、CSV 和 JSON 目录。
- `objects/`：按内容哈希去重的 PDF 实体。
- 分类 `scores/`：保持音乐家/作品/文件的可读路径。
- `metadata/`：全局作品、文件、分类关系、翻译和覆盖记录。
- `scripts/`：元数据、下载、渲染和验证工具。

## 证据边界

当前只有现有三吉他改编库经过全量文件完整性验证；其“纯三吉他”范围需要按新规则重新审计。其他分类的页面数量来自 2026-08-30 的 IMSLP API 设计快照，不代表已经下载或完成。
