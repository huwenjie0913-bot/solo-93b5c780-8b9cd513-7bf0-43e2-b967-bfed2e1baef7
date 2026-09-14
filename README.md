# 字体回退链审查 API（font-fallback-audit）

多语种界面逐层回退字体时，组合附标、emoji ZWJ 序列和变体选择符可能被拆开到不同字体，
普通缺字抽查发现不了。本服务按 **Unicode 字素簇（UAX #29）** 切分语料，结合
**HarfBuzz 实际塑形结果**审计回退链，定位这类隐蔽问题。

## 能发现的问题

| kind | 含义 |
|---|---|
| `missing_glyph` | 整条链没有字体覆盖该码点 |
| `notdef` | cmap 声称覆盖，但 HarfBuzz 塑形产出 .notdef |
| `cluster_split` | 簇内码点被拆到后续字体（如基字用 A 字体、组合附标落到 B 字体） |
| `zwj_broken` | emoji ZWJ 序列未被塑形为联合字形（家庭/职业 emoji 散成多个人形） |
| `variation_lost` | 变体选择符（U+FE00–FE0F、U+E0100–E01EF）在采用字体中无 cmap14 专门字形，被静默丢弃 |
| `required_font_mismatch` | 指定脚本的必用字体未被实际采用 |
| `normalization` | 信息项：NFC/NFKC 规范化改变了文本（记录原文与规范化结果） |

同时记录**每段文本实际采用的字体**（segments），支持两条回退链对比（diffs），
每项差异给出码点、脚本、原文位置与最小复现片段（触发差异的最小字素簇）。

## 运行

```bash
pip install -r requirements.txt
uvicorn fontaudit.main:app --host 0.0.0.0 --port 8000
```

数据落盘：默认 `fontaudit.db`（SQLite，WAL）+ `fonts_store/`（上传的字体），
可用环境变量 `FONTAUDIT_DB`、`FONTAUDIT_FONTS` 改路径。
**重启后**：已完成任务与结果照常可查；崩溃时处于 pending/running 的任务自动标记
`interrupted`，调 `POST /tasks/{id}/rerun` 重跑。

## API 一览

### 字体管理
- `POST /fonts` — 上传 TTF/OTF（multipart），返回 id、family、cmap 覆盖数、是否含 cmap14
- `GET /fonts` / `GET /fonts/{id}` / `DELETE /fonts/{id}`

### 审查任务
- `POST /tasks` — 创建任务（后台执行），请求体：
  ```json
  {
    "name": "首页多语种审查",
    "corpus": [{"text": "Café 👨‍👩‍👧 A️", "lang": "en"},
               {"text": "你好，世界", "lang": "zh-Hans"}],
    "chain":  [1, 2, 3],
    "chain_b": [2, 1, 3],
    "normalization": "NFC",
    "ignore_pua": true,
    "pua_allowlist": ["U+E000"],
    "required_fonts": {"Hani": 3, "Latn": 1}
  }
  ```
  - `chain`：回退链 A（字体 id 按回退顺序）；`chain_b` 可选，用于双链对比
  - `normalization`：`none` / `NFC` / `NFKC`（分析前规范化，偏移量按规范化后文本记录）
  - `ignore_pua`：忽略私用区（U+E000–F8FF、平面 15/16 PUA）；`pua_allowlist` 可点名例外
  - `required_fonts`：ISO 15924 四字母脚本码 → 必用字体 id
- `GET /tasks` / `GET /tasks/{id}` — 列表/详情（含按类型汇总）
- `POST /tasks/{id}/rerun` — 重跑（清空旧结果重新分析）
- `DELETE /tasks/{id}`

### 结果查询（均分页：`page`、`page_size`，上限 500）
- `GET /tasks/{id}/findings?kind=&script=&chain=&text_index=`
- `GET /tasks/{id}/segments?chain=&text_index=&font_id=` — 每段文本实际采用的字体
- `GET /tasks/{id}/diffs?kind=&text_index=` — 双链差异（`font_changed` / `issues_changed` / `font_and_issues_changed`）
- `GET /tasks/{id}/export?what=findings|segments|diffs&format=json|csv` — 文件下载

### 差异记录示例

```json
{
  "kind": "issues_changed",
  "cluster": "é", "codepoints": ["U+0065", "U+0301"], "script": "Latn",
  "text_index": 0, "start": 3, "end": 5,
  "chain_a": {"font_id": 2, "issues": ["missing_glyph"]},
  "chain_b": {"font_id": 2, "issues": ["cluster_split"]},
  "repro": "é", "context": "a Café b"
}
```

## 实现说明

- **字素簇切分**（`fontaudit/clusters.py`）：UAX #29 实用子集——组合附标（Extend/Mc）、
  emoji ZWJ 序列（GB11）、变体选择符、emoji 修饰符、标签字符、区域指示符成对（国旗）、
  Hangul L/V/T、Prepend/SpacingMark。
- **字体选择模型**：首个覆盖簇基字的字体为"实际采用字体"；簇内其余码点（组合符、
  emoji 部件、肤色修饰符）逐一核对——后续字体接住记 `cluster_split`，全链都没有记
  `missing_glyph`。
- **ZWJ 断裂判定**：HarfBuzz 会把 ZWJ 序列合并为单一簇，不能看簇值；若字体有 GSUB
  联合字形，整序列塑形应为 1 个字形，否则为多个独立字形 → `zwj_broken`。
- **.notdef 判定**：组合符并入基字簇后 gid 0 无法逐码点回溯，改为计数比较——
  未覆盖码点本就会产出 gid 0，超出部分才是真 .notdef。
- **变体选择符**：查 cmap format 14；`default UVS` 视同无变体。链上后续字体能补时，
  `detail.supported_by` 会指出可恢复的字体 id。
- **文字系统识别**（`fontaudit/scripts.py`）：常见文字的码点范围表；标点/数字归
  `Zyyy`，组合符归 `Zinh`，未知归 `Zzzz`。
- **持久层**（`fontaudit/db.py`）：fonts / tasks / findings / segments / diffs 五张表，
  任务参数（含语料原文）完整存入 `tasks.params`，可随时重跑追溯。

## 测试

```bash
python3 -m pytest tests/ -q
```

19 个端到端测试：用 fontTools 现场生成含 cmap/cmap14 的最小 TTF，覆盖上传校验、
分段归属、组合符缺字/拆分、ZWJ 断裂、变体丢失、NFC/NFKC、PUA 规则、必用字体、
双链对比、分页、JSON/CSV 导出、参数校验与重启恢复。
