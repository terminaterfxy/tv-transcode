# TV转码

把下载的影片（视频文件、蓝光原盘）整理成电视经 Jellyfin 能直接播放的格式。一个跑在 NAS 上的 Docker 网页面板。

*TV Transcode: a self-hosted web panel that repackages downloaded movies so a TV can direct-play them through Jellyfin. The UI is available in Chinese and English.*

这是个人项目的分享。不提供支持，不保证适用于你的设备，按现状使用。

## 它解决什么问题

电视上的 Jellyfin 遇到不支持的音轨、文件格式或字幕时，会让服务器临时转码：起播慢、快进卡、音质下降，杜比视界也可能点不亮。这个面板在入库前把影片一次整理好：

- 画面**原样复制**，不重新编码，分辨率、帧率、HDR 都不变。
- 只调整文件格式、音轨和字幕的放法。
- 每部影片转换前先告诉你：现在播会怎样、转换后会怎样、代价是什么。

## 默认规则

默认规则来自 **LG G5 + Jellyfin 10.11** 的实测。别的电视不一定相同，所有规则都能在设置页修改。

| 片源 | 默认处理 |
| --- | --- |
| 杜比视界 | 成品用 MP4（LG G5 上 MKV 里的杜比视界偶尔点不亮） |
| 双层杜比视界（Profile 7） | 转为 Profile 8.1 |
| DTS、DTS-HD、TrueHD、MLP、FLAC | 新增一条 E-AC-3，保留原音轨 |
| LPCM、E-AC-3、AC-3、AAC、MP3 | 不转换 |
| 字幕 | 导出为影片旁的外挂文件（内嵌字幕在 Jellyfin 里要读完整个文件才显示） |
| 蓝光原盘（BDMV 文件夹、ISO） | 取出正片，再按以上规则处理 |

## 需要什么

- 一台能跑 Docker 的 NAS 或主机，x86_64 或 ARM64。
- 一个放影片的共享文件夹。
- 可选：Jellyfin（在线搜索字幕、刷新媒体库要用）。
- 可选：MakeMKV 容器（部分蓝光原盘要用它读取）。MakeMKV 是第三方专有软件，不包含在本项目里。

## 安装

```bash
git clone https://github.com/terminaterfxy/tv-transcode.git
```

```bash
cd tv-transcode
```

打开 `docker-compose.yml`，改掉标了“改”的几处（时区、共享文件夹路径），然后：

```bash
docker compose up -d --build
```

首次构建需要几分钟，期间要从 GitHub 下载 dovi_tool。完成后浏览器打开 `http://NAS地址:8097`。

装好后先看 **设置 → 运行环境**，确认各项都是“正常”。

## 怎么用

1. 把影片下载到下载文件夹（默认是共享文件夹里的 `download`）。
2. 面板自动读取影片信息，每部影片显示“现在 / 转换后 / 代价”。
3. 选好要保留的音轨和字幕，点“排队转换”；不需要转换的可以“直接入库”。
4. 成品出现在影片文件夹（默认 `Movies`），Jellyfin 的媒体库指向这里。
5. 在电视上确认成品正常后，再回面板删除源文件。

入库后的影片可以在面板里改名、搜索并下载中文字幕、校准字幕时间。

下载文件夹和影片文件夹可以在 **设置 → 媒体目录** 里改成 NAS 上的任意文件夹。

## 环境变量

都不是必填，不设就用默认值。

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 时区 |
| `PUID` / `PGID` | 不设（文件归 root） | 成品文件交给哪个用户、用户组 |
| `PORT` | `8097` | 面板端口 |
| `PANEL_CONTAINER` | `mediapanel` | 面板自己的容器名，升级和切换文件夹时用来找到自己 |
| `MAKEMKV_CONTAINER` | `makemkv` | MakeMKV 容器名 |
| `JELLYFIN_CONTAINER` | `jellyfin` | Jellyfin 容器名 |
| `JELLYFIN_URL` | 同一台机器的 8096 端口 | 面板访问 Jellyfin 的地址 |
| `JELLYFIN_PORT` | `8096` | 没设 `JELLYFIN_URL` 时用的端口 |
| `JELLYFIN_PUBLIC_URL` | 同上 | 页面上“Jellyfin”按钮打开的地址 |
| `JELLYFIN_API_KEY` | 不设 | Jellyfin 的 API 密钥；也可以在页面上填 |
| `MEDIAPANEL_ALLOWED_HOSTS` | 不设 | 用域名访问面板时，把域名加在这里，逗号分隔 |
| `DOVI_TOOL` | 镜像自带的那份 | dovi_tool 的位置 |

## 升级

```bash
git pull
```

```bash
docker compose up -d --build
```

也可以在页面右上角“升级”里上传新版 `app.py`：面板会在本机重新构建镜像并切换，失败自动回退。

## 安全

**只在家里局域网使用，不要暴露到公网。**

- 面板没有登录。
- 面板可以删除下载文件夹里的源文件。
- 面板挂载了 Docker 套接字，等于拥有这台机器的最高权限。升级、选择尚未挂载的文件夹、调用 MakeMKV 都依赖它。
- 选文件夹时，面板会临时启动一个把宿主机根目录只读挂进去的小容器，用来列出文件夹名。

## 已知限制

- 规则只在 LG G5 上验证过。其他电视请自己试播后在设置页调整，`电视试播样片` 里有生成测试片的脚本和说明。
- H.264、HEVC 以外的画面编码没有验证过；面板不改变画面编码。
- 在线字幕只搜索中文字幕。
- 英语界面不完整：后台返回的报错大多是中文。
- 以下路径只有自动化测试，没有用真实环境验证过：双层杜比视界转换、音轨转 AC-3 的选项、`PUID` / `PGID`、桥接网络部署、ARM64。
- 没有预先发布的镜像，需要本地构建。

## 开发

整个应用是一个文件：`app.py`，页面和 Dockerfile 都内嵌在里面（仓库里的 `Dockerfile` 与内嵌的那份必须一致，有测试把关）。

```bash
python3 -W error -m unittest discover -s tests
```

测试不需要 Docker 和 Flask。装了 `node`、`ffmpeg`、`mkvmerge`、`dovi_tool` 时会多跑几项，没装则自动跳过。

本地预览页面（假数据）：

```bash
python3 tests/serve_ui_fixture.py
```

然后打开 `http://127.0.0.1:8765`。

## 许可

本项目以 MIT 许可发布，见 `LICENSE`。

镜像里用到的第三方软件各有自己的许可：FFmpeg、MKVToolNix、dovi_tool、Tesseract、pgsrip、ffsubsync。MakeMKV 不包含在内。
