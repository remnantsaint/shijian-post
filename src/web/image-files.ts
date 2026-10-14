import type { ImageInput } from '../shared/images';
export async function compressImage(file: File): Promise<ImageInput> {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type))
    throw new Error('请选择 JPG、PNG 或 WebP 图片');
  if (file.size > 15_000_000) throw new Error('原图超过 15 MB，请先缩小再插入');
  const bitmap = await createImageBitmap(file);
  try {
    if (bitmap.width * bitmap.height > 40_000_000) throw new Error('原图尺寸太大，请先缩小再插入');
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d');
    if (!context) throw new Error('浏览器暂时无法处理图片');
    let side = 1600;
    let blob: Blob | null = null;
    let mime: ImageInput['contentType'] = file.type === 'image/png' ? 'image/png' : 'image/jpeg';
    for (let attempt = 0; attempt < 7; attempt++) {
      const scale = Math.min(1, side / Math.max(bitmap.width, bitmap.height));
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      if (mime === 'image/jpeg') {
        context.fillStyle = '#fff';
        context.fillRect(0, 0, canvas.width, canvas.height);
      }
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, mime, 0.82));
      if (blob && blob.size <= 330_000) break;
      if (mime === 'image/png') mime = 'image/jpeg';
      else side = Math.round(side * 0.75);
    }
    if (!blob || blob.size > 330_000) throw new Error('图片压缩后仍然太大，请选择较小的图片');
    const content = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('图片读取失败，请重新选择'));
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.readAsDataURL(blob!);
    });
    const extension = mime === 'image/png' ? '.png' : '.jpg';
    const base =
      file.name
        .replace(/\.[^.]+$/, '')
        .replace(/[\x00-\x1f\x7f/\\]/g, '')
        .slice(0, 90) || '照片';
    return { id: crypto.randomUUID(), name: base + extension, contentType: mime, content };
  } finally {
    bitmap.close();
  }
}
