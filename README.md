# Gesture Lab V2

场景手势采集实验控制台。程序在本机运行，可控制演示材料、被试扩展屏、双摄像头记录、手势打标、同步校准和 HDF5 数据导出。

## Windows 运行

1. 安装 Python 3，并在安装时勾选 `Add Python to PATH`。
2. 双击 `START_GESTURE_LAB.bat`。
3. 首次启动需要联网安装 `numpy` 和 `h5py`。
4. 浏览器会自动打开控制台；启动窗口在实验期间不能关闭。

## macOS 运行

1. 双击 `启动审阅版_Mac.command`。
2. 如果系统阻止运行，在“系统设置 > 隐私与安全性”中允许该文件。
3. 浏览器会自动打开控制台；终端窗口在实验期间不能关闭。

## HDF5 检查

打开控制台后访问 `/api/status`。只有显示以下内容时才具备 HDF5 输出能力：

```json
{
  "hdf5_ready": true,
  "hdf5_schema": "xgr.capture/2.0"
}
```

格式规范见 `docs/Capture HDF5 数据格式规范.md`。完整 Session 采集完成后运行：

```bash
make check <session_id> -- --h5
```

本轮实际产生的 Capture 必须编号连续，并且全部通过、状态为 `valid`，才可进入标注流程。Capture 数量不设固定上限。

## 实验数据

实验数据默认保存在 `sessions/<被试编号>/<本轮编号>/`。该目录已被 `.gitignore` 排除，不应提交至 GitHub。

## 更新协作

修改程序后提交源码和必要素材，不要提交被试数据、HDF5、摄像头录像、缓存或完整运行压缩包。正式运行包应通过 GitHub Releases 单独发布。

## 端口

Windows 默认从 `http://127.0.0.1:8766/` 启动；端口被占用时，启动器会自动选择后续可用端口。
