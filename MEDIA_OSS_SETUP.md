# 图片与视频（阿里云 OSS）配置

扫码拍照、图片与视频管理用的是阿里云 OSS。手机拍好的照片和视频**直接从手机上传到 OSS**，不经过我们的服务器，所以服务器的硬盘和带宽不受影响。文件按原样保存，不压缩、不转码。

数据库里不存任何东西：每只包的文件就放在 OSS 的一个文件夹里，文件夹本身就是记录。

```text
item-media/<SKU>/main_raw/      首图（原始）
item-media/<SKU>/main_edit/     首图（精修）
item-media/<SKU>/detail_raw/    细节图（原始）
item-media/<SKU>/detail_edit/   细节图（精修）
item-media/<SKU>/model/         上身图
item-media/<SKU>/video/         视频
```

文件名格式：`<上传时间>-<随机码>-<原文件名>`。

## 1. 创建 Bucket

在阿里云控制台 → 对象存储 OSS → Bucket 列表 → 创建 Bucket：

- **地域**：选和服务器（ECS）相同的地域，比如新加坡 `ap-southeast-1`。这样服务器读写 OSS 可以走内网，不收流量费。
- **存储类型**：标准存储。
- **读写权限**：**私有**。必须是私有，系统会给每次查看和上传生成有时效的签名链接。
- 其他保持默认。

## 2. 设置跨域（CORS）

手机浏览器要直接上传到 OSS，所以必须设置跨域。

Bucket → 数据安全 → 跨域设置 → 创建规则：

| 项目 | 填写 |
|---|---|
| 来源 | 系统的网址，例如 `https://manage.luxuryevermore.com`（有几个域名就都填上） |
| 允许 Methods | `GET`、`PUT`、`POST`、`HEAD` |
| 允许 Headers | `*` |
| 暴露 Headers | `ETag` |
| 缓存时间 | `600` |

没有设置跨域的话，上传会提示“网络中断”。

## 3. 生命周期规则（建议）

视频上传如果中途放弃，会留下没完成的分片，这些分片也占空间。

Bucket → 数据管理 → 生命周期 → 创建规则：

- 前缀：`item-media/`
- 碎片：**7 天后删除**。

只删“没完成的分片”，不会删任何正常文件。

## 4. 创建专用的 RAM 用户

不要用主账号的 AccessKey。

1. 进入 RAM 访问控制 → 用户 → 创建用户，勾选“OpenAPI 调用访问”，然后保存它的 AccessKey ID 和 Secret。
2. 给这个用户授权一条**只能访问这个 Bucket** 的自定义策略。下面的 `le-media` 换成你的 Bucket 名：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "oss:PutObject",
        "oss:GetObject",
        "oss:DeleteObject",
        "oss:ListParts",
        "oss:AbortMultipartUpload"
      ],
      "Resource": ["acs:oss:*:*:le-media/item-media/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["oss:ListObjects", "oss:GetBucketInfo"],
      "Resource": ["acs:oss:*:*:le-media"]
    }
  ]
}
```

## 5. 服务器配置

在服务器的 `.env` 里加上下面几行：

```text
OSS_ACCESS_KEY_ID=...
OSS_ACCESS_KEY_SECRET=...
OSS_BUCKET=le-media
OSS_REGION=ap-southeast-1
OSS_ENDPOINT=https://oss-ap-southeast-1.aliyuncs.com
# 服务器和 Bucket 在同一地域时（推荐）：
OSS_INTERNAL_ENDPOINT=https://oss-ap-southeast-1-internal.aliyuncs.com
# 可选，默认 item-media/
# MEDIA_PREFIX=item-media/
```

然后安装新依赖（`oss2`）并重启：

```bash
pip install -r requirements.txt
sudo systemctl restart <你的服务名>
```

配置好以前，页面上会显示“媒体存储（阿里云 OSS）未配置：缺少 …”，系统其他功能不受影响。

## 6. 使用

- **扫码拍照**（菜单里，所有角色都有）：扫包上的二维码，或者输入 SKU。
- **拍首图 / 拍细节图 / 拍视频**：点了直接打开手机相机，拍完自动上传到“原始”分类。上传在后台进行，可以继续拍。每拍完一张，底部会出现一个大按钮，点一下就能接着拍下一张。
- **从相册上传**：网页里打开的相机不能调焦距、曝光，也不能切换 0.5×/2×/3× 镜头。需要这些时，先用手机自带的相机 App 拍，再在四个大按钮下面的虚线框里点“首图 / 细节图 / 视频”，从相册一次选多张。
- **管理媒体**：6 个分类都可以添加（从相册或文件里选，可以多选）和删除。
  - 点缩略图会打开大图查看器：左右滑动切换，点图片或“看原图”可以放大看原图，点“存到相册”保存这一张。视频会直接播放。
  - 手机上，图片分类有“全部存到相册”：点了以后在弹出的菜单里选“存储图像”。超过 20 张会分批，每批点一次。如果按钮变成“准备好了，点这里保存”，再点一下就行。
  - 电脑上，细节图（原始）和细节图（精修）可以“全部下载（zip）”。zip 里是原文件，不压缩。
- **商品详情页**：每个商品详情（Admin、Staff、Management）都有同样的“图片与视频”区域。
- **Management**：只能看到和管理自己的货。

## 关于“原图”

- 系统保存的是浏览器交给它的文件，一个字节都不改，不会再压缩。
- iPhone 在网页里用相机拍照时，交给浏览器的是全尺寸 JPEG（不是 HEIC）。
- 从“文件”App 选择上传时，保留的是原始文件格式，比如 HEIC、DNG、MOV。
- 想保留 HEIC 或 ProRAW 原片，可以先用相机 App 拍，再在“管理媒体”里选“文件”上传。

## 费用参考

费用以阿里云官网为准。

- 存储：标准存储大约 0.12 元/GB/月。
- 上传：免费。
- 查看和下载：按流量计费。服务器走内网的部分不收费。
- 缩略图：由 OSS 图片处理生成，视频封面由 OSS 视频截帧生成，用量很小。
