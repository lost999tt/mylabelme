# MyLabelme

基于 [Labelme 6.3.1](https://github.com/wkentaro/labelme/tree/v6.3.1) 的定制版，用于视频抽帧后的行人和非机动车目标标注。桌面程序显示名称为 Labelme，Conda 环境名称为 mylabelme。

本仓库保存实际使用的定制源码、依赖版本及恢复配置。完整 Windows x64 Conda 环境位于 [Releases](https://github.com/lost999tt/mylabelme/releases)。当前备份日期为 2026-11-26。

## 恢复完整环境

1. 从 Releases 下载 mylabelme-conda-win64.zip，完整解压到 %USERPROFILE%\.conda\envs\mylabelme。目标目录应为空。
2. 在 Anaconda Prompt 中执行以下命令完成路径修复并启动。

    conda activate mylabelme
    conda-unpack
    python -m labelme

此压缩包包含 Python 3.10、实际安装的全部依赖以及定制 Labelme，适用平台为 Windows x64。首次解压到新目录后运行一次 conda-unpack。已修复路径的环境再次换目录时，应从原始压缩包重新解压。

mylabelmerc.example.yaml 提供快捷键和预设标签配置。可将需要的条目合并到用户目录下的 .mylabelmerc。程序会按首次启动的默认值初始化窗口、显隐和其他界面状态。

## 从源码安装

    git clone https://github.com/lost999tt/mylabelme.git
    cd mylabelme
    conda env create -f environment-win64.yml
    conda activate mylabelme
    python -m pip install --no-deps .
    python -m labelme

environment-win64.yml 和 requirements-win64.txt 记录备份时的版本。创建环境后，最后一步安装当前目录的定制源码。

## 主要功能

- 矩形边角编辑、空白区域框选，方向键移动 1 像素，Shift 加方向键移动 5 像素。
- 标签和 ID 按当前标注目录汇总，批量改名、合并、清除 ID 及删除匹配标注。
- 标签和 ID 的眼睛图标控制显隐，支持跨帧记忆。重复目标检测、按标签和 ID 配色、框上名称显示。
- 选中目标可跨过缺框或删框的帧，在之后匹配同标签和 ID 时继续选中。
- 复制到后续帧，已有匹配目标时跳过。
- 同步修改支持递减和插值。下一帧采用当前框的位置和大小，后续帧按所选方法调整。
- 自动补齐共用同步帧数，填入后续缺少对应标签及 ID 的方框。
- 轨迹预测在所选帧范围内，以已有方框分段插值补齐缺框，执行前后提供提示。
- 自由缩放和平移，图片缩小到窗口以内仍可中键拖动。保持缩放时切图保留视野。
- 显示像素开关，放大时显示清晰像素块。
- 亮度与对比度跨帧和重启记忆，弹窗提供复原按钮。
- 文件名或完整路径切换、文件搜索与计数、即时删除开关及界面开关记忆。

### 快捷键

| 操作 | 按键 |
| --- | --- |
| 新建矩形 | Ctrl+D |
| 编辑模式 | Esc |
| 复制形状 | Ctrl+R |
| 缩放 | 鼠标滚轮 |
| 平移画布 | 按住鼠标中键拖动 |
| 微调方框 | 方向键 |
| 加大微调步长 | Shift+方向键 |
| 删除选中形状 | Delete，是否确认由立即删除开关控制 |

## 标注文件

沿用 Labelme 的 JSON 字段。矩形保存两个对角点，位置使用原图浮点坐标。配色、显隐、亮度、像素显示和界面开关保存在程序配置里。

新标注默认保存到图片目录里的同名 _json 子目录。例如 project/project_json/001.json 对应 project/001.jpg，imagePath 相对于 JSON 目录。显式设置输出目录时优先使用指定目录，已有标注兼容旧保存位置。

## 备份验证

labelme-source-sha256.json 记录定制包内源码和资源文件的 SHA-256。patches/labelme-6.3.1.patch 可用于核对相对上游的修改。Release 的 SHA256SUMS.txt 校验完整环境压缩包。

## 许可证

Labelme 及本仓库定制源码使用 GPL-3.0-only，详见 [LICENSE](LICENSE)。完整环境中第三方依赖保留各自的许可证。
