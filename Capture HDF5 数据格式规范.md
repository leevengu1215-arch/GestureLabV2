# Capture HDF5 数据格式规范

## 1. 文档状态

- Schema 名称：`xgr.capture`
- Schema 版本：`2.0`
- 文件类型：HDF5
- 适用范围：一个时间标识的 Session 中的单个 Capture。

本文档是采集端、标注端和建模端之间的格式契约。HDF5 文件保存原始观测值及采集时产生的同步/流程信息；人工标注、派生切片和模型特征不能回写到原始 HDF5。

## 2. 术语与身份规则

- **Session**：一次完整采集的唯一实体；后续数据模型中不再存在独立的 participant 概念或字段。
- **Session ID**：唯一身份，统一使用采集时间字符串，例如 `20260810T001`。不得使用 `P001`、`P002` 等参与者编号。
- **Capture**：Session 内的一段连续采集。每个 Session 必须包含 **3 或 4 个** Capture。
- **Capture 编号**：同一 Session 内必须连续为 `01`、`02`、`03`（及可选 `04`）；重录在采集完成前替换对应编号，发布后不得覆盖已发布文件。
- **Cam-01 时间轴**：本规范中唯一的统一时间轴。所有 `aligned_time` 均映射到它。

目录结构：

```text
data/raw/
└── 20260810T001/
    ├── session.json
    ├── 20260810T001_capture_01.h5
    ├── 20260810T001_capture_02.h5
    ├── 20260810T001_capture_03.h5
    └── 20260810T001_capture_04.h5   # 可选；仅四段 Capture 时存在
```

## 3. 文件命名与根属性

文件名固定为：

```text
<session_id>_capture_NN.h5
```

例如 `20260810T001_capture_01.h5`。`NN` 至少两位；发布后文件名和内容均不得修改。

每个 HDF5 根节点必须有：

| 属性 | 类型 | 值/含义 |
| --- | --- | --- |
| `schema_name` | UTF-8 string | `xgr.capture` |
| `schema_version` | UTF-8 string | `2.0` |
| `session_id` | UTF-8 string | 时间形式的 Session ID |
| `capture_index` | uint8 | `1`–`4` |
| `timebase` | UTF-8 string | `cam-01` |
| `time_unit` | UTF-8 string | `ms` |
| `status` | UTF-8 string | `valid`、`incomplete`、`invalid` 或 `superseded` |

`status` 的含义固定如下：

- `valid`：所有本次采集应有的流已写入，时间对齐和结构校验通过；
- `incomplete`：采集或导出未完成，或必需流缺失；不得进入标注或训练；
- `invalid`：已知质量或同步问题使数据不可用；不得进入标注或训练；
- `superseded`：被同一 Capture 编号的重新采集结果替代；不得进入标注或训练。

## 4. 完整层级

```text
/
├── video
│   ├── cam-01
│   │   ├── rgb
│   │   ├── time
│   │   └── aligned_time
│   │       ├── value
│   │       ├── start_flash
│   │       ├── end_flash
│   │       └── bias
│   ├── cam-02                                    # 与 cam-01 同构
│   └── cam-03                                    # 与 cam-01 同构
├── watch
│   ├── accel
│   │   ├── value
│   │   ├── time
│   │   └── aligned_time
│   │       ├── value
│   │       ├── start_clap
│   │       ├── end_clap
│   │       └── bias
│   ├── gyroscope                                 # 与 accel 同构
│   └── <other-signal>
│       ├── value
│       ├── time
│       └── aligned_time
│           ├── value
│           └── bias
├── events
│   ├── event_id
│   ├── event_type
│   ├── phase
│   ├── time
│   └── payload_json
└── sync
    └── <gesture-N | baseline-N | static-N | pre-gesture-N>
        ├── aligned_time
        ├── event_id
        └── label
```

`aligned_time/value` 是为标注和切割新增的必需数据集。仅保存 flash/clap 与一个 `bias` 无法可靠处理两端同步点显示出的时钟漂移；它不能满足跨传感器精确切割的要求。

## 5. 时间与同步

### 5.1 通用约定

- 所有 `time` 和 `aligned_time` 均为 `float64`，单位为毫秒。
- 每个 `time` 数据集只表达该设备自己的原始时钟；跨设备比较、显示、标注和切割只能使用 `aligned_time/value`。
- `/video/cam-01/time` 是基准时间轴；`/video/cam-01/aligned_time/value` 必须与其逐元素相等，`bias = 0.0`。
- 对其余每个流，`time` 为设备原始时间，`aligned_time/value` 是映射后的 Cam-01 时间。两者都必须严格递增，且与该流 `rgb` 或 `value` 的第 0 维一一对应。
- `start_flash`、`end_flash`、`start_clap`、`end_clap` 是用于计算映射的同步点，均记录为 Cam-01 时间（毫秒）。找不到时写 `NaN`，不得用 `0` 代替。
- `bias` 为 `float64 (2,)`：`[start_bias_ms, end_bias_ms]`，其中 `bias = aligned_time - time`。Cam-01 为 `[0.0, 0.0]`。两个值不同即表示存在时钟漂移，必须以 `aligned_time/value` 而不是常量 bias 做切割。

同步误差记录为流 Group attribute `alignment_residual_ms`（`float64`）；无法同步时设置 `alignment_status = "unavailable"`，但仍保留原始 `time`。

### 5.2 视频

`/video/cam-01`、`/video/cam-02`、`/video/cam-03` 三个 Group 必须存在。`cam-01` 是基准机位，必须 `available = true`；其他未录制通道设置 Group attribute `available = false`，且可以不创建子数据集。

可用通道必须包含：

| Dataset | dtype / shape | 含义 |
| --- | --- | --- |
| `rgb` | `uint8 (F, H, W, 3)` | RGB 帧，通道顺序固定为 RGB |
| `time` | `float64 (F,)` | 相机原始帧时间 |
| `aligned_time/value` | `float64 (F,)` | 对齐后的 Cam-01 帧时间 |
| `aligned_time/start_flash` | `float64` scalar | 起始 flash 的 Cam-01 时间 |
| `aligned_time/end_flash` | `float64` scalar | 结束 flash 的 Cam-01 时间 |
| `aligned_time/bias` | `float64 (2,)` | 两个同步点对应的 bias |

视频 Group 还必须有 `available`、`frame_height`、`frame_width` 和 `rgb_sha256` 属性。`rgb` 使用按帧或小帧组的 chunk；可使用 gzip level 4 + shuffle，但不得改变帧顺序。

### 5.3 手表信号

每种手表信号一个 Group，例如 `accel`、`gyroscope`、`magnetic`、`barometer`、`ppg-green`。`accel` 和 `gyroscope` 为必需信号；其他实际采集到的信号也必须写入。信号名只用小写 ASCII、数字和连字符。

| Dataset | dtype / shape | 含义 |
| --- | --- | --- |
| `value` | `float32/float64 (N, C)` | 原始数值；`C` 由 `columns_json` attribute 定义 |
| `time` | `float64 (N,)` | 设备原始采样时间 |
| `aligned_time/value` | `float64 (N,)` | 对齐后的 Cam-01 采样时间 |
| `aligned_time/start_clap` | `float64` scalar | 起始 clap 的 Cam-01 时间（加速度、陀螺仪必填） |
| `aligned_time/end_clap` | `float64` scalar | 结束 clap 的 Cam-01 时间（加速度、陀螺仪必填） |
| `aligned_time/bias` | `float64 (2,)` | 两个同步点对应的 bias |

`accel` 与 `gyroscope` 的 `value` 必须为 `(N, 3)`，并设置 `columns_json = "[\"x\", \"y\", \"z\"]"` 和 `unit` attribute。其他信号若没有可用 clap 同步点，可只保存 `aligned_time/value` 与 `bias`；无法建立映射则 `aligned_time/value` 全为 `NaN` 且 `alignment_status = "unavailable"`。

## 6. `/events`：采集流程事件

所有事件时间已转换到 Cam-01 时间轴。五个平行数据集必须等长：

| Dataset | dtype | 含义 |
| --- | --- | --- |
| `event_id` | UTF-8 string | 同一动作/阶段的唯一 ID；开始与结束事件必须相同 |
| `event_type` | UTF-8 string | 如 `gesture_start`、`gesture_end`、`baseline_start`、`baseline_end`、`static_start`、`static_end`、`pre_gesture_start`、`pre_gesture_end` |
| `phase` | UTF-8 string | 阶段，例如 `gesture`、`baseline`、`static`、`pre-gesture` |
| `time` | float64 | Cam-01 时间（ms） |
| `payload_json` | UTF-8 string | 原始 workflow 信息和补充上下文 |

事件按 `time` 非递减排序。同一 `event_id` 的 `*_start` 与 `*_end` 必须各有一条，且结束时间不早于开始时间。这样标注工具可以直接以时间范围选取全部相机帧和手表采样；`payload_json` 可保存任务、场景或方案等上下文，但不得替代上述结构化字段。

## 7. `/sync`：可直接使用的切割区间

`/sync` 记录采集端已确定的候选切割区间。Group 必须存在；没有已知切割区间时可为空。每个子 Group 名为 `<phase>-NN`，例如 `gesture-01`、`baseline-02`、`static-01`、`pre-gesture-01`。

| 项目 | 类型 | 含义 |
| --- | --- | --- |
| `aligned_time` | float64 `(2,)` | `[start_ms, end_ms]`，均在 Cam-01 时间轴上，闭区间 |
| `event_id` | UTF-8 scalar | 对应 `/events/event_id` |
| `label` | UTF-8 scalar | 已知类别；未知时为空字符串 |

切割端以 `/sync/*/aligned_time` 为优先边界；不存在时由 `/events` 中同一 `event_id` 的 start/end 事件恢复。人工调整后的边界存于 annotation 文件，不修改本 Group。

## 8. 标注与分类切割适用性

该结构满足需求，前提是执行以下约束：

1. 每个流的观测数组与 `time`、`aligned_time/value` 等长；
2. 以 Cam-01 时间范围切割时，对每个流用 `aligned_time/value` 做二分查找，而不是按帧号或样本号猜测；
3. 以相同 `event_id` 成对的 start/end 事件，或 `/sync/*/aligned_time`，定义一个训练样本的范围和标签；
4. 训练集划分按 `session_id` 分组，绝不能把同一时间 Session 的不同 Capture 分入训练集与测试集；
5. 同步不可用、flash/clap 缺失或 `alignment_residual_ms` 超阈值的 Capture 必须在 `status` 或 annotation 中标为不可用。

这使得同一 `aligned_time` 区间能稳定地抽取三路视频帧与全部手表信号，并保留可追溯的原始时间、同步锚点和事件标签。

人工标注路径统一使用时间 ID：

```text
data/annotations/<session_id>/<session_id>_capture_NN.json
data/processed/<session_id>/<session_id>_capture_NN/...
```

## 9. `session.json` 索引

`session.json` 仅用于快速列举，HDF5 内元数据是单个 Capture 的完整来源。示例：

```json
{
  "session_id": "20260810T001",
  "capture_storage": {
    "format": "hdf5",
    "schema_name": "xgr.capture",
    "schema_version": "2.0",
    "filename_template": "<session_id>_capture_NN.h5"
  },
  "captures": [
    {"capture_index": 1, "hdf5_file": "20260810T001_capture_01.h5", "status": "valid"},
    {"capture_index": 2, "hdf5_file": "20260810T001_capture_02.h5", "status": "valid"},
    {"capture_index": 3, "hdf5_file": "20260810T001_capture_03.h5", "status": "valid"}
  ]
}
```

`captures` 必须恰有 3 或 4 项，`capture_index` 连续且不重复；`hdf5_file` 必须严格等于 `<session_id>_capture_NN.h5`。不得再写入 `participant_id`、`capture_id`、`capture_segment`、`video_channels`、`watch_channels`、`scene_label` 等旧索引字段；通道和流程事实均以 HDF5 内容为准。

## 10. 写入、完整性与验收

采集端必须先写入 `<filename>.partial`，完成后 flush、关闭、重新打开并验证，最后原子 rename 为 `.h5`，再更新 `session.json`。`.partial` 永远不是有效 Capture。

发布前至少验证：

1. Session ID 仅为时间标识，文件名符合 `<session_id>_capture_NN.h5`；
2. Session 内 Capture 数量为 3 或 4，且编号连续；
3. 根属性及 `/video`、`/watch`、`/events`、`/sync` 存在；
4. 三个 camera Group 均存在，`cam-01` 可用；`accel`、`gyroscope` 存在；所有可用流的值、原始时间和对齐时间长度一致；
5. `alignment_status` 可用的流，其 `aligned_time/value` 严格递增；Cam-01 的 value 等于其 `time`；
6. `/events` 平行数组等长，成对事件与 `/sync` 区间均合法；
7. 不包含 `P001` 等 participant ID、机器绝对路径或人工标注结果。

发布前运行：

```bash
make check <session_id> -- --h5
```

只有输出通过的 `valid` Capture 才可进入 `make annotate` 和后续分类数据集。
