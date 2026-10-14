import { Buffer } from 'node:buffer';
import { MAX_IMAGE_BYTES, bodyParts, type ImageInfo, type ImageInput } from '../shared/images';
import type { Env, TaskRow } from './types';
export interface StoredImage extends ImageInfo {
  sha256: string;
}
export function encodeImage(bytes: Uint8Array): string {
  return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString('base64');
}
async function hashBytes(bytes: Uint8Array) {
  const hash = await crypto.subtle.digest('SHA-256', bytes.slice().buffer);
  return Array.from(new Uint8Array(hash), (n) => n.toString(16).padStart(2, '0')).join('');
}
// Inspect the raster header. SVG, remote URLs and arbitrary HTML are never accepted.
function dimensions(bytes: Uint8Array, mime: string) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (
    mime === 'image/png' &&
    bytes.length >= 33 &&
    bytes.slice(0, 8).every((b, i) => b === [137, 80, 78, 71, 13, 10, 26, 10][i]) &&
    view.getUint32(8) === 13 &&
    view.getUint32(12) === 0x49484452
  ) {
    let offset = 8;
    let hasData = false;
    let ended = false;
    while (offset + 12 <= bytes.length) {
      const size = view.getUint32(offset);
      const type = view.getUint32(offset + 4);
      if (offset + 12 + size > bytes.length) throw new Error('INVALID_IMAGES');
      if (type === 0x49444154 && size > 0) hasData = true;
      offset += size + 12;
      if (type === 0x49454e44) {
        ended = size === 0 && offset === bytes.length;
        break;
      }
    }
    if (!hasData || !ended) throw new Error('INVALID_IMAGES');
    return [view.getUint32(16), view.getUint32(20)];
  }
  if (
    mime === 'image/jpeg' &&
    bytes.length >= 12 &&
    bytes[0] === 255 &&
    bytes[1] === 216 &&
    bytes.at(-2) === 255 &&
    bytes.at(-1) === 217
  ) {
    let i = 2;
    while (i + 4 < bytes.length) {
      if (bytes[i++] !== 255) break;
      while (bytes[i] === 255) i++;
      const marker = bytes[i++];
      if (marker === 0xda || marker === 0xd9) break;
      if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
      if (i + 2 > bytes.length) throw new Error('INVALID_IMAGES');
      const length = view.getUint16(i);
      if (length < 2 || i + length > bytes.length) break;
      if ([0xc0, 0xc1, 0xc2].includes(marker) && length >= 8)
        return [view.getUint16(i + 5), view.getUint16(i + 3)];
      i += length;
    }
  }
  throw new Error('INVALID_IMAGES');
}
export async function prepareImages(body: string, input: ImageInput[] = []) {
  const refs = bodyParts(body).flatMap((p) => ('imageId' in p ? [p.imageId] : []));
  if (
    refs.length !== input.length ||
    new Set(refs).size !== refs.length ||
    refs.some((id) => !input.some((img) => img.id === id))
  )
    throw new Error('INVALID_IMAGES');
  let total = 0;
  return Promise.all(
    input.map(async (img) => {
      const bytes = new Uint8Array(Buffer.from(img.content, 'base64'));
      total += bytes.length;
      if (!bytes.length || total > MAX_IMAGE_BYTES || encodeImage(bytes) !== img.content)
        throw new Error('INVALID_IMAGES');
      const [width, height] = dimensions(bytes, img.contentType);
      if (!width || !height || width > 4096 || height > 4096) throw new Error('INVALID_IMAGES');
      return {
        bytes,
        info: {
          id: img.id,
          name: img.name,
          contentType: img.contentType,
          size: bytes.length,
          width,
          height,
          sha256: await hashBytes(bytes),
        } satisfies StoredImage,
      };
    }),
  );
}
export async function loadImages(env: Env, task: TaskRow) {
  const metadata = JSON.parse(task.images_metadata || '[]') as StoredImage[];
  return Promise.all(
    metadata.map(async (info) => {
      const row = await env.DB.prepare(
        'SELECT content FROM task_images WHERE task_id=? AND image_id=?',
      )
        .bind(task.id, info.id)
        .first<{ content: number[] | ArrayBuffer }>();
      if (!row) throw new Error('IMAGE_MISSING');
      const bytes = new Uint8Array(row.content);
      if (bytes.length !== info.size || (await hashBytes(bytes)) !== info.sha256)
        throw new Error('IMAGE_MISSING');
      return { ...info, content: encodeImage(bytes) };
    }),
  );
}
export async function cleanupImages(env: Env, id?: string) {
  // Status is checked again in the transaction. Uncertain, sending and failed tasks retain originals.
  const eligible =
    "SELECT id FROM tasks WHERE status IN ('accepted','simulated','cancelled','expired') AND images_cleaned_at IS NULL AND images_metadata != '[]'" +
    (id ? ' AND id=?' : ' LIMIT 100');
  const rows = await (id ? env.DB.prepare(eligible).bind(id) : env.DB.prepare(eligible)).all<{
    id: string;
  }>();
  for (const task of rows.results) {
    await env.DB.batch([
      env.DB.prepare(
        "DELETE FROM task_images WHERE task_id=? AND task_id IN (SELECT id FROM tasks WHERE status IN ('accepted','simulated','cancelled','expired'))",
      ).bind(task.id),
      env.DB.prepare(
        "UPDATE tasks SET images_cleaned_at=? WHERE id=? AND status IN ('accepted','simulated','cancelled','expired')",
      ).bind(new Date().toISOString(), task.id),
    ]);
  }
}
