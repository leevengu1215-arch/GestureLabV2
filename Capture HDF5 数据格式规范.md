# Capture HDF5 数据格式规范

## 1. 文档状态

- Schema 名称：`xgr.capture`
- Schema 版本：`1.0`
- 文件类型：HDF5
- 适用范围：3 路视频、1 块手表及流程事件组成的单次连续 Capture

本文档是数据采集端和数据处理端之间的格式契约。采集端生成的文件必须满足本文档中的必填路径、类型和命名规则。未标记为必填的数据集可以延后生成，但不得改变既有字段的含义。

## 2. Session 目录

```text
data/raw/
└── <session_id>/
    ├── session.json
    ├── workflow_events.jsonl
    ├── workflow_events.csv
    ├── <session_tag>_capture_01.h5
    ├── <session_tag>_capture_02.h5
    └── <session_tag>_capture_03.h5
```

`session.json` 保存 Session 索引；每个 HDF5 文件保存一个 Capture 的全部原始数据。人工标注、转码视频和分析结果不得写回 raw HDF5。

## 3. 文件命名

格式：

```text
<session_tag>_capture_NN.h5
```

示例：

```text
20260716T131831Z_capture_01.h5
20260716T131831Z_capture_02.h5
```

规则：

- `session_tag` 在 `session.json` 中显式声明；
- 默认 `session_tag` 等于 `session_id`；
- `session_tag` 只能包含 ASCII 字母、数字、连字符和 `T`、`Z`；
- `NN` 对应 `capture_segment`，至少两位，范围扩大后允许三位；
- Capture 重录必须分配新的编号，不能覆盖旧文件；
- 文件名一旦发布不得修改。

P001 旧数据从 `capture_segment=2` 开始，因此迁移结果应为 `..._capture_02.h5`，不能为了连续命名改成 `capture_01`。

## 4. HDF5 根属性

每个文件根节点必须包含：

| 属性 | 类型 | 必填 | 固定值/含义 |
| --- | --- | --- | --- |
| `schema_name` | UTF-8 string | 是 | `xgr.capture` |
| `schema_version` | UTF-8 string | 是 | 当前为 `1.0` |

## 5. 完整层级

```text
/
├── meta
│   └── notes
├── video
│   ├── cam-01
│   │   ├── encoded
│   │   ├── pts_s                  # 可选
│   │   └── keyframe              # 可选
│   ├── cam-02
│   └── cam-03
├── watch
│   ├── accel
│   │   ├── source_csv
│   │   ├── table
│   │   ├── timestamps
│   │   │   ├── current_ms
│   │   │   └── event_ms
│   │   └── values
│   ├── gyroscope
│   │   ├── source_csv
│   │   └── table
│   └── <other-signal>
├── events
│   ├── epoch_ms
│   ├── event_type
│   ├── payload_json
│   └── source_line
├── sync
│   └── markers
└── integrity
    ├── source_files
    ├── source_sizes
    ├── sha256
    └── datasets
```

## 6. `/meta`

`/meta` 是 Group。以下属性为必填：

| 属性 | HDF5 类型 | 含义 |
| --- | --- | --- |
| `session_id` | string | Session ID |
| `participant_id` | string | 参与者编号 |
| `capture_id` | string | 兼容 ID，如 `cap-001` |
| `capture_segment` | integer | Capture 数字编号 |
| `status` | string | `valid`、`incomplete`、`invalid` 或 `superseded` |
| `start_epoch_ms` | int64 | Capture 开始 Unix epoch 毫秒 |

以下属性允许缺失：

- `end_epoch_ms`：采集中断且没有结束时间时缺失；
- `scene_id`、`scene_label`；
- `operator`、`batch_id`；
- `started_at`、`ended_at`：ISO 8601 UTC 文本。

`/meta/notes` 是 UTF-8 标量字符串数据集。不要把长文本放入 attribute。

## 7. `/video`

### 7.1 固定通道

以下 Group 必须始终存在：

```text
/video/cam-01
/video/cam-02
/video/cam-03
```

通道不可用时：

```text
available = false
reason = "not-recorded"
```

不得创建空的 `encoded` 数据集冒充视频。

### 7.2 编码视频

可用通道必须包含：

```text
/video/<camera>/encoded
```

规范：

| 项目 | 要求 |
| --- | --- |
| dtype | `uint8` |
| shape | `(原视频字节数,)` |
| 内容 | 原始 WebM/MP4 文件逐字节内容 |
| HDF5 compression | 不使用 |
| SHA-256 | `encoded.attrs["sha256"]` |

视频已经由 VP8、VP9、H.264 等编码器压缩，对 `encoded` 再使用 gzip 会降低写入和随机读取效率，通常不会显著减小文件。

视频 Group 属性：

| 属性 | 必填 | 示例 |
| --- | --- | --- |
| `available` | 是 | `true` |
| `container` | 是 | `webm` |
| `mime_type` | 是 | `video/webm` |
| `start_epoch_ms` | 是 | `1784208001588` |
| `end_epoch_ms` | 否 | `1784208204901` |
| `codec` | 推荐 | `vp8` |
| `width` / `height` | 推荐 | `640` / `480` |
| `frame_count` | 推荐 | `6097` |
| `duration_s` | 推荐 | `203.313` |

### 7.3 PTS 与关键帧

采集端能够获得帧时间戳时，建议同时写入：

```text
/video/<camera>/pts_s       float64 (frame_count,)
/video/<camera>/keyframe    bool    (frame_count,)
```

`pts_s` 必须按显示顺序严格递增。两个数据集必须与视频帧一一对应。

## 8. `/watch`

每个手表信号一个 Group，Group 名使用规范化信号名：

```text
accel
gyroscope
magnetic
barometer
ppg-green
ppg-red
ppg-ir
```

### 8.1 原始 CSV 字节

每个信号必须包含：

```text
/watch/<signal>/source_csv
```

| 项目 | 要求 |
| --- | --- |
| dtype | `uint8` |
| shape | `(CSV 文件字节数,)` |
| 内容 | CSV 原始字节，禁止换行或编码转换 |
| compression | gzip level 4 + shuffle |
| SHA-256 | `source_csv.attrs["sha256"]` |

保留 `source_csv` 的目的是保证迁移后仍能逐字节恢复原始采集结果。

### 8.2 数值表

CSV 能完全解析为数值矩阵时，应写入：

```text
/watch/<signal>/table       float64 (N, C)
```

Group 属性：

```text
columns_json          = "[\"CurrentTimestamp(ms)\", ...]"
metadata_json         = "{\"SampleRate\": \"100\", ...}"
sampling_rate_hz      = 100.0
numeric_table_available = true
```

存在文本行或不规则列时，可以不创建 `table`，但必须设置：

```text
numeric_table_available = false
```

原始 `source_csv` 仍然必须保留。

### 8.3 加速度标准视图

`accel` 必须额外提供：

```text
/watch/accel/timestamps/current_ms    float64 (N,)
/watch/accel/timestamps/event_ms      float64 (N,)
/watch/accel/values                   float64 (N, 3)
```

`values` 列顺序固定为 `[x, y, z]`，并设置：

```text
values.attrs["columns_json"] = "[\"x\", \"y\", \"z\"]"
```

其中：

- `current_ms` 是 Unix epoch 毫秒；
- `event_ms` 是手表内部单调事件时间；
- 两种时间不能互相替代。

## 9. `/events`

只保存时间落入当前 Capture 或显式带有当前 `capture_segment` 的 workflow 事件。

四个平行数据集长度必须相同：

| Dataset | dtype | 含义 |
| --- | --- | --- |
| `epoch_ms` | int64 | 事件 Unix epoch 毫秒 |
| `event_type` | UTF-8 string | workflow status/type |
| `payload_json` | UTF-8 string | 完整原始事件 JSON |
| `source_line` | int64 | 在 Session JSONL 中的原始行号 |

使用平行数组而不是 compound dtype，以提高 Python、MATLAB 和 R 的兼容性。

## 10. `/sync`

raw 文件只保存采集时产生的同步标记：

```text
/sync/markers
```

人工确认的映射、拟合时钟模型和标注边界属于可变数据，应保存在：

```text
data/annotations/<participant>/<session>/<capture>.json
```

不得因人工标注而修改 raw HDF5。

## 11. `/integrity`

以下四个平行数据集必须存在且长度相同：

| Dataset | dtype | 含义 |
| --- | --- | --- |
| `source_files` | UTF-8 string | 打包前相对 Session 的源路径 |
| `source_sizes` | int64 | 源文件字节数 |
| `sha256` | UTF-8 string | 源文件 SHA-256，小写十六进制 |
| `datasets` | UTF-8 string | 保存源字节的 HDF5 dataset 路径 |

发布 HDF5 前，采集端必须重新从 `datasets` 指向的数据集流式计算 SHA-256，并与 `sha256` 比较。只检查 HDF5 文件能否打开是不充分的。

## 12. `session.json` 索引

转换后 Capture 条目示例：

```json
{
  "capture_id": "cap-001",
  "capture_segment": 1,
  "scene_id": "baseline",
  "scene_label": "基线与准备任务",
  "status": "incomplete",
  "start_epoch_ms": 1784208001588,
  "end_epoch_ms": 1784208204901,
  "hdf5_file": "20260716T131831Z_capture_01.h5",
  "video_channels": ["cam-01", "cam-02"],
  "watch_channels": ["accel", "gyroscope", "ppg-green"]
}
```

Session 顶层必须包含：

```json
{
  "session_tag": "20260716T131831Z",
  "capture_storage": {
    "format": "hdf5",
    "schema_name": "xgr.capture",
    "schema_version": "1.0",
    "filename_template": "<session_tag>_capture_NN.h5"
  }
}
```

`session.json` 只负责快速列举 Capture，HDF5 内部元数据是单个 Capture 的完整来源。

## 13. 写入事务

采集端必须采用以下顺序：

1. 写入 `<filename>.partial`；
2. 写完所有 Group、Dataset 和 attributes；
3. 调用 HDF5 flush 并关闭文件；
4. 重新打开文件；
5. 校验 schema、必填路径、长度和全部 SHA-256；
6. 使用原子 rename 将 `.partial` 改为 `.h5`；
7. 最后更新 `session.json`。

程序崩溃后存在 `.partial` 时，不得将其当作有效 Capture。应重新校验或重新采集。

## 14. 压缩与 Chunk 建议

| 数据 | 压缩 | Chunk |
| --- | --- | --- |
| 编码视频 `encoded` | 无 | contiguous |
| 原始 CSV `source_csv` | gzip 4 + shuffle | 约 8 MiB |
| Watch 数值数组 | gzip 4 + shuffle | 约 4,096 行 |
| PTS/时间戳 | gzip 4 + shuffle | 自动或 4,096～16,384 项 |

不要对已经编码的视频使用 gzip。不要为传感器数据创建单行 chunk，否则会显著增加元数据和随机访问开销。

## 15. Raw、Processed 和 Annotation 边界

```text
data/raw/<session>/<session_tag>_capture_NN.h5
data/processed/<participant>/<session>/<capture>/...
data/annotations/<participant>/<session>/<capture>.json
```

- Raw HDF5：创建后不可变；
- Processed：转码媒体、PTS 缓存和算法缓存，可以重建；
- Annotation：人工边界、同步确认、质量状态和 revision，使用原子 JSON 保存。

禁止将人工标注写入 raw HDF5，避免频繁修改大型容器并破坏原始数据的不可变性。

## 16. 最低验收条件

一个 Capture HDF5 可以发布的最低条件：

1. 文件名符合 `<session_tag>_capture_NN.h5`；
2. 根属性 `schema_name` 和 `schema_version` 正确；
3. `/meta`、`/video`、`/watch`、`/events`、`/sync`、`/integrity` 存在；
4. 三个 camera Group 均存在，并正确设置 `available`；
5. 所有可用视频包含 `encoded`；
6. 所有 Watch 信号包含 `source_csv`；
7. Accel 存在时具有标准时间戳和三轴视图；
8. `/integrity` 四个数组等长；
9. 从 HDF5 重新计算的每个 SHA-256 与记录一致；
10. 文件中不存在机器相关的 `/Users/...` 绝对路径。

