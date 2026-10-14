export const MAX_IMAGES = 3;
export const MAX_IMAGE_BYTES = 1_000_000;
export interface ImageInput {
  id: string;
  name: string;
  contentType: 'image/jpeg' | 'image/png';
  content: string;
}
export interface ImageInfo {
  id: string;
  name: string;
  contentType: 'image/jpeg' | 'image/png';
  size: number;
  width: number;
  height: number;
}
export const imageMarker = (id: string) => `[[image:${id}]]`;
export function bodyParts(body: string): ({ text: string } | { imageId: string })[] {
  return body
    .split(/(\[\[image:[0-9a-f-]{36}\]\])/gi)
    .filter(Boolean)
    .map((part) => {
      const match = /^\[\[image:([0-9a-f-]{36})\]\]$/i.exec(part);
      return match ? { imageId: match[1] } : { text: part };
    });
}
export const templateText = (body: string) =>
  bodyParts(body)
    .map((p) => ('text' in p ? p.text : ''))
    .join('')
    .trim();
