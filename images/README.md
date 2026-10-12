# 邮件图片附件

将需要发送的图片放在此目录，可以在 GitHub 仓库的此目录页面选择 **Add file → Upload files** 上传。
支持 PNG、JPEG、GIF、WebP，单张图片最大 5 MiB。

发送时，在 **Actions → QQ Mail Bot → Run workflow** 的 `image_path` 中填写
`images/文件名`，例如 `images/风景.png`。留空则只发送文字。

此目录只放准备提交到仓库的图片，不要放邮箱凭据、授权码或其他配置文件。
