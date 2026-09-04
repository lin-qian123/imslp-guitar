# 2026-09-04 乐曲中文名全库审校

本目录保存本次审校的输入快照、三份互不重叠的分片审校结果，以及按顺序应用的补充审校。最终生产目录使用上一级的 `title_overrides_reviewed_zh.json`。

## 范围与方法

- 输入：352 个分类、12,548 条分类作品记录、11,393 个不重复 IMSLP `work_id`。
- 三份 `shard_*.json` 按语料顺序轮转分片，合计复核全部 11,393 个作品；只记录需要修改的条目。
- 补充审校顺序：修正结果回查、跨分类/同名一致性、著名作品通行名、最终编号/调性/术语/标点审计、全库曲式词汇反查，以及小品、速度术语、历史舞曲、宗教曲名和目录号的第二轮专项复核。
- 优先采用中国大陆古典音乐资料中常见的体裁名和作品通行名；没有稳定通行译名的冷门作品采用忠实参考译名，并保留英文原名作为主要身份字段。
- `source_refs` 为空表示该条属于术语、语义、格式或语料一致性判断，不代表逐条找到了权威出版物的正式中译。
- `raw_*_supplement_v2.json` 保留三个并行专项的原始建议；`supplement_final_sweeps.json` 是解决重叠与冲突、并通过二次规则扫描后实际进入生产目录的最终补充集。

## 可复现构建

在乐谱库根目录运行：

```bash
python scripts/review_title_translations.py build \
  --corpus metadata/translations/review_2026-09-04/corpus.json \
  --review metadata/translations/review_2026-09-04/shard_1.json \
  --review metadata/translations/review_2026-09-04/shard_2.json \
  --review metadata/translations/review_2026-09-04/shard_3.json \
  --supplement metadata/translations/review_2026-09-04/supplement_correction_qc.json \
  --supplement metadata/translations/review_2026-09-04/supplement_cross_consistency.json \
  --supplement metadata/translations/review_2026-09-04/supplement_famous_works.json \
  --supplement metadata/translations/review_2026-09-04/supplement_final_audit.json \
  --supplement metadata/translations/review_2026-09-04/supplement_terminology_sweep.json \
  --supplement metadata/translations/review_2026-09-04/supplement_final_sweeps.json \
  --reviewed-at 2026-09-04 \
  --output metadata/translations/title_overrides_reviewed_zh.json
```

生成器以后应以 `work_id` 为键优先读取该最终审校表，不能再让按英文题名缓存的机器译名覆盖它。
