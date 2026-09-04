# 客户单 HDF5 实验

## 状态与边界

`cw12eu_client_hdf5_v1` 是本地兼容性实验，不是当前生产交付格式。生产仍使用
`cw12eu_client_delivery_v2` ZIP；实验文件不得上传到客户交付 Bucket，也不得替换任何
`current` 指针。

严格训练 HDF5 仍是 schema 3.1.0，只有根数据集 `samples`、`sequences` 和
`annotations`。客户单 HDF5 是独立容器契约，不能冒充严格训练 HDF5；它只是把这三个
数据集复制到根目录，并增加客户查看所需内容。

## 实验结构

```text
/
├── samples
├── sequences
├── annotations
├── media
│   ├── index
│   ├── videos/<sequence_index>
│   └── timing/<sequence_index>
└── labels
    ├── catalog
    └── sequence_versions
```

- `/media/videos/*` 是连续、未压缩的 `uint8` MP4 字节。
- `/media/index` 冻结录制身份、MIME、字节长度、文件物理偏移、SHA-256、视频时长和样本
  零点对应的视频时间。
- `/media/timing/*` 保存 `recording_time_ns` 与 `media_time_ns` 的逐帧映射。
- `/labels/catalog` 保存稳定 code、显示 name、跌倒类别及 active 状态。
- `/labels/sequence_versions` 把每条录制固定到具体 taxonomy ID 和版本。

容器不增加 `/delivery`、`/documents` 或 `/integrity`。来源快照身份保存在根属性中；视频
完整性保存在 `/media/index`，生成后验证器会按物理偏移重新读取每段 MP4 并核对 SHA-256。

## 真实规模验证

2026-09-04 使用 `snapshot-84f6cd83ce754f9cd5cd9e49` 的生产 ZIP 做了本地实验：

| 项目 | 结果 |
| --- | ---: |
| 来源 ZIP | 2,842,101,659 bytes |
| 单 HDF5 | 2,839,336,957 bytes |
| 训练 HDF5 + 视频净载荷 | 2,837,495,521 bytes |
| HDF5 相对净载荷开销 | 0.0649% |
| 录制 / 视频 | 11 / 11 |
| 单 HDF5 SHA-256 | `a8a8314ae8dfdf6f4dc0a6d3e3ceb3ceaba2e4d7560e14b495e47318eba79b24` |

Viewer 浏览器验收确认：无需解包即可读取 HDF5、移动 25 Hz 时间轴、加载内嵌 MP4、同步
显示 IMU，并打开独立 H5Web 高级结构视图。首段视频报告时长 399.099 秒。

## 复现实验

```bash
uv run scripts/experiment-client-hdf5.py \
  cw12eu-delivery-<snapshot-id>.zip \
  /tmp/cw12eu-client-prototype.h5
```

单 HDF5 的优点是客户只需保存一个文件，并能继续使用普通 HDF5 工具读取数值表。代价是
生成和更新都要重写大文件，通用 HDF5 工具不会把 `uint8` 自动当作视频播放，而且浏览器的
零拷贝播放依赖连续数据集物理偏移。因此第一版客户交付继续使用更成熟、可单独取出视频且
更容易恢复传输的 ZIP v2；待不同浏览器、操作系统和客户工具完成验收后再决定是否升级。
